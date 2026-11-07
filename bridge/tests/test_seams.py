"""Whole-image ownership, compact G-code and controlled fill diagnostics."""
from unittest.mock import patch
import unittest
from bridge import job,quality
from bridge.sequence import compile_sequence
from bridge.tests.test_banding import source_spans
from bridge.tests.test_dpi import bitmap_source
from bridge.tests.test_runtime import RuntimeBase,FakeDevice,wait_for,no_job_writes
from bridge.tests import test_service
from bridge.calibration import Calibration,apply_stage
from bridge.orientation import rotate_stage


class SeamTests(unittest.TestCase):
    def test_crossing_old_5000_move_limit_never_restarts_image(self):
        raw=bitmap_source(254,rows=120,columns=100)
        seq=compile_sequence(raw,image_mode='continuous')
        images=[s for s in seq.stages if s.marking_vectors]
        self.assertEqual(len(images),1)
        self.assertEqual(len(images[0].marking_vectors),6000)
        expected=source_spans(raw)
        # Independently interpret the actual COMPACT WIRE G-code, not metadata.
        actual=source_spans(images[0].payload.decode('ascii'))
        self.assertEqual(actual,expected)
        self.assertEqual(images[0].normalized_line_count,len(images[0].payload.splitlines())+1)

    def test_compact_controls_preserve_laser_off_moves_and_changed_feeds(self):
        raw='G21G90\nG0X30Y30\nM3S300\nG1X31F6000\nG1X32\nM5S0\nG1X33\nM3S300\nG1X34F3000\nM5\nG0X0Y0\nM2'
        normal=job.parse_job(raw,power_cap=1000);compact=job.parse_job(raw,power_cap=1000,compact=True)
        self.assertEqual(source_spans(normal.payload.decode()),source_spans(compact.payload.decode()))
        self.assertLess(len(compact.payload),len(normal.payload))
        self.assertEqual(compact.marking_powers,(300,))
        self.assertIn(b'M5 S0\nG1 X33.000',compact.payload)
        self.assertTrue(compact.payload.endswith(b'M5\nS0\n'))
        self.assertEqual(compact.final_host_position,(0,0))

    def test_compact_image_can_be_calibrated_and_rotated_without_modal_loss(self):
        raw=bitmap_source(204);original=compile_sequence(raw,image_mode='continuous').stages[0]
        model=Calibration(ax=.95,ay=.98,bx=0,by=0,enabled=True)
        result=rotate_stage(apply_stage(original,model),180)
        replay=source_spans(result.payload.decode())
        self.assertEqual(len(replay),len(result.marking_vectors))
        self.assertEqual(replay,list(result.marking_vectors))
        self.assertEqual(result.marking_powers,(200,))

    def test_oversized_image_rejects_instead_of_silently_restarting(self):
        with patch('bridge.job.MAX_OUTPUT_BYTES',1024):
            with self.assertRaisesRegex(job.JobError,'single-file'):
                compile_sequence(bitmap_source(254,rows=20,columns=20),image_mode='continuous')


class QualityTests(unittest.TestCase):
    def test_eight_patches_and_labels_share_one_constant_power_stock_file(self):
        seq=compile_sequence(quality.pattern(full=True),image_mode='continuous')
        self.assertEqual(len(seq.stages),1)
        stage=seq.stages[0];self.assertEqual(stage.marking_powers,(10,))
        self.assertEqual(stage.final_host_position,(50,50))
        for p in quality.PATCHES:
            runs=[(a,b) for a,b,f in stage.marking_vectors
                  if p.x<=min(a[0],b[0]) and max(a[0],b[0])<=p.x+6
                  and p.y<=min(a[1],b[1]) and max(a[1],b[1])<=p.y+6]
            self.assertEqual(len(runs),round(6/p.interval))
            axis=0 if p.axis=='X' else 1
            directions=[1 if b[axis]>a[axis] else -1 for a,b in runs]
            self.assertEqual(directions,[(-1 if p.alternating and i%2 else 1) for i in range(len(runs))])
            self.assertTrue(all(a[1-axis]==b[1-axis] for a,b in runs))

    def test_invalid_diagnostic_settings_reject(self):
        for power,speed in ((0,50),(101,50),(True,50),(5,0),(5,float('nan')),(5,'50')):
            with self.subTest(power=power,speed=speed),self.assertRaises(job.JobError):quality.pattern(power,speed)


class SeamRuntimeTests(RuntimeBase):
    def test_image_above_old_limit_has_one_start_plus_park_and_one_ack(self):
        raw=bitmap_source(254,rows=105,columns=100)
        device=FakeDevice(active_polls=100);device.upload_chunk_size=1048576;device.upload_chunk_delay=0
        c,d=self.make(device,image_mode='continuous',power_cap=1000,job_budget=600)
        acks=self.start(c)
        for line in raw.splitlines():response=c.submit_line(line.encode())
        self.assertIsNone(response)
        for i in range(2):
            self.assertTrue(wait_for(lambda:len(d.starts)==i+1));self.assertEqual(acks,[]);d._active_left=0
        self.assertTrue(wait_for(lambda:len(acks)==1))
        self.assertTrue(any('POWER: requested 20%; sent stock 20%' in m for m in c._events))

    def test_whole_image_bound_fails_before_prior_valid_cut_uploads(self):
        raw='G21G90\nG0X40Y40\nM3S200\nG1X41F3000\nM5\n'+bitmap_source(254,rows=20,columns=20)
        c,d=self.make(image_mode='continuous');self.start(c)
        with patch('bridge.job.MAX_OUTPUT_BYTES',1024):
            for line in raw.splitlines():response=c.submit_line(line.encode())
        self.assertEqual(response,'error:20');self.assertTrue(no_job_writes(d))


class QualityServiceTests(unittest.TestCase):
    make=test_service.ServiceTests.make
    def test_diagnostic_uses_internal_owner_without_lightburn_success_ack(self):
        service,device,calls=self.make();device.active_polls=100;device.upload_chunk_size=1048576;device.upload_chunk_delay=0
        service.start(dict(transport='usb',tcp_port=0))
        self.assertTrue(wait_for(lambda:service.snapshot()['connected']))
        service.submit_quality_pattern()
        self.assertTrue(wait_for(lambda:bool(device.starts)))
        self.assertTrue(service.snapshot()['internal_job'])
        device._active_left=0
        self.assertTrue(wait_for(lambda:not service.snapshot()['internal_job']))


if __name__=='__main__':unittest.main()
