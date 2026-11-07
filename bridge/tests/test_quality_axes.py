"""Separate scan-axis comparison from fixed field positions."""
import json
import math
import unittest
from bridge import quality
from bridge.job import JobError
from bridge.sequence import compile_sequence
from bridge.tests import test_service
from bridge.tests.test_runtime import wait_for

def fill_lines(stage,patch):
    return [(a,b,f) for a,b,f in stage.marking_vectors
        if patch.x<=min(a[0],b[0]) and max(a[0],b[0])<=patch.x+6
        and patch.y<=min(a[1],b[1]) and max(a[1],b[1])<=patch.y+6]

class QualityAxisTests(unittest.TestCase):
    def test_swapping_only_changes_fill_axis_at_the_same_patch_positions(self):
        original=compile_sequence(quality.pattern(20,50,full=True),image_mode='continuous').stages[0]
        swapped=compile_sequence(quality.pattern(20,50,full=True,swap_axes=True),image_mode='continuous').stages[0]
        self.assertEqual(original.marking_powers,swapped.marking_powers)
        self.assertEqual(original.bounds,swapped.bounds)
        self.assertEqual(original.speed_plan,swapped.speed_plan)
        for p in quality.PATCHES:
            before,after=fill_lines(original,p),fill_lines(swapped,p)
            self.assertEqual(len(before),48);self.assertEqual(len(after),48)
            axis=1 if p.axis=='X' else 0
            for i,(a,b,f) in enumerate(after):
                self.assertEqual(a[1-axis],b[1-axis]);self.assertEqual(f,3000)
                self.assertAlmostEqual(math.dist(a,b),6)
                self.assertEqual(b[axis]>a[axis],not(p.alternating and i%2))
            self.assertAlmostEqual(sum(math.dist(a,b) for a,b,f in before),sum(math.dist(a,b) for a,b,f in after))
        # Printed number strokes stay unchanged: only the filled squares swap.
        numbers=lambda stage:[v for v in stage.marking_vectors if not any(v in fill_lines(stage,p) for p in quality.PATCHES)]
        self.assertEqual(numbers(original),numbers(swapped))

    def test_swapped_single_patch_keeps_low_default_and_single_selection(self):
        stage=compile_sequence(quality.pattern(swap_axes=True),image_mode='continuous').stages[0]
        self.assertEqual(stage.marking_powers,(10,));self.assertEqual(stage.speed_plan.requested_mm_s,100)
        self.assertEqual(len(fill_lines(stage,quality.PATCHES[0])),48)
        self.assertTrue(all(not fill_lines(stage,p) for p in quality.PATCHES[1:]))

    def test_non_boolean_axis_option_rejects(self):
        for value in (1,'yes',None):
            with self.assertRaises(JobError):quality.pattern(swap_axes=value)

class QualityRecipeTests(unittest.TestCase):
    make=test_service.ServiceTests.make
    def test_recipe_records_actual_comparison_settings(self):
        service,device,calls=self.make();device.upload_chunk_size=1048576;device.upload_chunk_delay=0
        service.start(dict(transport='usb',tcp_port=0))
        self.assertTrue(wait_for(lambda:service.snapshot()['connected']))
        service.submit_quality_pattern(20,50,full=True,swap_axes=True)
        self.assertTrue(wait_for(lambda:bool(device.starts)))
        records=[]
        for path in service.data_dir.glob('*-serial.jsonl'):
            records += [json.loads(s) for s in path.read_text().splitlines()]
        recipe=next(r for r in records if r['kind']=='quality_pattern_recipe')
        self.assertEqual((recipe['power_percent'],recipe['speed_mm_s'],recipe['full'],recipe['swap_axes']),(20,50,True,True))
        self.assertEqual(recipe['interval_mm'],.125)

if __name__=='__main__':unittest.main()
