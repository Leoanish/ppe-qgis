"""4.2.1: on RGB-only images dark pixels are split by colour (shaded leaves vs unclear), and pure-black
pixels inside a camera photo are drawn black, not see-through. Images with NIR are unchanged."""
import os
import shutil
import tempfile
import unittest

import numpy as np
from osgeo import gdal, osr

from ppe_qgis.native_engine import NativePPEEngine, classify_findings


class ColourSplitTests(unittest.TestCase):
    def test_dark_green_is_shaded_leaf_dark_brown_is_unclear(self):
        # columns: dark green leaf, dark brown soil, bright leaf (not dark), dark green but no voter says plant
        r = np.array([[0.02, 0.06, 0.20, 0.02]], np.float32); g = np.array([[0.05, 0.04, 0.45, 0.05]], np.float32)
        b = np.array([[0.02, 0.03, 0.15, 0.02]], np.float32); vote = np.array([[0.8, 0.8, 1.0, 0.0]], np.float32)
        fit = {"dark_threshold": 0.12, "shaded_by_colour": True}
        code = classify_findings({"red": r, "green": g, "blue": b}, np.ones((1, 4), bool), fit, None, 0, vote)
        self.assertEqual(code.tolist(), [[3, 4, 0, 0]])

    def test_without_the_colour_split_everything_dark_is_unclear(self):
        r = np.array([[0.02, 0.06]], np.float32); g = np.array([[0.05, 0.04]], np.float32); b = np.array([[0.02, 0.03]], np.float32)
        code = classify_findings({"red": r, "green": g, "blue": b}, np.ones((1, 2), bool), {"dark_threshold": 0.12}, None, 0,
                                 np.array([[0.8, 0.8]], np.float32))
        self.assertEqual(code.tolist(), [[4, 4]])


class CameraPhotoTests(unittest.TestCase):
    """An 8-bit RGB photo with leaves, soil, shaded leaves, shaded soil and a few pure-black pixels."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="ppe_421_"); rng = np.random.default_rng(2); n = 240
        kind = np.zeros((n, n), np.uint8); kind[:, :80] = 1; kind[:, 80:110] = 2; kind[:, 110:140] = 3      # 0 soil, 1 leaf, 2 shaded leaf, 3 shaded soil
        colours = {0: (150, 115, 80), 1: (60, 140, 50), 2: (14, 34, 12), 3: (40, 30, 22)}
        rgb = np.zeros((3, n, n), np.float32)
        for k, c in colours.items():
            for i in range(3): rgb[i][kind == k] = c[i]
        rgb *= rng.normal(1, 0.05, rgb.shape).astype(np.float32); rgb = np.clip(rgb, 1, 255)
        cls.black = np.zeros((n, n), bool); cls.black[::17, 115:135:6] = True; rgb[:, cls.black] = 0      # deep-shadow specks
        srs = osr.SpatialReference(); srs.ImportFromEPSG(32617)
        path = os.path.join(cls.root, "photo.tif")
        ds = gdal.GetDriverByName("GTiff").Create(path, n, n, 3, gdal.GDT_Byte)
        ds.SetGeoTransform((300000.0, 0.001, 0, 3500000.0, 0, -0.001)); ds.SetProjection(srs.ExportToWkt())
        for i, nm in enumerate(("red", "green", "blue")):
            ds.GetRasterBand(i + 1).WriteArray(rgb[i].astype(np.uint8)); ds.GetRasterBand(i + 1).SetDescription(nm)
        ds = None
        cls.kind = kind
        cls.result = NativePPEEngine().run({"source_mode": "stacked", "stacked_path": path, "ortho_type": "rgb",
                                            "output_dir": os.path.join(cls.root, "out"), "work_dir": os.path.join(cls.root, "work")})

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_shaded_leaves_found_by_colour(self):
        f = gdal.Open(self.result["findings_path"]).ReadAsArray()
        self.assertIn("shaded leaves (by colour, no NIR) and unclear", self.result["findings_fit"]["checks"])
        self.assertNotIn(4, set(np.unique(f[self.kind == 2])) - {0})          # shaded leaves never called unclear
        self.assertNotIn(3, set(np.unique(f[self.kind == 3])))                 # shaded soil never called a shaded leaf

    def test_pure_black_inside_a_photo_is_drawn_black(self):
        d = gdal.Open(self.result["rgb_path"]).ReadAsArray()
        self.assertTrue(np.all(d[:, self.black] >= 1))                         # not the see-through 0


class BorderTests(unittest.TestCase):
    def test_a_black_border_stays_see_through(self):
        root = tempfile.mkdtemp(prefix="ppe_421b_")
        try:
            n = 200; rng = np.random.default_rng(3); srs = osr.SpatialReference(); srs.ImportFromEPSG(32617)
            rgb = np.zeros((3, n, n), np.float32); rgb[0] = 150; rgb[1] = 115; rgb[2] = 80
            rgb[0, :, :60] = 60; rgb[1, :, :60] = 140; rgb[2, :, :60] = 50
            rgb *= rng.normal(1, 0.05, rgb.shape).astype(np.float32); rgb[:, :, 170:] = 0       # 15 percent border
            path = os.path.join(root, "ortho.tif"); ds = gdal.GetDriverByName("GTiff").Create(path, n, n, 3, gdal.GDT_Byte)
            ds.SetGeoTransform((300000.0, 0.02, 0, 3500000.0, 0, -0.02)); ds.SetProjection(srs.ExportToWkt())
            for i in range(3): ds.GetRasterBand(i + 1).WriteArray(np.clip(rgb[i], 0, 255).astype(np.uint8))
            ds = None
            r = NativePPEEngine().run({"source_mode": "stacked", "stacked_path": path, "ortho_type": "rgb",
                                       "output_dir": os.path.join(root, "out"), "work_dir": os.path.join(root, "work")})
            d = gdal.Open(r["rgb_path"]).ReadAsArray()
            self.assertTrue(np.all(d[:, :, 175:] == 0))
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
