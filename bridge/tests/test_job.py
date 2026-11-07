"""Complete-job rejection and translation tests; no physical IO."""
from pathlib import Path
import unittest
from unittest.mock import patch

from bridge.job import JobError, is_finish_line, map_s, parse_job

ROOT = Path(__file__).resolve().parents[2]

class JobTests(unittest.TestCase):
    def test_real_lightburn_m3_export(self):
        job = parse_job((ROOT/'bridge/tests/fixtures/rectangle-lightburn.gc').read_bytes())
        self.assertEqual(job.final_host_position,(0,0))
        self.assertEqual(job.max_s,51)
        self.assertEqual(job.max_feed,120)
        self.assertEqual(job.header_geometry['width_px'],650)
        self.assertEqual(job.header_geometry['height_px'],600)
        self.assertEqual(job.normalized_line_count,len(job.payload.decode().split('\n')))
        self.assertNotIn(b'M2',job.payload)
        self.assertEqual(job.payload.count(b'S51\n'),4)
        self.assertTrue(job.payload.endswith(b'M5\nS0\n'))

    def test_modal_power_and_feed_inherit(self):
        job = parse_job('G21G90\nG0X50Y50\nM3S200\nF120\nG1X51\nY51\nX50\nM2')
        self.assertEqual(job.payload.count(b'S51\n'),3)
        self.assertIn(b'G1 X51.000 Y51.000 F120',job.payload)

    def test_inches_relative_and_negative_offsets(self):
        job = parse_job('G20 G90\nG0X1Y1\nF10\nG91\nM3S100\nG1X-.1Y+.1\nM5\nM30')
        self.assertEqual(job.final_host_position,(22.86,27.94))
        self.assertIn(b'G1 X22.860 Y27.940 F254',job.payload)

    def test_header_geometry_and_vendor_mm_coordinates(self):
        job = parse_job('G0X35Y40\nM3S100\nG1X36Y41F120\nM2')
        self.assertEqual(job.bounds,(350,400,360,410))
        self.assertEqual(job.header_geometry['width_px'],10)
        self.assertEqual(job.header_geometry['nx_px'],350)
        self.assertIn(b'G0 X35.000 Y40.000',job.payload)
        self.assertIn(b'G1 X36.000 Y41.000 F120',job.payload)

    def test_no_beam_on_travel_and_no_zero_length_burn(self):
        job = parse_job('G0X50Y50\nM3S200\nG1X50Y50F120\nG0X51\nM2')
        self.assertNotIn(b'M3',job.payload)
        self.assertEqual(job.max_s,0)

    def test_s_mapping_off_band_and_configured_cap(self):
        self.assertEqual([map_s(s) for s in (0,1,9,10,200)], [0,0,0,3,51])
        self.assertEqual(map_s(1000,1000),253)
        with self.assertRaises(JobError): map_s(201)
        with self.assertRaises(JobError): map_s(1,-1)
        with self.assertRaises(JobError): parse_job('G0X1Y1\nM5S1000\nM2')

    def test_startup_m4_is_allowed_but_dynamic_marking_is_not(self):
        parse_job('M4\nG0X50Y50\nM3\nG1X51F120S100\nM2')
        for code in ('M4\nG0X50Y50\nG1X51F120S100\nM2',
                     'G0X50Y50\nM3S100\nM4\nG1X51F120\nM2'):
            with self.assertRaisesRegex(JobError,'M4'): parse_job(code)

    def test_late_invalid_command_rejects_entire_job(self):
        prefix = 'G0X50Y50\nM3S100\nG1X51F120\n'
        for suffix in ('G1X101\nM2','G92X0\nM2','M8\nM2','G2X52Y50I1\nM2','G1Z1\nM2'):
            with self.subTest(suffix=suffix), self.assertRaises(JobError): parse_job(prefix+suffix)

    def test_per_block_modal_conflicts_and_duplicates(self):
        for line in ('G0G1X1Y1','G20G21','M3M5','G0X1X2Y1','G0G0X1Y1'):
            with self.assertRaises(JobError): parse_job(line+'\nM2')
        parse_job('G00 G17 G40 G21 G54\nG90\nG0X1Y1\nG1X2F120\nG0X0Y0\nM2')

    def test_malformed_and_unsupported_input(self):
        for value in (b'G0X1Y1\x00\nM2', 'G0XNaNY1\nM2', 'G0X1e1Y1\nM2',
                      'G0X1 2Y1\nM2','G0X1Y1\n((comment))\nM2','G0X1Y1\n(open\nM2',
                      'G0X1Y1\nM2\nM3', 'G0X1Y1\nM2\nM30', 'G0X1Y1\nM2S0',
                      '$H\nM2', 'G0X1Y1\nM2\né', 123, b'G0X1Y1\nM2\x0b'):
            with self.subTest(value=value), self.assertRaises(JobError): parse_job(value)

    def test_spool_line_count_and_line_byte_bounds(self):
        for value in (b';'+b'x'*1048576, ';'+('x'*256)+'\nG0X1Y1\nM2', '\n'*20001):
            with self.assertRaises(JobError): parse_job(value)
        with patch('bridge.job.MAX_OUTPUT_BYTES',128):
            with self.assertRaisesRegex(JobError,'normalized'):
                parse_job('G0X1Y1\nM3S100\nF120\n'+('G1X2\nG1X1\n'*10)+'M2')
        with patch('bridge.job.MAX_INPUT_BYTES',128):
            with self.assertRaisesRegex(JobError,'input exceeds'):
                parse_job('G0X1Y1\n'+('G1X2F120\n'*20)+'M2')
        with patch('bridge.job.MAX_LINES',10):
            with self.assertRaisesRegex(JobError,'too many input lines'):
                parse_job('G0X1Y1\n'+('G1X2F120\n'*20)+'M2')

    def test_comment_finish_detection(self):
        self.assertFalse(is_finish_line(';M2'))
        self.assertFalse(is_finish_line('(M2) G90'))
        self.assertTrue(is_finish_line('M02 (end)'))
        self.assertTrue(is_finish_line('M30;done'))
        parse_job('G0X50Y50 (point)\nM2\n; allowed\n')

    def test_feed_limits_and_known_position(self):
        for code in ('G0X1Y1\nG1X2F119\nM2','G0X1Y1\nG1X2F12121\nM2',
                     'G0X1Y1\nG1X2\nM2','G91G0X1Y1\nM2','G0X1\nM2'):
            with self.assertRaises(JobError): parse_job(code)

if __name__ == '__main__': unittest.main()
