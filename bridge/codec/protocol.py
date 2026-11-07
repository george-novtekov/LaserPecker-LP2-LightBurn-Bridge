"""LP2 stock-bridge wire codec — pure Python stdlib, no IO, no imports from the sealed prototype.

Framing (pyrograph-protocol.md §2, verified against LDS 2.12.0 vendor source
evidence/usb-route/vendor/index.43a989ee.js, sha256
9fd68035bdba8fd81955b305bc2440ad99c20a5fb8b396b82c4b9c1bc77df2c7):

  short frame : AA BB <len> <func> <data n bytes> <ck_hi> <ck_lo>,  len = n + 3
  long frame  : AA CC <len32 BE> <payload incl. trailing 2 ck bytes>
                checksum = sum over payload bytes excluding the last 2, BE.
  checksum    : 16-bit additive sum over func + data (short) / payload (long),
                big-endian. Header and len are NOT covered.

This module implements only the bounded codec slice approved for this step:
frame encode/decode (stream, fragments, back-to-back, noise, bad checksum,
bounded buffers), the actual 14 B version / 27 B status / 25 B settings /
133 B file-list parsers, the 64-byte G-code header encoder (vendor
genGcodeDataCommand byte-for-byte, oracle-verified), the 0x05 announce frame,
and the stock query/pause/resume/stop builders as bytes. No physical IO, no
host S/F conversion claim, no job IO.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Iterator, List, Optional

SHORT_HEADER = b"\xAA\xBB"
LONG_HEADER = b"\xAA\xCC"
MAX_SHORT_LEN = 250          # approved bound: len < 0xFB  => data n <= 247
MAX_LONG_PAYLOAD = 1 << 20   # 1 MiB bound for AA CC frames

FUNC_QUERY = 0x00
FUNC_PRINT = 0x01
FUNC_FILE = 0x05
FUNC_STOP = 0xFF

GCODE_HEADER_TAG = 0x20
GCODE_NAME_MAX_BYTES = 29   # approved bound (vendor array tolerates exactly 30)


class CodecError(ValueError):
    pass


# --------------------------------------------------------------------------
# encoding
# --------------------------------------------------------------------------

def _u16(v: int, name: str, lo: int = 0, hi: int = 0xFFFF) -> bytes:
    if not isinstance(v, int) or isinstance(v, bool) or not (lo <= v <= hi):
        raise CodecError(f"{name}={v!r} out of bounds [{lo}..{hi}]")
    return v.to_bytes(2, "big")


def _u32(v: int, name: str, lo: int = 0, hi: int = 0xFFFFFFFF) -> bytes:
    if not isinstance(v, int) or isinstance(v, bool) or not (lo <= v <= hi):
        raise CodecError(f"{name}={v!r} out of bounds [{lo}..{hi}]")
    return v.to_bytes(4, "big")


def _u8(v: int, name: str, lo: int = 0, hi: int = 0xFF) -> bytes:
    if not isinstance(v, int) or isinstance(v, bool) or not (lo <= v <= hi):
        raise CodecError(f"{name}={v!r} out of bounds [{lo}..{hi}]")
    return bytes([v])


def checksum(data: bytes) -> int:
    return sum(data) & 0xFFFF


def _bytes(value, name: str) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise CodecError(f"{name} must be bytes")
    return bytes(value)


def _name(value: str) -> bytes:
    if not isinstance(value, str) or '\x00' in value:
        raise CodecError("name must be text without NUL")
    try:
        result = value.encode('utf-8')
    except UnicodeError as exc:
        raise CodecError("name is not valid UTF-8") from exc
    if len(result) > GCODE_NAME_MAX_BYTES:
        raise CodecError("UTF-8 name exceeds 29 bytes")
    return result


def encode_frame(func: int, payload: bytes) -> bytes:
    """Short AA BB frame. All bounds validated before encoding; no truncation."""
    _u8(func, "func")
    payload = _bytes(payload, 'payload')
    n = len(payload)
    if n > MAX_SHORT_LEN - 3:
        raise CodecError(f"payload {n} B exceeds short-frame bound (len would be {n + 3})")
    body = bytes([func]) + payload          # checksum covers func + data
    return SHORT_HEADER + bytes([n + 3]) + body + checksum(body).to_bytes(2, "big")


def encode_long_frame(payload: bytes) -> bytes:
    """AA CC long frame; len32 counts payload INCLUDING its trailing 2 ck bytes."""
    payload = _bytes(payload, 'payload')
    n = len(payload)
    if n < 3 or n > MAX_LONG_PAYLOAD:
        raise CodecError(f"long payload {n} B outside [3..{MAX_LONG_PAYLOAD}]")
    body = payload[:-2]
    return LONG_HEADER + n.to_bytes(4, "big") + body + checksum(body).to_bytes(2, "big")


# --- command builders (bytes only; no physical execution here) -------------

def status_command(state: int) -> bytes:
    """Vendor statusCommand: func 0, data [state,0,0,0,0]. Covers queries
    state 0 (status), 1 (file IDs), 2 (settings), 3 (version)."""
    _u8(state, 'state')
    if state not in (0, 1, 2, 3):
        raise CodecError('only reviewed query states 0..3 are implemented')
    return encode_frame(FUNC_QUERY, bytes([state, 0, 0, 0, 0]))


def query_status() -> bytes:
    return status_command(0)


def query_file_ids() -> bytes:
    return status_command(1)


def query_settings() -> bytes:
    return status_command(2)


def query_version() -> bytes:
    return status_command(3)


def stop_command() -> bytes:
    """Vendor stopCommand (func 0xFF, exit)."""
    return encode_frame(FUNC_STOP, bytes(5))


def print_command(*, state: int, laser: int = 0, depth: int = 0, file_id: int = 0,
                  nx: int = 0, ny: int = 0, custom: int = 0, times: int = 0,
                  ltype: int = 0, obj_d: int = 0, precision: int = 0,
                  fan_speed: int = 0, frequency: int = 0, speed: int = 0,
                  mount: int = 1) -> bytes:
    """Vendor printCommand, 23-byte data per protocol doc §3.2 (depth field is
    already the wire value, i.e. 101 - UI depth; caller passes the wire value)."""
    _u8(state, 'state', 1, 4)
    data = (_u8(state, 'state') + _u8(laser, "laser", 0, 100) + _u8(depth, "depth", 0, 101)
            + _u32(file_id, "file_id") + _u16(nx, "nx") + _u16(ny, "ny")
            + _u8(custom, "custom") + _u8(times, "times") + _u8(ltype, "ltype")
            + _u16(obj_d, "obj_d") + _u8(precision, "precision")
            + _u8(fan_speed, "fan_speed") + _u8(frequency, "frequency")
            + _u16(speed, "speed") + _u8(mount, "mount"))
    return encode_frame(FUNC_PRINT, data)


def print_pause() -> bytes:
    """Vendor togglePrint: UI speed=0 becomes depth=101; mount defaults to 1."""
    return print_command(state=4, depth=101)


def print_resume() -> bytes:
    return print_command(state=2, depth=101)


def print_stop() -> bytes:
    """Print-stop (state 3) in the zeroed form; the vendor stop path passes real
    parameters, so prefer print_command(state=3, ...) when parameters are known."""
    return print_command(state=3, depth=101)


def announce_gcode(payload_len: int) -> bytes:
    """func 0x05 announce: data [1, (64+payload_len) BE32, 0]."""
    if not isinstance(payload_len, int) or isinstance(payload_len, bool) or payload_len < 0:
        raise CodecError(f"payload_len={payload_len!r} invalid")
    total = 64 + payload_len
    if total > 0xFFFFFFFF:
        raise CodecError("64+payload_len overflows uint32")
    return encode_frame(FUNC_FILE, bytes([1]) + total.to_bytes(4, "big") + bytes([0]))


# --------------------------------------------------------------------------
# 64-byte G-code header (vendor genGcodeDataCommand, oracle-verified)
# --------------------------------------------------------------------------

def gen_gcode_header(*, file_id: int, width: int, height: int, nx: int, ny: int,
                     dpi: int, line_count: int, name: str) -> bytes:
    """Exactly 64 bytes. Layout (vendor source, char 3997744 of index.43a989ee.js):
    [0]=0x20, w BE16@1, h BE16@3, fileId BE32@5, gcodeNums BE32@9, nx BE16@13,
    ny BE16@15, dpi BE16@17, zeros 19..33, UTF-8 name at 34 (<= 29 B, zero padded).
    All bounds validated before encoding; nothing truncates or overflows."""
    h = bytearray(64)
    name_b = _name(name)
    fields = [
        (0, 1, bytes([GCODE_HEADER_TAG])),
        (1, 2, _u16(width, "width", 1, 0xFFFF)),
        (3, 2, _u16(height, "height", 1, 0xFFFF)),
        (5, 4, _u32(file_id, "file_id", 1, 0xFFFFFFFE)),
        (9, 4, _u32(line_count, "line_count")),
        (13, 2, _u16(nx, "nx")),
        (15, 2, _u16(ny, "ny")),
        (17, 2, _u16(dpi, "dpi", 1, 0xFFFF)),
        (34, len(name_b), name_b),
    ]
    for off, size, raw in fields:
        h[off:off + size] = raw
    return bytes(h)


def gcode_content_digest(*, width: int, height: int, nx: int, ny: int, dpi: int,
                         line_count: int, name: str, payload: bytes) -> bytes:
    """Canonical SHA-256 over payload + geometry content. Canonical input:
    payload bytes, then BE fields w,h (u16), nx,ny (u16), dpi (u16),
    line_count (u32), name UTF-8. Bounds validated as in gen_gcode_header."""
    name_b = _name(name)
    canon = (_bytes(payload, 'payload')
             + _u16(width, "width", 1, 0xFFFF) + _u16(height, "height", 1, 0xFFFF)
             + _u16(nx, "nx") + _u16(ny, "ny") + _u16(dpi, "dpi", 1, 0xFFFF)
             + _u32(line_count, "line_count") + name_b)
    return hashlib.sha256(canon).digest()


def select_file_id(content_digest: bytes, occupied: List[int]) -> int:
    """Derive a 32-bit file ID from the content digest, avoiding every occupied
    ID plus 0 and 0xFFFFFFFF (never valid: 0 = none, FFFFFFFF = reported
    sentinel). Selection: first 4 digest bytes BE; on collision re-hash
    (SHA-256 of the previous digest) and retry. Deterministic, with at most
    1000 attempts; exhausting that bound raises CodecError."""
    content_digest = _bytes(content_digest, 'content_digest')
    if len(content_digest) != 32:
        raise CodecError("content_digest must be a 32-byte SHA-256 digest")
    for value in occupied:
        _u32(value, 'occupied ID')
    excluded = set(occupied) | {0, 0xFFFFFFFF}
    d = content_digest
    for _ in range(1000):
        cand = int.from_bytes(d[:4], "big")
        if cand not in excluded:
            return cand
        d = hashlib.sha256(d).digest()
    raise CodecError("no free file ID within 1000 hash steps")


# --------------------------------------------------------------------------
# stream decoder
# --------------------------------------------------------------------------

@dataclass
class DecodedFrame:
    kind: str                 # "short" | "long"
    func: Optional[int]       # short frames only
    payload: bytes            # data bytes, checksum excluded
    checksum_ok: bool
    raw: bytes


@dataclass
class DecoderStats:
    noise_bytes: int = 0
    rejected_checksum: int = 0
    oversize: int = 0


class StreamDecoder:
    """Accumulates a byte stream, emits complete frames. Handles fragments,
    back-to-back concatenation, leading/inter-frame noise (dropped, counted),
    and bad-checksum frames (reported with checksum_ok=False, then resync).
    Bounded: short frames <= MAX_SHORT_LEN len field; long frames <= 1 MiB."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self.stats = DecoderStats()

    def feed(self, data: bytes) -> List[DecodedFrame]:
        data = _bytes(data, 'stream input')
        frames = []
        # Process large caller inputs in bounded chunks. A partial long frame
        # retains at most its declared bound plus one input chunk.
        for offset in range(0, len(data), 4096):
            frames.extend(self._feed(data[offset:offset + 4096]))
        return frames

    def _feed(self, data: bytes) -> Iterator[DecodedFrame]:
        self._buf.extend(data)
        while True:
            # sync: drop noise until AA BB or AA CC
            if len(self._buf) >= 2 and bytes(self._buf[:2]) not in (SHORT_HEADER, LONG_HEADER):
                candidates = [i for i in (self._buf.find(SHORT_HEADER), self._buf.find(LONG_HEADER)) if i >= 0]
                skip = min(candidates) if candidates else len(self._buf) - (self._buf[-1] == 0xAA)
                del self._buf[:skip]
                self.stats.noise_bytes += skip
            if len(self._buf) < 2:
                if self._buf and self._buf[0] != 0xAA:
                    del self._buf[0]
                    self.stats.noise_bytes += 1
                return
            if bytes(self._buf[:2]) == SHORT_HEADER:
                if len(self._buf) < 3:
                    return
                ln = self._buf[2]
                if ln < 3 or ln > MAX_SHORT_LEN:
                    self.stats.oversize += 1
                    self._buf.clear()
                    raise CodecError(f"short frame len {ln} outside [3..{MAX_SHORT_LEN}]; decoder cleared")
                total = ln + 3
                if len(self._buf) < total:
                    return
                frame = bytes(self._buf[:total])
                del self._buf[:total]
                body = frame[3:3 + ln - 2]          # func + data (ck excluded)
                ck = checksum(body)
                ok = ck == int.from_bytes(frame[-2:], "big")
                if not ok:
                    self.stats.rejected_checksum += 1
                yield DecodedFrame("short", body[0], body[1:], ok, frame)
            else:
                if len(self._buf) < 6:
                    return
                ln = int.from_bytes(self._buf[2:6], "big")
                if ln < 2 or ln > MAX_LONG_PAYLOAD:
                    self.stats.oversize += 1
                    self._buf.clear()
                    raise CodecError(f"long frame len {ln} outside [2..{MAX_LONG_PAYLOAD}]; decoder cleared")
                total = 6 + ln
                if len(self._buf) < total:
                    return
                frame = bytes(self._buf[:total])
                del self._buf[:total]
                body = frame[6:6 + ln - 2]
                ok = checksum(body) == int.from_bytes(frame[-2:], "big")
                if not ok:
                    self.stats.rejected_checksum += 1
                yield DecodedFrame("long", None, body, ok, frame)


# --------------------------------------------------------------------------
# reply parsers (offsets are absolute frame offsets, byte 0 = 0xAA)
# --------------------------------------------------------------------------

def _query_raw(frame: DecodedFrame, min_len: int) -> bytes:
    raw = frame.raw
    if (not frame.checksum_ok or frame.kind != 'short' or frame.func != 0
            or len(raw) < min_len or raw[:2] != SHORT_HEADER
            or not 3 <= raw[2] <= MAX_SHORT_LEN or len(raw) != raw[2] + 3
            or raw[3] != 0 or checksum(raw[3:-2]) != int.from_bytes(raw[-2:], 'big')
            or frame.payload != raw[4:-2]):
        raise CodecError('not a valid func00 query reply of the required length')
    return raw


def _require_short(frame: DecodedFrame, expect_len: int) -> bytes:
    raw = _query_raw(frame, expect_len)
    if len(raw) != expect_len:
        raise CodecError(f"expected {expect_len}-byte frame, got {len(frame.raw)}")
    return raw


def parse_version(frame: DecodedFrame) -> dict:
    """Actual 14-byte frame: SW = (buf[4]<<8)+buf[5]; HW = hex of bytes 6..9
    (opaque); tail bytes 10..11 (observed 0003)."""
    raw = _require_short(frame, 14)
    return {
        "sw_field": (raw[4] << 8) + raw[5],
        "hw_hex": raw[6:10].hex(),
        "tail_hex": raw[10:12].hex(),
    }


# §4.1 field map: name -> (absolute offset, size). Optional fields are parsed
# only when they lie entirely BEFORE the CRC (the "optional fields only before
# CRC" rule): an optional field overlapping the CRC region is left None.
_STATUS_FIELDS = [
    ("mode", 4, 1), ("w_state", 5, 1), ("rate", 6, 1), ("laser", 7, 1),
    ("speed", 8, 1), ("error", 9, 1), ("name_id", 11, 4), ("temp", 15, 1),
    ("z_connect", 17, 1), ("print_times", 18, 1), ("angle", 19, 1),
    ("m_state", 20, 1), ("r_conn", 21, 1), ("s_conn", 22, 1),
    ("car_conn", 23, 1), ("u_b_cnn", 24, 1),
]
_STATUS_OPTIONAL = [
    ("stop", 25, 1), ("safe_key", 26, 1), ("cover_bitmap", 27, 2),
    ("cover_conn", 29, 1), ("standard", 30, 1), ("s_cover_conn", 31, 1),
]


def parse_status(frame: DecodedFrame) -> dict:
    """Actual 27-byte frame: core fields through u_b_cnn; stop/safe_key would
    overlap the CRC at 25..26 and are therefore NOT consumed."""
    raw = _require_short(frame, 27)
    out = {}
    for name, off, size in _STATUS_FIELDS + _STATUS_OPTIONAL:
        out[name] = None
    for name, off, size in _STATUS_FIELDS:
        out[name] = int.from_bytes(raw[off:off + size], "big")
    return out


def parse_status_long(frame: DecodedFrame) -> dict:
    """General status frame (>= 27 B): optional fields parsed only if fully
    before the CRC region (last 2 bytes)."""
    raw = _query_raw(frame, 27)
    crc_start = len(raw) - 2
    out = {}
    for name, off, size in _STATUS_FIELDS:
        out[name] = int.from_bytes(raw[off:off + size], "big")
    for name, off, size in _STATUS_OPTIONAL:
        out[name] = (int.from_bytes(raw[off:off + size], "big")
                     if off + size <= crc_start else None)
    return out


# §4.2 settings field map (absolute offsets). Dash entries are unnamed gaps.
_SETTINGS_FIELDS = [
    ("free", 4), ("buzzer", 5), ("view", 6), ("g_view", 7), ("safe", 8),
    ("z_flag", 10), ("z_dir", 11), ("key_view", 12), ("ir_dst", 13),
    ("key_print", 14), ("r_flag", 15), ("s_flag", 16), ("dir", 17),
    ("g_pwr", 18), ("s_rep", 19), ("car_flag", 20), ("print_dir", 21),
]
_SETTINGS_OPTIONAL = [
    ("overscan_flag", 23), ("led_level", 24), ("fan_level", 25),
    ("fan_time", 26), ("recover_state", 28),
]


def parse_settings(frame: DecodedFrame) -> dict:
    """Actual 25-byte frame: fields at offsets 4..22 (the trailing 0x02 at
    offset 22 is a data byte covered by the checksum). Optional fields at
    23+ would consume the CRC bytes and are skipped as fields."""
    raw = _require_short(frame, 25)
    out = {}
    for name, off in _SETTINGS_FIELDS:
        out[name] = raw[off]
    for name, off in _SETTINGS_OPTIONAL:
        out[name] = None      # would overlap CRC on a 25-byte frame
    return out


def parse_settings_long(frame: DecodedFrame) -> dict:
    raw = _query_raw(frame, 25)
    crc_start = len(raw) - 2
    out = {}
    for name, off in _SETTINGS_FIELDS:
        out[name] = raw[off]
    for name, off in _SETTINGS_OPTIONAL:
        size = 2 if name == 'fan_time' else 1
        out[name] = int.from_bytes(raw[off:off+size], 'big') if off+size <= crc_start else None
    return out


@dataclass
class FileIdEntry:
    word_hex: str
    value: int
    duplicate_of_first_index: Optional[int] = None
    is_sentinel_ffffffff: bool = False
    vendor_phantom: bool = False


@dataclass
class FileListResult:
    declared_field: int               # byte 4 of the frame (observed 30) — NOT a
                                      # physical file count; do not truncate to it
    words: List[FileIdEntry]          # all complete BE words, offsets 5..128
    tail_hex: str                     # opaque 2 bytes before the CRC (observed 0001)
    duplicates: List[int] = field(default_factory=list)
    sentinels: List[int] = field(default_factory=list)
    vendor_view: List[FileIdEntry] = field(default_factory=list)
    exclude_for_new_job: List[int] = field(default_factory=list)


def parse_file_list(frame: DecodedFrame) -> FileListResult:
    """Actual 133-byte frame. After offset 5 there are 31 complete 4-byte BE
    words and a 2-byte opaque tail before the CRC. The vendor getDeviceFiles
    loop (char 4037704) removes the last 3 bytes, then loops from offset 4
    with stride 4 starting at 1 — its final 'word' reads only one real byte
    (the tail's first byte) and fabricates a phantom value from it (a phantom
    zero for 00-prefixed padding). We return the honest structure and keep the
    vendor view for fidelity. Duplicates and FFFFFFFF are retained and flagged.
    exclude_for_new_job covers ALL candidate words plus 0 and FFFFFFFF."""
    raw = _query_raw(frame, 7)
    declared = raw[4]
    body = raw[5:-2]
    words = [body[i:i+4] for i in range(0, len(body)-3, 4)]
    tail = body[len(words)*4:]
    first: dict = {}
    entries: List[FileIdEntry] = []
    dupes: List[int] = []
    sentinels: List[int] = []
    for i, w in enumerate(words):
        v = int.from_bytes(w, "big")
        e = FileIdEntry(w.hex(), v)
        if v in first:
            e.duplicate_of_first_index = first[v]
            dupes.append(i)
        else:
            first[v] = i
        if v == 0xFFFFFFFF:
            e.is_sentinel_ffffffff = True
            sentinels.append(i)
        entries.append(e)
    # vendor view: data = frame minus last 3 bytes, from offset 4, stride 4, start 1
    vdata = raw[:-3][4:]
    vview: List[FileIdEntry] = []
    o = 1
    while o < len(vdata):
        chunk = vdata[o:o + 4]
        v = 0
        for shift, b in zip((24, 16, 8, 0), chunk):
            v |= b << shift
        # JS: undefined bytes contribute 0 (undefined<<n === 0)
        vview.append(FileIdEntry("%08x" % v, v, vendor_phantom=len(chunk) < 4))
        o += 4
    return FileListResult(
        declared_field=declared, words=entries, tail_hex=tail.hex(),
        duplicates=dupes, sentinels=sentinels, vendor_view=vview,
        exclude_for_new_job=sorted({e.value for e in entries} | {0, 0xFFFFFFFF}),
    )
