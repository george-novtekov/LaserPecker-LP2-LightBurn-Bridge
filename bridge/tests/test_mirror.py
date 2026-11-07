"""Whole-bed handedness, including payload replay and the shared pipeline."""
import tempfile
import unittest
from bridge.orientation import rotate_stage
from bridge.job import Job,JobError,parse_job
from bridge.raster import RasterJob,prepare_wire
from bridge.runtime import Controller,RuntimeConfig
from bridge.service import BridgeService
from bridge.calibration import Calibration,apply_stage
from bridge.tests.test_calibration import profile
from bridge.tests.test_dpi import cells,firmware_dots
from bridge.tests.test_banding import source_spans
from bridge.tests.test_m4_scan import MIXED
from bridge.tests.test_vertical_scan import vertical_source
from bridge.tests.test_runtime import FakeDevice,wait_for,no_job_writes

class MirrorTests(unittest.TestCase):
    def test_asymmetric_vector_handedness_and_parking_for_each_mapping(self):
        job=parse_job('G21G90\nG0X10Y20\nM3S200\nG1X13Y27F6000\nM5\nG0X0Y0\nM2')
        for rotation,mirror,a,b,park in (
            (0,False,(10,20),(13,27),(0,0)),
            (0,True,(90,20),(87,27),(100,0)),
            (180,False,(90,80),(87,73),(100,100)),
            (180,True,(10,80),(13,73),(0,100))):
            with self.subTest(rotation=rotation,mirror=mirror):
                out=rotate_stage(job,rotation,mirror_x=mirror)
                self.assertEqual(out.marking_vectors,((a,b,6000.),))
                self.assertEqual(source_spans(out.payload.decode()),list(out.marking_vectors))
                self.assertEqual(out.final_host_position,park)
                self.assertEqual(out.marking_powers,job.marking_powers)
                self.assertEqual(out.speed_plan,job.speed_plan)
                self.assertEqual(rotate_stage(out,rotation,mirror_x=mirror).payload,job.payload)
        self.assertEqual(job.final_host_position,(0,0))

    def test_asymmetric_bitmap_placement_padding_and_firmware_dots(self):
        original=RasterJob(bytes.fromhex('5ff7ffffbfff'),13,3,100,200,200,6000,(0,0),4,1,(.01,.02),(),finish_points=((0,0),))
        for rotation,mirror,origin,expected in (
            (0,True,(887,200),{(12,0),(10,0),(0,0),(11,2)}),
            (180,False,(887,797),{(12,2),(10,2),(0,2),(11,0)}),
            (180,True,(100,797),{(0,2),(2,2),(12,2),(1,0)})):
            with self.subTest(rotation=rotation,mirror=mirror):
                out=rotate_stage(original,rotation,mirror_x=mirror)
                self.assertEqual((out.nx_px,out.ny_px),origin)
                self.assertEqual(cells(out),expected)
                self.assertTrue(all(b&7==7 for b in out.payload[1::2]))
                wire=prepare_wire(out)
                self.assertEqual(set(firmware_dots(wire)),{(origin[0]+x,origin[1]+y) for x,y in expected})
                self.assertEqual(rotate_stage(out,rotation,mirror_x=mirror).payload,original.payload)
                self.assertEqual(out.requested_s,200);self.assertEqual(out.requested_feed,6000)
        self.assertEqual(rotate_stage(original,180,mirror_x=True).finish_points,((0,100),))
        self.assertEqual(rotate_stage(original,180,mirror_x=True).snap_mm,(.01,-.02))
        with self.assertRaises(JobError):rotate_stage(prepare_wire(original),180,mirror_x=True)

    def test_mixed_runtime_pipeline_corrects_every_stage_and_keeps_logical_host(self):
        for mode in ('bitmap','continuous'):
            c=Controller(RuntimeConfig(rotation=180,mirror_x=True,power_cap=1000,image_mode=mode))
            logical,prepared=c._compile_job(MIXED.encode(),None)
            self.assertEqual(len(logical.stages),3)
            for stage in logical.stages:
                out=prepared[id(stage)]
                if isinstance(stage,Job):
                    self.assertEqual(out.marking_vectors,tuple(((a[0],round(100-a[1],3)),(b[0],round(100-b[1],3)),f) for a,b,f in stage.marking_vectors))
                    self.assertEqual(source_spans(out.payload.decode()),list(out.marking_vectors))
                    self.assertEqual(out.marking_powers,stage.marking_powers)
                    self.assertEqual(out.speed_plan,stage.speed_plan)
                    l,t,r,b=stage.bounds
                    self.assertEqual(out.bounds,(l,1000-b,r,1000-t))
                    self.assertEqual(out.header_geometry['nx_px'],l)
                else:
                    self.assertEqual(set(firmware_dots(out)),{(stage.nx_px+x,1000-stage.ny_px-stage.height_px+stage.height_px-1-y) for x,y in cells(stage)})
            self.assertEqual(logical.final_host_position,(0,0))
            self.assertEqual(prepared[id(logical.stages[-1])].final_host_position,(0,100))

    def test_vertical_image_retains_scan_axis_timing_and_source_spacing(self):
        c=Controller(RuntimeConfig(rotation=180,mirror_x=True,power_cap=1000,image_mode='continuous'))
        logical,prepared=c._compile_job(vertical_source(204).encode(),None)
        for stage in logical.stages:
            wire=prepared[id(stage)]
            self.assertEqual(source_spans(wire.payload.decode()),list(wire.marking_vectors))
            self.assertTrue(all(a[0]==b[0] for a,b,f in wire.marking_vectors))
            self.assertEqual(wire.speed_plan,stage.speed_plan)
            for (a,b,f),(aa,bb,ff) in zip(stage.marking_vectors,wire.marking_vectors):
                self.assertEqual((aa,bb,ff),((a[0],round(100-a[1],3)),(b[0],round(100-b[1],3)),f))

    def test_measured_correction_precedes_handedness(self):
        p=profile();model=Calibration.from_profile(p)
        raw=b'G21G90\nG0X20Y80\nM3S200\nG1X80F6000\nM5\nG0X50Y50\nM2'
        c=Controller(RuntimeConfig(rotation=180,mirror_x=True,power_cap=1000,image_mode='continuous'))
        logical,prepared=c._compile_job(raw,p)
        stage=logical.stages[0];corrected=apply_stage(stage,model)
        out=prepared[id(stage)]
        self.assertGreater(len(out.marking_vectors),60)
        for (a,b,f),(aa,bb,ff) in zip(corrected.marking_vectors,out.marking_vectors):
            self.assertEqual((aa,bb,ff),((a[0],round(100-a[1],3)),(b[0],round(100-b[1],3)),f))
        self.assertEqual(out.marking_powers,corrected.marking_powers)

    def test_service_defaults_to_correction_and_accepts_explicit_opt_out(self):
        for explicit in (None,False,True):
            with self.subTest(explicit=explicit),tempfile.TemporaryDirectory() as folder:
                device=FakeDevice();service=BridgeService(port_factory=lambda name:device,data_dir=folder)
                cfg=dict(transport='usb',tcp_port=0,rotation=180)
                if explicit is not None:cfg['mirror_x']=explicit
                try:
                    service.start(cfg)
                    self.assertTrue(wait_for(lambda:service.snapshot()['connected']))
                    self.assertIs(service._controller.config.mirror_x,explicit is not False)
                    self.assertTrue(no_job_writes(device))
                finally:
                    service.stop();self.assertTrue(wait_for(lambda:not service.snapshot()['running'],timeout=4))

    def test_non_boolean_options_reject_before_opening_device(self):
        for invalid in (1,0,'false',None):
            with self.subTest(invalid=invalid):
                with self.assertRaises(JobError):rotate_stage(parse_job('G0X0Y0\nM2'),180,mirror_x=invalid)
                with self.assertRaises(ValueError):Controller(RuntimeConfig(mirror_x=invalid))
                with tempfile.TemporaryDirectory() as folder:
                    service=BridgeService(data_dir=folder)
                    with self.assertRaises(ValueError):service.start(dict(mirror_x=invalid))

if __name__=='__main__':unittest.main()
