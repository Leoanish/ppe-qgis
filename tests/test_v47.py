"""4.7: touching plants in a row are split by the plant spacing; split points can be merged back."""
import json
import os
import shutil
import tempfile
import unittest

import numpy as np
from osgeo import gdal, ogr

from ppe_qgis.plant_count import (SPACING_MIN_GAPS, auto_split, count_plants, merge_points, plant_spacing, read_points,
                                  split_part, summarize_points)
try:
    from test_v43 import tif
except ImportError:  # run as tests.test_v47
    from tests.test_v43 import tif

gdal.UseExceptions()


def rows_field(n_x=400, touching=3, big_single=True):
    """Four crop rows 45 px (cm) apart, plants every 20 px; in row 2 `touching` neighbours grown big enough
    to touch; in row 4 one big plant on its own."""
    h, w = 230, n_x; yy, xx = np.mgrid[0:h, 0:w]; m = np.zeros((h, w), np.uint8); truth = []
    for r, y in enumerate((45, 90, 135, 180)):
        for i, x in enumerate(range(15, w - 10, 20)):
            rad = 4
            if r == 1 and 6 <= i < 6 + touching: rad = 11
            if r == 3 and i == 8 and big_single: rad = 9
            m[(xx - x) ** 2 + (yy - y) ** 2 <= rad * rad] = 1; truth.append((x, y, rad))
    return m, truth


class PlantSpacing(unittest.TestCase):
    def test_spacing_along_any_row_direction_and_too_few_gaps(self):
        for angle in (0.0, 30.0, 90.0):
            t = np.radians(angle); u = np.arange(0, 200, 20.0); px, py = [], []
            for d in (0, 45, 90):
                px += list(d * np.cos(t) - u * np.sin(t)); py += list(d * np.sin(t) + u * np.cos(t))
            px = np.array(px) + 1000; py = np.array(py) + 1000
            keep = np.ones(len(px), bool); keep[[3, 14]] = False                    # two missing plants
            s, gaps = plant_spacing(px[keep], py[keep], angle, 45, 400)
            self.assertAlmostEqual(s, 20.0, places=6)
            self.assertGreaterEqual(gaps, SPACING_MIN_GAPS)
        s, gaps = plant_spacing(px[:6], py[:6], 90.0, 45, 400)
        self.assertIsNone(s)


class AutoSplitAndMerge(unittest.TestCase):
    def test_touching_neighbours_split_big_single_plant_kept(self):
        tmp = tempfile.mkdtemp()
        try:
            m, _ = rows_field()
            mask = tif(os.path.join(tmp, "mask.tif"), m, gdal.GDT_Byte, nodata=255)
            r = count_plants({"mask_path": mask, "output_dir": os.path.join(tmp, "c"), "min_plant_cm2": 0.05, "join_cm": 0, "find_rows": True})
            self.assertTrue(r["rows"]["rows"])
            self.assertAlmostEqual(r["plant_spacing_cm"], 20.0, delta=0.5)
            self.assertEqual(r["split_by_ppe"], 3)                 # the three touching plants
            self.assertEqual(r["flagged_big"], 1)                  # the big single plant stays 'check'
            ds = ogr.Open(r["points_path"]); L = ds.GetLayer()
            auto = [(f.GetGeometryRef().GetX(), f.GetGeometryRef().GetY(), f.GetField("split_of")) for f in L if f.GetField("status") == "auto split"]
            ds = None
            self.assertEqual(len({a[2] for a in auto}), 1)          # all from one group
            xs = sorted((a[0] - 500000) / 0.01 for a in auto)       # one point near each true plant centre (x = 135, 155, 175)
            for got, want in zip(xs, (135, 155, 175)):
                self.assertLess(abs(got - want), 4)
            note = r["auto_split"]["note"]
            self.assertIn("plant spacing in the row 20.", note)
            self.assertIn("longer than 30.", note)                   # 1.5 x spacing
            self.assertAlmostEqual(r["auto_split"]["split_longer_than_cm"], 1.5 * r["plant_spacing_cm"])
            self.assertEqual(r["auto_split"]["patches"][0]["plants"], 3)
            # one part of the split patch split again: only that part changes
            pts = [a for a in auto]
            mid = sorted(pts, key=lambda a: a[0])[1]
            parts = split_part(r["points_path"], mid[0], mid[1], 2, [(a[0], a[1]) for a in pts], None)
            self.assertEqual(len(parts), 2)
            px = sorted((q[0] - 500000) / 0.01 for q in parts)
            self.assertTrue(all(145 <= v <= 165 for v in px))          # both inside the middle plant's share
            with open(r["summary_path"]) as fh: head = fh.readline()
            self.assertIn("split_longer_than_cm", head)
            self.assertIn("of_which_split_by_ppe", head); self.assertIn("plant_spacing_cm", head)
            # the same with the spacing typed in; and nothing without rows
            shutil.rmtree(os.path.join(tmp, "c"))
            r2 = count_plants({"mask_path": mask, "output_dir": os.path.join(tmp, "c"), "min_plant_cm2": 0.05, "join_cm": 0, "find_rows": True,
                               "plant_spacing_cm": 20})
            self.assertEqual(r2["split_by_ppe"], 3); self.assertEqual(r2["auto_split"]["spacing_from"], "given")
            r3 = count_plants({"mask_path": mask, "output_dir": os.path.join(tmp, "d"), "min_plant_cm2": 0.05, "join_cm": 0, "find_rows": False})
            self.assertEqual(r3["split_by_ppe"], 0); self.assertEqual(r3["flagged_big"], 2)
            r4 = count_plants({"mask_path": mask, "output_dir": os.path.join(tmp, "e"), "min_plant_cm2": 0.05, "join_cm": 0, "find_rows": True,
                               "auto_split": False})
            self.assertEqual(r4["split_by_ppe"], 0)
            res = auto_split(r4["points_path"])                    # can be run again later on saved points
            self.assertEqual(res["groups_split"], 1)
            self.assertEqual(summarize_points(r4["points_path"])["split_by_ppe"], 3)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_too_few_plants_to_measure(self):
        tmp = tempfile.mkdtemp()
        try:
            m, _ = rows_field(n_x=120)
            mask = tif(os.path.join(tmp, "mask.tif"), m, gdal.GDT_Byte, nodata=255)
            r = count_plants({"mask_path": mask, "output_dir": os.path.join(tmp, "c"), "min_plant_cm2": 0.05, "join_cm": 0, "find_rows": True,
                              "row_spacing_cm": 45})
            note = (r.get("auto_split") or {}).get("note", "")
            self.assertEqual(r["split_by_ppe"], 0)
            self.assertIn("too few", note)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_merge_points(self):
        x, y, a = merge_points([(0.0, 0.0, 1.0), (3.0, 0.0, 2.0)])
        self.assertAlmostEqual(x, 2.0); self.assertAlmostEqual(y, 0.0); self.assertAlmostEqual(a, 3.0)


if __name__ == "__main__":
    unittest.main()
