"""Preflight a LightBurn stream into ordered, independently timed stock jobs.

Vector cuts and hatch fills share the same G0/G1 representation. Relative
LightBurn image sections compile to native packed bitmaps. No device IO here.
"""
from dataclasses import dataclass
from . import job as j, raster as r, raster_timing, speed

MAX_STAGES = 512
MAX_STAGE_MOVES = 5000

@dataclass
class JobSequence:
    stages: tuple
    final_host_position: tuple
    notes: tuple = ()

def compile_sequence(data, *, power_cap=1000, job_budget=600, image_mode='bitmap', _single_scan=False):
    if image_mode not in ('bitmap','continuous'):raise j.JobError('image mode must be bitmap or continuous')
    if isinstance(data,str): data=data.encode('ascii')
    data=bytes(data)
    if len(data)>j.MAX_INPUT_BYTES: raise j.JobError('input exceeds 16 MiB')
    if any(b<32 and b not in (9,10,13) or b>126 for b in data): raise j.JobError('unsupported input byte')
    lines=data.decode('ascii').splitlines()
    if len(lines)>j.MAX_LINES: raise j.JobError('too many input lines')
    groups=dict(j.GROUPS,M8='coolant')
    position=None;units=1.;absolute=True;motion='G0';spindle='M5';feed=None;s=0;finished=False
    stages=[];vector=[];vector_key=None;marked=False;image=None;notes=[]
    def push(stage):
        if len(stages)>=MAX_STAGES: raise j.JobError('mixed job exceeds 512 stock stages')
        stages.append(stage)
    def flush_vector(*, discard_blank=False):
        nonlocal vector,vector_key,marked
        if vector and not (discard_blank and not marked):
            stage=j.parse_job('\n'.join(vector+['M2']),power_cap=power_cap,compact=_single_scan)
            stage.speed_plan=speed.plan_speed(stage)
            push(stage)
        vector=[];vector_key=None;marked=False
    def scan_vectors(source,*,picture=False):
        # M3 prevents recursive reclassification of these relative moves.
        # Preserve source burn spans instead of repeating bitmap columns.
        converted=[]
        for line in source:
            words=j.words(line)
            for a,v in words:
                if a=='G' and v==20:raise j.JobError('M4 scan fallback requires millimetres')
            converted.append(' '.join(f'{a}{3 if a=="M" and v==4 else v:.12g}' for a,v in words))
        try:
            fallback=compile_sequence('\n'.join(converted),power_cap=power_cap,job_budget=job_budget,image_mode='bitmap',_single_scan=picture)
        except j.JobError as exc:
            if picture and 'normalized job exceeds' in str(exc):
                raise j.JobError('continuous image exceeds the 1 MiB single-file limit; reduce image size or select Native bitmap to avoid restart seams') from exc
            raise
        powers={power for s in fallback.stages for power in s.marking_powers}
        if len(powers)!=1:raise j.JobError('continuous M4 scan requires one nonzero power; grayscale is unsupported')
        axes=set()
        for s in fallback.stages:
            for start,end,feed in s.marking_vectors:
                dx,dy=abs(end[0]-start[0]),abs(end[1]-start[1])
                if dx>1e-9 and dy>1e-9:
                    raise j.JobError('continuous M4 scan requires axis-aligned marking and laser-off row turns')
                axes.add('Y' if dy>1e-9 else 'X')
        if len(axes)!=1:
            raise j.JobError('continuous M4 scan requires one scan axis per section; row turns must be laser-off')
        for s in fallback.stages:
            push(s)
        if picture:
            notes.append(f'SCAN: continuous image runs ({next(iter(axes))} axis) preserve LightBurn spacing in one stock file per power/feed; no arbitrary chunk restarts; bypass native packed-byte pauses and bitmap resampling; larger upload, constant-power M4 approximation')
        else:notes.append('SCAN: non-pixel M4 fill preserved as vector segments at constant power; dynamic M4 modulation is not emulated')
    def flush_image():
        nonlocal image
        source=image+['M5','G90','M2']
        if image_mode=='continuous':
            scan_vectors(source,picture=True)
            image=None
            return
        try:stage=r.parse_raster('\n'.join(source),power_cap=power_cap)
        except r.RasterGridError:
            scan_vectors(source)
            image=None
            return
        stage.speed_plan=raster_timing.plan_raster_timing(stage)
        if stage.speed_plan.estimated_seconds+2>job_budget:
            raise j.JobError('native bitmap exceeds the bounded job time budget; reduce image size')
        push(stage);image=None
    def seed(pos):
        return ['G21G90','M5S0']+([] if pos is None else [f'G0X{pos[0]:.6f}Y{pos[1]:.6f}'])
    for number,line in enumerate(lines,1):
        if len(line)>j.MAX_LINE_BYTES:raise j.JobError(f'line {number}: exceeds 256 bytes')
        tokens=j.words(line,number)
        if not tokens:continue
        if finished:raise j.JobError('command after finish')
        g,p={},{}
        for a,v in tokens:
            if a in 'GM':
                code=a+str(int(v))
                if v!=int(v) or code not in groups:raise j.JobError(f'line {number}: unsupported {a}{v:g}')
                group=groups[code]
                if group in g:raise j.JobError(f'line {number}: conflicting {group}')
                g[group]=code
            else:
                if a not in 'XYFS' or a in p:raise j.JobError(f'line {number}: unsupported/duplicate {a}')
                p[a]=v
        if 'finish' in g:
            if len(g)!=1 or p:raise j.JobError('finish must be a separate command')
            if image is not None:flush_image()
            flush_vector();finished=True;continue
        oldpos=position
        # LightBurn uses G91 for both hatch fills and pictures. Constant-power
        # M3 fills remain vector moves (arbitrary interval/direction); M4 image
        # scans use the compact bitmap compiler.
        if g.get('distance')=='G91' and absolute and g.get('spindle',spindle)=='M4':
            if position is None or units!=1:raise j.JobError('image requires an established G21 absolute origin')
            flush_vector(discard_blank=True)
            image=['G21G90','M5S0',f'G0X{position[0]:.6f}Y{position[1]:.6f}',spindle+f'S{s:g}']
            if feed is not None:image.append(f'F{feed:g}')
        if image is not None and g.get('distance')=='G90':flush_image()
        units={'G20':25.4,'G21':1.}.get(g.get('units'),units)
        absolute={'G90':True,'G91':False}.get(g.get('distance'),absolute)
        motion=g.get('motion',motion);spindle=g.get('spindle',spindle)
        if 'F' in p:
            feed=p['F']*units
            if not j.FEED_MIN<=feed<=j.FEED_MAX:raise j.JobError('feed outside 120..12120 mm/min')
        if 'S' in p:s=p['S'];j.map_s(s,power_cap)
        if image is not None:image.append(line)
        if not any(a in p for a in 'XY'):continue
        if position is None and (not absolute or not all(a in p for a in 'XY')):
            raise j.JobError('first move must establish both absolute X and Y')
        end=tuple((p[a]*units if a in p else position[i]) if absolute else position[i]+p.get(a,0)*units for i,a in enumerate('XY'))
        if any(not -1e-9<=v<=100+1e-9 for v in end):raise j.JobError('move outside 0..100 mm')
        end=tuple(round(min(100,max(0,v)),6) for v in end)
        if motion=='G1' and feed is None:raise j.JobError('G1 requires an established feed')
        position=end
        if image is not None:continue
        burning=motion=='G1' and spindle in ('M3','M4') and j.map_s(s,power_cap)>0 and oldpos!=end
        if burning and spindle=='M4':
            note='SCAN: M4 vector paths use native constant power; dynamic M4 modulation is not emulated'
            if note not in notes:notes.append(note)
        key=(feed,s) if burning else None
        if vector and (burning and vector_key is not None and key!=vector_key or not _single_scan and len(vector)>MAX_STAGE_MOVES*2):flush_vector()
        if not vector:vector=seed(oldpos)
        if burning:vector_key=key;marked=True
        vector.append(('M3' if burning else 'M5')+f'S{s:g}' if burning else 'M5S0')
        vector.append(f'{motion}X{end[0]:.3f}Y{end[1]:.3f}'+(f'F{feed:g}' if motion=='G1' else ''))
    if not finished:raise j.JobError('complete job requires M2 or M30')
    if not stages:raise j.JobError('job contains no coordinate movement')
    return JobSequence(tuple(stages),position,tuple(notes))
