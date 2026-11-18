"""4.2: What PPE found follows the image's own green-leaf level, and finds dry or yellowing plant
parts inside the canopy. The vote is never changed."""
import os
import shutil
import tempfile
import unittest

import numpy as np
from osgeo import gdal, osr

from ppe_qgis.native_engine import (NativePPEEngine, finding_shares, fit_levels, level_cut,
                                    FINDING_NAMES, REFERENCE_LEAF_NDVI)

ORDER = ("blue", "green", "red", "re", "nir")
SIZE, PIX = 200, 0.02
S = lambda *v: dict(zip(ORDER, v))
LEAF = S(0.030, 0.081, 0.035, 0.150, 0.540)          # Kritis medians: NDVI 0.88
SOIL = S(0.057, 0.108, 0.156, 0.193, 0.213)
DRY = S(0.075, 0.150, 0.150, 0.260, 0.400)            # drying leaf: NDVI 0.45, red as bright as green
MIX = {k: 0.7 * LEAF[k] + 0.3 * SOIL[k] for k in ORDER}
WEAK_LEAF = S(0.060, 0.130, 0.061, 0.180, 0.300)      # leaves at NDVI 0.66, as on Tifton
WEAK_SOIL = S(0.070, 0.090, 0.100, 0.110, 0.120)
WEAK_SHADE = {k: 0.45 * WEAK_LEAF[k] for k in ORDER}


def run_field(spectra, kind, seed):
    folder_root = tempfile.mkdtemp(prefix="ppe_levels_"); folder = os.path.join(folder_root, "bands"); os.makedirs(folder)
    rng = np.random.default_rng(seed); srs = osr.SpatialReference(); srs.ImportFromEPSG(32617)
    for k in ORDER:
        values = np.choose(kind, [s[k] for s in spectra]).astype(np.float32)
        values *= rng.normal(1.0, 0.04, values.shape).astype(np.float32)
        ds = gdal.GetDriverByName("GTiff").Create(os.path.join(folder, "field_{}.tif".format(k)), SIZE, SIZE, 1, gdal.GDT_Float32)
        ds.SetGeoTransform((300000.0, PIX, 0, 3500000.0, 0, -PIX)); ds.SetProjection(srs.ExportToWkt())
        ds.GetRasterBand(1).WriteArray(values); ds = None
    result = NativePPEEngine().run({"source_mode": "folder", "folder_path": folder, "ortho_type": "ms",
                                    "output_dir": os.path.join(folder_root, "out"), "work_dir": os.path.join(folder_root, "work")})
    return folder_root, result


class LevelRuleTests(unittest.TestCase):
    def test_leaf_side_cut_offs_scale_soil_side_do_not(self):
        weak = {"leaf_ndvi": 0.66, "soil_ndvi": 0.20}
        self.assertAlmostEqual(level_cut(weak, "leaf", 0.8), 0.8 * 0.66 / REFERENCE_LEAF_NDVI)
        self.assertEqual(level_cut(weak, "soil", 0.25), 0.25)               # bare soil looks alike everywhere
        self.assertEqual(level_cut(None, "leaf", 0.8), 0.8)                 # no levels: the fixed value
        self.assertAlmostEqual(level_cut({"leaf_ndvi": 0.88, "soil_ndvi": 0.15}, "plant", 0.6), 0.6, places=2)
        self.assertEqual(level_cut({"leaf_ndvi": 0.20, "soil_ndvi": 0.0}, "leaf", 0.8), 0.8 * 0.6)   # never below 0.6 x

    def test_levels_need_agreement(self):
        rng = np.random.default_rng(0)
        ndvi = np.concatenate([rng.normal(0.85, 0.02, 1000), rng.normal(0.15, 0.03, 1000)])
        vote = np.concatenate([np.ones(1000), np.zeros(1000)])
        lv = fit_levels(ndvi, vote)
        self.assertAlmostEqual(lv["leaf_ndvi"], 0.85, places=1); self.assertAlmostEqual(lv["soil_ndvi"], 0.15, places=1)
        self.assertIsNone(fit_levels(ndvi, np.full(2000, 0.5)))             # nobody agrees: fixed cut-offs

    def test_old_runs_keep_working(self):
        old = np.zeros((5, 1201), np.int64); old[0, 1000] = 90; old[3, 1000] = 10   # a 3.9 run: four kinds
        shares = finding_shares(old.tolist(), 0.5)
        self.assertEqual(set(shares), set(FINDING_NAMES)); self.assertAlmostEqual(shares[3], 10.0); self.assertEqual(shares[5], 0.0)


class WeakLeafImageTests(unittest.TestCase):
    """Leaves at NDVI 0.66: with the fixed 0.8 no leaf counted as green, so nothing was judged."""

    @classmethod
    def setUpClass(cls):
        kind = np.zeros((SIZE, SIZE), np.uint8); kind[:, :60] = 1
        cls.shaded = np.zeros((SIZE, SIZE), bool); cls.shaded[100:130, 80:100] = True; kind[cls.shaded] = 2
        cls.dir, cls.result = run_field([WEAK_LEAF, WEAK_SOIL, WEAK_SHADE], kind, 2)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_judged_and_shaded_leaves_found(self):
        self.assertTrue(self.result["findings_path"])
        codes = gdal.Open(self.result["findings_path"]).ReadAsArray()
        self.assertGreater((codes[self.shaded] == 3).mean(), 0.9)
        self.assertLess(self.result["findings_fit"]["levels"]["leaf_ndvi"], 0.7)


class DryPartsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        kind = np.zeros((SIZE, SIZE), np.uint8); kind[:, :60] = 1; kind[:, 60:63] = 3      # 0 leaf, 1 soil, 2 dry, 3 edge mix
        cls.dry = np.zeros((SIZE, SIZE), bool); cls.dry[90:110, 120:140] = True; kind[cls.dry] = 2
        cls.dry_on_soil = np.zeros((SIZE, SIZE), bool); cls.dry_on_soil[20:30, 20:30] = True; kind[cls.dry_on_soil] = 2
        cls.kind = kind
        cls.dir, cls.result = run_field([LEAF, SOIL, DRY, MIX], kind, 1)
        cls.codes = gdal.Open(cls.result["findings_path"]).ReadAsArray()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_dry_parts_inside_the_canopy_are_found(self):
        self.assertGreater((self.codes[self.dry] == 5).mean(), 0.8)

    def test_not_on_bare_soil_leaves_or_edges(self):
        self.assertEqual(int((self.codes[self.dry_on_soil] == 5).sum()), 0)    # beside bare soil: not a plant part
        self.assertEqual(int((self.codes[self.kind == 0] == 5).sum()), 0)      # green leaves
        self.assertEqual(int((self.codes[self.kind == 3] == 5).sum()), 0)      # leaf-soil mixes stay leaf edges
        self.assertEqual(int((self.codes[self.kind == 1] == 5).sum()), 0)

    def test_leave_out_dry_parts(self):
        out = os.path.join(self.dir, "out")
        base = {"vote_path": self.result["vote_path"], "threshold": 0.15, "output_dir": out, "findings_path": self.result["findings_path"]}
        plain = gdal.Open(NativePPEEngine().apply(dict(base))["mask_path"]).ReadAsArray().copy()
        clipped = NativePPEEngine().apply(dict(base, clip_codes=[5]))
        mask = gdal.Open(clipped["mask_path"]).ReadAsArray()
        expected = plain.copy(); expected[(plain == 1) & (self.codes == 5)] = 0
        self.assertTrue(np.array_equal(mask, expected))
        self.assertGreater(clipped["left_out"]["dry or yellowing plant parts"], 0)
        self.assertTrue(clipped["mask_path"].endswith("_without_dry.tif"))


if __name__ == "__main__":
    unittest.main()


FAINT_SHARE = 0.30


class SparseSpotsTests(unittest.TestCase):
    """4.0: in thin canopy, faint plants are marked (code 6) and can be added to the mask."""

    @classmethod
    def setUpClass(cls):
        from ppe_qgis.native_engine import NativePPEEngine as Engine
        rng = np.random.default_rng(7); N = 450
        kind = np.ones((N, N), np.uint8)                                   # 1 soil
        for c0 in range(0, 200, 12):
            kind[:, c0:c0 + 9] = 0                                         # dense half: wide rows of green leaves
        cls.faint = np.zeros((N, N), bool)
        for r in range(10, N - 10, 20):                                    # thin half: small, pale plants
            for c in range(260, N - 10, 40):
                kind[r:r + 4, c:c + 4] = 2; cls.faint[r:r + 4, c:c + 4] = True
        FAINT = {k: FAINT_SHARE * LEAF[k] + (1 - FAINT_SHARE) * SOIL[k] for k in ORDER}
        cls.dir = tempfile.mkdtemp(prefix="ppe_sparse_"); folder = os.path.join(cls.dir, "bands"); os.makedirs(folder)
        srs = osr.SpatialReference(); srs.ImportFromEPSG(32617)
        for k in ORDER:
            values = np.choose(kind, [LEAF[k], SOIL[k], FAINT[k]]).astype(np.float32) * rng.normal(1.0, 0.03, kind.shape).astype(np.float32)
            ds = gdal.GetDriverByName("GTiff").Create(os.path.join(folder, "f_{}.tif".format(k)), N, N, 1, gdal.GDT_Float32)
            ds.SetGeoTransform((300000.0, PIX, 0, 3500000.0, 0, -PIX)); ds.SetProjection(srs.ExportToWkt())
            ds.GetRasterBand(1).WriteArray(values); ds = None
        cls.out = os.path.join(cls.dir, "out")
        cls.result = Engine().run({"source_mode": "folder", "folder_path": folder, "ortho_type": "ms", "output_dir": cls.out,
                                   "work_dir": os.path.join(cls.dir, "work")})
        cls.codes = gdal.Open(cls.result["findings_path"]).ReadAsArray()
        cls.vote = gdal.Open(cls.result["vote_path"]).ReadAsArray()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_marked_only_below_the_threshold_and_in_thin_squares(self):
        s = self.result["sparse_spots"]; t = self.result["threshold"]
        self.assertGreater(s["pixels"], 0)
        marked = self.codes == 6
        self.assertTrue(np.all(self.vote[marked] < t - 0.05))                       # out of the mask at the suggestion
        self.assertTrue(np.all(self.vote[marked] >= s["vote_from"] - 1e-6))
        self.assertEqual(int(marked[:, :200].sum()), 0)                             # nothing in the dense half

    def test_add_to_mask_and_take_back(self):
        t = self.result["threshold"]
        base = {"vote_path": self.result["vote_path"], "threshold": t, "output_dir": self.out, "findings_path": self.result["findings_path"]}
        plain = gdal.Open(NativePPEEngine().apply(dict(base))["mask_path"]).ReadAsArray().copy()
        res = NativePPEEngine().apply(dict(base, add_codes=[6]))
        added = gdal.Open(res["mask_path"]).ReadAsArray()
        expected = plain.copy(); expected[(plain == 0) & (self.codes == 6)] = 1
        self.assertTrue(np.array_equal(added, expected))                            # exactly the marked pixels
        self.assertEqual(res["added_pixels"], int(((plain == 0) & (self.codes == 6)).sum()))
        self.assertTrue(res["mask_path"].endswith("_with_sparse.tif"))
        self.assertGreater(res["canopy_pct"], res["canopy_before_clip_pct"])
        back = gdal.Open(NativePPEEngine().apply(dict(base))["mask_path"]).ReadAsArray()
        self.assertTrue(np.array_equal(back, plain))

    def test_can_be_switched_off(self):
        from ppe_qgis.native_engine import mark_sparse_spots
        self.assertIn("vote_from", self.result["sparse_spots"])
        self.assertTrue(callable(mark_sparse_spots))
