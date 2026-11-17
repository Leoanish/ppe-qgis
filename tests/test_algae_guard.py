"""Algae on wet soil: green, moderate NDVI, but no red-edge rise. Spectra taken from the
Tifton 2024-05-22 test field (median reflectance of leaves, bare soil and furrow algae)."""
import os
import tempfile
import unittest

import numpy as np
from osgeo import gdal

from ppe_qgis.native_engine import NativePPEEngine, compute_vi

LEAF = {"red": 0.009, "green": 0.015, "blue": 0.008, "re": 0.030, "nir": 0.060}
SOIL = {"red": 0.030, "green": 0.026, "blue": 0.020, "re": 0.040, "nir": 0.045}
ALGAE = {"red": 0.020, "green": 0.030, "blue": 0.015, "re": 0.050, "nir": 0.058}
NAMES = ("red", "green", "blue", "re", "nir")


def field(seed=2, noise=0.0012):
    """Plant rows, bare furrows, and algae in the furrows of the lower half."""
    rng = np.random.default_rng(seed)
    kind = np.zeros((200, 200), np.uint8)                 # 0 soil, 1 plant, 2 algae
    for x0 in range(0, 200, 40):
        kind[:, x0:x0 + 14] = 1
        kind[100:, x0 + 20:x0 + 34] = 2
    bands = {}
    for name in NAMES:
        values = np.choose(kind, [SOIL[name], LEAF[name], ALGAE[name]]).astype(np.float32)
        bands[name] = (values + rng.normal(0, noise, kind.shape)).clip(0.0005, 1).astype(np.float32)
    return bands, kind


class AlgaeGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp.cleanup()

    def run_engine(self, bands, **cfg):
        path = os.path.join(self.temp.name, "img.tif")
        ds = gdal.GetDriverByName("GTiff").Create(path, 200, 200, 5, gdal.GDT_Float32)
        for i, (name, label) in enumerate(zip(NAMES, ("Red", "Green", "Blue", "RedEdge", "NIR")), 1):
            ds.GetRasterBand(i).WriteArray(bands[name]); ds.GetRasterBand(i).SetDescription(label)
        ds = None
        out = os.path.join(self.temp.name, "out")
        result = NativePPEEngine().run(dict({"source_mode": "stacked", "stacked_path": path, "output_dir": out, "work_dir": out + "/work"}, **cfg))
        applied = NativePPEEngine().apply({"vote_path": result["vote_path"], "threshold": 0.5, "output_dir": out})
        return result, gdal.Open(applied["mask_path"]).ReadAsArray()

    def test_algae_looks_like_a_plant_to_ndvi_but_not_to_ndre(self):
        one = lambda spec: {k: np.array([v], np.float32) for k, v in spec.items()}
        ndvi = {n: float(compute_vi("ndvi", one(s))[0]) for n, s in (("leaf", LEAF), ("soil", SOIL), ("algae", ALGAE))}
        ndre = {n: float(compute_vi("ndre", one(s))[0]) for n, s in (("leaf", LEAF), ("soil", SOIL), ("algae", ALGAE))}
        self.assertGreater(ndvi["algae"], ndvi["soil"] + 0.2)          # NDVI lifts algae well above soil
        self.assertLess(abs(ndre["algae"] - ndre["soil"]), 0.03)       # NDRE leaves it at soil level
        self.assertGreater(ndre["leaf"], ndre["algae"] + 0.2)

    def test_guard_floor_sits_between_soil_and_plants(self):
        bands, kind = field()
        flat = {k: v.ravel() for k, v in bands.items()}
        guard = NativePPEEngine()._fit_algae_guard(flat)
        self.assertIsNotNone(guard)
        ndre = compute_vi("ndre", bands)
        self.assertGreater(guard["ndre_floor"], float(np.median(ndre[kind == 0])))
        self.assertLess(guard["ndre_floor"], float(np.percentile(ndre[kind == 1], 1)))
        rejected = NativePPEEngine._algae_guard_mask(bands, guard)
        self.assertLess(rejected[kind == 1].mean(), 0.001)             # no leaf is ever rejected
        self.assertGreater(rejected[kind == 2].mean(), 0.9)

    def test_engine_keeps_plants_and_drops_algae(self):
        bands, kind = field()
        result, mask = self.run_engine(bands, shadow_guard=False)
        self.assertTrue(result["biofilm_guard"])
        self.assertGreater((mask[kind == 1] == 1).mean(), 0.99)
        self.assertLess((mask[kind == 2] == 1).mean(), 0.02)
        self.assertLess((mask[kind == 0] == 1).mean(), 0.01)

    def test_guard_is_the_safety_net_when_more_layers_vote(self):
        """With all five spectral groups voting, visible greenness and red-edge/red side with
        the algae. The guard must still remove it; without the guard it gets through."""
        bands, kind = field()
        without = self.run_engine(bands, shadow_guard=False, biofilm_guard=False, max_layers=5)[1]
        guarded = self.run_engine(bands, shadow_guard=False, biofilm_guard=True, max_layers=5)[1]
        self.assertGreater((without[kind == 2] == 1).mean(), 0.5)
        self.assertLess((guarded[kind == 2] == 1).mean(), 0.02)
        self.assertGreater((guarded[kind == 1] == 1).mean(), 0.99)

    def test_pale_plants_in_a_dense_field_are_not_taken_for_algae(self):
        """IH, 6 June 2023: most plots are dense and vigorous, a few are pale. Pale plants have a
        weaker red-edge rise than the field's typical plant; the guard must still keep them.
        Median reflectance of soil, vigorous leaf and pale leaf taken from that image."""
        soil = {"red": 0.135, "green": 0.082, "blue": 0.046, "re": 0.185, "nir": 0.252}
        leaf = {"red": 0.035, "green": 0.077, "blue": 0.025, "re": 0.169, "nir": 0.410}
        pale = {"red": 0.060, "green": 0.079, "blue": 0.033, "re": 0.172, "nir": 0.350}
        rng = np.random.default_rng(4); kind = np.zeros((200, 200), np.uint8)       # 0 soil, 1 vigorous, 2 pale
        kind[:, 10:] = 1; kind[:, ::9] = 0; kind[150:, 10:] = 2; kind[150:, ::3] = 0
        bands = {n: (np.choose(kind, [soil[n], leaf[n], pale[n]]) + rng.normal(0, 0.004, kind.shape)).clip(0.001, 1).astype(np.float32) for n in NAMES}
        guard = NativePPEEngine()._fit_algae_guard({k: v.ravel() for k, v in bands.items()})
        self.assertIsNotNone(guard)
        rejected = NativePPEEngine._algae_guard_mask(bands, guard)
        self.assertLess(rejected[kind == 2].mean(), 0.02)
        self.assertLess(rejected[kind == 1].mean(), 0.001)

    def test_guard_needs_red_edge(self):
        bands, _ = field()
        flat = {k: v.ravel() for k, v in bands.items() if k != "re"}
        self.assertIsNone(NativePPEEngine()._fit_algae_guard(flat))


if __name__ == "__main__":
    unittest.main()
