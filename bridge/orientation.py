"""Map logical LightBurn coordinates to the LP2 device work area."""
from dataclasses import replace
import re
from .job import Job,JobError
from .raster import RasterJob

BED_MM=100
BED_PIXELS=1000

def rotate_stage(stage,rotation,*,mirror_x=False):
    """Map the whole bed to device axes; preserve the logical job object.

    Compose left/right correction with rotation, after calibration and before
    packed-raster wire phase compensation.
    """
    if type(rotation) is not int or rotation not in (0,180):raise JobError('rotation must be0or180degrees')
    if type(mirror_x) is not bool:raise JobError('mirror_x must be boolean')
    flip_x=(rotation==180)^mirror_x
    flip_y=rotation==180
    if not flip_x and not flip_y:return stage
    def point(p):
        return tuple(round(BED_MM-v if flip else v,3) for v,flip in zip(p,(flip_x,flip_y)))
    if isinstance(stage,RasterJob):
        if stage.wire_compensated:raise JobError('rotate logical image before wire row correction')
        w,h=stage.width_px,stage.height_px;stride=(w+7)//8
        if len(stage.payload)!=stride*h:raise JobError('invalid raster payload for rotation')
        payload=bytearray(b'\xff'*(stride*h))
        for y in range(h):
            for x in range(w):
                if not stage.payload[y*stride+x//8] & (0x80>>(x%8)):
                    rx,ry=(w-1-x if flip_x else x),(h-1-y if flip_y else y)
                    payload[ry*stride+rx//8]&=~(0x80>>(rx%8))
        limit=4000//stage.px
        return replace(stage,payload=bytes(payload),
            nx_px=max(0,min(limit-w,round(4000/stage.px-stage.nx_px-w))) if flip_x else stage.nx_px,
            ny_px=max(0,min(limit-h,round(4000/stage.px-stage.ny_px-h))) if flip_y else stage.ny_px,
            final_host_position=point(stage.final_host_position),
            finish_points=tuple(point(p) for p in stage.finish_points),
            snap_mm=tuple(-v if flip else v for v,flip in zip(stage.snap_mm,(flip_x,flip_y))))
    if not isinstance(stage,Job):raise JobError('unsupported job type for rotation')
    # Job payload is already normalized to absolute millimetres. Preserve its
    # mapped S values, feeds, line count, and stock setup/laser-off commands.
    def axis(match):
        if not (flip_x if match.group(1)=='X' else flip_y):return match.group(0)
        um=100000-round(float(match.group(2))*1000)
        return f'{match.group(1)}{um/1000:.3f}'
    payload=re.sub(r'([XY])([+-]?(?:\d+(?:\.\d*)?|\.\d+))',axis,stage.payload.decode('ascii')).encode('ascii')
    left,top,right,bottom=stage.bounds
    bounds=((BED_PIXELS-right if flip_x else left),(BED_PIXELS-bottom if flip_y else top),
            (BED_PIXELS-left if flip_x else right),(BED_PIXELS-top if flip_y else bottom))
    geometry=dict(stage.header_geometry,left_px=bounds[0],top_px=bounds[1],nx_px=bounds[0],ny_px=bounds[1])
    return replace(stage,payload=payload,bounds=bounds,header_geometry=geometry,
        final_host_position=point(stage.final_host_position),
        marking_vectors=tuple((point(a),point(b),f) for a,b,f in stage.marking_vectors))
