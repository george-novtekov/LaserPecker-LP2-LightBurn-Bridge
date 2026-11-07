"""Threaded GUI service; one shared controller owns USB/BLE and job staging."""
from collections import deque
import asyncio
import copy
import datetime
import hashlib
import json
import math
from pathlib import Path
import sys
import threading
import time
import uuid
from . import runtime,server,transport,calibration

def default_data_dir():
    return (Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parents[1])/'LP2BridgeData'

class EventCapture(transport.Capture):
    def __init__(self,path,observer):super().__init__(path);self.observer=observer
    def event(self,kind,**fields):
        super().event(kind,**fields)
        self.observer(kind,fields)

class BridgeService:
    def __init__(self,*,port_factory=None,data_dir=None):
        self.data_dir=Path(data_dir) if data_dir else default_data_dir()
        self._lock=threading.RLock();self._thread=None;self._server=None;self._controller=None
        self._stopping=False;self._error='';self._messages=deque(maxlen=100);self._profile=None
        self._port_factory=port_factory;self._config={}

    def _message(self,message):
        stamp=datetime.datetime.now().strftime('%H:%M:%S')
        with self._lock:self._messages.append(stamp+'  '+str(message))

    def _observe(self,kind,fields):
        if kind=='runtime':self._message(fields.get('message',''))
        elif kind=='identity':self._message(f"LP2 firmware {fields['sw_field']}, identity {fields['hw_hex']}")
        elif kind=='serial_uncertain':self._message('Transfer uncertain; restart the LP2 before reconnecting.')
        elif kind=='serial_closed':self._message('Device connection closed')

    @staticmethod
    def scan_ports():
        from serial.tools import list_ports
        return [dict(port=p.device,description=p.description) for p in list_ports.comports() if (p.vid,p.pid)==(0x1a86,0x7523)]

    @staticmethod
    def scan_bluetooth():
        from .ble import scan_lp2
        return asyncio.run(scan_lp2())

    def start(self,config):
        cfg=copy.deepcopy(config);transport_kind=cfg.get('transport','usb')
        if transport_kind not in ('usb','bluetooth'):raise ValueError('choose USB or Bluetooth')
        if cfg.get('image_mode','continuous') not in ('bitmap','continuous'):raise ValueError('choose a valid image scan mode')
        rotation=int(cfg.get('rotation',180));port=int(cfg.get('tcp_port',23230));cap=int(cfg.get('power_cap',1000))
        cfg.setdefault('mirror_x',True)
        if type(cfg['mirror_x']) is not bool:raise ValueError('mirror_x must be boolean')
        if rotation not in (0,180) or not 0<=port<=65535 or not 0<=cap<=1000:raise ValueError('invalid rotation, TCP port or power ceiling')
        budget=float(cfg.get('job_budget',600));start_power=int(cfg.get('start_laser_percent',100))
        if not math.isfinite(budget) or not 1<=budget<=7200 or not 1<=start_power<=100:
            raise ValueError('stage budget must be1..7200seconds and start ceiling1..100%')
        profile=cfg.get('calibration_profile');calibration.Calibration.from_profile(profile)
        with self._lock:
            if self._thread and self._thread.is_alive():raise RuntimeError('bridge is already running')
            self._error='';self._stopping=False;self._profile=profile;self._config=cfg
            self._server=self._controller=None
            self._thread=threading.Thread(target=self._run,args=(cfg,),name='lp2-gui-server',daemon=True)
            self._thread.start()

    def _run(self,cfg):
        serial_cap=tcp_cap=None
        try:
            self.data_dir.mkdir(parents=True,exist_ok=True)
            label='live-gui-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
            serial_cap=EventCapture(self.data_dir/(label+'-serial.jsonl'),self._observe)
            tcp_cap=transport.Capture(self.data_dir/(label+'-tcp.jsonl'))
            kind=cfg.get('transport','usb');name=cfg.get('ble_address','') if kind=='bluetooth' else cfg.get('port','COM3')
            factory=self._port_factory
            if kind=='bluetooth' and factory is None:
                def factory(address):
                    from .ble import open_ble_port
                    if not address:
                        found=self.scan_bluetooth()
                        if len(found)!=1:raise transport.TransportError('Bluetooth discovery requires exactly one LP2; select its address first')
                        address=found[0]['address'];self._message('Using '+str(found[0]['name']))
                    return open_ble_port(address)
            here=Path(__file__).resolve().parent
            hashes=(json.loads((here/'build_manifest.json').read_text())['source_hashes'] if getattr(sys,'frozen',False) else
                    {str(p.relative_to(here)):hashlib.sha256(p.read_bytes()).hexdigest() for p in here.rglob('*.py') if 'tests' not in p.parts})
            metadata=dict(scope='GUI LP2 bridge',config=cfg,source_hashes=hashes)
            serial_cap.event('metadata',**metadata);tcp_cap.event('metadata',**metadata)
            controller=runtime.Controller(runtime.RuntimeConfig(port_name=name,port_factory=factory,capture=serial_cap,
                image_mode=cfg.get('image_mode','continuous'),
                power_cap=int(cfg.get('power_cap',1000)),rotation=int(cfg.get('rotation',180)),
                mirror_x=cfg['mirror_x'],
                start_laser_percent=int(cfg.get('start_laser_percent',100)),job_budget=float(cfg.get('job_budget',600)),
                calibration_profile=cfg.get('calibration_profile')))
            srv=server.BridgeServer(tcp_port=int(cfg.get('tcp_port',23230)),controller=controller,capture=tcp_cap)
            with self._lock:
                self._server,self._controller=srv,controller
                if self._stopping:srv.stop()
            srv._log=lambda message:None
            def initialize():
                if not srv.ready.wait(5):return
                with controller._lock:
                    if controller._worker is None and not srv._stop.is_set():
                        controller.start(on_m2_ack=lambda:None,on_notice=lambda message:None)
            threading.Thread(target=initialize,name='lp2-gui-initialize',daemon=True).start()
            self._message('Listening at127.0.0.1:'+str(cfg.get('tcp_port',23230)))
            srv.run()
        except Exception as exc:
            with self._lock:self._error=str(exc)
            self._message('Connection failed: '+str(exc))
        finally:
            if self._controller and self._controller._worker and self._controller._worker.is_alive():
                self._controller._stop.set()
                while self._controller._worker.is_alive():self._controller._worker.join(1)
            if serial_cap:serial_cap.close()
            if tcp_cap:tcp_cap.close()
            with self._lock:self._stopping=False

    def stop(self):
        with self._lock:
            self._stopping=True;srv=self._server
        if srv:srv.stop()

    def cancel_job(self):
        with self._lock:c=self._controller
        if c:c.cancel_pending(detach=False)

    def submit_pattern(self,size_mm,power_percent,speed_mm_s,corrected):
        with self._lock:c=self._controller
        if c is None:raise RuntimeError('connect to the LP2 first')
        raw=calibration.measurement_pattern(size_mm,power_percent,speed_mm_s)
        c.submit_internal_job(raw,corrected=corrected)

    def submit_quality_pattern(self,power_percent=1,speed_mm_s=100,*,full=False,swap_axes=False):
        from .quality import pattern
        with self._lock:c=self._controller
        if c is None:raise RuntimeError('connect to the LP2 first')
        # Baseline is deliberately independent of PC correction. Rotation and
        # the ordinary power/transport/ownership checks still apply.
        c.submit_internal_job(pattern(power_percent,speed_mm_s,full=full,swap_axes=swap_axes),corrected=False)
        recipe=dict(power_percent=power_percent,speed_mm_s=speed_mm_s,full=full,swap_axes=swap_axes,
                    interval_mm=.125,patch_size_mm=6)
        if c.config.capture:c.config.capture.event('quality_pattern_recipe',**recipe)
        self._message(f'Quality test: {"all 8" if full else "patch 1"}; {"swapped axes" if swap_axes else "original axes"}; {power_percent}% / {speed_mm_s:g} mm/s')

    def apply_profile(self,profile):
        calibration.Calibration.from_profile(profile)
        with self._lock:c=self._controller
        if self._thread and self._thread.is_alive():
            if c is None:raise RuntimeError('wait for the connection to initialize')
            c.apply_profile(copy.deepcopy(profile))
        with self._lock:self._profile=copy.deepcopy(profile)

    def snapshot(self):
        with self._lock:
            running=bool(self._thread and self._thread.is_alive());c=self._controller;srv=self._server
            error=self._error;stopping=self._stopping;messages=list(self._messages);profile=copy.deepcopy(self._profile)
        state='disconnected';position=(0.,0.);progress=temp=None;internal=False;ready=False
        if c:
            with c._lock:
                ready=bool(c._ready.is_set() and c._worker and c._worker.is_alive() and c._state!='alarm')
                state=c._state if running else 'disconnected';position=c._host_pos;internal=c._internal_job
                if c._state=='alarm' and c._ready.is_set() and running:error=c.last_error
                if c._last_status:progress=c._last_status.get('rate');temp=c._last_status.get('temp')
        return dict(connected=ready,running=running,starting=running and not ready and not error,
            stopping=stopping,state=state,position=position,progress=progress,temperature=temp,error=error,
            client_connected=bool(srv and srv._client is not None),internal_job=internal,
            profile_name=profile.get('name','Measured profile') if profile else 'Uncalibrated',messages=messages)
