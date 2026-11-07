"""Compile LightBurn scan spacing onto the stock LP2's integer galvo grid."""
from dataclasses import dataclass, replace
from bisect import bisect_left
import hashlib
import math
import re
from . import job as j

class RasterError(j.JobError): pass
class RasterGridError(RasterError): pass

@dataclass
class RasterJob:
    payload: bytes
    width_px: int
    height_px: int
    nx_px: int
    ny_px: int
    requested_s: int
    requested_feed: float
    final_host_position: tuple
    black_pixels: int
    source_lines: int
    snap_mm: tuple
    notes: tuple
    finish_points: tuple = ()
    dpi: int = 254
    px: int = 4
    speed_plan: object = None
    finish_job: object = None
    source_pitch_mm: float = .1
    wire_compensated: bool = False

    @property
    def effective_dpi(self):return 1016/self.px

    @property
    def pitch_mm(self):return self.px/40

def is_raster_candidate(data):
    if not isinstance(data,(bytes,bytearray,memoryview)):return False
    return (re.search(rb'G0*91(?![0-9.])',data) is not None
            and re.search(rb'M0*[34](?![0-9.])',data) is not None
            and re.search(rb'Y[+]?0?\.[1-9]',data) is not None)

def _um(value):
    scaled=value*1000
    if abs(scaled-round(scaled))>1e-6:raise RasterError('raster coordinates require at most 0.001 mm precision')
    return round(scaled)

def _nm(value):
    # Keep emitted rounding intact; the device grid is coarser (25 microns).
    scaled=value*1_000_000
    if abs(scaled-round(scaled))>1e-5:raise RasterError('scan coordinates require at most six decimal places')
    return round(scaled)

def _pack_coverage(coverage_rows,width,height):
    """Area coverage -> binary dots, without locked duplicate-column bands.

    Serpentine Floyd-Steinberg distributes fractional coverage over neighbouring
    dots. Exact black/white grids retain their original bits and white padding.
    Keep only two error rows, even for large images.
    """
    stride=(width+7)//8;payload=bytearray(b'\xff'*(stride*height));black=0
    errors=[0.]*(width+2)
    for y,coverage in enumerate(coverage_rows):
        following=[0.]*(width+2);direction=1 if y%2==0 else -1
        for x in range(width) if direction==1 else range(width-1,-1,-1):
            i=x+1;value=min(1.,max(0.,coverage[x]))+errors[i]
            dot=1 if value>=.5 else 0;error=value-dot
            if dot:payload[y*stride+x//8]&=~(0x80>>(x%8));black+=1
            errors[i+direction]+=error*7/16
            following[i-direction]+=error*3/16
            following[i]+=error*5/16
            following[i+direction]+=error/16
        errors=following
    return bytes(payload),black

def _area_rows(rows,left,bottom,pitch,source_pitch,width,height):
    """Integrate each emitted horizontal burn rectangle over native cells."""
    ys=sorted(rows)
    for y in range(height):
        y0=bottom+y*pitch;y1=y0+pitch;coverage=[0.]*width
        a=bisect_left(ys,y0-source_pitch)
        b=bisect_left(ys,y1)
        for sy in ys[a:b]:
            overlap_y=max(0,min(y1,sy+source_pitch)-max(y0,sy))
            if not overlap_y:continue
            for lo,hi in rows[sy]:
                start=max(0,math.floor((lo-left)/pitch))
                stop=min(width,math.ceil((hi-left)/pitch))
                for x in range(start,stop):
                    x0=left+x*pitch
                    overlap_x=max(0,min(x0+pitch,hi)-max(x0,lo))
                    coverage[x]+=overlap_x*overlap_y/(pitch*pitch)
        yield coverage

def parse_raster(data,*,power_cap=1000):
    if isinstance(data,str):
        try:data=data.encode('ascii')
        except UnicodeError as exc:raise RasterError('non-ASCII raster input') from exc
    if not isinstance(data,(bytes,bytearray,memoryview)):raise RasterError('raster input must be bytes')
    data=bytes(data)
    if len(data)>j.MAX_INPUT_BYTES:raise RasterError('raster input exceeds 16 MiB')
    if any(b<32 and b not in (9,10,13) or b>126 for b in data):raise RasterError('unsupported raster input byte')
    lines=data.decode('ascii').splitlines()
    if len(lines)>j.MAX_LINES:raise RasterError('too many raster input lines')
    j.map_s(0,power_cap)
    groups=dict(j.GROUPS,M8='coolant')
    allowed=set(groups)-{'G20'}
    unit=False;absolute=True;motion='G0';spindle='M5';s=0;feed=None
    position=None;anchor=None;relative_seen=False;return_phase=False;finished=False
    rows={};scanned_rows=set();black_power=None;finish_points=[];air=False
    legacy_grid=True;row_steps=[];row_direction=0
    for number,line in enumerate(lines,1):
        if len(line)>j.MAX_LINE_BYTES:raise RasterError(f'line {number}: exceeds 256 bytes')
        tokens=j.words(line,number)
        if not tokens:continue
        if finished:raise RasterError('command after raster finish')
        g,p={},{}
        for letter,value in tokens:
            if letter in ('G','M'):
                if value!=int(value):raise RasterError('fractional raster command')
                code=letter+str(int(value))
                if code not in allowed:raise RasterError(f'unsupported raster {code}')
                group=groups[code]
                if group in g:raise RasterError('conflicting raster modal group')
                g[group]=code
            else:
                if letter not in 'XYFS' or letter in p:raise RasterError('unsupported or duplicate raster word')
                p[letter]=value
        if 'finish' in g:
            if len(g)!=1 or p:raise RasterError('raster finish must be standalone M2/M30')
            finished=True;continue
        if g.get('units')=='G21':unit=True
        if 'distance' in g:
            absolute=g['distance']=='G90'
            if not absolute:
                if return_phase:raise RasterError('raster phase after finish positioning')
                if position is None:raise RasterError('raster relative mode requires an absolute G0 origin')
                relative_seen=True
        motion=g.get('motion',motion);spindle=g.get('spindle',spindle)
        if 'coolant' in g:air=True
        if 'S' in p:
            j.map_s(p['S'],power_cap)
            s=int(p['S'])
        if 'F' in p:
            if not j.FEED_MIN<=p['F']<=j.FEED_MAX:raise RasterError('raster feed outside 120..12120 mm/min')
            if scanned_rows and feed is not None and not math.isclose(feed,p['F'],abs_tol=1e-6,rel_tol=0):
                raise RasterError('mixed raster feeds are unsupported')
            feed=p['F']
        if not ('X' in p or 'Y' in p):continue
        if not unit:raise RasterError('raster requires G21')
        coords={axis:_nm(p[axis]) for axis in 'XY' if axis in p}
        if position is None:
            if not absolute or motion!='G0' or set(coords)!={'X','Y'}:
                raise RasterError('raster requires both absolute X/Y in initial G0')
            end=(coords['X'],coords['Y'])
        else:
            end=tuple(coords.get(axis,position[i]) if absolute else position[i]+coords.get(axis,0)
                      for i,axis in enumerate('XY'))
        if any(not 0<=v<=100_000_000 for v in end):raise RasterError('raster motion outside 0..100 mm')
        burning=motion=='G1' and spindle in ('M3','M4') and j.map_s(s,power_cap)>0
        if position is None:
            position=anchor=end;continue
        if absolute:
            if not relative_seen:raise RasterError('intermediate absolute raster positioning unsupported')
            if burning:raise RasterError('marking after raster return phase')
            return_phase=True;finish_points.append(tuple(v/1_000_000 for v in end));position=end;continue
        if motion!='G1' or return_phase:raise RasterError('unsupported raster repositioning')
        if feed is None:raise RasterError('raster G1 requires feed')
        dx,dy=end[0]-position[0],end[1]-position[1]
        legacy_grid=legacy_grid and dx%100_000==0 and dy%100_000==0
        if dy:
            direction=1 if dy>0 else -1
            if row_direction and direction!=row_direction:raise RasterError('scan rows must progress in one Y direction')
            row_direction=direction;row_steps.append(abs(dy))
        if dy and burning:raise RasterError('powered raster row turn is unsupported')
        scanned_rows.update((position[1],end[1]))
        if burning:
            if dx==0:raise RasterError('zero-length raster marking')
            if black_power is not None and s!=black_power:raise RasterError('varying nonzero raster powers unsupported')
            black_power=s
            lo,hi=sorted((position[0],end[0]))
            rows.setdefault(position[1],[]).append((lo,hi))
        position=end
    if not finished:raise RasterError('complete raster job requires M2/M30')
    if not relative_seen or len(scanned_rows)<2:raise RasterError('raster requires multiple scan rows')
    if not rows:raise RasterError('raster contains no marked pixels')
    left=min(lo for runs in rows.values() for lo,hi in runs)
    right=max(hi for runs in rows.values() for lo,hi in runs)
    bottom,top=min(rows),max(rows)
    for runs in rows.values():
        previous=-1
        for lo,hi in sorted(runs):
            if lo<previous:raise RasterError('overlapping raster marks would lose pass count')
            previous=hi
    # A single irregular span per row is also how LightBurn encodes curved
    # hatch fills. Keep those exact contours on the vector path.
    if not legacy_grid and all(len(runs)==1 for runs in rows.values()):
        raise RasterGridError('non-pixel single-span scan retained as vector fill')
    smallest=min(row_steps)
    cluster=[v for v in row_steps if v<=smallest*1.02+2]
    source_pitch=sum(cluster)/len(cluster)
    if legacy_grid and any(abs((v-left)/source_pitch-round((v-left)/source_pitch))>1e-7
                           for runs in rows.values() for run in runs for v in run):
        # Retain omitted white rows on older exact 0.1-mm fixtures.
        source_pitch=100_000
    # The firmware multiplies native coordinates by header px in 1/40 mm
    # units. Select a grid no coarser than the source, down to px=1.
    # Refine further when necessary to keep an individual pulse <=2 ms.
    max_dwell_pitch=math.sqrt(.002*(feed/60)*(source_pitch/1_000_000))*40
    px=max(1,min(255,math.floor(source_pitch/25_000+1e-9),math.floor(max_dwell_pitch+1e-9)))
    pitch=px*25_000;limit=4000//px
    width=math.ceil((right-left)/pitch);height=math.ceil((top-bottom+source_pitch)/pitch-1e-9)
    nx,ny=math.floor(left/pitch+.5),math.floor(bottom/pitch+.5)
    if not 1<=width<=limit or not 1<=height<=limit or nx+width>limit or ny+height>limit:
        raise RasterError('native raster geometry outside 100 x 100 mm')
    stride=(width+7)//8
    if stride*height>j.MAX_OUTPUT_BYTES:raise RasterError('resampled bitmap exceeds 1 MiB stock job limit; reduce image size')
    payload,black=_pack_coverage(_area_rows(rows,left,bottom,pitch,source_pitch,width,height),width,height)
    if not black:raise RasterError('image has no marked pixels at the 1016-DPI device grid limit')
    final=tuple(v/1_000_000 for v in position)
    if not finish_points:finish_points.append(final)
    notes=('M4 is represented as native binary pixels; dynamic-power semantics are not emulated',)
    if air:notes+=('M8/M9 have no air-assist device action on this native bitmap path',)
    notes+=(f'source spacing {source_pitch/1_000_000:.6f} mm (~{25_400_000/source_pitch:.3f} DPI) area-resampled to {1016/px:.3f} native DPI with serpentine error diffusion',)
    return RasterJob(bytes(payload),width,height,nx,ny,black_power,feed,final,black,len(lines),
                     (round((nx*pitch-left)/1_000_000,6),round((ny*pitch-bottom)/1_000_000,6)),
                     notes,tuple(finish_points),dpi=round(1016/px),px=px,source_pitch_mm=source_pitch/1_000_000)

def prepare_wire(job,*,job_budget=600):
    """Correct the stock packed scanner's pre-increment/decrement row phase.

    Call AFTER calibration and rotation. Forward bit x burns at nx+x+1;
    reverse bit x burns at nx+x. One white column permits shifting reverse
    row data right without losing pixels. Move the origin left where possible.
    """
    if job.wire_compensated:return job
    w,h=job.width_px,job.height_px;stride=(w+7)//8
    nx=max(0,job.nx_px-1);width=w+1;newstride=(width+7)//8
    if nx+width>4000//job.px:
        raise RasterError('bidirectional image correction needs one native pixel of horizontal margin; slightly reduce image width')
    if newstride*h>j.MAX_OUTPUT_BYTES:raise RasterError('corrected bitmap exceeds 1 MiB stock job limit')
    payload=bytearray(b'\xff'*(newstride*h))
    for y in range(h):
        shift=y%2
        for x in range(w):
            if not job.payload[y*stride+x//8]&(0x80>>(x%8)):
                xx=x+shift;payload[y*newstride+xx//8]&=~(0x80>>(xx%8))
    result=replace(job,payload=bytes(payload),width_px=width,nx_px=nx,wire_compensated=True,
                   snap_mm=(round(job.snap_mm[0]+(nx-job.nx_px)*job.pitch_mm,6),job.snap_mm[1]))
    from .raster_timing import plan_raster_timing
    result.speed_plan=plan_raster_timing(result)
    if result.speed_plan.estimated_seconds+2>job_budget:raise RasterError('corrected native bitmap exceeds the bounded job time budget')
    return result

def gen_raster_header(job,*,file_id,name):
    values=(job.width_px,job.height_px,job.nx_px,job.ny_px,file_id)
    if any(type(v) is not int for v in values):raise RasterError('raster header values must be integers')
    w,h,nx,ny,_=values
    if type(job.px) is not int or not 1<=job.px<=255:raise RasterError('invalid native pixel pitch')
    limit=4000//job.px
    if (not 1<=w<=limit or not 1<=h<=limit or nx<0 or ny<0 or nx+w>limit or ny+h>limit
            or not 0<=file_id<=0xffffffff or job.dpi!=round(1016/job.px)):
        raise RasterError('invalid raster header geometry')
    if not isinstance(name,str):raise RasterError('raster name must be text')
    try:encoded=name.encode('utf-8')
    except UnicodeError as exc:raise RasterError('invalid raster name') from exc
    if not encoded or len(encoded)>29 or b'\0' in encoded:raise RasterError('invalid raster name byte length')
    header=bytearray(64);header[0]=0x60
    header[1:5]=w.to_bytes(2,'big')+h.to_bytes(2,'big')
    header[5:9]=file_id.to_bytes(4,'big');header[9]=job.px
    header[10:16]=nx.to_bytes(2,'big')+ny.to_bytes(2,'big')+job.dpi.to_bytes(2,'big')
    header[34:34+len(encoded)]=encoded
    return bytes(header)

def raster_digest(job,*,name):
    return hashlib.sha256(gen_raster_header(job,file_id=0,name=name)+job.payload).digest()
