import unittest
from pathlib import Path
from bridge.sequence import compile_sequence
from bridge.job import Job,JobError
from bridge.raster import RasterJob
from bridge.tests.test_runtime import RuntimeBase,FakeDevice,wait_for,no_job_writes

MIXED='''G21G90
G0X40Y40
M3S50
G1X42F3000
G1Y42
G1X40
G1Y40
M5
G0X45Y40
M3S100
G1X47F6000
M5
G0X45Y40.1
M3
G1X47
M5
G0X50Y40
M4
G91
G1X.3F6000S50
G1X-.3Y.1S0
G1X.3S50
M5
G90
G0X0Y0
M2'''

class CompilerTests(unittest.TestCase):
    def test_all_modes_keep_order_settings_and_positions(self):
        stages=compile_sequence(MIXED).stages
        self.assertEqual([type(x) for x in stages],[Job,Job,RasterJob,Job])
        self.assertEqual([x.speed_plan.wire_depth for x in stages],[97,99,51,1])
        self.assertEqual(stages[0].marking_powers,(50,))
        self.assertEqual(stages[1].marking_powers,(100,))
        self.assertEqual(stages[2].payload,b'\x1f\x1f')
        self.assertEqual(stages[-1].final_host_position,(0,0))

    def test_image_first_then_vectors_then_second_image(self):
        first=MIXED[MIXED.index('G0X50Y40'):MIXED.index('G0X0Y0')]
        raw='G21G90\n'+first+'G0X20Y20\nM3S100\nG1X22F3000\nM5\n'+first+'G0X0Y0\nM2'
        stages=compile_sequence(raw).stages
        self.assertEqual([type(x) for x in stages],[RasterJob,Job,RasterJob,Job])

    def test_actual_lightburn_mixed_fixture_and_arbitrary_fill_interval(self):
        raw=(Path(__file__).resolve().parents[2]/'bridge/tests/fixtures/mixed-lightburn.gc').read_bytes()
        stages=compile_sequence(raw).stages
        self.assertEqual([type(x) for x in stages],[Job,Job,RasterJob,Job])
        self.assertEqual([x.speed_plan.wire_depth for x in stages],[97,99,51,1])
        self.assertEqual((stages[2].width_px,stages[2].height_px),(20,20))
        self.assertEqual(len(stages[1].marking_vectors),20)
        fill='G21G90\nG0X40Y40\nM3S50\nG91\nG1X2F3000\nG1X-2Y.05S0\nG1X2S50\nM5\nG90\nM2'
        parsed=compile_sequence(fill).stages
        self.assertEqual(len(parsed),1)
        self.assertEqual(parsed[0].marking_vectors[-1][1],(42,40.05))

    def test_late_unknown_and_out_of_bounds_are_rejected(self):
        for bad in ('G1X101','M7','G2X10I1','M2S50'):
            with self.subTest(bad=bad),self.assertRaises(JobError):compile_sequence(MIXED.replace('M2',bad+'\nM2'))

class SequenceRuntimeTests(RuntimeBase):
    def test_bluetooth_with_usb_attached_refuses_before_upload(self):
        d=FakeDevice();d.transport_kind='bluetooth'
        c,d=self.make(d);self.start(c)
        self.assertIn('Alarm',c.status_report());self.assertIn('USB unplugged',c.last_error)
        self.assertTrue(no_job_writes(d));self.assertEqual(d.starts,[])

    def test_one_final_ack_after_all_four_stages(self):
        c,d=self.make(FakeDevice(active_polls=100),job_budget=5);acks=self.start(c)
        for line in MIXED.splitlines():response=c.submit_line(line.encode())
        self.assertIsNone(response)
        for i in range(4):
            self.assertTrue(wait_for(lambda:len(d.starts)==i+1))
            self.assertEqual(acks,[]);d._active_left=0
        self.assertTrue(wait_for(lambda:len(acks)==1));self.assertEqual(len(d.starts),4)
        self.assertIn('Idle|MPos:0.000,0.000',c.status_report())

    def test_invalid_last_stage_never_uploads_first_stage(self):
        c,d=self.make(job_budget=5);acks=self.start(c)
        for line in MIXED.replace('G0X0Y0','G0X101Y0').splitlines():response=c.submit_line(line.encode())
        self.assertEqual(response,'error:20');self.assertTrue(no_job_writes(d));self.assertEqual(acks,[])

    def test_cancel_first_stage_discards_entire_remaining_job(self):
        c,d=self.make(FakeDevice(active_polls=1000),job_budget=5);acks=self.start(c)
        for line in MIXED.splitlines():c.submit_line(line.encode())
        self.assertTrue(wait_for(lambda:d.starts));c.realtime('\x18')
        self.assertTrue(wait_for(lambda:not c._started));self.assertEqual(len(d.starts),1)
        self.assertEqual(acks,[]);self.assertFalse(c._remaining)
