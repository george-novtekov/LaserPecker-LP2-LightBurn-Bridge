"""Single-owner stock LP2 serial transactions. No firmware/settings/delete API."""
from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path
import threading
import time

from .codec import protocol as p


class TransportError(RuntimeError):
    pass


class Capture:
    """Append-only, ordered raw bytes and state events; never overwrite a capture."""
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open('x', encoding='utf-8', buffering=1)
        self.lock = threading.Lock()
        self.seq = 0

    def event(self, kind, **fields):
        with self.lock:
            self.seq += 1
            row = dict(seq=self.seq, utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                       monotonic=time.monotonic(), kind=kind, **fields)
            self.file.write(json.dumps(row) + '\n')

    def close(self):
        with self.lock:
            self.file.close()


def open_windows_port(name='COM3'):
    import serial
    from serial.tools import list_ports
    matches = [port for port in list_ports.comports()
               if port.device == name and (port.vid, port.pid) == (0x1a86, 0x7523)]
    if len(matches) != 1:
        raise TransportError('port does not match the observed CH340 LP2 bridge')
    port = serial.Serial(port=None, baudrate=460800, bytesize=8, parity='N', stopbits=1,
                         timeout=.03, write_timeout=.5, xonxoff=False, rtscts=False, dsrdtr=False)
    port.dtr = False
    port.rts = False
    port.port = name
    port.open()
    return port


class LP2Session:
    """Methods must all run in one worker thread; cancel flag can be set by another.

    Physical bytes and timing are logged by the supplied Capture. Invalid or
    unexpected replies fail the transaction. A failed bulk upload poisons the
    session: it cannot send framed bytes into the uncertain raw receive state.
    """
    def __init__(self, port, capture, *, timeout=3.0):
        self.port, self.capture, self.timeout = port, capture, timeout
        self.decoder = p.StreamDecoder()
        self.owner = threading.get_ident()
        self.pending = []
        self.poisoned = False
        self.closed = False
        self.phase = 'idle'
        self._announce_exit_echo = None
        self._allow_zero_exit = False
        self.capture.event('serial_opened', transport=getattr(port,'transport_kind','usb'),
                           baud=None if getattr(port,'transport_kind','usb')=='bluetooth' else 460800,
                           note='Bluetooth UART' if getattr(port,'transport_kind','usb')=='bluetooth' else 'DTR/RTS requested False; line glitches unmeasured')

    def _owner(self):
        if threading.get_ident() != self.owner:
            raise TransportError('serial access attempted outside its owning worker')
        if self.closed or self.poisoned:
            raise TransportError('serial session closed or uncertain; no automatic recovery')

    def _write(self, data, purpose):
        self._owner()
        written = self.port.write(data)
        self.capture.event('serial_tx', purpose=purpose, hex=data[:written].hex(), n=written)
        if written != len(data):
            raise TransportError('short serial write; no retry')

    def _frame(self, predicate, timeout=None):
        self._owner()
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while time.monotonic() < deadline:
            while self.pending:
                frame = self.pending.pop(0)
                if not frame.checksum_ok:
                    raise TransportError('reply checksum failed')
                if predicate(frame):
                    return frame
                if frame.func == 0xff:
                    if self._announce_exit_echo is not None and frame.payload == self._announce_exit_echo:
                        self._announce_exit_echo = None
                        self.capture.event('async_transfer_exit', hex=frame.raw.hex())
                        continue
                    if self._allow_zero_exit and frame.payload == bytes(5):
                        self._allow_zero_exit = False
                        self.capture.event('async_print_exit', hex=frame.raw.hex(),
                                           note='Exit notice only; fresh status must still confirm completion')
                        continue
                    raise TransportError('unexpected func FF reply; no completion inferred')
                self.capture.event('unmatched_reply', hex=frame.raw.hex())
            chunk = self.port.read(min(4096, max(1, self.port.in_waiting)))
            if chunk:
                self.capture.event('serial_rx', hex=chunk.hex(), n=len(chunk))
                try:
                    self.pending.extend(self.decoder.feed(chunk))
                except p.CodecError as exc:
                    raise TransportError(str(exc)) from exc
            else:
                time.sleep(.002)
        raise TransportError('LP2 reply timeout')

    def _exchange(self, request, purpose, predicate, timeout=None):
        self._write(request, purpose)
        return self._frame(predicate, timeout)

    def status(self):
        frame = self._exchange(p.query_status(), 'status',
                               lambda f: f.kind == 'short' and f.func == 0 and len(f.raw) >= 27)
        status = p.parse_status_long(frame)
        self.capture.event('device_status', **status)
        return status

    def identify(self):
        frame = self._exchange(p.query_version(), 'version',
                               lambda f: f.kind == 'short' and f.func == 0 and len(f.raw) == 14)
        version = p.parse_version(frame)
        if version['sw_field'] != 376 or version['hw_hex'] != '01321e86':
            raise TransportError('device differs from the surveyed LP2 V376; requalify first')
        self.capture.event('identity', **version)
        return version

    def files(self):
        frame = self._exchange(p.query_file_ids(), 'file_ids',
                               lambda f: f.kind == 'short' and f.func == 0 and len(f.raw) >= 7)
        return p.parse_file_list(frame)

    @staticmethod
    def _file_ack(frame):
        return (frame.kind == 'short' and frame.func == 5 and len(frame.payload) >= 2)

    def upload(self, header: bytes, payload: bytes, cancel: threading.Event):
        """Ordinary job-storage write only; reviewed header and payload required."""
        self._owner()
        if (len(header) != 64 or header[0] not in (0x20,0x60) or not isinstance(payload, bytes)
                or not 0 < len(payload) <= 1048576):
            raise TransportError('invalid or oversized stock job')
        if header[0]==0x60:
            w,h=int.from_bytes(header[1:3],'big'),int.from_bytes(header[3:5],'big')
            nx,ny=int.from_bytes(header[10:12],'big'),int.from_bytes(header[12:14],'big')
            px=header[9];limit=4000//px if px else 0
            if (not 1<=w<=limit or not 1<=h<=limit or nx+w>limit or ny+h>limit
                    or not px or int.from_bytes(header[14:16],'big')!=round(1016/px) or header[16]!=0 or header[21]!=0
                    or len(payload)!=((w+7)//8)*h):
                raise TransportError('invalid native packed bitmap geometry or payload length')
            pad=(8-w%8)%8
            if pad and any(value & ((1<<pad)-1)!=(1<<pad)-1 for value in payload[(w+7)//8-1::(w+7)//8]):
                raise TransportError('bitmap row padding must be white')
        if cancel.is_set():
            raise TransportError('cancelled before upload')
        status = self.status()
        if status['mode'] != 6 or status['w_state'] == 4 or status['error']:
            raise TransportError('LP2 is not idle and error-free')
        if getattr(self.port,'transport_kind','usb')=='bluetooth' and status['u_b_cnn']!=1:
            raise TransportError('Bluetooth upload requires USB unplugged and external LP2 power; restart LP2 after a failed upload')
        # Vendor sends EXIT before announcing a job, never after its raw payload.
        self._exchange(p.stop_command(), 'exit_before_upload', lambda f: f.func == 0xff)
        self.phase = 'announcing'
        try:
            # V376 emits this exact echo after its successful file ACK. It
            # may be concatenated with that ACK or arrive on the next read.
            self._announce_exit_echo = b'\x01' + (64+len(payload)).to_bytes(4,'big')
            ack = self._exchange(p.announce_gcode(len(payload)), 'announce_gcode', self._file_ack, 10)
            if ack.payload[1] != 1:
                raise TransportError('LP2 declined job storage')
            self.phase = 'raw_upload'
            self.capture.event('job_upload', bytes=len(payload), header_hex=header.hex(),
                               payload_sha256=hashlib.sha256(payload).hexdigest())
            for data, purpose in ((header, 'gcode_header'), (payload, 'gcode_payload')):
                for offset in range(0, len(data), getattr(self.port,'upload_chunk_size',80)):
                    if cancel.is_set():
                        raise TransportError('cancelled during raw upload; physical receive state uncertain')
                    chunk_size=getattr(self.port,'upload_chunk_size',80)
                    self._write(data[offset:offset+chunk_size], purpose)
                    time.sleep(getattr(self.port,'upload_chunk_delay',.015))
            ack = self._frame(self._file_ack, 10)
            if ack.payload[1] != 1:
                raise TransportError('LP2 did not accept the completed upload')
            self.phase = 'uploaded'
            self.capture.event('job_upload_accepted', reply_hex=ack.raw.hex())
        except Exception:
            self.poisoned = True
            self.capture.event('serial_uncertain', phase=self.phase,
                               note='No framed stop/reset is sent into a possibly raw receiver; power cycle may be required')
            raise

    def start(self, *, file_id, nx, ny, laser_percent=100, wire_depth=1):
        self._owner()
        if self.phase != 'uploaded':
            raise TransportError('start requires this session to have just uploaded a job')
        if type(wire_depth) is not int or not 1 <= wire_depth <= 100:
            raise TransportError('wire depth must be an integer in 1..100')
        # LP2's stock DDA rate comes from this timer setting, not G-code F.
        request = p.print_command(state=1, laser=laser_percent, depth=wire_depth, file_id=file_id,
                                  nx=nx, ny=ny, times=1, frequency=60)
        # If the acknowledgement is lost, the device may already be moving;
        # mark this as active so the caller can still send the stock stop.
        self.phase = 'running'
        ack = self._exchange(request, 'print_start',
                             lambda f: f.func == 1 and f.payload[:1] == b'\x01')
        if ack.payload != b'\x01\x00\x00\x00\x00':
            raise TransportError('unexpected V376 print-start acknowledgement')
        self._allow_zero_exit = True
        self.capture.event('print_start_acknowledged', file_id=file_id, hex=ack.raw.hex())

    def control(self, command):
        self._owner()
        if self.phase not in ('running', 'paused'):
            raise TransportError('control requires an active uploaded job')
        builders = {'pause': p.print_pause, 'resume': p.print_resume, 'stop': p.print_stop}
        if command not in builders:
            raise TransportError('unsupported job control')
        if command == 'stop': self._allow_zero_exit = True
        self._write(builders[command](), 'print_' + command)
        self.phase = {'pause':'paused', 'resume':'running', 'stop':'stopping'}[command]

    def close(self):
        if not self.closed:
            self.port.close()
            self.closed = True
            self.capture.event('serial_closed', phase=self.phase, uncertain=self.poisoned)
