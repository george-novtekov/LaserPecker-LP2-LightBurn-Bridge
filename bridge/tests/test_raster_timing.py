import unittest
from types import SimpleNamespace
from bridge.job import JobError
from bridge.raster_timing import plan_raster_timing

class RasterTimingTests(unittest.TestCase):
    def job(self,feed):return SimpleNamespace(requested_feed=feed,width_px=100,height_px=40,black_pixels=1700)

    def test_native_rate_is_explicitly_distinct_from_vector_rate(self):
        fast=plan_raster_timing(self.job(6000))
        self.assertEqual((fast.wire_depth,fast.exposure_us),(51,1000))
        self.assertTrue(fast.capped_fast)
        self.assertAlmostEqual(fast.estimated_black_mm_s,46.511628,places=5)
        self.assertLessEqual(fast.exposure_us,100000/fast.requested_mm_s)

    def test_slow_feed_maps_to_more_dwell_and_impossible_feed_rejects(self):
        slow=plan_raster_timing(self.job(3000))
        self.assertEqual((slow.wire_depth,slow.exposure_us),(1,2000))
        self.assertLessEqual(slow.estimated_black_mm_s,50)
        with self.assertRaises(JobError):plan_raster_timing(self.job(120))

if __name__=='__main__':unittest.main()
