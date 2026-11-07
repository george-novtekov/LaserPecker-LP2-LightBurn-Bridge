"""Bounded V376 timer model, corroborated by speed-baseline/corrected-01.

The 72MHz timer-clock assumption fits those live timings; it is not a readout
of the installed clock registers. Rate estimates are not measured galvo feedback.
"""
from dataclasses import dataclass
import math

from .job import JobError

TIMER_HZ=72_000_000
PRESCALER=720
POINTS_PER_MM=40
MAX_RELATIVE_ERROR=.05

@dataclass(frozen=True)
class SpeedPlan:
    wire_depth:int
    requested_mm_s:float
    estimated_mm_s:float
    relative_error:float
    minimum_mm_s:float = 0
    maximum_mm_s:float = 0

def timer_rate(depth):
    if type(depth) is not int or not 1<=depth<=100:raise JobError('timer depth outside 1..100')
    reload=int((101-depth)*12.5)
    return TIMER_HZ/(PRESCALER+1)/(reload+1)/POINTS_PER_MM

def plan_speed(job):
    vectors=job.marking_vectors
    if not vectors:return SpeedPlan(1,0,0,0)
    feed=vectors[0][2]
    factors=[]
    for start,end,rate in vectors:
        if start is None:raise JobError('first marking move requires a prior laser-off absolute position')
        if not math.isclose(rate,feed,rel_tol=0,abs_tol=1e-6):
            raise JobError('LP2 stock timer cannot honor mixed marking feeds in one job; use one speed')
        dx,dy=abs(end[0]-start[0]),abs(end[1]-start[1])
        # Stock DDA advances the major axis on each timer tick. Choose one
        # conservative timer for the stage, accounting for diagonal distance.
        factors.append(math.hypot(dx,dy)/max(dx,dy))
    if len(job.marking_powers)>1:
        raise JobError('LP2 stock DDA cannot honor varying marking power in one job')
    requested=feed/60
    target=requested/max(factors)
    candidates=[(timer_rate(d),d) for d in range(1,101) if timer_rate(d)<=target+1e-9]
    if not candidates:raise JobError('requested speed is below the qualified stock LP2 timer range')
    actual,depth=max(candidates)
    error=1-actual/target
    return SpeedPlan(depth,requested,actual,error,actual*min(factors),actual*max(factors))
