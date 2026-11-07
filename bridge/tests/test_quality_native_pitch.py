"""Replay wire coordinates through the saved firmware's float32 conversion."""
import struct
import unittest
from bridge import quality
from bridge.sequence import compile_sequence
from bridge.orientation import rotate_stage
from bridge.tests.test_banding import source_spans

def f32(value):return struct.unpack('<f',struct.pack('<f',value))[0]
def native_tick(value):return int(f32(f32(f32(value)*40)+.5))

class NativePitchTests(unittest.TestCase):
    def test_every_wire_scan_row_is_exactly_five_native_ticks_apart(self):
        for swapped in (False,True):
            stage=compile_sequence(quality.pattern(15,60,full=True,swap_axes=swapped),image_mode='continuous').stages[0]
            for rotation in (0,180):
                wire=rotate_stage(stage,rotation)
                replay=source_spans(wire.payload.decode('ascii'))
                logical=lambda p:tuple(100-v for v in p) if rotation else p
                for patch in quality.PATCHES:
                    with self.subTest(swapped=swapped,rotation=rotation,patch=patch.number):
                        rows=[]
                        axis=patch.axis if not swapped else ('Y' if patch.axis=='X' else 'X')
                        along=0 if axis=='X' else 1;across=1-along
                        for a,b,feed in replay:
                            la,lb=logical(a),logical(b)
                            if (patch.x-1e-6<=min(la[0],lb[0]) and max(la[0],lb[0])<=patch.x+6+1e-6
                                and patch.y-1e-6<=min(la[1],lb[1]) and max(la[1],lb[1])<=patch.y+6+1e-6):
                                rows.append(native_tick(a[across]))
                                self.assertEqual(abs(native_tick(b[along])-native_tick(a[along])),240)
                                self.assertEqual(native_tick(a[across]),native_tick(b[across]))
                                self.assertTrue(all(abs(v*40-round(v*40))<1e-6 for v in a+b))
                        self.assertEqual(len(rows),48)
                        self.assertEqual([abs(b-a) for a,b in zip(rows,rows[1:])],[5]*47)

if __name__=='__main__':unittest.main()
