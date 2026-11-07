"""Synchronous serial-like BLE port for LP2 (ISSC service) over Bleak.

Background asyncio loop thread owns the Bleak connection; read()/in_waiting are
non-blocking against a notification byte buffer, write()/connect()/close()
marshal onto the loop via run_coroutine_threadsafe. No reconnect and no replay:
a disconnect fails the session. Bleak is imported lazily so tests can inject a
fake client without bleak installed.
"""
from __future__ import annotations

import asyncio
import threading
import time
from uuid import UUID

ISSC_SERVICE = UUID('49535343-fe7d-4ae5-8fa9-9fafd205e455')
ISSC_WRITE = UUID('49535343-8841-43f4-a8d4-ecbe34729bb3')
ISSC_NOTIFY = UUID('49535343-1e4d-4bd9-ba61-23c647249616')

LP2_NAME_PREFIXES = ('laserpecker', 'lp2')

MAX_BUFFERED_RX = 1 << 20  # 1 MiB
CONNECT_TIMEOUT = 20.0
READ_TIMEOUT = 0.2
# Vendor WebBLE waits100ms per chunk plus15ms in the bulk payload loop.
DEFAULT_CHUNK_DELAY = 0.115


class BleError(RuntimeError):
    pass


def _load_bleak():
    try:
        import bleak  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise BleError('bleak is required to connect; inject a fake client for tests') from exc
    return bleak


class BLEPort:
    """Serial-like facade: read(n)/in_waiting/write(data)/close().

    client_factory(address) -> awaitable Bleak-compatible client is injectable
    for tests; production uses BleakClient. Writes use write-without-response
    split at min(upload_chunk_size, characteristic max) with at least
    upload_chunk_delay seconds between chunks, counted across write calls.
    """

    transport_kind = "bluetooth"
    upload_chunk_size = 179
    upload_chunk_delay = DEFAULT_CHUNK_DELAY

    def __init__(self, address, *, client_factory=None, read_timeout=READ_TIMEOUT,
                 connect_timeout=CONNECT_TIMEOUT, max_buffered_rx=MAX_BUFFERED_RX):
        self.address = address
        self.read_timeout = read_timeout
        self.connect_timeout = connect_timeout
        self.max_buffered_rx = max_buffered_rx
        self._client_factory = client_factory
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._overflow = False
        self._loop = None
        self._loop_thread = None
        self._loop_started = None
        self._loop_stop = None
        self._client = None
        self._write_char = None
        self._buffer = bytearray()
        self._connected = False
        self._closed = False
        self._last_write = None
        self._write_lock = threading.Lock()

    # -- lifecycle ----------------------------------------------------------

    def connect(self):
        with self._lock:
            if self._closed:
                raise BleError('port closed')
            if self._connected:
                raise BleError('already connected')
            self._start_loop()
        try:
            self._submit(self._connect_async(), self.connect_timeout)
        except BaseException:
            self.close()
            raise
        return self

    def close(self):
        """Idempotent; safe from any thread, never joins its own loop thread."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            loop, thread, stop = self._loop, self._loop_thread, self._loop_stop
            self._loop = self._loop_thread = self._loop_started = self._loop_stop = None
            client = self._client
        if loop is None:
            return
        if thread is threading.current_thread():
            loop.create_task(self._disconnect_async(client)).add_done_callback(lambda _: loop.stop())
            return
        if client is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._disconnect_async(client), loop).result(
                    timeout=self.connect_timeout)
            except Exception:
                pass  # final disconnect is best-effort; device already gone or loop wedged
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        # Closing from inside the loop thread: the loop exits once control
        # returns to it and the daemon thread cleans up on its own.

    # -- serial-like API ----------------------------------------------------

    @property
    def in_waiting(self):
        with self._lock:
            self._check_connected()
            return len(self._buffer)

    def read(self, n=1):
        """Return up to n buffered bytes; waits up to read_timeout for at least one."""
        if n <= 0:
            return b''
        deadline = time.monotonic() + self.read_timeout
        with self._cond:
            while not self._buffer:
                self._check_connected()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return b''
                self._cond.wait(remaining)
            self._check_connected()
            data = bytes(self._buffer[:n])
            del self._buffer[:n]
            return data

    def _check_connected(self):
        if self._overflow: raise BleError('rx buffer overflow')
        if self._closed or not self._connected: raise BleError('Bluetooth disconnected; no automatic reconnect')

    def write(self, data):
        with self._write_lock:
            return self._write(data)

    def _write(self, data):
        """Write-without-response in chunked pieces; returns bytes written."""
        data = bytes(data)
        if not data:
            return 0
        with self._lock:
            if self._closed or not self._connected:
                raise BleError('not connected; writes rejected after disconnect')
            chunk_size = min(self.upload_chunk_size,
                             getattr(self._write_char, 'max_write_without_response_size',
                                     self.upload_chunk_size))
            if type(chunk_size) is not int or chunk_size<1: raise BleError("invalid BLE chunk limit")
        written = 0
        for offset in range(0, len(data), chunk_size):
            with self._lock:
                if self._closed or not self._connected:
                    raise BleError('disconnected during write')
                wait=0 if self._last_write is None else self.upload_chunk_delay-(time.monotonic()-self._last_write)
            if wait>0:time.sleep(wait)
            self._submit(self._write_async(data[offset:offset + chunk_size]), timeout=10.0)
            written += min(chunk_size, len(data) - offset)
            with self._lock:
                self._last_write = time.monotonic()
        return written

    # -- internals ----------------------------------------------------------

    def _start_loop(self):
        if self._loop_thread is not None:
            return
        loop = asyncio.new_event_loop()
        ready = threading.Event()
        stop = threading.Event()
        thread = threading.Thread(target=self._run_loop, args=(loop, ready, stop),
                                  name=f'ble-{self.address}', daemon=True)
        self._loop = loop
        self._loop_thread = thread
        self._loop_started = ready
        self._loop_stop = stop
        try:
            thread.start()
            # Wait until the loop thread owns the loop before submitting to it,
            # so the first submitted coroutine never races a dead loop.
            if not ready.wait(5.0):
                raise BleError('event loop thread failed to start')
        except BaseException:
            # Initialization-failure cleanup: no leaked running loop. The
            # daemon thread (if it started) exits via the stop event; closing a
            # still-running loop would raise, so only close after it stops.
            stop.set()
            if thread.is_alive():
                thread.join(timeout=5.0)
            if not thread.is_alive() and not loop.is_closed():
                loop.close()
            self._loop = self._loop_thread = self._loop_started = self._loop_stop = None
            raise

    @staticmethod
    def _run_loop(loop, ready, stop):
        asyncio.set_event_loop(loop)
        loop.call_soon_threadsafe(ready.set)
        loop.run_forever()
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()

    def _submit(self, coro, timeout):
        loop = self._loop
        if loop is None or not loop.is_running():
            coro.close()
            raise BleError('loop not running')
        future=asyncio.run_coroutine_threadsafe(coro, loop)
        try: return future.result(timeout=timeout)
        except BaseException:
            future.cancel()
            raise

    async def _connect_async(self):
        if self._client_factory is not None:
            client = self._client_factory(self.address)
            self._client = client
            maybe = client.connect()
            if asyncio.iscoroutine(maybe) or isinstance(maybe, asyncio.Future):
                await maybe
            self._client = client
        else:
            bleak = _load_bleak()
            self._client = bleak.BleakClient(self.address, timeout=self.connect_timeout,
                                            disconnected_callback=self._on_disconnected)
            await self._client.connect()
        await self._verify_characteristics()

    async def _verify_characteristics(self):
        service = None
        for srv in self._client.services:
            if UUID(srv.uuid) == ISSC_SERVICE:
                service = srv
                break
        if service is None:
            raise BleError('ISSC service 49535343-fe7d-4ae5-8fa9-9fafd205e455 not found')
        write_char = notify_char = None
        for char in service.characteristics:
            if UUID(char.uuid) == ISSC_WRITE:
                write_char = char
            elif UUID(char.uuid) == ISSC_NOTIFY:
                notify_char = char
        if write_char is None:
            raise BleError('ISSC write characteristic not found')
        if notify_char is None:
            raise BleError('ISSC notify characteristic not found')
        self._write_char = write_char
        if 'write-without-response' not in getattr(write_char,'properties',['write-without-response']): raise BleError('ISSC write-without-response missing')
        if 'notify' not in getattr(notify_char,'properties',['notify']): raise BleError('ISSC notifications missing')
        await self._client.start_notify(str(notify_char.uuid), self._on_notification)
        with self._lock:
            self._connected = True

    def _on_disconnected(self, client):
        with self._lock:
            self._connected = False
            self._buffer.clear()
            cond = self._cond
        if cond is not None:
            with cond:
                cond.notify_all()

    def _on_notification(self, sender, data):
        """Bleak callback invoked on the loop thread; sync so it is never awaited."""
        with self._cond:
            if len(self._buffer) + len(data) > self.max_buffered_rx:
                self._overflow = True
                self._cond.notify_all()
                return
            self._buffer.extend(data)
            self._cond.notify_all()

    async def _write_async(self, chunk):
        await self._client.write_gatt_char(self._write_char, chunk, response=False)

    async def _disconnect_async(self, client):
        try:
            await client.disconnect()
        except Exception:
            pass  # final disconnect must not raise; already gone counts as clean


async def scan_lp2(timeout=8, scanner=None):
    """Return only LP2 candidates, including actual advertisement service data."""
    scanner=scanner or _load_bleak().BleakScanner
    discovered=await scanner.discover(timeout=timeout,return_adv=True)
    candidates=[]
    for device,adv in discovered.values():
        display=adv.local_name or device.name or ''
        services=adv.service_uuids or []
        if display.lower().startswith(LP2_NAME_PREFIXES) or str(ISSC_SERVICE) in [x.lower() for x in services]:
            candidates.append(dict(address=device.address,name=display or None))
    return sorted(candidates,key=lambda x:x['address'])

def open_ble_port(address):
    return BLEPort(address).connect()
