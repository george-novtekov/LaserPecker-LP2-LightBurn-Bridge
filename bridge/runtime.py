"""A single serial worker stages whole jobs and reports observed LP2 state."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import hashlib
import copy
import threading
import time

from . import job as job_mod
from . import speed as speed_mod
from . import raster as raster_mod
from . import raster_timing
from . import sequence
from . import orientation
from . import calibration
from . import transport as t
from .codec import protocol as p

class RuntimeCommandError(ValueError): pass

@dataclass
class RuntimeConfig:
    image_mode: str = 'bitmap'
    calibration_profile: object = None
    rotation: int = 0
    mirror_x: bool = False
    port_name: str = 'COM3'
    port_factory: object = None
    capture: object = None
    power_cap: int = 200
    poll_interval: float = .2
    start_timeout: float = 5
    job_budget: float = 600
    idle_spool_timeout: float = 10
    read_only: bool = False
    start_laser_percent: int = 100

class NullCapture:
    def event(self, kind, **fields): pass

class Controller:
    def __init__(self, config=None):
        self.config = config or RuntimeConfig()
        if self.config.image_mode not in ('bitmap','continuous'):raise ValueError('invalid image mode')
        self.config.calibration_profile=copy.deepcopy(self.config.calibration_profile)
        if type(self.config.rotation) is not int or self.config.rotation not in (0,180):raise ValueError('rotation must be0or180')
        if type(self.config.mirror_x) is not bool:raise ValueError('mirror_x must be boolean')
        calibration.Calibration.from_profile(self.config.calibration_profile)
        job_mod.map_s(0,self.config.power_cap)
        self._lock = threading.RLock()
        self._stop, self._ready, self._cancel = threading.Event(),threading.Event(),threading.Event()
        self._worker = None
        self._state, self._host_pos = 'alarm',(0.0,0.0)
        self._last_status, self._last_status_mono = None,None
        self._device_idle_confirmed, self._poisoned = False,False
        self._alarm_latched = False
        self._spool, self._spool_bytes, self._last_input = [],0,0
        self._pending, self._file_id, self._ack_pending = None,None,False
        self._remaining = deque()
        self._device_jobs = {}
        self._internal_job=False
        self._started, self._saw_active, self._idle_observations = False,False,0
        self._previous_job_id,self._saw_matching_id=0,False
        self._staging_hold, self._pause_request, self._resume_request = False,False,False
        self._attached, self._m2_ack, self._notice = False,None,None
        self._events = deque(maxlen=256)
        self.last_error = 'serial worker has not initialized'

    def start(self, *, on_m2_ack, on_notice=None):
        with self._lock:
            self._attached,self._m2_ack,self._notice = True,on_m2_ack,on_notice
            if self._worker is not None: return  # reconnect never reopens COM
            self._worker = threading.Thread(target=self._run,name='lp2-serial-owner',daemon=True)
            self._worker.start()

    def _note(self, message):
        self._events.append(message)
        if self.config.capture: self.config.capture.event('runtime', message=message, state=self._state)
        if self._notice:
            try: self._notice(message)
            except Exception: pass

    def _fail_alarm(self, message):
        with self._lock:
            self.last_error = message
            self._alarm_latched = True
            self._state = 'alarm'
            self._cancel.set()
            self._spool,self._spool_bytes,self._pending,self._ack_pending = [],0,None,False
            self._remaining.clear()
            self._device_jobs.clear()
            self._internal_job=False
            self._note('ALARM: '+message)

    def _reject_complete_job(self,message,tokens):
        """A known job boundary can be discarded without creating a device alarm.

        Early framing/parser errors and device/transport faults still latch.
        This never clears an active or uncertain physical operation.
        """
        terminal=tokens in ([('M',2.0)],[('M',30.0)])
        fresh_idle=(self._device_idle_confirmed and self._last_status_mono is not None
                    and time.monotonic()-self._last_status_mono<=1)
        if (not terminal or self._alarm_latched or self._started or self._pending is not None
                or self._poisoned or not fresh_idle or not self._worker or not self._worker.is_alive()):
            self._fail_alarm(message)
            return
        self.last_error=message
        self._cancel.set()
        self._spool,self._spool_bytes,self._pending,self._ack_pending=[],0,None,False
        self._remaining.clear()
        self._device_jobs.clear()
        self._staging_hold,self._pause_request,self._resume_request=False,False,False
        self._state='idle'
        self._note('REJECTED: '+message+'; complete job discarded, ready for corrected retry')

    def _compile_job(self,raw,profile):
        compiled=sequence.compile_sequence(raw,power_cap=self.config.power_cap,job_budget=self.config.job_budget,image_mode=self.config.image_mode)
        model=calibration.Calibration.from_profile(profile)
        prepared={}
        for stage in compiled.stages:
            wire=orientation.rotate_stage(calibration.apply_stage(stage,model,job_budget=self.config.job_budget),
                                          self.config.rotation,mirror_x=self.config.mirror_x)
            if isinstance(wire,raster_mod.RasterJob):wire=raster_mod.prepare_wire(wire,job_budget=self.config.job_budget)
            prepared[id(stage)]=wire
        if model.enabled:self._note('CALIBRATION: measured PC profile applied to all marking stages')
        for note in compiled.notes:self._note(note)
        parsed=compiled.stages[0]
        for index,stage in enumerate(compiled.stages,1):
            if isinstance(stage,raster_mod.RasterJob):
                stage=prepared[id(stage)]
                sp=stage.speed_plan
                self._note(f'RASTER: stage {index}/{len(compiled.stages)}; source ~{25.4/stage.source_pitch_mm:.3f} DPI; native {stage.effective_dpi:.3f} DPI; {stage.width_px}x{stage.height_px}; aligned bidirectional rows; {len(stage.payload)} bytes; exposure {sp.exposure_us} us/pixel; estimate {sp.estimated_seconds:.0f}s')
                if self.config.capture:
                    self.config.capture.event('raster_compiled',stage=index,input_sha256=hashlib.sha256(raw).hexdigest(),
                        width=stage.width_px,height=stage.height_px,nx=stage.nx_px,ny=stage.ny_px,
                        black_pixels=stage.black_pixels,payload_bytes=len(stage.payload),
                        payload_sha256=hashlib.sha256(stage.payload).hexdigest(),requested_s=stage.requested_s,
                        requested_feed=stage.requested_feed,exposure_us=sp.exposure_us,
                        source_pitch_mm=stage.source_pitch_mm,native_px=stage.px,native_dpi=stage.effective_dpi,
                        bidirectional_corrected=stage.wire_compensated,
                        estimated_seconds=sp.estimated_seconds,snap_mm=stage.snap_mm)
            elif stage.speed_plan.requested_mm_s:
                sp=prepared[id(stage)].speed_plan
                self._note(f'SPEED: stage {index}/{len(compiled.stages)}; requested {sp.requested_mm_s:g} mm/s; modeled {sp.minimum_mm_s:.3f}..{sp.maximum_mm_s:.3f} mm/s; depth {sp.wire_depth}; quantized, no motion sensor feedback')
        return compiled,prepared

    def submit_line(self, data):
        with self._lock:
            if (self._internal_job or self._alarm_latched or not self._ready.is_set() or self._state not in ('idle','receiving')
                    or not self._worker or not self._worker.is_alive()): return 'error:9'
            if len(data)>job_mod.MAX_LINE_BYTES or any(b<32 and b!=9 or b>126 for b in data):
                self._fail_alarm('invalid/overlong input line'); return 'error:20'
            try:
                text = data.decode('ascii')
                tokens = job_mod.words(text)
            except (UnicodeError,job_mod.JobError) as exc:
                self._fail_alarm(str(exc)); return 'error:20'
            if not tokens: return 'ok'
            if self._pending is not None: return 'error:9'
            if not self._spool:
                self._cancel = threading.Event()
            # LightBurn sends a standalone G0 when connecting. Modal-only
            # preamble is retained, but it is not a pending movement/job.
            if any(letter in ('X','Y') for letter,value in tokens): self._state='receiving'
            self._spool.append(data)
            self._spool_bytes += len(data)+1
            self._last_input = time.monotonic()
            if self._spool_bytes>job_mod.MAX_INPUT_BYTES or len(self._spool)>job_mod.MAX_LINES:
                self._fail_alarm('spool bound exceeded: maximum 16 MiB / 1100000 lines'); return 'error:20'
            if not job_mod.is_finish_line(text): return 'ok'
            if self.config.read_only:
                raw=b'\n'.join(self._spool)
                if self.config.capture:
                    self.config.capture.event('whole_job_captured',input_bytes=len(raw),input_lines=len(self._spool),
                                              sha256=hashlib.sha256(raw).hexdigest(),validated_for_execution=False)
                self._reject_complete_job('read-only capture complete; no USB job uploaded',tokens)
                return 'error:20'
            try:
                raw=b'\n'.join(self._spool)
                compiled,prepared=self._compile_job(raw,self.config.calibration_profile)
                parsed=compiled.stages[0]
                self._remaining=deque(compiled.stages[1:])
                self._device_jobs=prepared
                if self.config.capture:self.config.capture.event('sequence_compiled',stage_count=len(compiled.stages),input_sha256=hashlib.sha256(raw).hexdigest())
            except job_mod.JobError as exc:
                self._reject_complete_job('job rejected before upload: '+str(exc),tokens); return 'error:20'
            self._pending,self._ack_pending = parsed,True
            self._spool,self._spool_bytes = [],0
            self._note('whole job validated; final M2 acknowledgement withheld')
            return None

    def _require_idle(self):
        if (self._state!='idle' or self._pending is not None or self._started or self._alarm_latched
                or self._poisoned or not self._device_idle_confirmed or self._last_status_mono is None
                or time.monotonic()-self._last_status_mono>1 or not self._worker or not self._worker.is_alive()):
            raise RuntimeCommandError('requires a connected, freshly verified idle LP2')
        if any(job_mod.words(line.decode('ascii')) not in ([],[('G',0.)]) for line in self._spool):
            raise RuntimeCommandError('LightBurn has staged commands; stop/reset its stream first')

    def apply_profile(self,profile):
        profile=copy.deepcopy(profile)
        calibration.Calibration.from_profile(profile)
        with self._lock:
            self._require_idle();self.config.calibration_profile=profile
            if self.config.capture:self.config.capture.event('calibration_profile_applied',profile=profile)
            self._note('Calibration profile applied: '+(profile.get('name','Measured profile') if profile else 'Uncalibrated'))

    def submit_internal_job(self,raw,*,corrected=False):
        with self._lock:
            self._require_idle()
            if self.config.read_only:raise RuntimeCommandError('read-only connection cannot engrave a pattern')
            profile=self.config.calibration_profile if corrected else None
            if corrected and profile is None:raise RuntimeCommandError('apply a measured calibration profile first')
            compiled,prepared=self._compile_job(raw,profile)
            self._cancel=threading.Event();self._spool,self._spool_bytes=[],0
            self._internal_job=True;self._attached=True
            self._pending=compiled.stages[0];self._remaining=deque(compiled.stages[1:])
            self._device_jobs=prepared;self._ack_pending=True;self._state='receiving'
            self._note('GUI calibration pattern queued: '+('corrected check' if corrected else 'uncorrected baseline'))

    def realtime(self, ch):
        if ch == '?': return self.status_report()
        with self._lock:
            if ch == '\x18':
                self.cancel_pending(detach=False)
                if self._state=='alarm' and not self._started and not self._poisoned and self._device_idle_confirmed and self._worker and self._worker.is_alive():
                    self._state='idle'
                    self._alarm_latched=False
                return "Grbl 1.1h ['$' for help]\n[MSG:LP2 USB bridge reset; staged job cleared]"
            if self._state=='alarm': return ''
            if ch=='!': self._staging_hold,self._pause_request,self._resume_request = True,True,False
            elif ch=='~': self._staging_hold,self._resume_request,self._pause_request = False,True,False
            else: self._fail_alarm('unsupported realtime byte')
            return ''

    def dollar_command(self,line):
        line=line.strip()
        if line=='$I': return '[VER:1.1h.LP2-USB-BRIDGE:host-scan-8]\n[OPT:V,15,128]\nok'
        if line=='$$':
            settings={0:10,1:25,2:0,3:0,4:0,5:0,6:0,10:1,11:.01,12:.002,13:0,
                      20:0,21:0,22:0,23:0,24:25,25:500,26:250,27:1,
                      30:1000,31:0,32:0,100:100,101:100,102:100,110:12120,111:12120,
                      112:0,120:1000,121:1000,122:0,130:100,131:100,132:0}
            return '\n'.join(f'${key}={value:g}' for key,value in settings.items())+'\nok'
        if line=='$G': return '[GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]\nok'
        if line=='$#':
            return '\n'.join(f'[G{i}:0.000,0.000,0.000]' for i in range(54,60))+'\n[G92:0.000,0.000,0.000]\n[TLO:0.000]\n[PRB:0.000,0.000,0]\nok'
        if line=='$X':
            with self._lock:
                if (self._poisoned or not self._device_idle_confirmed or self._pending or self._started
                        or not self._worker or not self._worker.is_alive()):
                    raise RuntimeCommandError('unlock requires a live, idle, error-free LP2')
                if self._state=='alarm': self._state='idle'
                self._alarm_latched=False
            return 'ok'
        raise RuntimeCommandError('unsupported dollar command')

    def status_report(self):
        with self._lock:
            label={'idle':'Idle','receiving':'Run','uploading':'Run','running':'Run',
                   'hold':'Hold:0','stopping':'Hold:1','alarm':'Alarm'}[self._state]
            if self._state=='receiving' and self._staging_hold: label='Hold:0'
            if self._alarm_latched: label='Alarm'
            x,y=self._host_pos
            return f'<{label}|MPos:{x:.3f},{y:.3f},0.000|FS:0,0|Bf:15,128>'

    def cancel_pending(self,detach=True):
        with self._lock:
            if detach: self._attached=False
            self._cancel.set()
            self._spool,self._spool_bytes,self._ack_pending = [],0,False
            self._remaining.clear()
            self._device_jobs.clear()
            self._internal_job=False
            self._staging_hold,self._pause_request,self._resume_request = False,False,False
            if self._state in ('running','hold','uploading','stopping'): self._state='stopping'
            elif self._state!='alarm': self._pending,self._state = None,'idle'
            self._note('pending job cancelled; automatic restart disabled')

    def shutdown(self):
        self.cancel_pending()
        self._stop.set()
        if self._worker and self._worker.is_alive(): self._worker.join(15)

    def _execute(self,session,job):
        with self._lock:
            if self._cancel.is_set() or not self._attached or self._pending is not job: return
            self._state='uploading'
            cancel=self._cancel
        device_job=self._device_jobs.get(id(job),job)
        listing=session.files()
        bitmap=isinstance(device_job,raster_mod.RasterJob)
        if bitmap:
            fields=dict(nx=device_job.nx_px,ny=device_job.ny_px)
            name='LB-R-'+hashlib.sha256(device_job.payload).hexdigest()[:16]
            digest=raster_mod.raster_digest(device_job,name=name)
            file_id=p.select_file_id(digest,listing.exclude_for_new_job)
            header=raster_mod.gen_raster_header(device_job,file_id=file_id,name=name)
        else:
            geometry=device_job.header_geometry
            fields=dict(width=geometry['width_px'],height=geometry['height_px'],
                        nx=geometry['nx_px'],ny=geometry['ny_px'],dpi=geometry['dpi'],line_count=geometry['line_count'])
            name='LB-'+hashlib.sha256(device_job.payload).hexdigest()[:16]
            digest=p.gcode_content_digest(**fields,name=name,payload=device_job.payload)
            file_id=p.select_file_id(digest,listing.exclude_for_new_job)
            header=p.gen_gcode_header(**fields,file_id=file_id,name=name)
        if cancel.is_set():
            with self._lock: self._pending,self._state=None,'idle'
            return
        session.upload(header,device_job.payload,cancel)
        # The live status ID initially lags; verify the committed ID before start.
        confirmed=session.files()
        if file_id not in [entry.value for entry in confirmed.words]:
            raise t.TransportError('uploaded content ID not found in the committed file list')
        self._note(f'uploaded content ID {file_id:08x} verified in file inventory')
        prestart=session.status()
        if prestart['mode']!=6 or prestart['w_state']==4 or prestart['error']:
            raise t.TransportError('LP2 not idle after committing the upload')
        while True:
            with self._lock:
                if cancel.is_set() or not self._attached or self._pending is not job:
                    self._pending,self._state=None,('alarm' if self._alarm_latched else 'idle')
                    self._note('cancelled after upload; no start sent')
                    return
                if not self._staging_hold:
                    self._file_id=file_id
                    self._previous_job_id=prestart['name_id']
                    self._saw_matching_id=False
                    requested_power=job.requested_s/10 if bitmap else (job.marking_powers[0]/10 if job.marking_powers else 0)
                    commanded_power=min(self.config.start_laser_percent,round(requested_power))
                    session.start(file_id=file_id,nx=fields['nx'],ny=fields['ny'],
                                  laser_percent=commanded_power,
                                  wire_depth=device_job.speed_plan.wire_depth)
                    self._note(f'POWER: requested {requested_power:g}%; sent stock {commanded_power}%; optical power is not measured')
                    if self.config.capture:self.config.capture.event('power_command',file_id=file_id,requested_percent=requested_power,stock_percent=commanded_power,optical_measured=False)
                    self._started,self._saw_active,self._idle_observations=True,False,0
                    self._start_mono=time.monotonic()
                    self._device_idle_confirmed=False
                    self._state='running'
                    self._note(f'start acknowledged for content-derived file {file_id:08x}')
                    return
            self._poll(session)
            self._stop.wait(self.config.poll_interval)

    def _poll(self,session):
        with self._lock:
            if self._started:
                if self._cancel.is_set() and session.phase in ('running','paused'):
                    session.control('stop'); self._state='alarm' if self._alarm_latched else 'stopping'
                elif self._pause_request and session.phase=='running':
                    session.control('pause'); self._pause_request=False
                elif self._resume_request and session.phase=='paused':
                    session.control('resume'); self._resume_request=False
        status=session.status()
        with self._lock:
            self._last_status,self._last_status_mono=status,time.monotonic()
            idle=status['mode']==6 and status['w_state']!=4 and not status['error']
            self._device_idle_confirmed=idle
            if status['error'] or status['mode']==5:
                raise t.TransportError(f'LP2 reported error/state: {status}')
            if not self._started: return
            if status['name_id']==self._file_id: self._saw_matching_id=True
            if status['mode']==1 and status['name_id'] not in (0,self._previous_job_id,self._file_id):
                raise t.TransportError('active status identifies a different job')
            if status['mode']==1 and status['w_state'] in (1,2,4) and status['name_id'] in (0,self._previous_job_id,self._file_id):
                if not self._saw_active and status['name_id']!=self._file_id:
                    self._note('V376 job ID is still initializing; no successful completion is possible before the current ID appears')
                self._saw_active,self._idle_observations=True,0
                if not self._cancel.is_set() and not self._alarm_latched: self._state='hold' if status['w_state']==4 else 'running'
            elif idle and (self._saw_active or self._cancel.is_set()):
                if self._cancel.is_set() or (self._saw_matching_id and status['name_id']==self._file_id):
                    self._idle_observations+=1
                else: self._idle_observations=0
                if self._idle_observations>=2:
                    job=self._pending
                    completed=not self._cancel.is_set() and self._saw_active and self._ack_pending and self._attached and job is not None
                    if completed and self._remaining:
                        self._host_pos=job.final_host_position
                        self._pending,self._started,self._file_id=self._remaining.popleft(),False,None
                        self._state='receiving';self._idle_observations=0
                        session.phase='idle'
                        self._note('stage completed by active-to-idle status; remaining stages queued, M2 still withheld')
                        return
                    if completed: self._host_pos=job.final_host_position
                    self._state,self._pending,self._started,self._file_id=('alarm' if self._alarm_latched else 'idle'),None,False,None
                    self._ack_pending=False
                    self._device_jobs.clear()
                    session.phase='idle'
                    self._note('job completed by active-to-idle status' if completed else 'cancel confirmed idle')
                    internal=self._internal_job
                    self._internal_job=False
                    if completed and internal:self._note('GUI calibration pattern completed')
                    elif completed and self._m2_ack:self._m2_ack()
            else: self._idle_observations=0
            if self._started:
                elapsed=time.monotonic()-self._start_mono
                if not self._saw_active and elapsed>self.config.start_timeout:
                    raise t.TransportError('start timeout: matching active job never observed')
                if not self._saw_matching_id and elapsed>self.config.start_timeout:
                    raise t.TransportError('current job ID never appeared in device status')
                if elapsed>self.config.job_budget: raise t.TransportError('job time budget exceeded')

    def _run(self):
        session=None
        try:
            capture=self.config.capture or NullCapture()
            port=(self.config.port_factory or t.open_windows_port)(self.config.port_name)
            session=t.LP2Session(port,capture)
            session.identify()
            self._poll(session)
            if not self._device_idle_confirmed: raise t.TransportError('LP2 not idle at startup')
            if (getattr(port,'transport_kind','usb')=='bluetooth' and not self.config.read_only
                    and self._last_status['u_b_cnn']!=1):
                raise t.TransportError('Bluetooth upload requires USB unplugged and external LP2 power; restart LP2 after a failed upload')
            with self._lock: self._state='idle'
            self._note('Ready: LP2 identity and idle status verified')
            self._ready.set()
            while not self._stop.is_set():
                with self._lock:
                    job=self._pending
                    if self._spool and self._state=='receiving' and time.monotonic()-self._last_input>self.config.idle_spool_timeout:
                        self._fail_alarm('incomplete spool timeout')
                    execute=job is not None and not self._started and not self._staging_hold and not self._cancel.is_set()
                if execute: self._execute(session,job)
                self._poll(session)
                self._stop.wait(self.config.poll_interval)
        except Exception as exc:
            self._poisoned=bool(session and session.poisoned)
            self._fail_alarm(str(exc))
        finally:
            self._ready.set()
            if session:
                if not session.poisoned and session.phase in ('running','paused','stopping'):
                    try:
                        if session.phase!='stopping': session.control('stop')
                        deadline,idle_count=time.monotonic()+3,0
                        while time.monotonic()<deadline and idle_count<2:
                            status=session.status()
                            idle_count=idle_count+1 if status['mode']==6 and status['w_state']!=4 and not status['error'] else 0
                            time.sleep(.05)
                        self._note('shutdown stop confirmed idle' if idle_count>=2 else 'shutdown stop not confirmed')
                    except Exception as exc: self._note('final stop failed: '+str(exc))
                session.close()
            with self._lock:
                self._device_idle_confirmed=False
                if self._state!='alarm': self._state='alarm'
