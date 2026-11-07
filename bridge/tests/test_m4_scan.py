"""M4 does not uniquely identify a bitmap: LightBurn uses it for hatch fills."""
import unittest
from bridge.sequence import compile_sequence
from bridge.raster import parse_raster,RasterJob
from bridge.job import Job,JobError
from bridge.tests.test_runtime import RuntimeBase,FakeDevice,wait_for,no_job_writes

PICTURE='G21G90\nG0X30Y30\nM4\nG91\nG1X.3F6000S200\nG1X-.3Y.1S0\nG1X.3S200\nG90\n'
FILL='G0X45.458Y63.05\nM4\nG91\nG1X2.5F6000S0\nG1X1.084S200\nG1X2.5S0\nG1X.402Y.1\nG1X-2.5\nG1X-1.888S200\nG1X-2.5S0\nG90\n'
CUT='G0X40Y40\nM4S200\nG1X42Y42F6000\nG1X40Y42\nM5\nG0X0Y0\nM2'
MIXED=PICTURE+FILL+CUT

class M4ScanTests(unittest.TestCase):
    def test_curved_fill_keeps_exact_endpoints_instead_of_snapping_pixels(self):
        result=compile_sequence('G21G90\n'+FILL+'M5\nM2')
        self.assertEqual(len(result.stages),1)
        self.assertIsInstance(result.stages[0],Job)
        vectors=result.stages[0].marking_vectors
        self.assertEqual(vectors,(((47.958,63.05),(49.042,63.05),6000.),
                                  ((49.444,63.15),(47.556,63.15),6000.)))
        self.assertIn('constant power',result.notes[0])

    def test_picture_pixels_unchanged_beside_m4_fill_and_cut(self):
        result=compile_sequence(MIXED)
        self.assertEqual([type(s) for s in result.stages],[RasterJob,Job,Job])
        reference=parse_raster(PICTURE+'M5\nM2')
        self.assertEqual(result.stages[0].payload,reference.payload)
        self.assertEqual(result.stages[-1].final_host_position,(0,0))
        self.assertTrue(any('M4 vector paths' in n for n in result.notes))

    def test_arbitrary_fill_spacing_and_descending_rows_preserve_endpoints(self):
        for interval in ('.085','-.1'):
            result=compile_sequence('G21G90\n'+FILL.replace('Y.1','Y'+interval)+'M5\nM2')
            self.assertIsInstance(result.stages[0],Job)
            self.assertAlmostEqual(result.stages[0].marking_vectors[-1][0][1],63.05+float(interval))

    def test_powered_turns_and_grayscale_still_reject(self):
        for raw in (FILL.replace('X-1.888S200','X-1.888S100'),
                    FILL.replace('X1.084S200','X1.084Y.1S200'),FILL+'M7\n'):
            with self.subTest(raw=raw),self.assertRaises(JobError):compile_sequence('G21G90\n'+raw+'M5\nM2')

class M4ScanRuntimeTests(RuntimeBase):
    def test_all_three_modes_finish_before_single_final_ack(self):
        c,d=self.make(FakeDevice(active_polls=100),job_budget=5);acks=self.start(c)
        for line in MIXED.splitlines():response=c.submit_line(line.encode())
        self.assertIsNone(response)
        for i in range(3):
            self.assertTrue(wait_for(lambda:len(d.starts)==i+1));self.assertEqual(acks,[]);d._active_left=0
        self.assertTrue(wait_for(lambda:len(acks)==1))

    def test_powered_turn_after_valid_image_never_uploads_anything(self):
        c,d=self.make(job_budget=5);self.start(c)
        for line in MIXED.replace('X.402Y.1','X.402Y.085S200').splitlines():response=c.submit_line(line.encode())
        self.assertEqual(response,'error:20');self.assertTrue(no_job_writes(d))
