"""4.3: faster reading (identical results), plant counter, training data, companion training script."""
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
from ppe_qgis.plant_count import count_plants, count_batch
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
        for x in range(20, n - 10, 30): plant |= (yy - y) ** 2 + (xx - x - (7 * r) % 13) ** 2 <= 36   # rows staggered, as in a field
        for x in range(35, n - 10, 70):                           # weeds scattered between the rows, not on a line
            wy = y + 22 + int(rng.integers(0, 17)); wx = x + int(rng.integers(-10, 11)); weeds |= (yy - wy) ** 2 + (xx - wx) ** 2 <= 16
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


class CounterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp()
        img, plant, weeds = field(); cls.n_plants = 4 * len(range(20, 290, 30)); cls.n_weeds = 4 * len(range(35, 290, 70))
        m = (plant | weeds).astype(np.uint8)
        m[150:153, 5:8] = 1; m[200, 2] = 1                                   # specks
        hole = m.copy(); hole[30, 20] = 0                                    # a hole inside a plant changes nothing
        cls.mask = tif(os.path.join(cls.root, "mask.tif"), hole, gdal.GDT_Byte, nodata=255)
        vote = np.where(plant | weeds, 1.0, 0.0).astype(np.float32)
        cls.vote = tif(os.path.join(cls.root, "vote.tif"), vote, gdal.GDT_Float32)
        spot = m.copy(); spot[100:110, 250:260] = 1                         # soil let in by a low threshold: no core
        cls.mask_spot = tif(os.path.join(cls.root, "mask_spot.tif"), spot, gdal.GDT_Byte, nodata=255)

    @classmethod
    def tearDownClass(cls): shutil.rmtree(cls.root)

    def run_count(self, **kw):
        cfg = {"mask_path": self.mask, "output_dir": os.path.join(self.root, "out"), "min_plant_cm2": 10}; cfg.update(kw)
        return count_plants(cfg)

    def test_counts_every_group_above_the_smallest_plant(self):
        r = self.run_count(); self.assertEqual(r["plants"], self.n_plants + self.n_weeds)
        ds = ogr.Open(r["points_path"]); self.assertEqual(ds.GetLayer().GetFeatureCount(), r["plants"])

    def test_hole_filling_does_not_change_the_count(self):
        self.assertEqual(self.run_count(fill_holes_px=0)["plants"], self.run_count()["plants"])

    def test_spots_without_a_sure_core_are_not_counted(self):
        without = self.run_count(mask_path=self.mask_spot)["plants"]; with_core = self.run_count(mask_path=self.mask_spot, vote_path=self.vote)
        self.assertEqual(without, self.n_plants + self.n_weeds + 1)
        self.assertEqual(with_core["plants"], self.n_plants + self.n_weeds); self.assertEqual(with_core["no_core_left_out"], 1)

    def test_row_spacing_leaves_out_weeds_between_rows(self):
        r = self.run_count(find_rows=True)          # rows 60 px apart at 1 cm pixels
        self.assertEqual(r["plants"], self.n_plants); self.assertEqual(r["left_out_between_rows"], self.n_weeds)

    def test_weeds_between_rows_are_kept_as_points_but_not_counted(self):
        r = self.run_count(find_rows=True)
        ds = ogr.Open(r["points_path"]); st = [f.GetField("status") for f in ds.GetLayer()]; ds = None
        self.assertEqual(st.count("weed?"), self.n_weeds); self.assertEqual(r["weeds_not_counted"], self.n_weeds)

    def test_corrections_on_the_points_update_the_totals(self):
        from ppe_qgis.plant_count import summarize_points
        r = self.run_count(find_rows=True); path = r["points_path"]; before = r["plants"]
        ds = ogr.Open(path, 1); lay = ds.GetLayer(); feats = list(lay)
        lay.DeleteFeature(next(f for f in feats if f.GetField("status") == "plant").GetFID())           # remove a wrong point
        w = next(f for f in feats if f.GetField("status") == "weed?"); w.SetField("status", "plant"); lay.SetFeature(w)   # a weed? that is a plant
        f = ogr.Feature(lay.GetLayerDefn()); g = ogr.Geometry(ogr.wkbPoint); g.AddPoint_2D(500001.0, 3499999.0); f.SetGeometry(g)
        f.SetField("status", "added"); lay.CreateFeature(f); ds = None                                   # a missed plant
        t = summarize_points(path)
        self.assertEqual(t["plants"], before - 1 + 1 + 1); self.assertEqual(t["added_by_user"], 1); self.assertEqual(t["weeds_not_counted"], self.n_weeds - 1)
        self.assertIn(str(t["plants"]), open(t["summary_path"]).read())

    def test_counts_per_plot_and_batch_table(self):
        plots = os.path.join(self.root, "plots.geojson")
        f = lambda x0, x1, i: {"type": "Feature", "properties": {"id": i}, "geometry": {"type": "Polygon", "coordinates": [[[x0, 3500000], [x1, 3500000], [x1, 3499997], [x0, 3499997], [x0, 3500000]]]}}
        json.dump({"type": "FeatureCollection", "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::32617"}}, "features": [f(500000, 500001.5, "A"), f(500001.5, 500003, "B")]}, open(plots, "w"))
        r = self.run_count(aoi_path=plots, aoi_id_field="id")
        rows = list(__import__("csv").reader(open(r["plot_counts_path"])))
        self.assertEqual([x[0] for x in rows[1:]], ["A", "B"]); self.assertEqual(sum(int(x[1]) for x in rows[1:]), r["plants"])
        b = count_batch({"count_items": [{"name": "one", "mask_path": self.mask, "vote_path": self.vote}], "output_dir": self.root, "min_plant_cm2": 10})
        self.assertTrue(os.path.isfile(b["batch_counts_csv"])); self.assertEqual(b["plants"], r["plants"])


class LeavesOfOnePlantTests(unittest.TestCase):
    """4.3.1: a plant whose three leaves show as separate pieces (thin stem not in the mask) is one plant."""
    def test_three_leaves_one_plant_two_plants_stay_two(self):
        root = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, root)
        n = 200; yy, xx = np.mgrid[0:n, 0:n]; m = np.zeros((n, n), np.uint8)
        for cy, cx in ((50, 50), (150, 150)):                        # two plants, 140 px (14 cm at 1 mm) apart
            for dy, dx in ((-12, 0), (8, -11), (8, 11)):              # three leaves around the centre, ~3 px gaps
                m[((yy - cy - dy) ** 2 + (xx - cx - dx) ** 2) <= 36] = 1
        mask = tif(os.path.join(root, "m.tif"), m, gdal.GDT_Byte, px=0.001, nodata=255)
        vote = tif(os.path.join(root, "v.tif"), m.astype(np.float32), gdal.GDT_Float32, px=0.001)
        base = {"mask_path": mask, "vote_path": vote, "output_dir": os.path.join(root, "o"), "min_plant_cm2": 0.5}
        self.assertEqual(count_plants(dict(base, join_cm=0))["plants"], 6)          # without joining: every leaf counted
        self.assertEqual(count_plants(dict(base, join_cm=1.0))["plants"], 2)


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
