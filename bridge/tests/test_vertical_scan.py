"""LightBurn-style vertical M4 image streams use ordinary bounded G-code."""
import re
import unittest
from bridge.job import Job,JobError
from bridge.sequence import compile_sequence
from bridge.orientation import rotate_stage
from bridge.tests.test_banding import source_spans
from bridge.tests.test_dpi import bitmap_source
from bridge.tests.test_m4_scan import FILL,CUT
from bridge.tests.test_runtime import RuntimeBase,FakeDevice,wait_for,no_job_writes

def vertical_source(dpi=204):
    # Transpose the synthetic source solely to exercise vertical G-code syntax.
    return re.sub('[XY]',lambda m:'Y' if m[0]=='X' else 'X',bitmap_source(dpi))

class VerticalScanTests(unittest.TestCase):
    def test_vertical_runs_preserve_source_endpoints_power_feed_and_orientation(self):
        for dpi in (154,204,254,300,508,1000):
            with self.subTest(dpi=dpi):
                raw=vertical_source(dpi);seq=compile_sequence(raw,image_mode='continuous')
                self.assertTrue(all(isinstance(s,Job) for s in seq.stages))
                expected=source_spans(raw)
                actual=[v for s in seq.stages for v in s.marking_vectors]
                self.assertEqual(len(expected),len(actual))
                for (a,b,f),(aa,bb,ff) in zip(expected,actual):
                    self.assertEqual(f,ff)
                    for p,q in zip(a+b,aa+bb):self.assertAlmostEqual(p,q,delta=.000501)
                wire=rotate_stage(seq.stages[0],180)
                self.assertEqual(source_spans(wire.payload.decode()),list(wire.marking_vectors))
                self.assertTrue(any('(Y axis)' in n for n in seq.notes))
                self.assertEqual(seq.stages[0].marking_powers,(200,))

    def test_vertical_image_horizontal_fill_and_cut_share_one_job(self):
        raw=vertical_source().replace('G0Y0X0\nM2',FILL+CUT)
        seq=compile_sequence(raw,image_mode='continuous')
        self.assertEqual(len(seq.stages),3)
        self.assertTrue(all(a[0]==b[0] for a,b,f in seq.stages[0].marking_vectors))
        self.assertTrue(all(a[1]==b[1] for a,b,f in seq.stages[1].marking_vectors))
        self.assertEqual(seq.final_host_position,(0,0))

    def test_powered_cross_axis_turns_and_diagonals_still_reject(self):
        for bad in ('G1X0.124510S200','G1X0.124510Y0.124510S200'):
            raw=vertical_source().replace('G1X0.124510S0',bad,1)
            with self.subTest(bad=bad),self.assertRaises(JobError):compile_sequence(raw,image_mode='continuous')

class VerticalRuntimeTests(RuntimeBase):
    def test_vertical_image_completion_and_late_invalid_turn_preflight(self):
        device=FakeDevice(active_polls=100);device.upload_chunk_size=1048576;device.upload_chunk_delay=0
        c,d=self.make(device,image_mode='continuous',rotation=180,job_budget=10);acks=self.start(c)
        for line in vertical_source().splitlines():response=c.submit_line(line.encode())
        self.assertIsNone(response)
        for index in range(2):
            self.assertTrue(wait_for(lambda:len(d.starts)==index+1));self.assertEqual(acks,[])
            self.assertTrue(wait_for(lambda:d.statuses and d.statuses[-1][0]==1 and d.statuses[-1][3]==d._file_id))
            d._active_left=0
        self.assertTrue(wait_for(lambda:len(acks)==1))
        c2,d2=self.make(image_mode='continuous');self.start(c2)
        raw='G21G90\nG0X40Y40\nM3S200\nG1X41F3000\nM5\n'+vertical_source().replace('G1X0.124510S0','G1X0.124510S200',1)
        for line in raw.splitlines():response=c2.submit_line(line.encode())
        self.assertEqual(response,'error:20');self.assertTrue(no_job_writes(d2))

if __name__=='__main__':unittest.main()
