"""4.3/4.4: faster reading (identical results), training data, companion training script."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import numpy as np
from osgeo import gdal, ogr, osr

from ppe_qgis.native_engine import NativePPEEngine
from ppe_qgis.training_data import export_training_tiles, label_array

gdal.UseExceptions()
SRS = osr.SpatialReference(); SRS.ImportFromEPSG(32617)


def tif(path, arr, dtype, px=0.01, nodata=None):
    arr = arr if arr.ndim == 3 else arr[None]
    d = gdal.GetDriverByName("GTiff").Create(path, arr.shape[2], arr.shape[1], arr.shape[0], dtype, options=["INTERLEAVE=PIXEL", "COMPRESS=DEFLATE", "TILED=YES"])
    d.SetGeoTransform((500000, px, 0, 3500000, 0, -px)); d.SetProjection(SRS.ExportToWkt())
    for i in range(arr.shape[0]):
        d.GetRasterBand(i + 1).WriteArray(arr[i])
        if nodata is not None: d.GetRasterBand(i + 1).SetNoDataValue(nodata)
    d = None
    return path


def field(n=300, seed=1):
    """A 5-band field: 4 rows of round plants (radius 6 px) 60 px apart, weeds between rows, soil."""
    rng = np.random.default_rng(seed); yy, xx = np.mgrid[0:n, 0:n]; plant = np.zeros((n, n), bool); weeds = np.zeros((n, n), bool)
    for r in range(4):
        y = 30 + 60 * r
        for x in range(20, n - 10, 30): plant |= (yy - y) ** 2 + (xx - x) ** 2 <= 36
        for x in range(35, n - 10, 70): weeds |= (yy - y - 30) ** 2 + (xx - x) ** 2 <= 16
    green = plant | weeds
    red = np.where(green, 0.05, 0.25) + rng.normal(0, 0.01, (n, n)); nir = np.where(green, 0.5, 0.3) + rng.normal(0, 0.01, (n, n))
    gr = np.where(green, 0.12, 0.2) + rng.normal(0, 0.01, (n, n)); bl = np.where(green, 0.04, 0.15) + rng.normal(0, 0.01, (n, n))
    re = np.where(green, 0.3, 0.27) + rng.normal(0, 0.01, (n, n))
    return np.stack([bl, gr, red, re, nir]).astype(np.float32), plant, weeds


class SpeedSameResults(unittest.TestCase):
    def test_reading_bands_together_gives_the_same_values(self):
        root = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, root)
        img, _p, _w = field(); path = tif(os.path.join(root, "f.tif"), img, gdal.GDT_Float32)
        e = NativePPEEngine(); e._resolve({"source_mode": "stacked", "stacked_path": path, "output_dir": root, "work_dir": root})
        names = list(e.sources); together = e._read_bands(names, y=10, h=50)
        for n in names: np.testing.assert_array_equal(together[n], e._read_band(n, y=10, h=50))


class TrainingDataTests(unittest.TestCase):
    def test_label_rule(self):
        vote = np.zeros((40, 40), np.float32); f = np.zeros((40, 40), np.uint8)
        vote[5:15, 5:15] = 1.0; vote[20:23, 20:23] = 1.0; vote[30:35, 30:35] = 0.6; f[5:7, 5:15] = 4; vote[0, 0] = np.nan
        y = label_array(vote, f)
        self.assertEqual(y[10, 10], 1); self.assertEqual(y[5, 10], 0)          # unclear (code 4) -> soil
        self.assertEqual(y[21, 21], 255)                                      # 9-pixel speck -> unsure
        self.assertEqual(y[32, 32], 255); self.assertEqual(y[38, 2], 0); self.assertEqual(y[0, 0], 255)

    def test_export_keeps_data_type_and_splits(self):
        root = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, root)
        img, plant, weeds = field(n=512); u16 = np.round(img * 10000).astype(np.uint16)
        path = tif(os.path.join(root, "f.tif"), u16, gdal.GDT_UInt16)
        vote = tif(os.path.join(root, "v.tif"), np.where(plant | weeds, 1.0, 0.0).astype(np.float32), gdal.GDT_Float32)
        find = tif(os.path.join(root, "c.tif"), np.zeros((512, 512), np.uint8), gdal.GDT_Byte)
        m = export_training_tiles({"items": [{"name": "f", "source": {"source_mode": "stacked", "stacked_path": path}, "vote_path": vote, "findings_path": find}],
                                   "output_dir": os.path.join(root, "td"), "tile": 128, "min_labelled": 0.1})
        self.assertEqual(len(m["tiles"]), 16); self.assertGreater(m["val"], 0); self.assertGreater(m["train"], m["val"])
        t = gdal.Open(os.path.join(root, "td", "images", m["tiles"][0]["tile"] + ".tif"))
        self.assertEqual(t.RasterCount, 5); self.assertEqual(t.GetRasterBand(1).DataType, gdal.GDT_UInt16)
        scale = m["images"][0]["value_scale"]; tile = m["tiles"][0]["tile"]; y0, x0 = int(tile.split("_")[-2]), int(tile.split("_")[-1])
        np.testing.assert_array_equal(t.ReadAsArray(), u16[:, y0:y0 + 128, x0:x0 + 128]); self.assertGreater(scale, 1)
        self.assertTrue(os.path.isfile(os.path.join(root, "td", "README.txt")))


@unittest.skipUnless(__import__("importlib").util.find_spec("torch"), "PyTorch not installed")
class CompanionScriptTests(unittest.TestCase):
    def test_train_predict_check_update(self):
        root = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, root)
        img, plant, weeds = field(n=256); path = tif(os.path.join(root, "f.tif"), img, gdal.GDT_Float32)
        vote = tif(os.path.join(root, "vote_fraction.tif"), np.where(plant | weeds, 1.0, 0.0).astype(np.float32), gdal.GDT_Float32)
        find = tif(os.path.join(root, "ppe_findings.tif"), np.zeros((256, 256), np.uint8), gdal.GDT_Byte)
        export_training_tiles({"items": [{"name": "f", "source": {"source_mode": "stacked", "stacked_path": path}, "vote_path": vote, "findings_path": find}],
                               "output_dir": os.path.join(root, "td"), "tile": 64, "min_labelled": 0.1})
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ppe_qgis", "tools", "ppe_train.py")
        run = lambda *a: subprocess.run([sys.executable, script] + list(a), capture_output=True, text=True, timeout=600)
        model = os.path.join(root, "m.pt")
        r = run("train", os.path.join(root, "td"), "--out", model, "--iters", "40"); self.assertEqual(r.returncode, 0, r.stderr); self.assertTrue(os.path.isfile(model))
        r = run("predict", model, path, "--out", os.path.join(root, "pred.tif")); self.assertEqual(r.returncode, 0, r.stderr)
        p = gdal.Open(os.path.join(root, "pred.tif")).ReadAsArray(); self.assertEqual(p.shape, (256, 256))
        r = run("check", model, path, root); self.assertIn(r.returncode, (0, 1), r.stderr); self.assertTrue("OK" in r.stdout or "CHECK" in r.stdout)
        r = run("update", model, os.path.join(root, "td"), os.path.join(root, "td"), "--iters", "20"); self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(root, "m_updated.pt")))


if __name__ == "__main__":
    unittest.main()
