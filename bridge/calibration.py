"""Measured residual scale/cross-axis bow correction, applied on the PC.

For command coordinates u=x-50,v=y-50 and test radius r, the measured model is
X=50+u*(ax+bx*(v/r)**2), Y=50+v*(ay+by*(u/r)**2).
This symmetric model is fitted to six caliper spans, not an optical sensor.
"""
from dataclasses import dataclass,replace
import datetime
import json
import math
from pathlib import Path
import uuid
from . import job as j,raster as r,speed,raster_timing

class CalibrationError(j.JobError):pass

def _finite(value,name):
    if isinstance(value,bool):raise CalibrationError(name+' must be a finite number')
    try:out=float(value)
    except (TypeError,ValueError):raise CalibrationError(name+' must be a finite number')
    if not math.isfinite(out):raise CalibrationError(name+' must be a finite number')
    return out

@dataclass(frozen=True)
class Calibration:
    ax:float=1.
    ay:float=1.
    bx:float=0.
    by:float=0.
    side_mm:float=80.
    enabled:bool=False

    @classmethod
    def from_profile(cls,profile):
        if profile is None:return cls()
        if not isinstance(profile,dict) or profile.get('schema')!=1:raise CalibrationError('unsupported calibration profile')
        try:
            coeff=profile['coefficients']
            model=cls(**{k:_finite(coeff[k],k) for k in ('ax','ay','bx','by')},
                      side_mm=_finite(profile['size_mm'],'pattern size'),enabled=True)
        except (KeyError,TypeError) as exc:raise CalibrationError('incomplete calibration profile') from exc
        if not 20<=model.side_mm<=80:raise CalibrationError('calibration pattern must be20..80mm')
        if not .5<=model.ax<=1.5 or not .5<=model.ay<=1.5 or abs(model.bx)>.25 or abs(model.by)>.25:
            raise CalibrationError('measurements imply excessive correction; check dimensions and focus')
        # det(J)=ax*ay+ay*bx*V+ax*by*U-3*bx*by*U*V,
        # U=u²/r²,V=v²/r². It is bilinear in U,V, so its minimum
        # over the bed occurs at their interval corners. The diagonal
        # terms are linear in U,V. These nine points cover every extremum.
        for x in (0,50,100):
            for y in (0,50,100):
                a,b,c,d=model._jacobian(x,y)
                if min(a,d)<.2 or a*d-b*c<.1:raise CalibrationError('profile folds the work area; check measurements')
        return model

    def _jacobian(self,x,y):
        u,v=x-50,y-50;r2=(self.side_mm/2)**2
        return self.ax+self.bx*v*v/r2,2*self.bx*u*v/r2,2*self.by*u*v/r2,self.ay+self.by*u*u/r2

    def forward(self,x,y):
        if not self.enabled:return float(x),float(y)
        u,v=x-50,y-50;r2=(self.side_mm/2)**2
        return 50+u*(self.ax+self.bx*v*v/r2),50+v*(self.ay+self.by*u*u/r2)

    def inverse(self,x,y):
        if not self.enabled:return float(x),float(y)
        x,y=_finite(x,'X'),_finite(y,'Y');cx,cy=50+(x-50)/self.ax,50+(y-50)/self.ay
        for _ in range(30):
            fx,fy=self.forward(cx,cy);rx,ry=fx-x,fy-y
            if max(abs(rx),abs(ry))<1e-8:return cx,cy
            a,b,c,d=self._jacobian(cx,cy);det=a*d-b*c
            if not math.isfinite(det) or det<.1-1e-12 or min(a,d)<.2-1e-12:break
            cx,cy=cx-(d*rx-b*ry)/det,cy-(-c*rx+a*ry)/det
        raise CalibrationError('calibration inverse did not converge; check measurements')

def profile_from_measurements(size_mm,widths,heights,name='LP2 calibration'):
    size=_finite(size_mm,'pattern size')
    if not 20<=size<=80:raise CalibrationError('pattern size must be20..80mm')
    if len(widths)!=3 or len(heights)!=3:raise CalibrationError('enter all six measured spans')
    widths=[_finite(v,'width') for v in widths];heights=[_finite(v,'height') for v in heights]
    if any(v<=0 for v in widths+heights):raise CalibrationError('measured spans must be positive')
    warnings=[]
    if abs(widths[0]-widths[2])>.5 or abs(heights[0]-heights[2])>.5:
        warnings.append('Opposite outer spans differ by more than0.5mm; this symmetric model may not correct all distortion.')
    profile=dict(schema=1,name=str(name).strip()[:100] or 'LP2 calibration',size_mm=size,
        coefficients=dict(ax=widths[1]/size,ay=heights[1]/size,
            bx=((widths[0]+widths[2])/2-widths[1])/size,
            by=((heights[0]+heights[2])/2-heights[1])/size),
        measurements=dict(widths=widths,heights=heights),warnings=warnings,
        created_utc=datetime.datetime.now(datetime.timezone.utc).isoformat())
    Calibration.from_profile(profile)
    return profile

def load_profile(path):
    path=Path(path)
    if path.stat().st_size>65536:raise CalibrationError('calibration profile exceeds64KiB')
    try:profile=json.loads(path.read_text(encoding='utf-8'))
    except (ValueError,UnicodeError) as exc:raise CalibrationError('invalid calibration JSON') from exc
    if not isinstance(profile,dict):raise CalibrationError('a calibration file must contain a profile object')
    Calibration.from_profile(profile)
    return profile

def save_profile(path,profile):
    if not isinstance(profile,dict):raise CalibrationError('there is no calibration profile to save')
    Calibration.from_profile(profile);path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        temporary.write_text(json.dumps(profile,indent=2,allow_nan=False),encoding='utf-8');temporary.replace(path)
    finally:
        try:
            if temporary.exists():temporary.unlink()
        except OSError:pass

def measurement_pattern(size_mm=80,power_percent=5,speed_mm_s=50):
    size=_finite(size_mm,'pattern size');power=_finite(power_percent,'power');feed=_finite(speed_mm_s,'speed')*60
    if not 20<=size<=80 or not 1<=power<=100 or not j.FEED_MIN<=feed<=j.FEED_MAX:
        raise CalibrationError('pattern requires20..80mm,1..100%power and2..202mm/s')
    lo,hi=50-size/2,50+size/2;lines=['G21G90','M5S0']
    for y in (hi,50,lo):lines += [f'G0X{lo:.3f}Y{y:.3f}',f'M3S{round(power*10)}',f'G1X{hi:.3f}Y{y:.3f}F{feed:g}','M5S0']
    for x in (lo,50,hi):lines += [f'G0X{x:.3f}Y{lo:.3f}',f'M3S{round(power*10)}',f'G1X{x:.3f}Y{hi:.3f}F{feed:g}','M5S0']
    # Laser-off parking only. No machine-settings commands.
    lines += ['G0X50Y50','M2']
    return '\n'.join(lines).encode('ascii')

def _inside(point):return all(-1e-7<=v<=100+1e-7 for v in point)
def _marked_point(model,p):
    out=model.inverse(*p)
    if not _inside(out):raise CalibrationError('calibrated marking exceeds the100mm command field; reduce or center the artwork')
    return tuple(min(100.,max(0.,v)) for v in out)

def _curve(model,a,b,depth=0):
    wa,wb=_marked_point(model,a),_marked_point(model,b)
    mid=tuple((a[i]+b[i])/2 for i in (0,1));wm=_marked_point(model,mid)
    deviation=math.dist(wm,tuple((wa[i]+wb[i])/2 for i in (0,1)))
    if math.dist(a,b)<=1 and deviation<=.005:return [wb]
    if depth>=16:raise CalibrationError('calibration subdivision bound exceeded')
    return _curve(model,a,mid,depth+1)+_curve(model,mid,b,depth+1)

def apply_stage(stage,model,*,job_budget=600):
    if not model.enabled:return stage
    if isinstance(stage,r.RasterJob):return _raster(stage,model,job_budget)
    if not isinstance(stage,j.Job):raise CalibrationError('unsupported calibration stage')
    out=[];pos=None;wirepos=None;motion='G0';spindle='M5';s=0;feed=0;points=[];vectors=[]
    for line in stage.payload.decode('ascii').splitlines():
        words=j.words(line);params={a:v for a,v in words if a not in 'GM'}
        for a,v in words:
            if a=='G' and v in (0,1):motion='G'+str(int(v))
            if a=='M' and v in (3,5):spindle='M'+str(int(v))
        if 'S' in params:s=params['S']
        if 'F' in params:feed=params['F']
        if 'X' not in params and 'Y' not in params:out.append(line);continue
        end=(params['X'],params['Y']);burning=motion=='G1' and spindle=='M3' and s>0
        if burning:
            if pos is None:raise CalibrationError('calibrated marking requires an off position')
            _marked_point(model,pos);destinations=_curve(model,pos,end)
        else:
            target=model.inverse(*end)
            destinations=[tuple(min(100.,max(0.,v)) for v in target)]
        for dest in destinations:
            dest=tuple(round(v,3) for v in dest)
            out.append(f'{motion} X{dest[0]:.3f} Y{dest[1]:.3f}'+(f' F{feed:g}' if motion=='G1' else ''))
            points.append(dest)
            if burning and wirepos!=dest:vectors.append((wirepos,dest,feed))
            wirepos=dest
        pos=end
        if len(out)>50000:raise CalibrationError('calibrated vector stage exceeds50000lines')
    payload=('\n'.join(out)+'\n').encode('ascii')
    if len(payload)>j.MAX_OUTPUT_BYTES:raise CalibrationError('calibrated vector stage exceeds1MiB')
    left,top=(math.floor(min(p[i] for p in points)*10) for i in (0,1))
    right,bottom=(math.ceil(max(p[i] for p in points)*10) for i in (0,1))
    geometry=dict(stage.header_geometry,width_px=max(1,right-left),height_px=max(1,bottom-top),
        left_px=left,top_px=top,nx_px=left,ny_px=top,line_count=len(out)+1)
    result=replace(stage,payload=payload,normalized_line_count=len(out)+1,bounds=(left,top,right,bottom),
        header_geometry=geometry,final_host_position=wirepos,marking_vectors=tuple(vectors))
    result.speed_plan=speed.plan_speed(result)
    return result

def _raster(stage,model,job_budget):
    if stage.wire_compensated:raise CalibrationError('calibrate logical image before wire row correction')
    pitch=stage.pitch_mm;limit=4000//stage.px
    left,bottom=stage.nx_px*pitch,stage.ny_px*pitch;right=left+stage.width_px*pitch;top=bottom+stage.height_px*pitch
    boundary=[]
    for i in range(33):
        f=i/32;x=left+(right-left)*f;y=bottom+(top-bottom)*f
        boundary += [_marked_point(model,p) for p in ((x,bottom),(x,top),(left,y),(right,y))]
    nx,ny=(max(0,math.floor(min(p[i] for p in boundary)/pitch+1e-7)) for i in (0,1))
    ex,ey=(min(limit,math.ceil(max(p[i] for p in boundary)/pitch-1e-7)) for i in (0,1))
    w,h=ex-nx,ey-ny;stride=(w+7)//8;oldstride=(stage.width_px+7)//8
    if not 1<=w<=limit or not 1<=h<=limit:raise CalibrationError('invalid calibrated image extent')
    if stride*h>j.MAX_OUTPUT_BYTES:raise CalibrationError('calibrated bitmap exceeds 1 MiB stock job limit')
    payload=bytearray(b'\xff'*(stride*h));black=0
    for y in range(h):
        for x in range(w):
            px,py=model.forward((nx+x+.5)*pitch,(ny+y+.5)*pitch)
            sx,sy=math.floor(px/pitch-stage.nx_px),math.floor(py/pitch-stage.ny_px)
            if 0<=sx<stage.width_px and 0<=sy<stage.height_px and not stage.payload[sy*oldstride+sx//8]&(0x80>>(sx%8)):
                payload[y*stride+x//8]&=~(0x80>>(x%8));black+=1
    if not black:raise CalibrationError('calibration removed all image pixels')
    result=replace(stage,payload=bytes(payload),width_px=w,height_px=h,nx_px=nx,ny_px=ny,black_pixels=black)
    result.speed_plan=raster_timing.plan_raster_timing(result)
    if result.speed_plan.estimated_seconds+2>job_budget:raise CalibrationError('calibrated image exceeds stage time budget')
    return result
