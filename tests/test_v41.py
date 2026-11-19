"""4.1: weak split warning (message only) and the confident-labels raster. The vote is never changed."""
import shutil
import unittest

import numpy as np
from osgeo import gdal

from ppe_qgis.native_engine import WEAK_SPLIT, separability
from test_findings_levels import LEAF, SOIL, SIZE, run_field


class SeparabilityTests(unittest.TestCase):
    def test_two_clear_groups_versus_one_blur(self):
        rng = np.random.default_rng(1)
        self.assertGreater(separability(np.r_[rng.normal(0.1, 0.02, 5000), rng.normal(0.8, 0.03, 5000)]), 0.9)
        self.assertLess(separability(rng.normal(0.3, 0.1, 10000)), WEAK_SPLIT)

    def test_few_or_flat_values(self):
        self.assertTrue(np.isnan(separability(np.array([0.1, 0.2]))))
        self.assertEqual(separability(np.full(100, 0.4)), 0.0)


class ClearFieldTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        kind = np.zeros((SIZE, SIZE), int); kind[:, : SIZE // 2] = 1      # left half leaves, right half soil
        cls.root, cls.result = run_field([SOIL, LEAF], kind, seed=3)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_no_warning_on_a_clear_field(self):
        self.assertEqual(self.result["weak_split_warning"], "")
        self.assertGreaterEqual(self.result["split_separability"], WEAK_SPLIT)

    def test_confident_labels_follow_the_vote(self):
        lab = gdal.Open(self.result["confident_labels_path"]).ReadAsArray()
        vote = gdal.Open(self.result["vote_path"]).ReadAsArray()
        self.assertTrue(np.all(lab[vote >= 1.0] == 1))
        self.assertTrue(np.all(lab[(vote >= 0) & (vote <= 0.1)] == 0))
        self.assertTrue(np.all(lab[(vote > 0.1) & (vote < 1.0)] == 255))
        self.assertGreater((lab[:, : SIZE // 2] == 1).mean(), 0.9)       # leaves labelled plant
        self.assertGreater((lab[:, SIZE // 2:] == 0).mean(), 0.9)        # soil labelled soil
        self.assertAlmostEqual(sum(self.result["confident_labels"][k] for k in ("plant_pct", "soil_pct", "unlabelled_pct")), 100.0, places=3)

    def test_labels_written_in_the_run_folder(self):
        self.assertTrue(self.result["confident_labels_path"].endswith("ppe_confident_labels.tif"))


if __name__ == "__main__":
    unittest.main()
