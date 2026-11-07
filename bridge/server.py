"""Loopback LightBurn endpoint for the experimental stock-USB M3 vector bridge."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import selectors
import socket
import threading
import time
import sys

from . import runtime as rt
from . import transport as t

BANNER="Grbl 1.1h ['$' for help]\n[MSG:LP2 bridge scan-8; positions and settings are host estimates]"

class BridgeServer:
    def __init__(self, *, tcp_host='127.0.0.1',tcp_port=23230,controller=None,capture=None,config=None):
        if tcp_host!='127.0.0.1': raise ValueError('loopback only')
        self.tcp_host,self.tcp_port=tcp_host,tcp_port
        self.controller=controller or rt.Controller(config)
        self.capture=capture
        self._sel=selectors.DefaultSelector()
        self._client=None
        self._client_lock,self._send_lock=threading.RLock(),threading.Lock()
        self._stop,self.ready=threading.Event(),threading.Event()
        self._buffer=bytearray()
        self._discard,self._was_cr=False,False
        self._realtime_only=False
        self._log=lambda message: None
        self.bound_port=None

    def _cap(self,kind,data):
        if self.capture: self.capture.event(kind,hex=data.hex(),n=len(data))

    def _send(self,conn,text):
        data=(text.rstrip('\n')+'\n').encode('ascii')
        try:
            with self._send_lock:
                offset=0
                while offset<len(data):
                    sent=conn.send(data[offset:])
                    if not sent: raise OSError('socket closed')
                    self._cap('tcp_tx',data[offset:offset+sent])
                    offset+=sent
        except OSError:
            self.controller.cancel_pending()
            try: conn.shutdown(socket.SHUT_RDWR)
            except OSError: pass

    def _accept(self,listener):
        conn,_=listener.accept()
        conn.settimeout(.5)
        with self._client_lock:
            if self._client is not None:
                try: conn.sendall(b'[MSG:LP2 bridge already has a client]\n')
                finally: conn.close()
                return
            self._client=conn
        self._buffer.clear()
        self._discard,self._was_cr=False,False
        self._realtime_only=False
        self._sel.register(conn,selectors.EVENT_READ,self._read)
        self.controller.start(on_m2_ack=lambda:self._m2_ack(conn),on_notice=lambda message:self._notice(conn,message))
        self.controller._ready.wait(7)
        self._send(conn,BANNER)
        if self.controller._state=='alarm' and self.controller.last_error:
            self._send(conn,'[MSG:LP2 '+self.controller.last_error.replace('[','(').replace(']',')')[:220]+']')

    def _m2_ack(self,conn):
        with self._client_lock:
            if self._client is conn: self._send(conn,'ok')

    def _notice(self,conn,message):
        self._log(message)
        # Actual power-start audit stays in GUI/capture logs. Keep the existing
        # TCP status/final-M2 exchange quiet while a physical stage is running.
        if message.startswith(('SPEED:','RASTER:','SCAN:','CALIBRATION:')) and self.controller._ready.is_set():
            with self._client_lock:
                if self._client is conn:self._send(conn,'[MSG:LP2 '+message+']')

    def _line(self,conn,raw):
        if not raw: self._send(conn,'ok'); return
        if raw.startswith(b'$'):
            try:
                response=self.controller.dollar_command(raw.decode('ascii'))
                self._send(conn,response or 'ok')
            except (UnicodeError,rt.RuntimeCommandError): self._send(conn,'error:20')
        else:
            response=self.controller.submit_line(raw)
            if response=='error:20':
                detail=self.controller.last_error.replace('[','(').replace(']',')').replace('\n',' ').replace('\r',' ')[:240]
                self._send(conn,'[MSG:LP2 '+detail+']')
            if response is not None: self._send(conn,response)

    def _read(self,conn):
        try: data=conn.recv(4096)
        except OSError: data=b''
        if not data: self._disconnect(conn); return
        self._cap('tcp_rx',data)
        for byte in data:
            if byte in (24,ord('?'),ord('!'),ord('~')):
                if not self._buffer: self._realtime_only=True
                response=self.controller.realtime(chr(byte))
                if response: self._send(conn,response)
                if byte==24:
                    self._buffer.clear()
                    self._discard=False
                    # Remaining bytes in this already-received chunk may be
                    # queued commands from the cancelled job: discard them.
                    break
                continue
            if byte in (10,13):
                if byte==10 and self._was_cr:
                    self._was_cr=False
                    continue
                if not self._discard and not (self._realtime_only and not self._buffer): self._line(conn,bytes(self._buffer))
                self._buffer.clear()
                self._discard=False
                self._realtime_only=False
                self._was_cr=byte==13
            else:
                self._was_cr=False
                self._realtime_only=False
                if self._discard: continue
                self._buffer.append(byte)
                if len(self._buffer)>256:
                    self.controller._fail_alarm('TCP line exceeds 256 bytes')
                    self._send(conn,'error:20')
                    self._buffer.clear()
                    self._discard=True

    def _disconnect(self,conn):
        with self._client_lock:
            if self._client is not conn: return
            self._client=None
        try: self._sel.unregister(conn)
        except (ValueError,KeyError): pass
        conn.close()
        self._buffer.clear()
        self.controller.cancel_pending()

    def run(self,*,duration=None):
        listener=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
        try:
            if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):
                listener.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
            else:listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            listener.bind((self.tcp_host,self.tcp_port))
        except OSError:
            listener.close();self._sel.close();self.controller.shutdown()
            raise
        self.bound_port=listener.getsockname()[1]
        listener.listen(2)
        self._sel.register(listener,selectors.EVENT_READ,self._accept)
        self.ready.set()
        deadline=time.monotonic()+duration if duration is not None else None
        try:
            while not self._stop.is_set() and (deadline is None or time.monotonic()<deadline):
                for key,_ in self._sel.select(.05): key.data(key.fileobj)
        finally:
            if self._client: self._disconnect(self._client)
            self.controller.shutdown()
            self._sel.close()
            listener.close()

    def stop(self): self._stop.set()

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',default='COM3')
    parser.add_argument('--rotation',type=int,choices=(0,180),default=180,help='device rotation about the100x100mm bed (default180)')
    parser.add_argument('--mirror-x',action=argparse.BooleanOptionalAction,default=True)
    parser.add_argument('--transport',choices=['usb','bluetooth'],default='usb')
    parser.add_argument('--ble-address')
    parser.add_argument('--tcp-port',type=int,default=23230)
    parser.add_argument('--max-power',type=int,default=1000)
    parser.add_argument('--start-laser-percent',type=int,default=100)
    parser.add_argument('--capture-dir',type=Path,default=Path('bridge/evidence'))
    parser.add_argument('--label',required=True)
    parser.add_argument('--read-only',action='store_true')
    parser.add_argument('--duration',type=float,default=None,help='optional bounded test lifetime in seconds')
    parser.add_argument('--job-budget',type=float,default=600,help='maximum execution seconds per stock stage')
    parser.add_argument('--image-mode',choices=('continuous','bitmap'),default='continuous')
    parser.add_argument('--calibration',type=Path)
    args=parser.parse_args(argv)
    if not args.label or not all(c.isalnum() or c in '-_' for c in args.label): parser.error('invalid capture label')
    if not 1<=args.start_laser_percent<=100: parser.error('start laser percent must be 1..100')
    if args.duration is not None and not 0<args.duration<=600: parser.error('test duration must be in (0,600] seconds')
    if not 1<=args.job_budget<=7200:parser.error('job budget must be 1..7200 seconds')
    if args.transport=='bluetooth' and not args.ble_address:parser.error('Bluetooth address required')
    factory=None
    from .calibration import load_profile
    profile=load_profile(args.calibration) if args.calibration else None
    if args.transport=='bluetooth':
        from .ble import open_ble_port
        factory=open_ble_port
    tcp_cap=t.Capture(args.capture_dir/(args.label+'-tcp.jsonl'))
    serial_cap=t.Capture(args.capture_dir/(args.label+'-serial.jsonl'))
    here=Path(__file__).resolve().parent
    hashes=(json.loads((here/'build_manifest.json').read_text())['source_hashes'] if getattr(sys,'frozen',False) else
            {str(p.relative_to(here)):hashlib.sha256(p.read_bytes()).hexdigest() for p in here.rglob('*.py') if 'tests' not in p.parts})
    metadata=dict(scope='physical stock LP2 bridge',config={k:str(v) if isinstance(v,Path) else v for k,v in vars(args).items()},source_hashes=hashes)
    serial_cap.event('metadata',**metadata)
    tcp_cap.event('metadata',**metadata)
    cfg=rt.RuntimeConfig(port_name=args.ble_address if factory else args.port,port_factory=factory,
                         image_mode=args.image_mode,
                         rotation=args.rotation,
                         mirror_x=args.mirror_x,
                         calibration_profile=profile,
                         power_cap=args.max_power,capture=serial_cap,job_budget=args.job_budget,
                         read_only=args.read_only,start_laser_percent=args.start_laser_percent)
    server=BridgeServer(tcp_port=args.tcp_port,capture=tcp_cap,controller=rt.Controller(cfg))
    def log(message):
        serial_cap.event('notice',message=message)
        print(message,flush=True)
    server._log=log
    try: server.run(duration=args.duration)
    except KeyboardInterrupt: pass
    finally: tcp_cap.close();serial_cap.close()
    return 0

if __name__=='__main__': raise SystemExit(main())
