import socket
import tempfile
import select
import time
import unittest
from bridge.service import BridgeService
from bridge.tests.test_runtime import FakeDevice,wait_for,no_job_writes
from bridge.calibration import profile_from_measurements

class ServiceTests(unittest.TestCase):
    def make(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        device=FakeDevice();calls=[]
        def factory(name):calls.append(name);return device
        service=BridgeService(port_factory=factory,data_dir=folder.name)
        def stop():service.stop();self.assertTrue(wait_for(lambda:not service.snapshot()['running'],timeout=4))
        self.addCleanup(stop)
        return service,device,calls

    def test_connect_without_lightburn_only_queries_and_clean_disconnect(self):
        service,device,calls=self.make();service.start(dict(transport='usb',tcp_port=0,rotation=180))
        self.assertTrue(wait_for(lambda:service.snapshot()['connected']))
        self.assertEqual(len(calls),1);self.assertTrue(no_job_writes(device))
        self.assertFalse(service.snapshot()['client_connected'])
        service.stop();self.assertTrue(wait_for(lambda:device.closed))

    def test_duplicate_listener_fails_before_device_is_opened(self):
        service,device,calls=self.make()
        with socket.socket() as blocker:
            if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):blocker.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
            blocker.bind(('127.0.0.1',0));blocker.listen()
            service.start(dict(transport='usb',tcp_port=blocker.getsockname()[1]))
            self.assertTrue(wait_for(lambda:bool(service.snapshot()['error'])))
            self.assertEqual(calls,[])

    def test_profile_validation_does_not_connect_or_engage_device(self):
        service,device,calls=self.make()
        p=profile_from_measurements(80,[78,76,78],[79,78,79],name='Measured')
        service.apply_profile(p);self.assertEqual(service.snapshot()['profile_name'],'Measured')
        self.assertEqual(calls,[])

    def test_gui_pattern_can_run_without_a_lightburn_client(self):
        service,device,calls=self.make();service.start(dict(transport='usb',tcp_port=0,rotation=180))
        self.assertTrue(wait_for(lambda:service.snapshot()['connected']))
        service.submit_pattern(20,5,50,False)
        self.assertTrue(wait_for(lambda:len(device.starts)==1))
        self.assertTrue(wait_for(lambda:service.snapshot()['state']=='idle',timeout=4))
        self.assertTrue(any('pattern completed' in m for m in service.snapshot()['messages']))

    def test_client_attach_keeps_m2_ack_callback_after_service_start(self):
        service,device,calls=self.make();service.start(dict(transport='usb',tcp_port=0,rotation=0))
        self.assertTrue(wait_for(lambda:service.snapshot()['connected']))
        with socket.create_connection(('127.0.0.1',service._server.bound_port),timeout=2) as sock:
            sock.settimeout(2);data=b''
            while data.count(b'\n')<2:data+=sock.recv(1024)
            sock.sendall(b'G0X40Y40\nM3S50\nG1X41F3000\nM2\n');data=b''
            end=time.monotonic()+4
            while data.count(b'ok\n')<4 and time.monotonic()<end:
                chunk=sock.recv(4096)
                if not chunk:break
                data+=chunk
            self.assertEqual(data.splitlines().count(b'ok'),4)

    def test_internal_pattern_with_live_client_sends_no_unsolicited_ok(self):
        service,device,calls=self.make();device.active_polls=100
        service.start(dict(transport='usb',tcp_port=0,rotation=0))
        self.assertTrue(wait_for(lambda:service.snapshot()['connected']))
        with socket.create_connection(('127.0.0.1',service._server.bound_port),timeout=2) as sock:
            sock.settimeout(2);data=b''
            while data.count(b'\n')<2:data+=sock.recv(1024)
            service.submit_pattern(20,5,50,False)
            self.assertTrue(wait_for(lambda:bool(device.starts)))
            # A start write precedes its ACK/status poll. Observe activity
            # before making the fake finish, as the completion contract needs.
            self.assertTrue(wait_for(lambda:device.statuses and device.statuses[-1][0]==1))
            device._active_left=0
            self.assertTrue(wait_for(lambda:not service.snapshot()['internal_job'],timeout=4))
            extra=b''
            while select.select([sock],[],[],.05)[0]:extra+=sock.recv(4096)
            self.assertNotIn(b'ok',extra.splitlines())

    def test_stop_unwinds_an_active_internal_job(self):
        service,device,calls=self.make();device.active_polls=1000
        service.start(dict(transport='usb',tcp_port=0,rotation=0))
        self.assertTrue(wait_for(lambda:service.snapshot()['connected']))
        service.submit_pattern(20,5,50,False);self.assertTrue(wait_for(lambda:bool(device.starts)))
        service.stop();self.assertTrue(wait_for(lambda:not service.snapshot()['running'],timeout=4))
        self.assertTrue(device.closed);self.assertTrue(device.controls)
