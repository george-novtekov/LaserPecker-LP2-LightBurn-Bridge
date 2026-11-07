"""Prevent the diagnostic's denser-row overexposure from returning."""
import math
import unittest
from bridge import quality
from bridge.job import JobError
from bridge.sequence import compile_sequence

class QualityExposureTests(unittest.TestCase):
    def test_default_is_one_low_power_patch(self):
        seq=compile_sequence(quality.pattern(),image_mode='continuous')
        self.assertEqual(len(seq.stages),1)
        stage=seq.stages[0]
        self.assertEqual(stage.marking_powers,(10,))
        self.assertEqual(stage.speed_plan.requested_mm_s,100)
        self.assertTrue(all(a[1]>=53 and b[1]>=53 for a,b,f in stage.marking_vectors))
        fill=[(a,b) for a,b,f in stage.marking_vectors if a[1]<=59 and b[1]<=59]
        self.assertEqual(len(fill),48)

    def test_full_test_has_equal_nominal_exposure_in_both_rows(self):
        stage=compile_sequence(quality.pattern(full=True),image_mode='continuous').stages[0]
        exposures=[]
        for patch in quality.PATCHES:
            lines=[(a,b) for a,b,f in stage.marking_vectors
                if patch.x<=min(a[0],b[0]) and max(a[0],b[0])<=patch.x+6
                and patch.y<=min(a[1],b[1]) and max(a[1],b[1])<=patch.y+6]
            self.assertEqual(len(lines),48)
            length=sum(math.dist(a,b) for a,b in lines)
            self.assertAlmostEqual(length,288)
            # Same stock power, timer and marked length per 36 mm². This is
            # nominal exposure equality, not an optical measurement.
            exposures.append(length/stage.speed_plan.estimated_mm_s/36)
        self.assertTrue(all(math.isclose(e,exposures[0]) for e in exposures))

    def test_requested_thirty_percent_limit_and_speed_bounds(self):
        stage=compile_sequence(quality.pattern(30,100),image_mode='continuous').stages[0]
        self.assertEqual(stage.marking_powers,(300,))
        for power,speed in ((31,100),(100,100),(0,100),(5,2),(1,49.9)):
            with self.subTest(power=power,speed=speed),self.assertRaises(JobError):
                quality.pattern(power,speed,full=True)

    def test_full_selection_is_explicit_boolean(self):
        for full in (1,'yes',None):
            with self.assertRaises(JobError):quality.pattern(full=full)

if __name__=='__main__':unittest.main()
