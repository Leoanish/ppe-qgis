"""Pale plant parts (3.9.0): tassels, flowers and heads inside the canopy are found and reported,
leaves, soil and leaf-soil mixed pixels at row edges are not, and the wording follows the crop and
the growth stage. Information only: the vote is not changed."""
import os
import shutil
import tempfile
import unittest

import numpy as np
from osgeo import gdal, osr

from ppe_qgis.native_engine import NativePPEEngine, fit_pale_plant, pale_plant_mask
from ppe_qgis.plant_parts import find_crop, likely_parts, stage_key

ORDER = ("blue", "green", "red", "re", "nir")
LEAF = dict(zip(ORDER, (0.030, 0.081, 0.035, 0.150, 0.540)))      # Kritis medians
SOIL = dict(zip(ORDER, (0.057, 0.108, 0.156, 0.193, 0.213)))
TASSEL = dict(zip(ORDER, (0.042, 0.090, 0.076, 0.161, 0.486)))
MIX = {k: 0.7 * LEAF[k] + 0.3 * SOIL[k] for k in ORDER}            # spectrally almost a tassel
SIZE, PIX = 200, 0.02


class PlantPartsTableTests(unittest.TestCase):
    def test_crop_and_stage_wording(self):
        self.assertEqual(stage_key("Flowering / tasselling"), "flowering")
        self.assertEqual(stage_key(""), "any")
        self.assertIn("tassels", likely_parts("Corn hybrid A", "Flowering / tasselling")[0])
        self.assertIn("husks", likely_parts("maize", "Drying down / senescence")[0])
        self.assertIn("yellow flowers", likely_parts("Peanut", "Flowering / tasselling")[0])
        self.assertIn("heads", likely_parts("winter wheat", None)[0])
        parts, tip, known = likely_parts("dragon fruit", "Flowering / tasselling")
        self.assertFalse(known); self.assertIn("flowers", parts)
        self.assertIsNone(find_crop("cornflower"))           # whole words only


class PaleRuleTests(unittest.TestCase):
    def test_rule_separates_tassels_from_leaves_and_soil(self):
        rng = np.random.default_rng(1)
        sample = {k: np.concatenate([rng.normal(LEAF[k], LEAF[k] * 0.05, 3000), rng.normal(SOIL[k], SOIL[k] * 0.05, 3000)])
                  for k in ORDER}
        fit = fit_pale_plant(sample)
        self.assertIsNotNone(fit)
        self.assertGreater(fit["nir_floor"], 1.3 * SOIL["nir"]); self.assertLess(fit["nir_floor"], LEAF["nir"])
        bands = lambda d: {k: np.array([d[k]]) for k in ORDER}
        self.assertTrue(pale_plant_mask(bands(TASSEL), fit)[0])
        self.assertFalse(pale_plant_mask(bands(LEAF), fit)[0])
        self.assertFalse(pale_plant_mask(bands(SOIL), fit)[0])

    def test_no_reference_leaves_no_fit(self):
        rng = np.random.default_rng(2)
        sample = {k: rng.normal(SOIL[k], SOIL[k] * 0.05, 3000) for k in ORDER}
        self.assertIsNone(fit_pale_plant(sample))


class PaleEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="ppe_pale_"); folder = os.path.join(cls.dir, "bands"); os.makedirs(folder)
        rng = np.random.default_rng(3)
        kind = np.zeros((SIZE, SIZE), np.uint8)               # 0 leaf, 1 soil, 2 tassel, 3 leaf-soil mix
        kind[:, :60] = 1
        kind[:, 60:63] = 3                                    # the row edge, next to soil
        cls.tassels = np.zeros((SIZE, SIZE), bool)
        for r in range(10, SIZE - 10, 15):
            for c in range(110, SIZE - 10, 15):
                cls.tassels[r:r + 3, c:c + 3] = True
        kind[cls.tassels] = 2
        cls.kind = kind
        srs = osr.SpatialReference(); srs.ImportFromEPSG(32617)
        for k in ORDER:
            values = np.choose(kind, [LEAF[k], SOIL[k], TASSEL[k], MIX[k]]).astype(np.float32)
            values *= rng.normal(1.0, 0.04, values.shape).astype(np.float32)
            ds = gdal.GetDriverByName("GTiff").Create(os.path.join(folder, "field_{}.tif".format(k)), SIZE, SIZE, 1, gdal.GDT_Float32)
            ds.SetGeoTransform((300000.0, PIX, 0, 3500000.0, 0, -PIX)); ds.SetProjection(srs.ExportToWkt())
            ds.GetRasterBand(1).WriteArray(values); ds = None
        cfg = {"source_mode": "folder", "folder_path": folder, "ortho_type": "ms",
               "output_dir": os.path.join(cls.dir, "out"), "work_dir": os.path.join(cls.dir, "work")}
        cls.result = NativePPEEngine().run(cfg)
        cls.pale = gdal.Open(cls.result["pale_plant_path"]).ReadAsArray()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_tassels_inside_canopy_are_found(self):
        self.assertGreater((self.pale[self.tassels] == 1).mean(), 0.8)

    def test_edges_leaves_and_soil_are_not(self):
        self.assertLess((self.pale[self.kind == 3] == 1).mean(), 0.02)    # mixed pixels beside soil
        self.assertLess((self.pale[self.kind == 0] == 1).mean(), 0.01)
        self.assertEqual(int((self.pale[self.kind == 1] == 1).sum()), 0)

    def test_reported_share(self):
        self.assertGreater(self.result["pale_plant_pct"], 0.5)
        self.assertLess(self.result["pale_plant_pct"], 10)
        self.assertTrue(self.result["pale_plant_fit"])
