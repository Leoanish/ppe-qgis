"""4.6: create labels - clear-pixel rule, cLHS spread, buffers that never cross into the other class,
the user's checks (switched and added points), and the slider preview."""
import json
import math
import os
import shutil
import tempfile
import unittest

import numpy as np
from osgeo import gdal, ogr

from ppe_qgis.label_points import (EDGE_PX, buffer_preview, clear_classes, clhs, create_points, read_points, run_and_create, write_labels)
try:
    from test_v43 import field, tif
except ImportError:  # run as tests.test_v46
    from tests.test_v43 import field, tif

gdal.UseExceptions()


class ClearPixels(unittest.TestCase):
    def test_only_sure_unflagged_pixels_away_from_edges(self):
        mask = np.zeros((9, 9), np.uint8); mask[2:7, 2:7] = 1
        vote = np.where(mask == 1, 1.0, 0.0).astype(np.float32); vote[4, 2] = 0.6      # uncertain plant pixel
        f = np.zeros((9, 9), np.uint8); f[3, 3] = 1                                     # PPE flagged a plant part
        p, s = clear_classes(mask, vote, f, edge=0)
        self.assertFalse(p[4, 2]); self.assertFalse(p[3, 3]); self.assertTrue(p[4, 4]); self.assertTrue(s[0, 0])
        p, s = clear_classes(mask, vote, None, edge=1)
        self.assertFalse(p[2, 4])                    # edge of the plant
        self.assertFalse(s[1, 4])                    # soil touching the plant
        self.assertTrue(p[4, 4]); self.assertTrue(s[0, 0] is np.False_ or not s[0, 0])  # image border counts as outside
        self.assertFalse((p & s).any())

    def test_clhs_spreads_better_than_random(self):
        rng = np.random.default_rng(0); X = np.column_stack([rng.lognormal(0, 1, 5000), rng.normal(0, 1, 5000), rng.gamma(1, 1, 5000)])
        n = 100

        def empty_share(idx):
            e = 0
            for j in range(X.shape[1]):
                q = np.quantile(X[:, j], np.linspace(0, 1, n + 1)[1:-1]); c = np.bincount(np.searchsorted(q, X[idx, j], side="right"), minlength=n)
                e += (c == 0).sum()
            return e / (n * X.shape[1])
        idx = clhs(X, n, seed=1)
        self.assertEqual(len(set(idx.tolist())), n)
        self.assertLess(empty_share(idx), 0.75 * empty_share(rng.choice(len(X), n, replace=False)))


class CreateAndBuffer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(); bands, plant, weeds = field(240)
        cls.img = tif(os.path.join(cls.tmp, "img.tif"), bands, gdal.GDT_Float32)
        vote = np.where(plant | weeds, 1.0, 0.0).astype(np.float32)
        cls.mask = tif(os.path.join(cls.tmp, "mask.tif"), (plant | weeds).astype(np.uint8), gdal.GDT_Byte, nodata=255)
        cls.vote = tif(os.path.join(cls.tmp, "vote.tif"), vote, gdal.GDT_Float32)
        cls.green = plant | weeds
        cls.out = os.path.join(cls.tmp, "labels")
        cls.info = create_points({"source_mode": "stacked", "stacked_path": cls.img, "mask_path": cls.mask, "vote_path": cls.vote, "findings_path": "",
                                  "n_points": 60, "plant_share": 0.5, "output_dir": cls.out})

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_points_sit_on_clear_pixels_of_their_class(self):
        pts = read_points(self.info["points_path"])
        self.assertEqual(sum(p["label"] == 1 for p in pts), 30); self.assertEqual(len(pts), 60)
        for p in pts:
            y, x = p["py"], p["px"]; win = self.green[y - EDGE_PX:y + EDGE_PX + 1, x - EDGE_PX:x + EDGE_PX + 1]
            self.assertTrue(win.all() if p["label"] == 1 else not win.any())

    def test_buffers_never_cross_into_the_other_class_and_preview_matches(self):
        pts = read_points(self.info["points_path"]); pre = buffer_preview(self.info, pts, 10.0)
        r = write_labels({"points_path": self.info["points_path"], "radius_cm": 10.0})
        lab = gdal.Open(r["raster_path"]).ReadAsArray()
        self.assertTrue(self.green[lab == 1].all()); self.assertFalse(self.green[lab == 0].any())
        self.assertGreater(r["plant_pixels"], 60); self.assertGreater(r["soil_pixels"], 60)
        self.assertGreaterEqual(pre["plant"], r["plant_pixels"]); self.assertGreaterEqual(pre["soil"], r["soil_pixels"])   # preview counts overlaps twice
        with open(r["csv_path"]) as f: rows = sum(1 for _ in f) - 1
        self.assertEqual(rows, r["plant_pixels"] + r["soil_pixels"])

    def test_quick_map_buffers_match_the_export(self):
        a = write_labels({"points_path": self.info["points_path"], "radius_cm": 8.0, "preview": True})
        b = write_labels({"points_path": self.info["points_path"], "radius_cm": 8.0})
        self.assertTrue(a["buffers_path"].endswith("ppe_label_buffers_preview.gpkg"))
        self.assertEqual((a["plant_pixels"], a["soil_pixels"]), (b["plant_pixels"], b["soil_pixels"]))
        def n(p):
            ds = ogr.Open(p); return ds.GetLayer().GetFeatureCount()
        self.assertEqual(n(a["buffers_path"]), n(b["buffers_path"]))

    def test_buffers_stay_in_one_piece_and_fuller_mode_takes_more(self):
        from ppe_qgis.label_points import _Rasters, _same_class
        R = _Rasters(self.mask, self.vote, ""); pts = read_points(self.info["points_path"])
        for p in pts[:10]:
            m, d, x0, y0, ok = _same_class(R, p["px"], p["py"], p["label"], 10.0)
            sel = m & (d <= 10.0)
            seen = np.zeros_like(sel); seen[p["py"] - y0, p["px"] - x0] = True       # flood from the point: reaches every buffer pixel
            for _ in range(60):
                p2 = np.pad(seen, 1); g = np.zeros_like(seen)
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1): g |= p2[1 + dy:1 + dy + seen.shape[0], 1 + dx:1 + dx + seen.shape[1]]
                seen = g & sel
            self.assertTrue((seen == sel).all())
        a = write_labels({"points_path": self.info["points_path"], "radius_cm": 10.0, "preview": True, "buffer_mode": "sure"})
        b = write_labels({"points_path": self.info["points_path"], "radius_cm": 10.0, "preview": True, "buffer_mode": "class"})
        self.assertGreaterEqual(b["plant_pixels"] + b["soil_pixels"], a["plant_pixels"] + a["soil_pixels"])

    def test_cap_limits_pixels_per_point(self):
        r = write_labels({"points_path": self.info["points_path"], "radius_cm": 20.0, "max_px": 5})
        self.assertLessEqual(r["plant_pixels"] + r["soil_pixels"], 5 * 60)

    def test_switched_and_added_points_keep_only_their_own_pixel(self):
        path = os.path.join(self.tmp, "labels", "ppe_labels.gpkg"); backup = path + ".bak"; shutil.copy(path, backup)
        try:
            ds = ogr.Open(path, 1); L = ds.GetLayer(); f = L.GetNextFeature(); fid = f.GetFID(); old = f.GetField("label")
            f.SetField("label", "soil" if old == "plant" else "plant"); f.SetField("status", "changed"); L.SetFeature(f)
            nf = ogr.Feature(L.GetLayerDefn()); g = ogr.Geometry(ogr.wkbPoint); g.AddPoint_2D(500000 + 0.005, 3500000 - 0.005); nf.SetGeometry(g)
            nf.SetField("id", 999); nf.SetField("label", "plant"); nf.SetField("status", "added"); L.CreateFeature(nf); ds = None
            r = write_labels({"points_path": path, "radius_cm": 10.0})
            self.assertEqual(r["points"], 61); self.assertGreaterEqual(r["point_only"], 1)
            ds = ogr.Open(r["buffers_path"]); B = ds.GetLayer(); px = {b.GetField("id"): (b.GetField("pixels"), b.GetField("point_only")) for b in B}
            first = read_points(backup)[0]["id"]
            self.assertEqual(px[first], (1, 1))
        finally:
            shutil.move(backup, path)


    def test_area_keeps_points_and_buffers_inside(self):
        out = os.path.join(self.tmp, "area"); x0, y0, px = 500000, 3500000, 0.01      # left half of the 240 px image
        wkt = "POLYGON(({a} {b},{c} {b},{c} {d},{a} {d},{a} {b}))".format(a=x0, b=y0, c=x0 + 120 * px, d=y0 - 240 * px)
        info = create_points({"source_mode": "stacked", "stacked_path": self.img, "mask_path": self.mask, "vote_path": self.vote, "findings_path": "",
                              "n_points": 40, "plant_share": 0.5, "output_dir": out, "area_wkts": [wkt]})
        pts = read_points(info["points_path"]); self.assertEqual(len(pts), 40)
        self.assertTrue(all(p["px"] < 120 for p in pts))
        r = write_labels({"points_path": info["points_path"], "radius_cm": 15.0})
        lab = gdal.Open(r["raster_path"]).ReadAsArray()
        self.assertTrue((lab[:, 120:] == 255).all()); self.assertTrue((lab[:, :120] != 255).any())
        self.assertTrue(os.path.isfile(os.path.join(out, "ppe_labels_area.gpkg")))


    def test_one_click_runs_ppe_and_creates_points(self):
        out = os.path.join(self.tmp, "oneclick")
        info = run_and_create({"source_mode": "stacked", "stacked_path": self.img, "output_dir": out, "n_points": 30, "plant_share": 0.5})
        self.assertTrue(0 < info["threshold"] < 1); self.assertTrue(os.path.isfile(info["mask_path"]))
        pts = read_points(info["points_path"]); self.assertEqual(len(pts), 30)
        self.assertTrue(info["points_path"].startswith(os.path.join(out, "ppe_labels")))
        for p in pts: self.assertEqual(bool(self.green[p["py"], p["px"]]), p["label"] == 1)


    def test_export_to_another_folder_takes_the_checked_points(self):
        exp = os.path.join(self.tmp, "my export")
        r = write_labels({"points_path": self.info["points_path"], "radius_cm": 5.0, "export_dir": exp})
        self.assertEqual(r["export_dir"], exp)
        for name in ("ppe_labels.gpkg", "ppe_label_buffers.gpkg", "ppe_label_raster.tif", "ppe_label_pixels.csv", "ppe_labels_info.json"):
            self.assertTrue(os.path.isfile(os.path.join(exp, name)), name)
        self.assertEqual(len(read_points(os.path.join(exp, "ppe_labels.gpkg"))), len(read_points(self.info["points_path"])))


    def test_plant_counter_counts_only_inside_the_area(self):
        from ppe_qgis.plant_count import count_plants
        whole = count_plants({"mask_path": self.mask, "vote_path": self.vote, "output_dir": os.path.join(self.tmp, "count_all"), "min_plant_cm2": 0.05})
        x0, y0, px = 500000, 3500000, 0.01
        wkt = "POLYGON(({a} {b},{c} {b},{c} {d},{a} {d},{a} {b}))".format(a=x0, b=y0, c=x0 + 120 * px, d=y0 - 240 * px)
        half = count_plants({"mask_path": self.mask, "vote_path": self.vote, "output_dir": os.path.join(self.tmp, "count_half"), "min_plant_cm2": 0.05,
                             "area_wkts": [wkt]})
        self.assertGreater(whole["plants"], 0); self.assertLess(half["plants"], whole["plants"]); self.assertGreater(half["plants"], 0)
        ds = ogr.Open(half["points_path"]); xs = [f.GetGeometryRef().GetX() for f in ds.GetLayer()]
        self.assertTrue(all(x <= x0 + 120 * px + 0.07 for x in xs))          # a plant cut by the edge keeps its centre near it
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "count_half", "ppe_count_area.gpkg")))
        self.assertAlmostEqual(half["plants_per_m2"] * (1.2 * 2.4), half["plants"], delta=0.5)   # per m² of the area, not of the image


if __name__ == "__main__":
    unittest.main()


class AddOrLeaveOutAnyKind(unittest.TestCase):
    """4.6.5: every kind PPE finds can be left out (-) or added (+)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        vote = np.full((20, 20), 0.9, np.float32); vote[:, 10:] = 0.2        # left half plant, right half soil at 0.5
        f = np.zeros((20, 20), np.uint8); f[0:5, :] = 3; f[5:10, :] = 2      # shaded leaves and leaf edges, both halves
        cls.vote = tif(os.path.join(cls.tmp, "vote.tif"), vote, gdal.GDT_Float32)
        cls.find = tif(os.path.join(cls.tmp, "find.tif"), f, gdal.GDT_Byte)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def apply(self, clip, add):
        from ppe_qgis.native_engine import NativePPEEngine
        out = tempfile.mkdtemp(dir=self.tmp)
        r = NativePPEEngine(log=lambda m: None).apply({"vote_path": self.vote, "findings_path": self.find, "threshold": 0.5, "output_dir": out,
                                                       "clip_codes": clip, "add_codes": add})
        return r, gdal.Open(r["mask_path"]).ReadAsArray()

    def test_add_any_kind_below_the_threshold(self):
        r, m = self.apply([], [3])
        self.assertTrue((m[0:5, 10:] == 1).all())            # shaded leaves on the soil side were added
        self.assertTrue((m[5:, 10:] == 0).all())             # nothing else
        self.assertEqual(r["added_pixels"], 50); self.assertEqual(r["add_codes"], [3])

    def test_leave_out_and_add_together_and_leaving_out_wins(self):
        r, m = self.apply([2], [3, 2])
        self.assertTrue((m[5:10, :10] == 0).all())            # leaf edges left out of the plant side
        self.assertTrue((m[0:5, 10:] == 1).all())             # shaded leaves added
        self.assertEqual(r["add_codes"], [3]); self.assertEqual(r["clip_codes"], [2])

    def test_live_preview_shows_added_kinds_at_any_threshold(self):
        from ppe_qgis.layer_utils import preview_source
        path = preview_source(self.vote, self.find, [2], [3])
        a = gdal.Open(path).ReadAsArray()
        self.assertTrue((a[0:5, 10:] >= 2).all())            # added: above any threshold
        self.assertTrue((a[5:10, :] == 0).all())              # left out: below any threshold
        self.assertTrue(np.allclose(a[10:, :10], 0.9))        # untouched


class SplitTouchingPlants(unittest.TestCase):
    """4.6.6: a 'check' group of touching plants is split into one point per plant (lobes)."""

    def test_two_and_three_touching_plants(self):
        from ppe_qgis.plant_count import count_plants, read_points, split_group, suggest_split, summarize_points
        tmp = tempfile.mkdtemp()
        try:
            n = 200; yy, xx = np.mgrid[0:n, 0:n]; m = np.zeros((n, n), np.uint8)
            singles = [(20 + 30 * i, 25 + 30 * j) for i in range(6) for j in range(2)]   # twelve lone plants, radius 8
            pair = [(40, 130), (54, 130)]                                    # two plants touching: under the 'check' size, but can be split
            trio = [(140, 120), (153, 130), (140, 140)]                      # three plants touching: flagged 'check'

            for cx, cy in singles + pair + trio: m[(xx - cx) ** 2 + (yy - cy) ** 2 <= 64] = 1
            mask = tif(os.path.join(tmp, "mask.tif"), m, gdal.GDT_Byte, nodata=255)
            r = count_plants({"mask_path": mask, "output_dir": os.path.join(tmp, "c"), "min_plant_cm2": 0.05, "join_cm": 0})
            self.assertEqual(r["plants"], 14); self.assertEqual(r["flagged_big"], 1)
            pts, _ = read_points(r["points_path"]); px = lambda c: (500000 + (c[0] + .5) * 0.01, 3500000 - (c[1] + .5) * 0.01)
            for group, k, status in ((pair, 2, "plant"), (trio, 3, "check")):
                gx = np.mean([px(c)[0] for c in group]); gy = np.mean([px(c)[1] for c in group])
                ds = ogr.Open(r["points_path"]); f = min(ds.GetLayer(), key=lambda f: (f.GetGeometryRef().GetX() - gx) ** 2 + (f.GetGeometryRef().GetY() - gy) ** 2)
                self.assertEqual(f.GetField("status"), status)
                parts = split_group(r["points_path"], gx, gy, k, f.GetField("area_cm2"))
                self.assertEqual(len(parts), k)
                for c in group:                                              # each plant centre gets a point within 4 px
                    x, y = px(c)
                    self.assertLess(min(math.hypot(x - a, y - b) for a, b, _ in parts), 0.04)
            # a split point counts; the summary says what is counted
            ds = ogr.Open(r["points_path"], 1); L = ds.GetLayer(); f = ogr.Feature(L.GetLayerDefn())
            g = ogr.Geometry(ogr.wkbPoint); g.AddPoint_2D(500000.5, 3499999.5); f.SetGeometry(g); f.SetField("status", "split"); L.CreateFeature(f); ds = None
            t = summarize_points(r["points_path"]); self.assertEqual(t["plants"], 15); self.assertEqual(t["split_by_user"], 1)
            with open(t["summary_path"]) as fh: head = fh.readline()
            self.assertIn("of_which_check_counted_as_one", head)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
