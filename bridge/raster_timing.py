"""Native V376 bitmap pixel-dwell model, distinct from the vector DDA timer.

650us white-pixel motion and 10ms row overhead are conservative fits to the
all-white 100x40 test. Saved V377 plus the two live checker tests corroborate
500us black-pixel settling and 20us per UI dwell step. These are timing
estimates. Dwell preserves nominal requested exposure per 0.1mm black pixel;
firmware movement/settling overhead makes native images slower than G-code F.
"""
from dataclasses import dataclass
import math
from .job import JobError

MOVE_US=650.0
BLACK_SETTLE_US=500.0
ROW_SECONDS=.010

@dataclass(frozen=True)
class RasterTiming:
    wire_depth:int
    exposure_us:int
    requested_mm_s:float
    estimated_black_mm_s:float
    estimated_seconds:float
    capped_fast:bool

def plan_raster_timing(job):
    requested=job.requested_feed/60
    pitch=getattr(job,'px',4)/40
    source_pitch=getattr(job,'source_pitch_mm',.1)
    # Preserve nominal exposure per area when changing the scan grid. Merely
    # retaining the old pulse would overburn rows added by upsampling.
    desired_us=1e6*pitch*pitch/(requested*source_pitch)
    ui=math.floor(desired_us/20+1e-9)
    if not 1<=ui<=100:
        raise JobError('requested scan exposure is outside the native 20..2000 us pulse range; increase speed')
    exposure=ui*20
    black_rate=1e6*pitch/(MOVE_US+BLACK_SETTLE_US+exposure)
    duration=(job.width_px*job.height_px*MOVE_US+job.black_pixels*(BLACK_SETTLE_US+exposure))/1e6
    duration+=job.height_px*ROW_SECONDS+1.2
    return RasterTiming(101-ui,exposure,requested,black_rate,duration,black_rate<requested)
