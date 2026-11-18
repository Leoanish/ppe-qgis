"""Separate runs per crop area (4.0.0): inside each outline the vote comes from a run on that area's
pixels only, outside it is the whole-field vote; with the option off Run is exactly the 3.9 run."""
import os
import shutil
import tempfile
import unittest

import numpy as np
from osgeo import gdal, osr

from ppe_qgis.native_engine import NativePPEEngine

ORDER = ("blue", "green", "red", "re", "nir")
LEAF_A = dict(zip(ORDER, (0.030, 0.081, 0.035, 0.150, 0.540)))
LEAF_B = dict(zip(ORDER, (0.040, 0.070, 0.050, 0.170, 0.380)))     # a paler crop
SOIL = dict(zip(ORDER, (0.057, 0.108, 0.156, 0.193, 0.213)))
SIZE, PIX, X0, Y0 = 160, 0.05, 300000.0, 3500000.0


def box(c0, c1, r0, r1):
    x0, x1, y0, y1 = X0 + c0 * PIX, X0 + c1 * PIX, Y0 - r1 * PIX, Y0 - r0 * PIX
    return "POLYGON(({0} {2}, {1} {2}, {1} {3}, {0} {3}, {0} {2}))".format(x0, x1, y0, y1)


class AreaRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="ppe_areas_"); folder = os.path.join(cls.dir, "bands"); os.makedirs(folder)
        rng = np.random.default_rng(4)
        kind = np.zeros((SIZE, SIZE), np.uint8)                   # 0 soil, 1 crop A, 2 crop B
        rows = (np.arange(SIZE) // 8) % 2 == 0                    # crop rows with soil between
        kind[rows, :80] = 1; kind[rows, 80:] = 2
        srs = osr.SpatialReference(); srs.ImportFromEPSG(32617)
        for k in ORDER:
            values = np.choose(kind, [SOIL[k], LEAF_A[k], LEAF_B[k]]).astype(np.float32)
            values *= rng.normal(1.0, 0.05, values.shape).astype(np.float32)
            ds = gdal.GetDriverByName("GTiff").Create(os.path.join(folder, "field_{}.tif".format(k)), SIZE, SIZE, 1, gdal.GDT_Float32)
            ds.SetGeoTransform((X0, PIX, 0, Y0, 0, -PIX)); ds.SetProjection(srs.ExportToWkt()); ds.GetRasterBand(1).WriteArray(values); ds = None
        base = {"source_mode": "folder", "folder_path": folder, "ortho_type": "ms"}
        cls.areas = [{"name": "crop A", "wkt": box(4, 76, 4, 156)}, {"name": "crop B", "wkt": box(84, 156, 4, 156)}]
        cls.plain = NativePPEEngine().run(dict(base, output_dir=os.path.join(cls.dir, "o1"), work_dir=os.path.join(cls.dir, "w1")))
        cls.off = NativePPEEngine().run_by_area(dict(base, crop_areas=cls.areas, separate_areas=False,
                                                    output_dir=os.path.join(cls.dir, "o2"), work_dir=os.path.join(cls.dir, "w2")))
        cls.on = NativePPEEngine().run_by_area(dict(base, crop_areas=cls.areas, separate_areas=True,
                                                   output_dir=os.path.join(cls.dir, "o3"), work_dir=os.path.join(cls.dir, "w3")))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_option_off_is_the_plain_run(self):
        a = gdal.Open(self.plain["vote_path"]).ReadAsArray(); b = gdal.Open(self.off["vote_path"]).ReadAsArray()
        self.assertTrue(np.array_equal(a, b)); self.assertNotIn("area_runs", self.off)

    def test_each_area_has_its_own_run(self):
        runs = self.on["area_runs"]
        self.assertEqual([r["crop"] for r in runs], ["crop A", "crop B"])
        for run in runs:
            self.assertTrue(run["chosen_layers"]); self.assertIsNotNone(run["soil_pct"])
            self.assertGreater(run["soil_pct"], 30)                # rows with soil between them

    def test_vote_inside_from_area_outside_from_whole(self):
        v = gdal.Open(self.on["vote_path"]).ReadAsArray(); whole = gdal.Open(self.on["whole_vote_path"]).ReadAsArray()
        self.assertEqual(v.shape, whole.shape)
        outside = np.ones_like(v, bool); outside[4:156, 4:76] = False; outside[4:156, 84:156] = False
        self.assertTrue(np.array_equal(v[outside], whole[outside]))
        for run in self.on["area_runs"]:
            c0, r0, w, h = run["window"]; area = gdal.Open(run["vote_path"]).ReadAsArray()
            inner = (slice(r0 + 2, r0 + h - 2), slice(c0 + 2, c0 + w - 2))
            self.assertTrue(np.array_equal(v[inner], area[2:h - 2, 2:w - 2]))

    def test_mask_still_separates_plant_and_soil(self):
        v = gdal.Open(self.on["vote_path"]).ReadAsArray()
        rows = (np.arange(SIZE) // 8) % 2 == 0
        inner_plant = v[np.ix_(rows, np.arange(10, 150))][:, :]; inner_soil = v[np.ix_(~rows, np.arange(10, 150))]
        self.assertGreater((inner_plant >= 0.45).mean(), 0.9); self.assertLess((inner_soil[inner_soil >= 0] >= 0.45).mean(), 0.1)
