import unittest
from bridge.orientation import rotate_stage
from bridge.job import parse_job,JobError
from bridge.raster import RasterJob,gen_raster_header
from bridge.tests.test_m4_scan import MIXED
from bridge.tests.test_runtime import RuntimeBase,FakeDevice,wait_for
from bridge.codec import protocol as p

class OrientationTests(unittest.TestCase):
    def test_vectors_rotate_about_bed_preserving_power_feed_and_source(self):
        job=parse_job('G21G90\nG0X10Y20\nM3S200\nG1X13Y27F6000\nM5\nG0X0Y0\nM2')
        rotated=rotate_stage(job,180)
        self.assertIn(b'G0 X90.000 Y80.000',rotated.payload)
        self.assertIn(b'G1 X87.000 Y73.000 F6000',rotated.payload)
        self.assertIn(b'G0 X100.000 Y100.000',rotated.payload)
        self.assertEqual(rotated.marking_vectors,(((90,80),(87,73),6000.),))
        self.assertEqual(rotated.payload.count(b'\n'),job.payload.count(b'\n'))
        self.assertEqual(rotated.marking_powers,job.marking_powers)
        self.assertEqual(rotate_stage(rotated,180).payload,job.payload)
        self.assertEqual(job.final_host_position,(0,0))
        self.assertIs(rotate_stage(job,0),job)

    def test_asymmetric_non_byte_aligned_bitmap_rotates_pixels_and_placement(self):
        # 13x3, black cells(0,0),(2,0),(12,0),(1,2); padding is white.
        original=bytes.fromhex('5ff7ffffbfff')
        job=RasterJob(original,13,3,100,200,200,6000,(0,0),4,1,(0,0),())
        rotated=rotate_stage(job,180)
        self.assertEqual((rotated.nx_px,rotated.ny_px),(887,797))
        cells={(x,y) for y in range(3) for x in range(13) if not rotated.payload[y*2+x//8]&(0x80>>(x%8))}
        self.assertEqual(cells,{(12,2),(10,2),(0,2),(11,0)})
        self.assertTrue(all(b&7==7 for b in rotated.payload[1::2]))
        self.assertEqual(rotate_stage(rotated,180).payload,original)
        self.assertEqual(rotated.black_pixels,4)
        header=gen_raster_header(rotated,file_id=1,name='rotation')
        self.assertEqual(header[10:14],(887).to_bytes(2,'big')+(797).to_bytes(2,'big'))
        self.assertEqual(job.payload,original)

    def test_full_bed_and_corners_remain_within_bounds(self):
        for x,y,w,h in ((0,0,1000,1000),(0,999,1,1),(999,0,1,1)):
            job=RasterJob(b'\xff'*(((w+7)//8)*h),w,h,x,y,200,6000,(0,0),0,1,(0,0),())
            out=rotate_stage(job,180)
            self.assertTrue(0<=out.nx_px<=1000-w and 0<=out.ny_px<=1000-h)
        with self.assertRaises(JobError):rotate_stage(job,90)

class OrientationRuntimeTests(RuntimeBase):
    def test_all_modes_rotate_on_wire_and_host_positions_stay_logical(self):
        c,d=self.make(FakeDevice(active_polls=3),rotation=180,job_budget=5);acks=self.start(c)
        for line in MIXED.splitlines():response=c.submit_line(line.encode())
        self.assertIsNone(response);self.assertTrue(wait_for(lambda:acks,timeout=3))
        self.assertEqual(len(d.starts),3)
        self.assertIn('Idle|MPos:0.000,0.000',c.status_report())
        self.assertFalse(c._device_jobs)
        frames=[p.StreamDecoder().feed(x)[0] for x in d.starts]
        # First image is3x2pixels at(300,300), rotated about1000px bed.
        # Wire-only phase correction adds a white column and moves nx left1.
        self.assertEqual(frames[0].payload[7:11],(696).to_bytes(2,'big')+(698).to_bytes(2,'big'))
