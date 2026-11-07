"""Offline codec regressions using physical captures and independent vendor goldens."""
import hashlib
import json
from pathlib import Path
import unittest

from bridge.codec import protocol as p

ROOT = Path(__file__).resolve().parents[2]

def decoded(raw):
    frames = p.StreamDecoder().feed(raw)
    assert len(frames) == 1
    return frames[0]

class ProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.samples = {}
        for path in (ROOT / 'bridge/tests/fixtures/metadata-responses.json',
                     ROOT / 'bridge/tests/fixtures/identity-responses.json'):
            for key, records in json.loads(path.read_text())['responses'].items():
                cls.samples[key] = bytes.fromhex(records[0]['hex'])

    def test_exact_query_and_announce_bytes(self):
        for state in range(4):
            self.assertEqual(p.status_command(state).hex(), 'aabb0800' + f'{state:02x}' + '00000000' + f'{state:04x}')
        self.assertEqual(p.announce_gcode(10).hex(), 'aabb0905010000004a000050')
        self.assertEqual(p.stop_command().hex(), 'aabb08ff000000000000ff')

    def test_control_wire_fields(self):
        for state, builder in ((4, p.print_pause), (2, p.print_resume), (3, p.print_stop)):
            frame = decoded(builder())
            self.assertTrue(frame.checksum_ok)
            self.assertEqual(frame.func, 1)
            self.assertEqual(frame.payload, bytes([state, 0, 101]) + bytes(18) + b'\x01')

    def test_every_split_of_live_frames(self):
        for name, raw in self.samples.items():
            for i in range(len(raw)+1):
                with self.subTest(name=name, split=i):
                    decoder = p.StreamDecoder()
                    frames = decoder.feed(raw[:i]) + decoder.feed(raw[i:])
                    self.assertEqual([f.raw for f in frames], [raw])
                    self.assertTrue(frames[0].checksum_ok)

    def test_concatenation_noise_and_single_byte_reads(self):
        expected = list(self.samples.values())
        stream = b'noise\xaa' + b'\x13'.join(expected) + b'x' * 20000 + b'\xaa'
        decoder = p.StreamDecoder()
        frames = decoder.feed(stream)
        self.assertEqual([f.raw for f in frames], expected)
        self.assertEqual(len(decoder._buf), 1)
        again = p.StreamDecoder()
        result = []
        for byte in b''.join(expected):
            result.extend(again.feed(bytes([byte])))
        self.assertEqual([f.raw for f in result], expected)

    def test_bad_crc_not_accepted_as_query(self):
        for name, parser in (('status', p.parse_status), ('settings', p.parse_settings),
                             ('files', p.parse_file_list), ('version', p.parse_version)):
            data = bytearray(self.samples[name]); data[-1] ^= 1
            frame = decoded(data)
            self.assertFalse(frame.checksum_ok)
            with self.assertRaises(p.CodecError):
                parser(frame)

    def test_malformed_lengths_reset_decoder(self):
        for prefix in [b'\xaa\xbb'+bytes([n]) for n in (0, 1, 2, 251, 255)] + [
                b'\xaa\xcc'+n.to_bytes(4, 'big') for n in (0, 1, 1048577, 0xffffffff)]:
            decoder = p.StreamDecoder()
            with self.assertRaises(p.CodecError):
                decoder.feed(prefix)
            self.assertEqual(decoder._buf, b'')
            self.assertTrue(decoder.feed(self.samples['status'])[0].checksum_ok)

    def test_bounded_long_frame_and_short_after(self):
        data = b'content' * 1000 + b'\x00\x00'
        raw = p.encode_long_frame(data)
        decoder = p.StreamDecoder()
        result = decoder.feed(raw[:13]) + decoder.feed(raw[13:]+self.samples['status'])
        self.assertEqual(result[0].payload, data[:-2])
        self.assertTrue(result[0].checksum_ok)
        self.assertEqual(result[1].raw, self.samples['status'])
        with self.assertRaises(p.CodecError):
            p.parse_status_long(result[0])

    def test_replies_have_expected_identity_and_state(self):
        self.assertEqual(p.parse_version(decoded(self.samples['version'])),
                         {'sw_field': 376, 'hw_hex': '01321e86', 'tail_hex': '0003'})
        status = p.parse_status(decoded(self.samples['status']))
        self.assertEqual((status['mode'], status['w_state'], status['error'], status['u_b_cnn']), (6, 0, 0, 2))
        self.assertTrue(all(status[k] is None for k in ('stop', 'safe_key', 'cover_bitmap')))
        settings = p.parse_settings(decoded(self.samples['settings']))
        self.assertEqual((settings['free'], settings['key_view']), (1, 1))
        self.assertIsNone(settings['overscan_flag'])

    def test_optional_fields_stop_before_crc(self):
        for size in range(27, 35):
            frame = decoded(p.encode_frame(0, bytes(range(size-6))))
            status = p.parse_status_long(frame)
            for name, off, width in p._STATUS_OPTIONAL:
                with self.subTest(size=size, name=name):
                    self.assertEqual(status[name] is not None, off+width <= size-2)
        for size in range(25, 32):
            frame = decoded(p.encode_frame(0, bytes(range(size-6))))
            settings = p.parse_settings_long(frame)
            width = 2
            self.assertEqual(settings['fan_time'] is not None, 26+width <= size-2)
            if 28 <= size-2:
                self.assertEqual(settings['fan_time'], int.from_bytes(frame.raw[26:28], 'big'))

    def test_wrong_function_rejected_by_all_query_parsers(self):
        for name, parser in (('status', p.parse_status_long), ('settings', p.parse_settings_long),
                             ('files', p.parse_file_list), ('version', p.parse_version)):
            good = decoded(self.samples[name])
            wrong = decoded(p.encode_frame(0xff, good.payload))
            with self.assertRaises(p.CodecError):
                parser(wrong)

    def test_file_list_ambiguity_is_preserved(self):
        listing = p.parse_file_list(decoded(self.samples['files']))
        self.assertEqual((listing.declared_field, len(listing.words), listing.tail_hex), (30, 31, '0001'))
        self.assertEqual(len(listing.duplicates), 2)
        self.assertEqual(len(listing.sentinels), 1)
        self.assertIn(0x60e6e3ea, listing.exclude_for_new_job)
        self.assertEqual(listing.vendor_view[0].value, 0x68f92481)
        self.assertTrue(listing.vendor_view[-1].vendor_phantom)
        self.assertEqual(listing.vendor_view[-1].value, 0)

    def test_variable_file_lists_and_tail(self):
        for words, tail in (([], b''), ([1, 2], b'\x00\x01'), ([0xffffffff, 7, 7], b'\x22')):
            payload = bytes([0]) + b''.join(i.to_bytes(4, 'big') for i in words) + tail
            listing = p.parse_file_list(decoded(p.encode_frame(0, payload)))
            self.assertEqual([entry.value for entry in listing.words], words)
            self.assertEqual(listing.tail_hex, tail.hex())

    def test_headers_match_full_vendor_64_byte_oracles(self):
        fixtures = [
            ({'file_id':0x12345678, 'width':100, 'height':200, 'nx':0, 'ny':0, 'dpi':254,
              'line_count':3, 'name':'test.gcode'},
             '20006400c812345678000000030000000000fe000000000000000000000000000000746573742e67636f64650000000000000000000000000000000000000000'),
            ({'file_id':0x7fffffff, 'width':1, 'height':65535, 'nx':65535, 'ny':1, 'dpi':1,
              'line_count':0xffffffff, 'name':'ünïcode'},
             '200001ffff7fffffffffffffffffff00010001000000000000000000000000000000c3bc6ec3af636f6465000000000000000000000000000000000000000000')]
        for args, golden in fixtures:
            result = p.gen_gcode_header(**args)
            self.assertEqual(len(result), 64)
            self.assertEqual(result.hex(), golden)

    def test_header_bounds_utf8_and_nul(self):
        base = dict(file_id=1, width=1, height=1, nx=0, ny=0, dpi=254, line_count=1, name='x')
        for key, value in (('file_id', 0), ('file_id', 0xffffffff), ('width',0), ('height',65536),
                           ('nx',-1), ('ny',65536), ('dpi',0), ('line_count',0x100000000),
                           ('name','é'*15), ('name','x\x00y'), ('width',True), ('name',42)):
            with self.subTest(key=key, value=value), self.assertRaises(p.CodecError):
                p.gen_gcode_header(**(base | {key:value}))
        self.assertEqual(len(p.gen_gcode_header(**(base | {'name':'é'*14+'x'}))),64)

    def test_numeric_and_type_bounds(self):
        for callback in (lambda:p.announce_gcode(-1), lambda:p.announce_gcode(0xffffffff-63),
                         lambda:p.encode_frame(256,b''), lambda:p.encode_frame(1,b'x'*248),
                         lambda:p.encode_frame(1,42), lambda:p.status_command(4),
                         lambda:p.status_command(True), lambda:p.print_command(state=0),
                         lambda:p.print_command(state=1,laser=101), lambda:p.StreamDecoder().feed('x')):
            with self.assertRaises(p.CodecError): callback()

    def test_content_hash_and_occupied_id_selection(self):
        args = dict(width=10, height=20, nx=0, ny=0, dpi=254, line_count=2, name='test', payload=b'M5\n')
        digest = p.gcode_content_digest(**args)
        other = p.gcode_content_digest(**(args | {'payload':b'G0X1\n'}))
        self.assertNotEqual(digest, other)
        first = p.select_file_id(digest, [])
        second = p.select_file_id(digest, [first])
        self.assertNotIn(second, (0, 0xffffffff, first))
        self.assertEqual(second, int.from_bytes(hashlib.sha256(digest).digest()[:4], 'big'))

if __name__ == '__main__':
    unittest.main()
