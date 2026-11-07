"""Finite fake-serial tests; this module never opens physical hardware."""
import json
from pathlib import Path
import threading
import unittest

from bridge.codec import protocol as p
from bridge.transport import LP2Session, TransportError

ROOT = Path(__file__).resolve().parents[2]

class Log:
    def __init__(self): self.rows = []
    def event(self, kind, **fields): self.rows.append(dict(kind=kind, **fields))

class FakeSerial:
    def __init__(self, *, refuse=False, truncate=False, no_completion=False):
        self.rx, self.writes = bytearray(), []
        self.closed, self.raw_remaining = False, None
        self.upload_id = None
        self.refuse, self.truncate, self.no_completion = refuse, truncate, no_completion
        self.fixture = {}
        for name in ('bridge/tests/fixtures/metadata-responses.json', 'bridge/tests/fixtures/identity-responses.json'):
            for key, value in json.loads((ROOT/name).read_text())['responses'].items():
                self.fixture[key] = bytes.fromhex(value[0]['hex'])

    @property
    def in_waiting(self): return min(3, len(self.rx))

    def read(self, count):
        data = bytes(self.rx[:count]); del self.rx[:count]
        return data

    def write(self, data):
        self.writes.append(bytes(data))
        if self.truncate: return len(data)-1
        if self.raw_remaining is not None:
            if self.upload_id is None and len(data)==64 and data[0] in (0x20,0x60):
                self.upload_id = int.from_bytes(data[5:9], 'big')
            self.raw_remaining -= len(data)
            if self.raw_remaining == 0:
                self.raw_remaining = None
                old = p.StreamDecoder().feed(self.fixture['files'])[0]
                self.fixture['files'] = p.encode_frame(0, old.payload[:-2]+self.upload_id.to_bytes(4,'big')+old.payload[-2:])
                if not self.no_completion:
                    self.rx.extend(p.encode_frame(5, b'\x02\x01\x06\x00\x00'))
            elif self.raw_remaining < 0:
                raise AssertionError('extra bulk bytes')
            return len(data)
        if data == p.query_status(): self.rx.extend(self.fixture['status'])
        elif data == p.query_version(): self.rx.extend(self.fixture['version'])
        elif data == p.query_file_ids(): self.rx.extend(self.fixture['files'])
        elif data == p.stop_command(): self.rx.extend(p.encode_frame(0xff, bytes(5)))
        elif data[3:5] == b'\x01\x01': self.rx.extend(p.encode_frame(1,b'\x01'+bytes(4)))
        elif data[3] == 5:
            if self.refuse: self.rx.extend(p.encode_frame(0xff, bytes(5)))
            else:
                self.raw_remaining = int.from_bytes(data[5:9], 'big')
                self.upload_id = None
                self.rx.extend(p.encode_frame(5, b'\x01\x01\x05\x00\x00'))
        return len(data)

    def close(self): self.closed = True

def job():
    payload = b'G21\nG90\nM5S0\nG0X50Y50\nM5S0\n'
    header = p.gen_gcode_header(file_id=1234, width=1, height=1, nx=500, ny=500,
                               dpi=254, line_count=payload.count(b'\n')+1, name='test')
    return header, payload

class TransportTests(unittest.TestCase):
    def test_packed_bitmap_upload_and_geometry_rejection(self):
        header=bytearray(64);header[0]=0x60
        header[1:5]=bytes.fromhex('000d0002');header[5:9]=(1234).to_bytes(4,'big')
        header[9]=4;header[10:16]=bytes.fromhex('000a001400fe')
        payload=bytes.fromhex('0007ffff')
        s,f,log=self.session();s.upload(bytes(header),payload,threading.Event())
        s.start(file_id=1234,nx=10,ny=20,wire_depth=100,laser_percent=0)
        self.assertEqual(s.phase,'running')
        for raw,body in ((bytes(header),bytes.fromhex('0000ffff')),(bytes(header),payload[:-1]),
                         (bytes([0xdd])+bytes(header)[1:],payload)):
            other,device,_=self.session()
            with self.assertRaises(TransportError):other.upload(raw,body,threading.Event())
            self.assertEqual(device.writes,[])

    def session(self, **args):
        fake, log = FakeSerial(**args), Log()
        return LP2Session(fake, log, timeout=.03), fake, log

    def test_live_shaped_readbacks_and_lossless_capture(self):
        session, fake, log = self.session()
        self.assertEqual(session.identify()['sw_field'], 376)
        self.assertEqual(session.status()['mode'], 6)
        self.assertEqual(len(session.files().words), 31)
        received = b''.join(bytes.fromhex(row['hex']) for row in log.rows if row['kind']=='serial_rx')
        self.assertEqual(received, fake.fixture['version']+fake.fixture['status']+fake.fixture['files'])
        self.assertEqual([bytes.fromhex(row['hex']) for row in log.rows if row['kind']=='serial_tx'], fake.writes)
        session.close(); self.assertTrue(fake.closed)

    def test_whole_upload_without_post_upload_exit_then_start(self):
        session, fake, log = self.session()
        header, payload = job()
        session.upload(header, payload, threading.Event())
        self.assertEqual(fake.writes, [p.query_status(), p.stop_command(), p.announce_gcode(len(payload)), header, payload])
        self.assertEqual(session.phase, 'uploaded')
        session.start(file_id=1234, nx=500, ny=500)
        start = p.StreamDecoder().feed(fake.writes[-1])[0]
        self.assertEqual((start.func, start.payload[0], start.payload[1]), (1, 1, 100))
        self.assertEqual(int.from_bytes(start.payload[3:7], 'big'), 1234)

    def test_refusal_prevents_raw_payload(self):
        session, fake, log = self.session(refuse=True)
        with self.assertRaisesRegex(TransportError, 'unexpected func FF'):
            session.upload(*job(), threading.Event())
        self.assertEqual(len(fake.writes), 3)
        self.assertTrue(session.poisoned)
        with self.assertRaises(TransportError): session.status()

    def test_cancel_before_upload_sends_nothing(self):
        session, fake, log = self.session()
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(TransportError): session.upload(*job(), cancel)
        self.assertEqual(fake.writes, [])

    def test_short_write_fails_without_retry(self):
        session, fake, log = self.session(truncate=True)
        with self.assertRaisesRegex(TransportError, 'short'):
            session.status()
        self.assertEqual(len(fake.writes), 1)

    def test_start_and_control_gates(self):
        session, fake, log = self.session()
        with self.assertRaises(TransportError): session.start(file_id=1234,nx=0,ny=0)
        with self.assertRaises(TransportError): session.control('pause')
        session.upload(*job(), threading.Event())
        session.start(file_id=1234, nx=500, ny=500)
        for command, state in (('pause',4), ('resume',2), ('stop',3)):
            session.control(command)
            self.assertEqual(p.StreamDecoder().feed(fake.writes[-1])[0].payload[0],state)

    def test_checksum_error_fails_transaction(self):
        session, fake, log = self.session()
        data = bytearray(fake.fixture['status']); data[-1] ^= 1
        fake.fixture['status'] = bytes(data)
        with self.assertRaisesRegex(TransportError, 'checksum'): session.status()

    def test_cross_thread_access_refused(self):
        session, fake, log = self.session()
        errors = []
        def work():
            try: session.status()
            except TransportError as exc: errors.append(str(exc))
        thread = threading.Thread(target=work); thread.start(); thread.join(1)
        self.assertEqual(len(errors),1)
        self.assertEqual(fake.writes,[])

    def test_observed_transfer_tail_is_not_a_refusal(self):
        session, fake, log = self.session()
        header,payload = job()
        session.upload(header,payload,threading.Event())
        fake.rx.extend(p.encode_frame(0xff,b'\x01'+(64+len(payload)).to_bytes(4,'big')))
        session.start(file_id=1234,nx=500,ny=500)
        self.assertTrue(any(row['kind']=='async_transfer_exit' for row in log.rows))
        session.control('stop')
        fake.rx.extend(p.encode_frame(0xff,bytes(5)))
        self.assertEqual(session.status()['mode'],6)
        self.assertTrue(any(row['kind']=='async_print_exit' for row in log.rows))

    def test_unknown_ff_still_fails_after_start(self):
        session,fake,log = self.session()
        session.upload(*job(),threading.Event())
        session.start(file_id=1234,nx=500,ny=500)
        fake.rx.extend(p.encode_frame(0xff,b'\x7f'+bytes(4)))
        with self.assertRaisesRegex(TransportError,'unexpected func FF'): session.status()

if __name__ == '__main__': unittest.main()
