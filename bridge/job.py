"""Bounded, complete-job LightBurn M3 vector validation and stock ASCII encoding.

Coordinates/feed are canonical millimetres/mm per minute. DPI254 headers use
10 pixels/mm. Output forms are qualified against the offline vendor vector
oracle; physical position, speed and optical power remain unmeasured.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re

# A 1000x1000 binary image can expand to a million short LightBurn runs.
# The stock encoded job remains separately bounded to 1 MiB.
MAX_INPUT_BYTES, MAX_OUTPUT_BYTES = 16 << 20, 1 << 20
MAX_LINES, MAX_LINE_BYTES = 1_100_000, 256
DPI, PX_PER_MM = 254, 10
FEED_MIN, FEED_MAX = 120.0, 12120.0
POWER_CAP_DEFAULT = 200

class JobError(ValueError):
    pass

@dataclass
class Job:
    payload: bytes
    normalized_line_count: int
    bounds: tuple
    header_geometry: dict
    final_host_position: tuple
    max_s: int
    max_feed: float
    estimated_duration_s: float
    marking_vectors: tuple = ()
    marking_powers: tuple = ()
    speed_plan: object = None

GROUPS = {'G0':'motion', 'G1':'motion', 'G20':'units', 'G21':'units',
          'G90':'distance', 'G91':'distance', 'G17':'plane', 'G40':'comp',
          'G54':'coord', 'G94':'feed', 'M3':'spindle', 'M4':'spindle',
          'M5':'spindle', 'M9':'coolant', 'M2':'finish', 'M30':'finish'}
TOKEN = re.compile(r'([A-Z])([+-]?(?:\d+(?:\.\d*)?|\.\d+))')

def _strip_comments(line, lineno=0):
    out, inside = [], False
    for ch in line:
        if ch == ';' and not inside: break
        if ch == '(':
            if inside: raise JobError(f'line {lineno}: nested comment')
            inside = True
        elif ch == ')':
            if not inside: raise JobError(f'line {lineno}: unmatched closing comment')
            inside = False
        elif not inside: out.append(ch)
    if inside: raise JobError(f'line {lineno}: unterminated comment')
    return ''.join(out)

def words(line, lineno=0):
    body = _strip_comments(line, lineno).upper()
    result, previous = [], 0
    for match in TOKEN.finditer(body):
        if body[previous:match.start()].strip():
            raise JobError(f'line {lineno}: malformed word')
        letter, text = match.groups()
        value = float(text)
        if not math.isfinite(value): raise JobError(f'line {lineno}: nonfinite value')
        result.append((letter, value))
        previous = match.end()
    if body[previous:].strip(): raise JobError(f'line {lineno}: malformed word')
    return result

def is_finish_line(line):
    return any(letter == 'M' and value in (2,30) for letter,value in words(line))

def map_s(value, cap=POWER_CAP_DEFAULT):
    if (not isinstance(cap, int) or isinstance(cap, bool) or not 0 <= cap <= 1000
            or not math.isfinite(value) or value != int(value) or not 0 <= value <= 1000):
        raise JobError('power and cap must be integers in 0..1000')
    if value > cap: raise JobError(f'requested S{value:g} exceeds host cap {cap}')
    mapped = round(value * 253 / 1000)
    return mapped if mapped >= 3 else 0

def _number(value):
    return f'{value:.3f}'.rstrip('0').rstrip('.') or '0'

def parse_job(data, *, power_cap=POWER_CAP_DEFAULT, compact=False):
    if isinstance(data, str):
        try: data = data.encode('ascii')
        except UnicodeError as exc: raise JobError('non-ASCII input') from exc
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise JobError('job must be text or bytes')
    data = bytes(data)
    if len(data) > MAX_INPUT_BYTES: raise JobError('input exceeds 16 MiB')
    if any(byte < 32 and byte not in (9,10,13) or byte > 126 for byte in data):
        raise JobError('input contains unsupported control or non-ASCII bytes')
    lines = data.decode('ascii').splitlines()
    if len(lines) > MAX_LINES: raise JobError('too many input lines')
    map_s(0, power_cap)
    units, absolute, motion, spindle = 1.0, True, 'G0', 'M5'
    position, feed, host_s, first_feed = None, None, 0, None
    finish, moves, max_s, max_feed, duration = False, [], 0, 0.0, 0.0
    marking_vectors,marking_powers=[],set()
    for lineno, line in enumerate(lines,1):
        if len(line) > MAX_LINE_BYTES: raise JobError(f'line {lineno}: exceeds 256 bytes')
        tokens = words(line, lineno)
        if not tokens: continue
        if finish: raise JobError(f'line {lineno}: command after finish')
        groups, params = {}, {}
        for letter,value in tokens:
            if letter in ('G','M'):
                if value != int(value) or value < 0: raise JobError('unsupported fractional/negative code')
                code = letter + str(int(value))
                if code not in GROUPS: raise JobError(f'line {lineno}: unsupported {code}')
                group = GROUPS[code]
                if group in groups: raise JobError(f'line {lineno}: duplicate/conflicting {group} codes')
                groups[group] = code
            else:
                if letter not in 'XYFS': raise JobError(f'line {lineno}: unsupported word {letter}')
                if letter in params: raise JobError(f'line {lineno}: duplicate {letter}')
                params[letter] = value
        if 'finish' in groups:
            if len(groups) != 1 or params: raise JobError('finish must be a separate command')
            finish = True
            continue
        units = {'G20':25.4, 'G21':1.0}.get(groups.get('units'), units)
        absolute = {'G90':True, 'G91':False}.get(groups.get('distance'), absolute)
        motion = groups.get('motion', motion)
        spindle = groups.get('spindle', spindle)
        if 'F' in params:
            feed = params['F'] * units
            if not FEED_MIN <= feed <= FEED_MAX: raise JobError('feed outside 120..12120 mm/min')
            if first_feed is None: first_feed = feed
        if 'S' in params:
            host_s = params['S']
            map_s(host_s, power_cap)
        if not ('X' in params or 'Y' in params): continue
        if position is None and (not absolute or 'X' not in params or 'Y' not in params):
            raise JobError('first move must establish both absolute X and Y')
        endpoint = []
        for index, axis in enumerate('XY'):
            value = params.get(axis)
            if absolute:
                value = value*units if value is not None else position[index]
            else:
                value = position[index] + (value*units if value is not None else 0)
            if not -1e-9 <= value <= 100+1e-9: raise JobError('move outside 0..100 mm')
            endpoint.append(round(min(100.0,max(0.0,value)),3))
        endpoint = tuple(endpoint)
        if spindle == 'M4' and host_s > 0: raise JobError('M4 marking is unsupported; use constant-power M3')
        if motion == 'G1' and feed is None: raise JobError('G1 requires an established feed')
        mapped = map_s(host_s, power_cap) if spindle == 'M3' and motion == 'G1' else 0
        if position != endpoint:
            if mapped:
                marking_vectors.append((position,endpoint,feed))
                marking_powers.add(host_s)
            if position is not None:
                speed = feed if motion == 'G1' else FEED_MAX
                duration += math.dist(position, endpoint) / speed * 60
            moves.append((motion, endpoint, feed, mapped))
            max_s = max(max_s,mapped)
            if motion == 'G1': max_feed = max(max_feed,feed)
        position = endpoint
    if not finish: raise JobError('complete job requires M2 or M30')
    if not moves: raise JobError('job contains no coordinate movement')
    output, output_size = [], 0
    def emit(line):
        nonlocal output_size
        output_size += len(line)+1
        if output_size > MAX_OUTPUT_BYTES: raise JobError('normalized job exceeds 1 MiB')
        output.append(line)
    for line in ('G21','G90','M8 F'+_number(first_feed or FEED_MIN),'M5','S0'): emit(line)
    modal_power=0;encoded_feed=first_feed or FEED_MIN
    for motion, endpoint, move_feed, mapped in moves:
        if compact:
            if mapped!=modal_power:emit(('M3' if mapped else 'M5')+' S'+str(mapped))
        else:
            emit('M3' if mapped else 'M5'); emit('S'+str(mapped))
        modal_power=mapped
        line = f'{motion} X{endpoint[0]:.3f} Y{endpoint[1]:.3f}'
        if motion == 'G1' and (not compact or move_feed!=encoded_feed):
            line += ' F'+_number(move_feed);encoded_feed=move_feed
        emit(line)
    emit('M5'); emit('S0')
    payload = ('\n'.join(output)+'\n').encode('ascii')
    points = [move[1] for move in moves]
    left, top = (math.floor(min(point[axis] for point in points)*PX_PER_MM) for axis in (0,1))
    right, bottom = (math.ceil(max(point[axis] for point in points)*PX_PER_MM) for axis in (0,1))
    count = len(output)+1
    geometry = dict(width_px=max(1,right-left),height_px=max(1,bottom-top),
                    left_px=left,top_px=top,nx_px=left,ny_px=top,dpi=DPI,line_count=count)
    return Job(payload,count,(left,top,right,bottom),geometry,position,max_s,max_feed,duration,
               tuple(marking_vectors),tuple(sorted(marking_powers)))
