"""Independent source-spacing and saved-firmware scanner regressions (no hardware)."""
from dataclasses import replace
import math
import threading
import unittest
from bridge.raster import RasterJob,parse_raster,prepare_wire,gen_raster_header
from bridge.raster_timing import plan_raster_timing
from bridge.orientation import rotate_stage
from bridge.calibration import Calibration,apply_stage
from bridge.sequence import compile_sequence
from bridge.job import JobError
from bridge.tests.test_transport import FakeSerial,Log
from bridge.transport import LP2Session,TransportError
from bridge.tests.test_runtime import RuntimeBase,FakeDevice,wait_for,no_job_writes


def bitmap_source(dpi,rows=7,columns=13,descending=False):
    pitch=25.4/dpi
    data=['G21G90','G0X20Y30','M4','G91','F6000']
    for y in range(rows):
        if y:data.append(f'G1Y{(-1 if descending else 1)*(round(y*pitch,6)-round((y-1)*pitch,6)):.6f}S0')
        xs=range(columns) if y%2==0 else range(columns-1,-1,-1)
        for x in xs:
            delta=round((x+1)*pitch,6)-round(x*pitch,6)
            power=200 if x%2==0 else 0
            data.append(f'G1X{delta if y%2==0 else -delta:.6f}S{power}')
    return '\n'.join(data+['M5','G90','G0X0Y0','M2'])


def cells(job):
    stride=(job.width_px+7)//8
    return {(x,y) for y in range(job.height_px) for x in range(job.width_px)
            if not job.payload[y*stride+x//8]&(0x80>>(x%8))}


def firmware_dots(job):
    # Independent replay of saved V377 FUN_0800d7d4, including padded-byte
    # traversal and move BEFORE pulse. The Y loop starts with a forward row.
    stride=(job.width_px+7)//8; x=job.nx_px; dots=[]
    for y in range(job.height_px):
        row=job.payload[y*stride:(y+1)*stride]
        if y%2==0:
            count=0
            for value in row:
                for bit in range(7,-1,-1):
                    count+=1
                    if count<=job.width_px:
                        x+=1
                        if not value&(1<<bit):dots.append((x,job.ny_px+y))
                    if count==job.width_px:break
        else:
            count=stride*8
            for value in reversed(row):
                for bit in range(8):
                    count-=1
                    if count<job.width_px:
                        x-=1
                        if not value&(1<<bit):dots.append((x,job.ny_px+y))
    return dots


class DpiTests(unittest.TestCase):
    def test_selected_dpi_preserves_physical_extent_and_packed_padding(self):
        for dpi in (50,100,127,154,200,254,300,508,600,1000,1016,2000):
            with self.subTest(dpi=dpi):
                r=parse_raster(bitmap_source(dpi))
                self.assertAlmostEqual(r.source_pitch_mm,25.4/dpi,delta=2e-6)
                self.assertGreaterEqual(r.px,1)
                # Native raster covers the same rectangle within one grid cell.
                self.assertAlmostEqual(r.width_px*r.pitch_mm,13*25.4/dpi,delta=r.pitch_mm+1e-5)
                self.assertAlmostEqual(r.height_px*r.pitch_mm,7*25.4/dpi,delta=r.pitch_mm+1e-5)
                sp=plan_raster_timing(r)
                dose=sp.exposure_us/1e6/r.pitch_mm**2
                requested=1/(100*r.source_pitch_mm)
                self.assertLessEqual(dose,requested+1e-7)
                self.assertLess(requested-dose,20e-6/r.pitch_mm**2+1e-6)
                wire=prepare_wire(rotate_stage(r,180))
                header=gen_raster_header(wire,file_id=1234,name='dpi-regression')
                device=FakeSerial();session=LP2Session(device,Log(),timeout=.03)
                session.upload(header,wire.payload,threading.Event())
                self.assertEqual(session.phase,'uploaded')
                self.assertEqual(len(firmware_dots(wire)),wire.black_pixels)

    def test_three_hundred_dpi_sampling_preserves_integrated_marked_area(self):
        r=parse_raster(bitmap_source(300))
        self.assertEqual(r.px,3)
        # 7 black columns x 7 rows; rectangular physical area, independently
        # calculated, should agree to within quantization by a few native dots.
        expected=49*(25.4/300)**2/r.pitch_mm**2
        self.assertAlmostEqual(r.black_pixels,expected,delta=3)

    def test_descending_rows_compile_to_same_image(self):
        r=parse_raster(bitmap_source(300,descending=True))
        forward=parse_raster(bitmap_source(300))
        self.assertEqual(cells(r),cells(forward))
        self.assertAlmostEqual(r.ny_px*r.pitch_mm,30-6*(25.4/300),delta=r.pitch_mm/2)

    def test_non_divisor_grid_rotation_is_bounded_at_bed_edges(self):
        for px in (3,6,7,9,15):
            limit=4000//px
            for nx in (0,1,limit-2):
                r=RasterJob(b'\x3f\x3f',2,2,nx,nx,200,6000,(0,0),4,1,(0,0),(),px=px,dpi=round(1016/px))
                out=rotate_stage(r,180)
                self.assertLessEqual(out.nx_px+2,limit)
                self.assertLessEqual(out.ny_px+2,limit)
                gen_raster_header(out,file_id=1,name='edge')

    def test_calibration_uses_variable_grid_before_final_row_correction(self):
        r=parse_raster(bitmap_source(300));model=Calibration(ax=.95,ay=.97,bx=.005,by=.002,enabled=True)
        out=apply_stage(r,model);stride=(out.width_px+7)//8;original=cells(r)
        expected=set()
        for y in range(out.height_px):
            for x in range(out.width_px):
                wx,wy=model.forward((out.nx_px+x+.5)*r.pitch_mm,(out.ny_px+y+.5)*r.pitch_mm)
                sx,sy=math.floor(wx/r.pitch_mm-r.nx_px),math.floor(wy/r.pitch_mm-r.ny_px)
                if (sx,sy) in original:expected.add((x,y))
        self.assertEqual(cells(out),expected)
        wire=prepare_wire(rotate_stage(out,180))
        self.assertEqual(len(firmware_dots(wire)),len(expected))
        with self.assertRaises(JobError):rotate_stage(wire,180)
        with self.assertRaises(JobError):apply_stage(wire,model)

    def test_invalid_scan_still_rejects_before_any_device_io(self):
        raw=bitmap_source(300).replace('G1Y0.084667S0','G1Y0.084667S200',1)
        with self.assertRaises(JobError):compile_sequence(raw)


class RowPhaseTests(unittest.TestCase):
    def test_corrected_scan_has_no_alternating_column_offset_or_lost_edge_bits(self):
        for width in (1,7,8,9,13,283,450):
            for height in (2,3,4,5):
                stride=(width+7)//8;payload=bytearray(b'\xff'*(stride*height))
                for y in range(height):
                    for x in {0,width//2,width-1}:
                        payload[y*stride+x//8]&=~(0x80>>(x%8))
                logical=RasterJob(bytes(payload),width,height,100,200,200,6000,(0,0),len({0,width//2,width-1})*height,1,(0,0),())
                for rotation in (0,180):
                    with self.subTest(width=width,height=height,rotation=rotation):
                        r=rotate_stage(logical,rotation)
                        before=firmware_dots(r)
                        self.assertNotEqual(set(before),{(r.nx_px+x,r.ny_px+y) for x,y in cells(r)})
                        wire=prepare_wire(r)
                        expected={(r.nx_px+x,r.ny_px+y) for x,y in cells(r)}
                        actual=firmware_dots(wire)
                        self.assertEqual(set(actual),expected)
                        self.assertEqual(len(actual),len(expected))
                        self.assertIs(prepare_wire(wire),wire)
                        self.assertEqual(len(firmware_dots(logical)),logical.black_pixels)

    def test_left_and_right_edges_never_crop_black_data(self):
        for nx,width in ((0,13),(987,13)):
            stride=(width+7)//8
            r=RasterJob(b'\x00\x07'*2,width,2,nx,100,200,6000,(0,0),26,1,(0,0),())
            wire=prepare_wire(r)
            self.assertEqual(len(firmware_dots(wire)),26)
            self.assertLessEqual(wire.nx_px+wire.width_px,1000)
        r=RasterJob(b'\x00'*250,1000,2,0,100,200,6000,(0,0),2000,1,(0,0),())
        with self.assertRaisesRegex(JobError,'margin'):prepare_wire(r)


class DpiRuntimeTests(RuntimeBase):
    def test_arbitrary_dpi_image_then_cut_one_final_ack(self):
        raw=bitmap_source(300).replace('G0X0Y0\nM2','G0X40Y40\nM3S200\nG1X41F3000\nM5\nG0X0Y0\nM2')
        c,d=self.make(FakeDevice(active_polls=100),rotation=180,job_budget=10);acks=self.start(c)
        for line in raw.splitlines():response=c.submit_line(line.encode())
        self.assertIsNone(response)
        for n in range(2):
            self.assertTrue(wait_for(lambda:len(d.starts)==n+1));self.assertEqual(acks,[]);d._active_left=0
        self.assertTrue(wait_for(lambda:len(acks)==1))
        self.assertTrue(any('native 338.667 DPI' in e for e in c._events))

    def test_late_invalid_raster_never_uploads_earlier_cut(self):
        prefix='G21G90\nG0X40Y40\nM3S200\nG1X41F3000\nM5\n'
        raw=prefix+bitmap_source(300).replace('G1Y0.084667S0','G1Y0.084667S200',1)
        c,d=self.make();self.start(c)
        for line in raw.splitlines():response=c.submit_line(line.encode())
        self.assertEqual(response,'error:20');self.assertTrue(no_job_writes(d))


if __name__=='__main__':unittest.main()
