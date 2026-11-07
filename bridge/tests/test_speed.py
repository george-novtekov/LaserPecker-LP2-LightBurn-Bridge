import unittest
from bridge.job import JobError,parse_job
from bridge.speed import plan_speed,timer_rate
from bridge.tests.test_runtime import RuntimeBase,wait_for
from bridge.codec import protocol as p

def job(feed=6000):return parse_job(f'G0X30Y36\nM3S200\nG1X66F{feed}\nG1Y77\nG1X30\nG1Y36\nM2')

class SpeedTests(unittest.TestCase):
    def test_observed_slow_timer_and_corrected_quantization(self):
        self.assertAlmostEqual(timer_rate(1),1.99563,places=4)
        s=plan_speed(job());self.assertEqual(s.wire_depth,99)
        self.assertEqual(s.requested_mm_s,100)
        self.assertAlmostEqual(s.estimated_mm_s,96.0205,places=3)
        self.assertLess(s.relative_error,.05)

    def test_single_stock_stage_rejects_mixed_settings(self):
        for text in ['G0X30Y36\nM3S200\nG1X66F6000\nG1Y77F3000\nM2',
                     'G0X30Y36\nM3S200\nG1X66F6000\nS100\nG1Y77\nM2']:
            with self.subTest(text=text),self.assertRaises(JobError):plan_speed(parse_job(text))

    def test_zero_power_uses_direct_path_without_timing_claim(self):
        s=plan_speed(parse_job('G0X30Y36\nM3S0\nG1X66F6000\nM2'))
        self.assertEqual(s.requested_mm_s,0)

class SpeedRuntimeTests(RuntimeBase):
    def test_correct_timer_and_power_reach_start_frame(self):
        c,d=self.make();self.start(c)
        for line in ('G0X30Y36','M3S200','G1X66F6000','M2'):c.submit_line(line.encode())
        self.assertTrue(wait_for(lambda:d.starts))
        frame=p.StreamDecoder().feed(d.starts[0])[0]
        self.assertEqual((frame.payload[1],frame.payload[2]),(20,99))

    def test_mixed_feed_splits_into_ordered_stock_jobs(self):
        c,d=self.make();acks=self.start(c)
        for line in ('G0X30Y36','M3S200','G1X66F6000','G1Y77F3000'):self.assertEqual(c.submit_line(line.encode()),'ok')
        self.assertIsNone(c.submit_line(b'M2'))
        self.assertTrue(wait_for(lambda:acks))
        self.assertEqual(len(d.starts),2)
        frames=[p.StreamDecoder().feed(x)[0] for x in d.starts]
        self.assertEqual([x.payload[2] for x in frames],[99,97])

    def test_diagonal_speed_is_conservative_and_quantized(self):
        s=plan_speed(parse_job('G0X30Y30\nM3S200\nG1X60Y60F6000\nG1X30\nM2'))
        self.assertLessEqual(s.maximum_mm_s,100)
        self.assertLess(s.minimum_mm_s,s.maximum_mm_s)
        s=plan_speed(job(9000));self.assertLessEqual(s.maximum_mm_s,150)

    def test_user_full_range_preserves_80_and_100_percent_layer_power(self):
        for requested,expected in ((800,80),(1000,100)):
            with self.subTest(requested=requested):
                c,d=self.make(power_cap=1000,start_laser_percent=100);self.start(c)
                for line in ('G0X30Y36',f'M3S{requested}','G1X66F6000','M2'):
                    response=c.submit_line(line.encode())
                self.assertIsNone(response);self.assertTrue(wait_for(lambda:d.starts))
                frame=p.StreamDecoder().feed(d.starts[0])[0]
                self.assertEqual((frame.payload[1],frame.payload[2]),(expected,99))
                c.shutdown()

if __name__=='__main__':unittest.main()
