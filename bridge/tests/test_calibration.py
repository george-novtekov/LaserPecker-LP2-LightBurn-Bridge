import math
from pathlib import Path
import tempfile
import unittest
from bridge.calibration import Calibration,CalibrationError,profile_from_measurements,measurement_pattern,apply_stage,save_profile,load_profile
from bridge.sequence import compile_sequence
from bridge.raster import RasterJob
from bridge.tests.test_runtime import RuntimeBase,FakeDevice,wait_for,no_job_writes

def profile(ax=.9,ay=.95,bx=.02,by=.01):
    return profile_from_measurements(80,[(ax+bx)*80,ax*80,(ax+bx)*80],[(ay+by)*80,ay*80,(ay+by)*80])

class CalibrationTests(unittest.TestCase):
    def test_jacobian_extrema_check_agrees_with_dense_field(self):
        model=Calibration.from_profile(profile(ax=.8,ay=.9,bx=.08,by=-.03))
        for x in range(101):
            for y in range(101):
                a,b,c,d=model._jacobian(x,y)
                self.assertGreaterEqual(min(a,d),.2);self.assertGreaterEqual(a*d-b*c,.1)

    def test_six_spans_recover_known_scale_and_bow_and_inverse(self):
        model=Calibration.from_profile(profile())
        self.assertAlmostEqual(model.ax,.9);self.assertAlmostEqual(model.ay,.95)
        self.assertAlmostEqual(model.bx,.02);self.assertAlmostEqual(model.by,.01)
        for x in range(10,91,10):
            for y in range(10,91,10):
                corrected=model.inverse(x,y);measured=model.forward(*corrected)
                self.assertAlmostEqual(measured[0],x,places=6);self.assertAlmostEqual(measured[1],y,places=6)
        self.assertGreater(abs(model.inverse(90,50)[0]-50),abs(model.inverse(90,90)[0]-50))

    def test_invalid_inputs_and_noninvertible_profiles_reject(self):
        for spans in ([80,0,80],[80,float('nan'),80],[80,20,80]):
            with self.assertRaises(CalibrationError):profile_from_measurements(80,spans,[80,80,80])
        with self.assertRaises(CalibrationError):Calibration.from_profile({'schema':1})
        with self.assertRaises(CalibrationError):profile_from_measurements(0,[80]*3,[80]*3)
        p=profile();p['coefficients'].update(ax=.5,ay=.5,bx=-.25,by=-.25)
        with self.assertRaises(CalibrationError):Calibration.from_profile(p)

    def test_pattern_has_six_spans_and_user_power_no_settings_writes(self):
        stage=compile_sequence(measurement_pattern(80,5,50)).stages[0]
        self.assertEqual(len(stage.marking_vectors),6);self.assertEqual(stage.marking_powers,(50,))
        self.assertTrue(all(math.isclose(math.dist(a,b),80) for a,b,_ in stage.marking_vectors))
        self.assertEqual(stage.final_host_position,(50,50))

    def test_vector_subdivision_corrects_curves_without_changing_requested_power(self):
        stage=compile_sequence('G0X20Y80\nM3S200\nG1X80F6000\nM5\nG0X0Y0\nM2').stages[0]
        model=Calibration.from_profile(profile());out=apply_stage(stage,model)
        self.assertGreater(len(out.marking_vectors),60);self.assertEqual(out.marking_powers,(200,))
        for a,b,_ in out.marking_vectors:
            actual=model.forward(*b);self.assertAlmostEqual(actual[1],80,places=2)
        self.assertNotEqual(out.marking_vectors[0][0][1],out.marking_vectors[len(out.marking_vectors)//2][0][1])
        self.assertEqual(out.normalized_line_count,len(out.payload.splitlines())+1)
        self.assertTrue(all(0<=v<=100 for v in out.final_host_position))

    def test_unreachable_marks_reject_but_laser_off_parking_is_bounded(self):
        model=Calibration.from_profile(profile(ax=.8,ay=.8,bx=0,by=0))
        stage=compile_sequence('G0X9Y10\nM3S100\nG1X91F3000\nM2').stages[0]
        with self.assertRaises(CalibrationError):apply_stage(stage,model)
        blank=compile_sequence('M5\nG0X0Y0\nG0X100Y100\nM2').stages[0]
        result=apply_stage(blank,model);self.assertEqual(result.final_host_position,(100,100));self.assertFalse(result.marking_vectors)

    def test_native_image_inverse_sampling_matches_forward_geometry_and_padding(self):
        original=RasterJob(b'\x3f\x7f',2,2,480,490,100,6000,(0,0),3,1,(0,0),())
        model=Calibration.from_profile(profile(ax=.8,ay=.8,bx=0,by=0));out=apply_stage(original,model)
        self.assertGreater(out.width_px,original.width_px)
        stride=(out.width_px+7)//8;oldstride=1;black=0
        for y in range(out.height_px):
            for x in range(out.width_px):
                px,py=model.forward((out.nx_px+x+.5)/10,(out.ny_px+y+.5)/10)
                sx,sy=math.floor(px*10-original.nx_px),math.floor(py*10-original.ny_px)
                expected=(0<=sx<2 and 0<=sy<2 and not original.payload[sy]&(0x80>>sx))
                marked=not out.payload[y*stride+x//8]&(0x80>>(x%8))
                self.assertEqual(marked,expected);black+=marked
        self.assertEqual(black,out.black_pixels)
        if out.width_px%8:self.assertTrue(all(v&((1<<(8-out.width_px%8))-1)==(1<<(8-out.width_px%8))-1 for v in out.payload[stride-1::stride]))

    def test_atomic_profile_round_trip_and_asymmetric_warning(self):
        p=profile_from_measurements(80,[74,72,73],[77,76,77]);self.assertTrue(p['warnings'])
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'profile.json';save_profile(path,p);self.assertEqual(load_profile(path),p)

class CalibrationRuntimeTests(RuntimeBase):
    def test_internal_patterns_do_not_acknowledge_lightburn_and_block_interleaving(self):
        c,d=self.make(FakeDevice(active_polls=100),job_budget=10);acks=self.start(c)
        c.submit_line(b'G0')
        c.submit_internal_job(measurement_pattern(20),corrected=False)
        self.assertEqual(c.submit_line(b'G0X20Y20'),'error:9')
        self.assertTrue(wait_for(lambda:d.starts));d._active_left=0
        self.assertTrue(wait_for(lambda:not c._internal_job));self.assertEqual(acks,[])
        self.assertEqual(c.submit_line(b'G0X20Y20'),'ok')

    def test_baseline_bypasses_selected_profile_and_verification_uses_it(self):
        c,d=self.make(FakeDevice(active_polls=100),job_budget=10,calibration_profile=profile())
        self.start(c);c.submit_internal_job(measurement_pattern(20),corrected=False)
        first=c._pending;self.assertEqual(c._device_jobs[id(first)].payload,first.payload)
        self.assertTrue(wait_for(lambda:d.starts));d._active_left=0
        self.assertTrue(wait_for(lambda:not c._internal_job))
        c.submit_internal_job(measurement_pattern(20),corrected=True)
        second=c._pending;self.assertNotEqual(c._device_jobs[id(second)].payload,second.payload)

    def test_unreachable_corrected_stage_fails_before_any_upload(self):
        c,d=self.make(calibration_profile=profile(ax=.5,ay=.5,bx=0,by=0));self.start(c)
        for line in ('G0X20Y20','M3S100','G1X80F3000'):c.submit_line(line.encode())
        self.assertEqual(c.submit_line(b'M2'),'error:20');self.assertTrue(no_job_writes(d))
