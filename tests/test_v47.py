"""4.7: touching plants in a row are split by the plant spacing; split points can be merged back."""
import json
import os
import shutil
import tempfile
import unittest

import numpy as np
from osgeo import gdal, ogr

from ppe_qgis.plant_count import (SPACING_MIN_GAPS, auto_split, confident_range, count_plants, flag_row_weeds, merge_points,
                                  plant_spacing, read_points, split_part, summarize_points)
try:
    from test_v43 import tif
except ImportError:  # run as tests.test_v47
    from tests.test_v43 import tif

gdal.UseExceptions()


def rows_field(n_x=400, touching=3, big_single=True, row_weeds=()):
    """Four crop rows 45 px (cm) apart, plants every 20 px; in row 2 `touching` neighbours grown big enough
    to touch; in row 4 one big plant on its own."""
    h, w = 230, n_x; yy, xx = np.mgrid[0:h, 0:w]; m = np.zeros((h, w), np.uint8); truth = []
    for r, y in enumerate((45, 90, 135, 180)):
        for i, x in enumerate(range(15, w - 10, 20)):
            rad = 4
            if r == 1 and 6 <= i < 6 + touching: rad = 11
            if r == 3 and i == 8 and big_single: rad = 9
            m[(xx - x) ** 2 + (yy - y) ** 2 <= rad * rad] = 1; truth.append((x, y, rad))
    for x, y in row_weeds:                                   # small weeds inside a row, near a plant
        m[(xx - x) ** 2 + (yy - y) ** 2 <= 4] = 1
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


    def test_confident_plants_only(self):
        """Leaf pieces next to plants make false short gaps: with all plants the spacing comes out short, with
        confident plants (normal size for the image) it is right."""
        px, py, area = [], [], []
        for d in (0, 45, 90):
            for i, u in enumerate(np.arange(0, 400, 20.0)):
                px.append(u); py.append(d); area.append(50.0 + (i % 5))
                if i % 2 == 0: px.append(u + 6); py.append(d); area.append(6.0)      # a leaf counted on its own
        px = np.array(px) + 1000; py = np.array(py) + 1000; area = np.array(area)
        s_all, _ = plant_spacing(px, py, 90.0, 45, 4000)
        med, lo, hi = confident_range(area)
        use = (area >= lo) & (area <= hi)
        s_conf, gaps = plant_spacing(px, py, 90.0, 45, 4000, use)
        self.assertLess(s_all, 17)
        self.assertAlmostEqual(s_conf, 20.0, places=6)
        self.assertGreaterEqual(gaps, SPACING_MIN_GAPS)
        self.assertFalse(any(use[area < 10]))
        tight = confident_range([50, 51, 49, 50, 52]); wide = confident_range([20, 35, 50, 80, 120])
        self.assertLess(tight[2] - tight[1], wide[2] - wide[1])                       # follows the image's spread


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

    def test_weeds_inside_the_row(self):
        tmp = tempfile.mkdtemp()
        try:
            m, _ = rows_field(row_weeds=((224, 45), (284, 135)))          # 9 px (cm) along the row from plants at 215 and 275
            mask = tif(os.path.join(tmp, "mask.tif"), m, gdal.GDT_Byte, nodata=255)
            r = count_plants({"mask_path": mask, "output_dir": os.path.join(tmp, "c"), "min_plant_cm2": 0.05, "join_cm": 0, "find_rows": True})
            self.assertEqual(r["weeds_in_row"], 2)
            self.assertIn("closer than 10.0 cm", r["row_weeds"]["note"])
            ds = ogr.Open(r["points_path"]); L = ds.GetLayer()
            flagged = sorted(round((f.GetGeometryRef().GetX() - 500000) / 0.01) for f in L if str(f.GetField("weed_reason") or "").startswith("in the row"))
            ds = None
            self.assertEqual(flagged, [224, 284])
            with open(r["summary_path"]) as fh: head = fh.readline()
            self.assertIn("of_which_weeds_in_row", head)
            r2 = count_plants({"mask_path": mask, "output_dir": os.path.join(tmp, "d"), "min_plant_cm2": 0.05, "join_cm": 0, "find_rows": True,
                               "row_weeds": False})
            self.assertEqual(r2["weeds_in_row"], 0); self.assertEqual(r2["plants"], r["plants"] + 2)
            self.assertEqual(flag_row_weeds(r2["points_path"])["flagged"], 2)    # can be run again on saved points
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


class WeedPixels(unittest.TestCase):
    def test_mark_weeds_puts_weed_pixels_in_the_findings(self):
        from ppe_qgis.plant_count import mark_weeds, WEED_IN_ROW_CODE
        tmp = tempfile.mkdtemp()
        try:
            m, _ = rows_field(row_weeds=((224, 45), (284, 135)))
            vote = tif(os.path.join(tmp, "vote.tif"), m.astype(np.float32), gdal.GDT_Float32)
            find = tif(os.path.join(tmp, "f.tif"), np.zeros(m.shape, np.uint8), gdal.GDT_Byte, nodata=255)
            r = mark_weeds(vote, find, 0.5, tmp, cfg={"min_plant_cm2": 0.05, "join_cm": 0})
            self.assertEqual(r["in_row"], 2)
            f = gdal.Open(find).ReadAsArray()
            self.assertEqual(int((f == WEED_IN_ROW_CODE).sum()), int(((f == WEED_IN_ROW_CODE) & (m == 1)).sum()))
            self.assertEqual(f[45, 224], WEED_IN_ROW_CODE); self.assertEqual(f[135, 284], WEED_IN_ROW_CODE)
            self.assertEqual(f[45, 215], 0)                               # the crop plant next to it stays
            self.assertFalse(os.path.exists(os.path.join(tmp, "_ppe_weeds_tmp")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class CropOnlyMask(unittest.TestCase):
    def test_reviewed_points_make_the_crop_only_mask(self):
        from ppe_qgis.plant_count import crop_only_mask
        tmp = tempfile.mkdtemp()
        try:
            m, _ = rows_field(row_weeds=((224, 45), (284, 135)))
            mask = tif(os.path.join(tmp, "mask.tif"), m, gdal.GDT_Byte, nodata=255)
            r = count_plants({"mask_path": mask, "output_dir": os.path.join(tmp, "c"), "min_plant_cm2": 0.05, "join_cm": 0, "find_rows": True})
            c = crop_only_mask(r["points_path"]); out = gdal.Open(c["mask_path"]).ReadAsArray()
            self.assertEqual(out[45, 224], 0); self.assertEqual(out[135, 284], 0)      # the two weeds are out
            self.assertEqual(out[45, 215], 1); self.assertEqual(int((out == 1).sum()), int((m == 1).sum()) - c["pixels_left_out"])
            self.assertEqual(c["pieces_left_out"], 2)
            gt = gdal.Open(mask).GetGeoTransform(); removed = None
            ds = ogr.Open(r["points_path"], 1); L = ds.GetLayer()       # the review: one weed is crop after all, one crop point is removed
            for f in list(L):
                g = f.GetGeometryRef(); col = int((g.GetX() - gt[0]) / gt[1]); row = int((g.GetY() - gt[3]) / gt[5])
                if col == 224: f.SetField("status", "plant"); L.SetFeature(f)
                elif removed is None and f.GetField("status") == "plant" and col < 100: L.DeleteFeature(f.GetFID()); removed = (row, col)
            ds = None
            c2 = crop_only_mask(r["points_path"]); out2 = gdal.Open(c2["mask_path"]).ReadAsArray()
            self.assertEqual(out2[45, 224], 1)                       # switched back to plant: kept
            self.assertEqual(out2[135, 284], 0)                      # still weed?: out
            self.assertEqual(out2[removed], 0)                       # point removed: its piece is out
            self.assertEqual(c2["pieces_left_out"], 2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_older_count_without_pieces_says_so(self):
        from ppe_qgis.plant_count import crop_only_mask
        tmp = tempfile.mkdtemp()
        try:
            m, _ = rows_field(); mask = tif(os.path.join(tmp, "mask.tif"), m, gdal.GDT_Byte, nodata=255)
            r = count_plants({"mask_path": mask, "output_dir": os.path.join(tmp, "c"), "min_plant_cm2": 0.05, "join_cm": 0, "keep_groups": False})
            with self.assertRaises(ValueError): crop_only_mask(r["points_path"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
