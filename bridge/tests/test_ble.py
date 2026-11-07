"""Offline BLEPort tests with a fake async Bleak client; bleak is never imported."""
import asyncio
import sys
import threading
import time
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bridge.ble import (  # noqa: E402
    BLEPort,
    BleError,
    ISSC_NOTIFY,
    ISSC_SERVICE,
    ISSC_WRITE,
    scan_lp2,
)


class FakeChar:
    def __init__(self, uuid, max_write_without_response_size=200):
        self.uuid = str(uuid)
        self.max_write_without_response_size = max_write_without_response_size


class FakeService:
    def __init__(self, chars):
        self.uuid = str(ISSC_SERVICE)
        self.characteristics = list(chars)


def fake_char(uuid, **kw):
    return FakeChar(uuid, **kw)


class FakeClient:
    """BleakClient-shaped fake; notifications are delivered by the test."""

    def __init__(self, address, *, fail_connect=False, missing=None, max_write=200):
        self.address = address
        self.fail_connect = fail_connect
        self.missing = missing or set()
        self.max_write = max_write
        self.connected = False
        self.disconnected = 0
        self.writes = []            # (chunk_bytes, response_flag)
        self.write_times = []
        self.notify_started = None
        self._callback = None
        self._loop = None
        chars = []
        if ISSC_WRITE not in self.missing:
            chars.append(FakeChar(ISSC_WRITE, max_write_without_response_size=max_write))
        if ISSC_NOTIFY not in self.missing:
            chars.append(FakeChar(ISSC_NOTIFY))
        services = [] if ISSC_SERVICE in self.missing else [FakeService(chars)]
        self.services = services

    async def connect(self):
        self._loop = asyncio.get_running_loop()
        if self.fail_connect:
            raise BleError('fake connect failure')
        self.connected = True

    async def disconnect(self):
        self.connected = False
        self.disconnected += 1

    async def start_notify(self, uuid, callback):
        if UUID_str(uuid) != str(ISSC_NOTIFY):
            raise BleError('unexpected notify uuid')
        self.notify_started = uuid
        self._callback = callback

    async def write_gatt_char(self, char, data, response=False):
        if not self.connected:
            raise BleError('not connected')
        if len(data) > self.max_write:
            raise BleError('chunk exceeds max_write_without_response_size')
        self.writes.append((bytes(data), response))
        self.write_times.append(time.monotonic())

    def deliver(self, data):  # from the loop thread, like real bleak callbacks
        self._loop.call_soon_threadsafe(self._callback, None, bytes(data))

    def fire_disconnect(self):  # sync callback path, like bleak disconnected_callback
        self.connected = False
        self.disconnected += 1


def UUID_str(u):
    return str(u)


def make_port(client, **kw):
    return BLEPort('AA:BB:CC:DD:EE:FF', client_factory=lambda address: client, **kw)


class BLEPortTests(unittest.TestCase):
    def test_connect_verifies_characteristics_and_notifications(self):
        client = FakeClient('AA:BB:CC:DD:EE:FF')
        port = make_port(client)
        port.connect()
        try:
            self.assertEqual(client.notify_started, str(ISSC_NOTIFY))
            self.assertEqual(client.max_write, 200)  # 179 cap still applies
            self.assertEqual(port.upload_chunk_size, 179)
            self.assertEqual(port.upload_chunk_delay, 0.115)
        finally:
            port.close()

    def test_missing_service_or_characteristics_fail_connect(self):
        for missing in (ISSC_SERVICE, ISSC_WRITE, ISSC_NOTIFY):
            client = FakeClient('x', missing={missing})
            port = make_port(client)
            with self.assertRaises(BleError):
                port.connect()
            port.close()  # idempotent cleanup after failed init

    def test_read_timeout_then_notification(self):
        client = FakeClient('x')
        port = make_port(client)
        port.connect()
        try:
            t0 = time.monotonic()
            self.assertEqual(port.read(10), b'')
            self.assertLess(time.monotonic() - t0, 1.0)
            self.assertEqual(port.in_waiting, 0)
            client.deliver(b'hello')
            time.sleep(0.05)
            self.assertEqual(port.in_waiting, 5)
            self.assertEqual(port.read(3), b'hel')
            self.assertEqual(port.read(10), b'lo')
        finally:
            port.close()

    def test_write_splits_at_179_and_respects_delay_across_calls(self):
        client = FakeClient('x')
        port = make_port(client)
        port.connect()
        try:
            self.assertEqual(port.write(b'a' * 400), 400)
            self.assertEqual(port.write(b'b' * 5), 5)  # second call: no forced initial wait
            self.assertEqual([len(w) for w, _ in client.writes], [179, 179, 42, 5])
            self.assertTrue(all(r is False for _, r in client.writes))
            gaps = [t2 - t1 for t1, t2 in zip(client.write_times, client.write_times[1:])]
            self.assertTrue(all(g >= 0.09 for g in gaps), gaps)
        finally:
            port.close()

    def test_smaller_characteristic_max_is_respected(self):
        client = FakeClient('x', max_write=20)
        port = make_port(client)
        port.connect()
        try:
            self.assertEqual(port.write(b'z' * 45), 45)
            self.assertEqual([len(w) for w, _ in client.writes], [20, 20, 5])
        finally:
            port.close()

    def test_rx_buffer_bound_rejects_overflow(self):
        client = FakeClient('x')
        port = make_port(client, max_buffered_rx=16)
        port.connect()
        try:
            client.deliver(b'x' * 10)
            time.sleep(0.05)
            client.deliver(b'y' * 10)  # would exceed 16
            time.sleep(0.05)
            with self.assertRaises(BleError): port.in_waiting
            with self.assertRaises(BleError):
                port.read(1)
        finally:
            port.close()

    def test_disconnect_rejects_writes_and_no_reconnect(self):
        client = FakeClient('x')
        port = make_port(client)
        port.connect()
        try:
            client.fire_disconnect()
            port._on_disconnected(client)
            time.sleep(0.05)
            with self.assertRaises(BleError):
                port.write(b'nope')
            self.assertEqual(client.writes, [])
        finally:
            port.close()

    def test_close_idempotent_and_final_disconnect_clean(self):
        client = FakeClient('x')
        port = make_port(client)
        port.connect()
        thread=port._loop_thread
        port.close()
        self.assertFalse(thread.is_alive())
        self.assertEqual(client.disconnected, 1)
        port.close()
        self.assertEqual(client.disconnected, 1)
        self.assertEqual(threading.active_count() < 10, True)  # loop thread joined

    def test_connect_failure_leaves_no_loop_thread(self):
        client = FakeClient('x', fail_connect=True)
        port = make_port(client)
        with self.assertRaises(BleError):
            port.connect()
        port.close()

    def test_disconnect_fails_reads_without_waiting_for_protocol_timeout(self):
        client=FakeClient('x');port=make_port(client);port.connect()
        try:
            port._on_disconnected(client)
            with self.assertRaises(BleError):port.read(1)
            with self.assertRaises(BleError):port.in_waiting
        finally:port.close()

    def test_concurrent_writers_keep_each_payload_contiguous(self):
        client=FakeClient('x',max_write=20);port=make_port(client);port.upload_chunk_delay=.005;port.connect()
        try:
            threads=[threading.Thread(target=port.write,args=(v*45,)) for v in (b'a',b'b')]
            for t in threads:t.start()
            for t in threads:t.join(2);self.assertFalse(t.is_alive())
            out=b''.join(x for x,_ in client.writes)
            self.assertIn(out,(b'a'*45+b'b'*45,b'b'*45+b'a'*45))
        finally:port.close()

    def test_no_bleak_import_on_linux_tests(self):
        self.assertNotIn('bleak', sys.modules)


class FakeScanner:
    def __init__(self, devices):
        self._devices = devices

    async def discover(self, timeout=8,return_adv=True):
        return {d.address:(d,FakeAdv(d.name or (d.advertisement_data.local_name if d.advertisement_data else None),d.service_uuids)) for d in self._devices}


class FakeAdv:
    def __init__(self, local_name=None,service_uuids=()):
        self.local_name = local_name
        self.service_uuids=list(service_uuids)


class FakeDevice:
    def __init__(self, address, name=None, service_uuids=(), adv_local=None):
        self.address = address
        self.name = name
        self.service_uuids = list(service_uuids)
        self.advertisement_data = FakeAdv(adv_local) if adv_local else None


class ScanTests(unittest.TestCase):
    def test_scan_returns_only_matching_candidates(self):
        scanner = FakeScanner([
            FakeDevice('1', name='LaserPecker 2'),
            FakeDevice('2', name='lp2-mini'),
            FakeDevice('3', name=None, service_uuids=[str(ISSC_SERVICE)]),
            FakeDevice('4', name='Xiaomi Bulb'),
            FakeDevice('5', name=None, service_uuids=['0000180d-0000-1000-8000-00805f9b34fb']),
            FakeDevice('6', name=None, adv_local='LP2 Pro'),
        ])
        found = asyncio.run(scan_lp2(timeout=1, scanner=scanner))
        self.assertEqual({d['address'] for d in found}, {'1', '2', '3', '6'})


if __name__ == '__main__':
    unittest.main()
