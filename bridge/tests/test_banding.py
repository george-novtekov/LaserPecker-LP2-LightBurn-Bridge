"""Image density conservation and continuous-run regressions; no physical IO."""
from decimal import Decimal
import math
import unittest
from bridge.raster import _area_rows,_pack_coverage,parse_raster,RasterJob
from bridge.sequence import compile_sequence
from bridge.job import Job,JobError
from bridge.tests.test_dpi import bitmap_source,cells
from bridge.tests.test_runtime import RuntimeBase,FakeDevice,wait_for,no_job_writes
from bridge.tests.test_m4_scan import MIXED
from bridge.tests import test_service


def source_spans(raw):
    # Independent Decimal interpreter for the limited synthetic LightBurn scans.
    import re
    x=y=None;absolute=True;spindle='M5';power=Decimal(0);feed=None;motion='G0';out=[]
    for line in raw.splitlines():
        words=re.findall(r'([GMXYFS])([+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+))',line)
        endx,endy=x,y
        params={}
        for axis,text in words:
            v=Decimal(text)
            if axis=='G':
                if v in (0,1):motion='G'+str(int(v))
                if v in (90,91):absolute=v==90
            elif axis=='M' and v in (3,4,5):spindle='M'+str(int(v))
            elif axis=='S':power=v
            elif axis=='F':feed=float(v)
            elif axis in 'XY':params[axis]=v
        if not params:continue
        endx=params.get('X',x) if absolute else x+params.get('X',0)
        endy=params.get('Y',y) if absolute else y+params.get('Y',0)
        if x is not None and motion=='G1' and spindle in ('M3','M4') and power>0:
            out.append(((float(x),float(y)),(float(endx),float(endy)),feed))
        x,y=endx,endy
    return out


class CoverageTests(unittest.TestCase):
    def test_rectangle_overlap_is_integrated_not_nearest_sampled(self):
        coverage=list(_area_rows({0:[(.25,.75)]},0,0,1,1.5,2,2))
        self.assertEqual(coverage,[[.5,0],[.25,0]])

    def test_error_diffusion_preserves_uniform_tone_and_white_padding(self):
        for tone in (.1,.25,.5,.75,.9):
            payload,black=_pack_coverage(([tone]*137 for _ in range(200)),137,200)
            self.assertAlmostEqual(black/(137*200),tone,delta=.003)
            self.assertTrue(all(v&127==127 for v in payload[17::18]))

    def test_204_to_254_density_error_is_lower_than_repeated_binary_columns(self):
        # Alternating source columns have analytically known partial coverage.
        # Nearest sampling locks the rounding error to vertical columns.
        source_pitch=25.4/204;pitch=.1;source_width=180;height=200
        width=math.ceil(source_width*source_pitch/pitch)
        runs=[(x*source_pitch,(x+1)*source_pitch) for x in range(0,source_width,2)]
        rows={0:runs};coverage=list(_area_rows(rows,0,0,pitch,height*pitch,width,height))
        expected=coverage[0]
        payload,black=_pack_coverage(coverage,width,height)
        stride=(width+7)//8
        density=[sum(not payload[y*stride+x//8]&(0x80>>(x%8)) for y in range(height))/height for x in range(width)]
        old=[float(math.floor((x+.5)*pitch/source_pitch)%2==0) if (x+.5)*pitch<source_width*source_pitch else 0 for x in range(width)]
        rmse=lambda values:math.sqrt(sum((a-b)**2 for a,b in zip(values,expected))/width)
        self.assertLess(rmse(density),rmse(old)*.5)
        self.assertAlmostEqual(black,sum(expected)*height,delta=width)

    def test_exact_254_grid_is_not_redithered(self):
        raw=bitmap_source(254);r=parse_raster(raw)
        self.assertEqual(cells(r),{(x,y) for x in range(13) if x%2==0 for y in range(7)})


class ContinuousTests(unittest.TestCase):
    def test_source_spans_preserved_at_all_requested_dpi(self):
        for dpi in (154,204,254,300,508,1000,2000):
            with self.subTest(dpi=dpi):
                raw=bitmap_source(dpi);seq=compile_sequence(raw,image_mode='continuous')
                self.assertTrue(all(isinstance(s,Job) for s in seq.stages))
                expected=source_spans(raw);actual=[v for s in seq.stages for v in s.marking_vectors]
                self.assertEqual(len(expected),len(actual))
                for (a,b,f),(aa,bb,ff) in zip(expected,actual):
                    self.assertEqual(f,ff)
                    for p,q in zip(a+b,aa+bb):self.assertAlmostEqual(p,q,delta=.000501)
                self.assertTrue(any('bypass native packed-byte' in n for n in seq.notes))

    def test_mixed_image_fill_cut_keep_span_order_and_layers(self):
        result=compile_sequence(MIXED,image_mode='continuous')
        self.assertTrue(all(isinstance(s,Job) for s in result.stages))
        self.assertEqual(len(result.stages),3)
        self.assertEqual(result.stages[0].marking_vectors,(((30,30),(30.3,30),6000.),((30,30.1),(30.3,30.1),6000.)))
        self.assertEqual(result.stages[-1].final_host_position,(0,0))

    def test_large_image_is_one_uninterrupted_stock_file_without_losing_runs(self):
        raw=bitmap_source(254,rows=120,columns=100)
        result=compile_sequence(raw,image_mode='continuous')
        actual=[v for s in result.stages for v in s.marking_vectors]
        expected=source_spans(raw)
        self.assertEqual(len([s for s in result.stages if s.marking_vectors]),1);self.assertEqual(len(actual),6000)
        self.assertEqual(actual,expected)
        self.assertTrue(all(len(s.payload)<=1048576 for s in result.stages))

    def test_bad_image_mode_powered_turns_and_grayscale_reject(self):
        with self.assertRaises(JobError):compile_sequence(MIXED,image_mode='unknown')
        for raw in (bitmap_source(204).replace('Y0.124510S0','Y0.124510S200',1),
                    bitmap_source(204).replace('S200','S100',1)):
            with self.assertRaises(JobError):compile_sequence(raw,image_mode='continuous')


class ContinuousRuntimeTests(RuntimeBase):
    def test_all_stock_headers_are_gcode_and_only_one_final_ack(self):
        c,d=self.make(FakeDevice(active_polls=100),image_mode='continuous',rotation=180,job_budget=10)
        acks=self.start(c)
        for line in MIXED.splitlines():response=c.submit_line(line.encode())
        self.assertIsNone(response)
        for i in range(3):
            self.assertTrue(wait_for(lambda:len(d.starts)==i+1));self.assertEqual(acks,[]);d._active_left=0
        self.assertTrue(wait_for(lambda:len(acks)==1))
        headers=[w for w in d.writes if len(w)==64 and w[0] in (0x20,0x60)]
        self.assertEqual([h[0] for h in headers],[0x20]*3)
        self.assertTrue(any('continuous image' in m for m in c._events))

    def test_invalid_later_image_never_uploads_prior_cut(self):
        raw='G21G90\nG0X40Y40\nM3S200\nG1X41F3000\nM5\n'+bitmap_source(204).replace('Y0.124510S0','Y0.124510S200',1)
        c,d=self.make(image_mode='continuous');self.start(c)
        for line in raw.splitlines():response=c.submit_line(line.encode())
        self.assertEqual(response,'error:20');self.assertTrue(no_job_writes(d))


class ContinuousServiceTests(unittest.TestCase):
    make=test_service.ServiceTests.make
    def test_gui_service_defaults_to_continuous_image_mode(self):
        service,device,calls=self.make();service.start(dict(transport='usb',tcp_port=0))
        self.assertTrue(wait_for(lambda:service.snapshot()['connected']))
        self.assertEqual(service._controller.config.image_mode,'continuous')
        self.assertTrue(no_job_writes(device))


if __name__=='__main__':unittest.main()
