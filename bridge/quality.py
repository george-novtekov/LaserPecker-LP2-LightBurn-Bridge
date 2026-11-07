"""Controlled fill diagnostics, independent of a user's image/dither content."""
from dataclasses import dataclass
import math
from .job import JobError
from .speed import POINTS_PER_MM

@dataclass(frozen=True)
class Patch:
    number:int
    x:float
    y:float
    interval:float
    axis:str
    alternating:bool

PATCHES=tuple(Patch(row*4+col+1,24+col*16,y,.125,'Y' if col>=2 else 'X',bool(col%2))
              for row,y in enumerate((53,33)) for col in range(4))
SIZE_MM=6
_DIGITS={1:'bc',2:'abged',3:'abgcd',4:'fgbc',5:'afgcd',6:'afgcde',7:'abc',8:'abcdefg'}
_SEGMENTS={'a':((0,2),(1,2)),'b':((1,2),(1,1)),'c':((1,1),(1,0)),
           'd':((1,0),(0,0)),'e':((0,0),(0,1)),'f':((0,1),(0,2)),'g':((0,1),(1,1))}

def pattern(power_percent=1,speed_mm_s=100,*,full=False,swap_axes=False):
    if (type(power_percent) is not int or not 1<=power_percent<=30 or
            isinstance(speed_mm_s,bool) or not isinstance(speed_mm_s,(int,float)) or not math.isfinite(speed_mm_s) or not 50<=speed_mm_s<=202):
        raise JobError('print-quality test requires 1..30% power and 50..202 mm/s; default is patch 1 at 1% and 100 mm/s')
    if type(full) is not bool:raise JobError('full diagnostic selection must be boolean')
    if type(swap_axes) is not bool:raise JobError('scan-axis selection must be boolean')
    out=['G21G90','M5S0'];s=power_percent*10;feed=speed_mm_s*60
    def line(a,b):
        out.extend(('M5S0',f'G0X{a[0]:.3f}Y{a[1]:.3f}',f'M3S{s}',f'G1X{b[0]:.3f}Y{b[1]:.3f}F{feed:g}','M5S0'))
    for patch in PATCHES if full else PATCHES[:1]:
        axis=('Y' if patch.axis=='X' else 'X') if swap_axes else patch.axis
        # Integer stroke labels identify patches on the actual material.
        for segment in _DIGITS[patch.number]:
            a,b=_SEGMENTS[segment]
            line((patch.x+a[0],patch.y+SIZE_MM+1+a[1]),(patch.x+b[0],patch.y+SIZE_MM+1+b[1]))
        count=round(SIZE_MM/patch.interval)
        for i in range(count):
            # Half-cell centres of a five-tick pitch lie between native ticks.
            # Rounding those to three decimals first alternated the firmware's
            # rounding direction, producing four-/six-tick gaps. Snap half-up
            # BEFORE decimal encoding so every row remains five ticks apart.
            offset=math.floor((i+.5)*patch.interval*POINTS_PER_MM+.5)/POINTS_PER_MM
            if axis=='X':a,b=(patch.x,patch.y+offset),(patch.x+SIZE_MM,patch.y+offset)
            else:a,b=(patch.x+offset,patch.y),(patch.x+offset,patch.y+SIZE_MM)
            if patch.alternating and i%2:a,b=b,a
            line(a,b)
    out+=['M5S0','G0X50Y50','M2']
    return '\n'.join(out)+'\n'
