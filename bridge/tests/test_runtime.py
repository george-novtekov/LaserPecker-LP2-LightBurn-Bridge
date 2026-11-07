"""Device-authoritative completion and cancellation regressions, entirely offline.

Based on Coder's independent scenarios; supervisor corrected the fixture's ID
endianness, control dispatch, invalid M8 input and socket/cleanup races.
"""
import select
import socket
import threading
import time
import unittest

from bridge import runtime as rt, server as srv
from bridge.codec import protocol as p
from bridge.tests.test_transport import FakeSerial

CFG=dict(poll_interval=.02,start_timeout=.2,job_budget=1.5,idle_spool_timeout=.25)
JOB_LINES=[b'G21',b'G90',b'G0X10Y10',b'M3S100',b'G1X11Y10F120',b'G0X50Y50',b'M2']

class Log:
    def __init__(self): self.rows=[]
    def event(self,kind,**fields): self.rows.append(dict(kind=kind,**fields))

class FakeDevice(FakeSerial):
    def __init__(self,*,active_polls=3,hang_start=False,error_after_start=0,bad_checksum=False,raw_gate=False,**kw):
        super().__init__(**kw)
        self.active_polls,self.hang_start,self.error_after_start=active_polls,hang_start,error_after_start
        self.bad_checksum,self.raw_gate=bad_checksum,raw_gate
        self.starts,self.controls=[],[]
        self._phase,self._w_state,self._file_id,self._active_left='idle',0,0,0
        self.raw_entered,self.raw_release=threading.Event(),threading.Event()
        self.statuses=[]
        self.reported_job_id=None

    def _status(self):
        active=self._phase=='active' and (self._active_left>0 or self._w_state==4)
        if active and self._w_state!=4: self._active_left-=1
        body=bytearray(21)
        body[0],body[1],body[5],body[20]=1 if active else 6,(self._w_state or 1) if active else 0,self.error_after_start if active else 0,2
        reported=self._file_id if self.reported_job_id is None else self.reported_job_id
        body[7:11]=reported.to_bytes(4,'big')
        frame=p.encode_frame(0,body)
        self.statuses.append((body[0],body[1],body[5],reported))
        return frame

    def write(self,data):
        data=bytes(data)
        if self.raw_remaining is not None:
            if self.raw_gate and not self.raw_entered.is_set():
                self.raw_entered.set(); self.raw_release.wait(1)
            return super().write(data)
        if data==p.query_status():
            self.writes.append(data)
            frame=self._status()
            self.rx.extend(frame[:-1]+bytes([frame[-1]^1]) if self.bad_checksum else frame)
            return len(data)
        if data in (p.print_pause(),p.print_resume(),p.print_stop()):
            self.writes.append(data);self.controls.append(data)
            if data==p.print_pause(): self._w_state=4
            elif data==p.print_resume(): self._w_state=2
            else: self._phase='idle';self._w_state=3
            return len(data)
        if data[:2]==b'\xaa\xbb' and len(data)>=7 and data[3:5]==b'\x01\x01':
            self.writes.append(data);self.starts.append(data)
            self._file_id=int.from_bytes(data[7:11],'big')
            self._phase='idle' if self.hang_start else 'active'
            self._active_left=self.active_polls;self._w_state=0
            self.rx.extend(p.encode_frame(1,b'\x01'+bytes(4)))
            return len(data)
        return super().write(data)

def wait_for(predicate,timeout=2):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        if predicate(): return True
        time.sleep(.005)
    return bool(predicate())

def no_job_writes(device):
    return all(data[:2]==b'\xaa\xbb' and data[3]==0 for data in device.writes)

def feed_job(controller):
    return [controller.submit_line(line) for line in JOB_LINES]

class RuntimeBase(unittest.TestCase):
    def make(self,device=None,**changes):
        device=device or FakeDevice()
        calls=[]
        def factory(name): calls.append(name);return device
        controller=rt.Controller(rt.RuntimeConfig(port_factory=factory,capture=Log(),**(CFG|changes)))
        controller.factory_calls=calls
        self.addCleanup(controller.shutdown)
        return controller,device

    def start(self,controller,acks=None):
        acks=[] if acks is None else acks
        controller.start(on_m2_ack=lambda:acks.append(time.monotonic()))
        self.assertTrue(controller._ready.wait(2))
        return acks

class ControllerTests(RuntimeBase):
    def test_picture_capture_exceeds_20000_lines_without_job_io(self):
        c,d=self.make(read_only=True,idle_spool_timeout=2);self.start(c)
        for line in (b'G0X30Y30',b'M4',b'M8',b'G91'):
            self.assertEqual(c.submit_line(line),'ok')
        for i in range(25000):
            self.assertEqual(c.submit_line(b'G1 X0.1S200' if i%2 else b'G1 X-0.1S0'),'ok')
        self.assertEqual(c.submit_line(b'M2'),'error:20')
        self.assertIn('read-only capture complete',c.last_error)
        self.assertTrue(no_job_writes(d));self.assertEqual(d.starts,[])
        captured=[r for r in c.config.capture.rows if r['kind']=='whole_job_captured']
        self.assertEqual(captured[0]['input_lines'],25005)

    def test_final_m2_waits_for_matching_active_then_two_idle(self):
        c,d=self.make(FakeDevice(active_polls=100))
        acks=self.start(c)
        for line in JOB_LINES[:-1]: self.assertEqual(c.submit_line(line),'ok')
        self.assertIsNone(c.submit_line(b'M2'))
        self.assertIn('MPos:0.000,0.000',c.status_report())
        self.assertTrue(wait_for(lambda:d.starts))
        time.sleep(.05)
        self.assertEqual(acks,[])
        self.assertIn('Run',c.status_report())
        d._active_left=0
        self.assertTrue(wait_for(lambda:acks))
        self.assertEqual(len(acks),1)
        self.assertIn('Idle|MPos:50.000,50.000',c.status_report())
        self.assertEqual(d.statuses[-2:],[ (6,0,0,d._file_id) ]*2)

    def test_late_invalid_job_sends_no_job_bytes(self):
        c,d=self.make();acks=self.start(c)
        for line in JOB_LINES[:-1]: c.submit_line(line)
        c.submit_line(b'G1X12S900')
        self.assertTrue(c.submit_line(b'M2').startswith('error:'))
        time.sleep(.05)
        self.assertIn('Idle',c.status_report());self.assertTrue(no_job_writes(d));self.assertEqual(acks,[])
        self.assertIsNone(feed_job(c)[-1])
        self.assertTrue(wait_for(lambda:acks))
        self.assertEqual(len(d.starts),1)

    def test_invalid_terminal_block_does_not_release_stream_tail(self):
        c,d=self.make();self.start(c)
        for line in JOB_LINES[:-1]:c.submit_line(line)
        self.assertEqual(c.submit_line(b'M2 G1 X12'),'error:20')
        self.assertIn('Alarm',c.status_report())
        self.assertEqual(c.submit_line(b'G0X10Y10'),'error:9')
        self.assertTrue(no_job_writes(d))

    def test_preflight_rejection_does_not_clear_stale_device_state(self):
        c,d=self.make();self.start(c)
        with c._lock:
            for line in JOB_LINES[:-1]:c.submit_line(line)
            c.submit_line(b'S800')
            c._last_status_mono=time.monotonic()-5
            self.assertEqual(c.submit_line(b'M2'),'error:20')
            self.assertIn('Alarm',c.status_report())
        self.assertTrue(no_job_writes(d))

    def test_upload_refusal_and_bad_checksum_do_not_ack(self):
        for device in (FakeDevice(refuse=True),FakeDevice(bad_checksum=True)):
            with self.subTest(device=device):
                c,d=self.make(device);acks=self.start(c)
                feed_job(c)
                self.assertTrue(wait_for(lambda:'Alarm' in c.status_report()))
                self.assertEqual(acks,[]);self.assertEqual(d.starts,[])
                c.shutdown()

    def test_never_active_and_device_error_fail_without_ack(self):
        for device in (FakeDevice(hang_start=True),FakeDevice(error_after_start=5)):
            with self.subTest(device=device):
                c,d=self.make(device);acks=self.start(c)
                feed_job(c)
                self.assertTrue(wait_for(lambda:'Alarm' in c.status_report()))
                self.assertEqual(acks,[])
                self.assertTrue(wait_for(lambda:d.closed))
                self.assertTrue(d.controls)

    def test_read_only_and_partial_timeout_prevent_job_writes(self):
        c,d=self.make(read_only=True);self.start(c)
        self.assertTrue(feed_job(c)[-1].startswith('error:'))
        self.assertTrue(no_job_writes(d))
        other,dev=self.make(idle_spool_timeout=.05);self.start(other)
        other.submit_line(b'G0X10Y10')
        self.assertTrue(wait_for(lambda:'Alarm' in other.status_report()))
        self.assertTrue(no_job_writes(dev))

    def test_cancel_staged_job_never_starts_on_resume(self):
        c,d=self.make();acks=self.start(c)
        c.realtime('!');feed_job(c);c.cancel_pending();c.realtime('~')
        time.sleep(.1)
        self.assertEqual(d.starts,[]);self.assertTrue(no_job_writes(d));self.assertEqual(acks,[])

    def test_raw_upload_cancel_closes_without_framed_control(self):
        c,d=self.make(FakeDevice(raw_gate=True));acks=self.start(c)
        feed_job(c)
        self.assertTrue(d.raw_entered.wait(2))
        c.cancel_pending();d.raw_release.set()
        self.assertTrue(wait_for(lambda:d.closed))
        self.assertTrue(c._poisoned);self.assertEqual(d.starts,[]);self.assertEqual(d.controls,[]);self.assertEqual(acks,[])
        self.assertTrue(any(r['kind']=='serial_uncertain' for r in c.config.capture.rows))

    def test_pause_resume_then_stop_are_confirmed_by_status(self):
        c,d=self.make(FakeDevice(active_polls=1000));acks=self.start(c)
        feed_job(c);self.assertTrue(wait_for(lambda:d.starts))
        c.realtime('!');self.assertTrue(wait_for(lambda:'Hold' in c.status_report()))
        self.assertIn(p.print_pause(),d.controls)
        c.realtime('~');self.assertTrue(wait_for(lambda:p.print_resume() in d.controls and 'Run' in c.status_report()))
        c.realtime('\x18');self.assertTrue(wait_for(lambda:p.print_stop() in d.controls))
        self.assertTrue(wait_for(lambda:not c._started));self.assertEqual(acks,[])
        before=len(d.starts);c.realtime('~');time.sleep(.05);self.assertEqual(len(d.starts),before)
        c.shutdown();self.assertTrue(d.closed)

    def test_shutdown_stops_active_job_and_closes(self):
        c,d=self.make(FakeDevice(active_polls=1000));self.start(c);feed_job(c)
        self.assertTrue(wait_for(lambda:d.starts));c.shutdown()
        self.assertIn(p.print_stop(),d.controls);self.assertTrue(d.closed)

    def test_reconnect_and_parseable_virtual_grbl(self):
        c,d=self.make();self.start(c);c.cancel_pending();c.start(on_m2_ack=lambda:None)
        self.assertEqual(c.factory_calls,['COM3'])
        for command in ('$I','$$','$G','$#'): self.assertTrue(c.dollar_command(command).endswith('\nok'))
        self.assertIn('$30=1000\n',c.dollar_command('$$'))
        for command in ('$H','$J=G91X1','$110=100'):
            with self.assertRaises(rt.RuntimeCommandError): c.dollar_command(command)

    def test_startup_modal_preamble_does_not_arm_spool_timeout(self):
        c,d=self.make(idle_spool_timeout=.05);self.start(c)
        self.assertEqual(c.submit_line(b'G0'),'ok')
        time.sleep(.1)
        self.assertIn('Idle',c.status_report());self.assertTrue(no_job_writes(d))

    def test_v376_delayed_status_id_requires_eventual_matching_id(self):
        c,d=self.make(FakeDevice(active_polls=100));d.reported_job_id=0;acks=self.start(c)
        feed_job(c);self.assertTrue(wait_for(lambda:any(s[0]==1 and s[3]==0 for s in d.statuses)))
        self.assertEqual(acks,[])
        d.reported_job_id=None;d._active_left=1
        self.assertTrue(wait_for(lambda:acks))
        self.assertTrue(any('still initializing' in event for event in c._events))
        self.assertTrue(any(r['kind']=='print_start_acknowledged' for r in c.config.capture.rows))

    def test_never_populated_status_id_cannot_complete(self):
        c,d=self.make();d.reported_job_id=0;acks=self.start(c)
        feed_job(c);self.assertTrue(wait_for(lambda:'Alarm' in c.status_report()))
        self.assertEqual(acks,[])

    def test_different_nonzero_status_id_fails_without_success(self):
        c,d=self.make();d.reported_job_id=3;acks=self.start(c)
        feed_job(c);self.assertTrue(wait_for(lambda:'Alarm' in c.status_report()))
        self.assertEqual(acks,[])

    def test_repeated_jobs_get_distinct_committed_ids(self):
        c,d=self.make();acks=self.start(c)
        feed_job(c);self.assertTrue(wait_for(lambda:len(acks)==1))
        feed_job(c);self.assertTrue(wait_for(lambda:len(acks)==2))
        ids=[int.from_bytes(data[7:11],'big') for data in d.starts]
        self.assertEqual(len(set(ids)),2)

class ServerTests(RuntimeBase):
    def test_second_listener_cannot_share_active_bridge_port(self):
        server,d,cap=self.serve()
        other,device=self.make()
        duplicate=srv.BridgeServer(controller=other,tcp_port=server.bound_port)
        with self.assertRaises(OSError):duplicate.run(duration=.05)
        self.assertEqual(device.writes,[])

    def serve(self):
        c,d=self.make(FakeDevice(active_polls=100))
        capture=Log();server=srv.BridgeServer(controller=c,capture=capture,tcp_port=0)
        thread=threading.Thread(target=server.run,kwargs={'duration':5},daemon=True);thread.start()
        def cleanup(): server.stop();thread.join(2)
        self.addCleanup(cleanup)
        self.assertTrue(server.ready.wait(2));self.buffers={}
        return server,d,capture

    def line(self,sock,timeout=2):
        buffer=self.buffers.get(sock,b'');end=time.monotonic()+timeout
        while b'\n' not in buffer:
            remaining=end-time.monotonic()
            if remaining<=0 or not select.select([sock],[],[],remaining)[0]: raise TimeoutError('TCP line')
            data=sock.recv(4096)
            if not data: raise EOFError('TCP closed')
            buffer+=data
        line,buffer=buffer.split(b'\n',1);self.buffers[sock]=buffer
        return line+b'\n'

    def connect(self,server):
        sock=socket.create_connection(('127.0.0.1',server.bound_port),timeout=2);self.addCleanup(sock.close)
        self.assertTrue(self.line(sock).startswith(b'Grbl'))
        self.assertIn(b'host estimates',self.line(sock))
        self.assertTrue(server.controller._ready.wait(2))
        return sock

    def test_real_socket_final_m2_barrier_and_capture(self):
        server,d,cap=self.serve();sock=self.connect(server)
        sent=b''
        for raw in JOB_LINES[:-1]:
            sock.sendall(raw+b'\n');sent+=raw+b'\n';self.assertEqual(self.line(sock),b'ok\n')
        sock.sendall(b'M2\n');sent+=b'M2\n'
        self.assertTrue(self.line(sock).startswith(b'[MSG:LP2 SPEED:'))
        self.assertTrue(wait_for(lambda:d.starts));self.assertFalse(select.select([sock],[],[],.05)[0])
        d._active_left=0
        self.assertEqual(self.line(sock),b'ok\n')
        self.assertEqual(b''.join(bytes.fromhex(r['hex']) for r in cap.rows if r['kind']=='tcp_rx'),sent)
        self.assertEqual(len(d.starts),1)

    def test_fragments_realtime_and_comment_finish(self):
        server,d,cap=self.serve();sock=self.connect(server)
        sock.sendall(b'G0X1');sock.sendall(b'?0Y10 (M2)\n')
        self.assertIn(b'MPos:',self.line(sock));self.assertEqual(self.line(sock),b'ok\n')
        sock.sendall(b'G1X11!Y11F120S100\n');self.assertEqual(self.line(sock),b'ok\n')
        self.assertEqual(d.starts,[])

    def test_poll_with_newline_never_acknowledges_pending_m2(self):
        server,d,cap=self.serve();sock=self.connect(server)
        for line in JOB_LINES[:-1]:
            sock.sendall(line+b'\n');self.assertEqual(self.line(sock),b'ok\n')
        sock.sendall(b'M2\n');self.assertTrue(wait_for(lambda:d.starts))
        self.assertTrue(self.line(sock).startswith(b'[MSG:LP2 SPEED:'))
        for _ in range(3):
            sock.sendall(b'?\n');self.assertIn(b'MPos:',self.line(sock))
            self.assertFalse(select.select([sock],[],[],.02)[0], 'poll newline must not emit an extra ok')
        d._active_left=0;self.assertEqual(self.line(sock),b'ok\n')

    def test_overflow_latches_alarm_and_second_client_is_refused(self):
        server,d,cap=self.serve();sock=self.connect(server)
        second=socket.create_connection(('127.0.0.1',server.bound_port),timeout=2);self.addCleanup(second.close)
        self.assertNotIn(b'ok',self.line(second))
        sock.sendall(b'G0X1Y1;'+b'x'*300+b'\n')
        self.assertTrue(self.line(sock).startswith(b'error:'))
        sock.sendall(b'G0X10Y10\nM2\n')
        self.assertTrue(self.line(sock).startswith(b'error:'));self.assertTrue(self.line(sock).startswith(b'error:'))
        self.assertEqual(d.starts,[])

    def test_rejected_job_explains_reason_in_lightburn_console(self):
        server,d,cap=self.serve();sock=self.connect(server)
        for line in (b'G0X10Y10',b'M7',b'G1X11F6000S200'):
            sock.sendall(line+b'\n');self.assertEqual(self.line(sock),b'ok\n')
        sock.sendall(b'M2\n')
        self.assertIn(b'unsupported M7',self.line(sock))
        self.assertEqual(self.line(sock),b'error:20\n')
        self.assertEqual(d.starts,[])

    def test_over_cap_rejection_allows_corrected_job_on_same_connection(self):
        server,d,cap=self.serve();sock=self.connect(server)
        for line in (b'G0X10Y10',b'M3S800',b'G1X11F6000'):
            sock.sendall(line+b'\n');self.assertEqual(self.line(sock),b'ok\n')
        sock.sendall(b'M2\n')
        self.assertIn(b'exceeds host cap 200',self.line(sock))
        self.assertEqual(self.line(sock),b'error:20\n')
        sock.sendall(b'?\n');self.assertIn(b'<Idle|',self.line(sock))
        self.assertEqual(d.starts,[]);self.assertTrue(no_job_writes(d))
        for line in JOB_LINES[:-1]:
            sock.sendall(line+b'\n');self.assertEqual(self.line(sock),b'ok\n')
        sock.sendall(b'M2\n')
        self.assertIn(b'SPEED:',self.line(sock))
        self.assertTrue(wait_for(lambda:d.starts));d._active_left=0
        self.assertEqual(self.line(sock),b'ok\n')
        self.assertEqual(len(d.starts),1)

if __name__=='__main__': unittest.main()
