"""Independent geometry/pixel fixtures for native 254-DPI picture compilation."""
import hashlib
import json
from pathlib import Path
import unittest
from bridge.job import JobError
from bridge.raster import RasterJob,parse_raster,is_raster_candidate,gen_raster_header,raster_digest

ROOT=Path(__file__).resolve().parents[2]

def source(body,origin='X1Y2',tail='M5\nG90\nG0X0Y0\nM2'):
    return ('G21G90\nM4\nM8\nG0'+origin+'\nG91\nF6000\n'+body+'\n'+tail).encode()

class RasterTests(unittest.TestCase):
    def test_13_black_bits_padding_blank_row_and_absolute_y(self):
        r=parse_raster(source('G1X1.3S200\nG1X-1.3Y.1S0\nG1Y.1\nG1X.1S200'))
        self.assertEqual((r.width_px,r.height_px,r.nx_px,r.ny_px),(13,3,10,20))
        self.assertEqual(r.payload,bytes.fromhex('0007ffff7fff'))
        self.assertEqual((r.black_pixels,r.requested_s,r.final_host_position),(14,200,(0,0)))

    def test_asymmetric_rows_are_not_byte_reversed(self):
        r=parse_raster(source('G1X.3S200\nG1X.7S0\nG1X.3S200\nG1X-1.3Y.2S0\nG1X.2\nG1X.3S200'))
        self.assertEqual((r.width_px,r.height_px),(13,3))
        self.assertEqual(r.payload,bytes.fromhex('1fc7ffffc7ff'))
        self.assertEqual(r.black_pixels,9)

    def test_reverse_motion_maps_to_same_physical_columns(self):
        r=parse_raster(source('G1X.3S200\nG1X.5Y.1S0\nG1X-.3S200'))
        self.assertEqual(r.payload,bytes.fromhex('1ff8'))

    def test_power_off_modal_inheritance_is_preserved(self):
        r=parse_raster(source('G1X.3S200\nG1X.2\nG1X.3S0\nG1X.5Y.1\nG1X-.1S200'))
        self.assertEqual(r.payload,bytes.fromhex('07fffff7'))
        self.assertEqual(r.black_pixels,6)

    def test_origin_snapping_and_finish_waypoints(self):
        r=parse_raster(source('G1X.3S200\nG1X-.3Y.1S0\nG1X.3S200',
                              origin='X33.768Y23.199',tail='M5\nG90\nG0X40Y25\nG0X0Y0\nM30'))
        self.assertEqual((r.nx_px,r.ny_px),(338,232))
        self.assertEqual(r.snap_mm,(.032,.001))
        self.assertEqual(r.finish_points,((40,25),(0,0)))

    def test_unknown_conflicting_and_powered_row_commands_reject(self):
        base='G1X.3S200\nG1X-.3Y.1S0\nG1X.3S200'
        for bad in ('G20','G2X1Y1','G1Z1','G90G91','G1X.1X.2','M3M4','M2S0',
                    'G1Y.1S200','G1X.1Y.1S200','G1Y-.1S0','S201.5'):
            with self.subTest(bad=bad),self.assertRaises(JobError):parse_raster(source(base+'\n'+bad))

    def test_grayscale_mixed_feed_overlaps_and_workspace_reject(self):
        for body in ('G1X.3S200\nG1Y.1S0\nG1X-.3S100',
                     'G1X.3S200\nG1Y.1S0\nG1X-.3F3000S200',
                     'G1X.3S200\nG1X-.3S200\nG1Y.1S0',
                     'G1X100S200\nG1Y.1S0'):
            with self.subTest(body=body),self.assertRaises(JobError):parse_raster(source(body))

    def test_terminal_and_power_cap_require_full_validation(self):
        data=source('G1X.3S200\nG1Y.1S0\nG1X-.3S200')
        for raw in (data.replace(b'M2',b''),data+b'\nM4',data+b'\x00'):
            with self.assertRaises(JobError):parse_raster(raw)
        with self.assertRaises(JobError):parse_raster(data,power_cap=100)
        self.assertTrue(is_raster_candidate(data))
        self.assertFalse(is_raster_candidate(b'G21\nM3\nG0X1Y1\nG1X2F6000S200\nM2'))

    def test_large_picture_above_twenty_thousand_lines_is_compact(self):
        lines=[]
        for y in range(558):
            if y:lines.append('G1Y.1S0')
            for x in range(100):lines.append('G1X'+('.1' if y%2==0 else '-.1')+('S200' if x%2==0 else 'S0'))
        data=source('\n'.join(lines));r=parse_raster(data)
        self.assertGreater(len(data.splitlines()),20000)
        self.assertEqual((r.width_px,r.height_px,r.black_pixels),(100,558,27900))
        self.assertEqual(len(r.payload),13*558)
        self.assertEqual(r.payload[:13],bytes.fromhex('55'*12+'5f'))
        self.assertEqual(r.payload[13:26],bytes.fromhex('aa'*12+'af'))

    def test_full_headers_match_executed_vendor_function(self):
        fixture=json.loads((ROOT/'bridge/tests/fixtures/oracle-raster-header.json').read_text())
        for c in fixture['cases']:
            a=c['args'];r=RasterJob(b'',a['w'],a['h'],a['nx'],a['ny'],200,6000,(0,0),0,0,(0,0),())
            self.assertEqual(gen_raster_header(r,file_id=a['fileId'],name=a['printName']).hex(),c['hex'])

    def test_header_ranges_utf8_and_content_identity(self):
        r=parse_raster(source('G1X.3S200\nG1Y.1S0\nG1X-.3S200'))
        for fid,name in ((-1,'x'),(1<<32,'x'),(True,'x'),(1,''),(1,'x\0'),(1,'图'*10)):
            with self.assertRaises(JobError):gen_raster_header(r,file_id=fid,name=name)
        digest=raster_digest(r,name='x');self.assertEqual(digest,hashlib.sha256(gen_raster_header(r,file_id=0,name='x')+r.payload).digest())
        r.nx_px+=1;self.assertNotEqual(digest,raster_digest(r,name='x'))

if __name__=='__main__':unittest.main()
