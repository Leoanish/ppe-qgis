"""Band folders (3.9.0): the folder scan lists every GeoTIFF with the band PPE would read and why a
file would not be used; the run imports exactly the files the user ticked."""
import os
import shutil
import tempfile
import unittest

import numpy as np
from osgeo import gdal, osr

from ppe_qgis.native_engine import NativePPEEngine, describe_image, scan_band_folder

LEAF = {"blue": 0.03, "green": 0.08, "red": 0.04, "re": 0.25, "nir": 0.50}
SOIL = {"blue": 0.10, "green": 0.14, "red": 0.18, "re": 0.22, "nir": 0.26}


def write(path, values, size=None):
    bands = values[None] if values.ndim == 2 else values          # (bands, rows, columns)
    rows, cols = bands.shape[1:]
    ds = gdal.GetDriverByName("GTiff").Create(path, cols, rows, bands.shape[0], gdal.GDT_Float32)
    srs = osr.SpatialReference(); srs.ImportFromEPSG(32617)
    ds.SetGeoTransform((300000, 0.05, 0, 3500000, 0, -0.05)); ds.SetProjection(srs.ExportToWkt())
    for i in range(bands.shape[0]):
        ds.GetRasterBand(i + 1).WriteArray(bands[i])
    ds = None


class BandFolderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="ppe_folder_")
        rng = np.random.default_rng(2)
        plant = np.zeros((80, 80), bool); plant[20:60, 20:60] = True
        cls.band = {k: (np.where(plant, LEAF[k], SOIL[k]) + rng.normal(0, .004, plant.shape)).astype(np.float32) for k in LEAF}
        for k, name in (("blue", "field_blue.tif"), ("green", "field_green.tif"), ("red", "field_red.tif"), ("re", "field_red edge.tif")):
            write(os.path.join(cls.dir, name), cls.band[k])
        write(os.path.join(cls.dir, "IMG_0001_4.tif"), cls.band["nir"])                 # the NIR band, unreadable name
        write(os.path.join(cls.dir, "field_nir_old.tif"), cls.band["nir"] * 0.9)         # a second NIR file
        write(os.path.join(cls.dir, "field_thermal.tif"), np.full((80, 80), 30, np.float32))
        write(os.path.join(cls.dir, "field_rgb.tif"), np.stack([cls.band["red"], cls.band["green"], cls.band["blue"]]))
        write(os.path.join(cls.dir, "dsm.tif"), np.full((40, 40), 100, np.float32))       # other size, no band name
        with open(os.path.join(cls.dir, "field_red.tif.aux.xml"), "w") as handle: handle.write("<PAMDataset/>")
        with open(os.path.join(cls.dir, "plots.geojson"), "w") as handle: handle.write("{}")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_scan_lists_rasters_with_reasons(self):
        scan = scan_band_folder(self.dir)
        by = {e["file"]: e for e in scan["files"]}
        self.assertEqual(scan["other_files"], 2)                       # .aux.xml and .geojson are not listed
        self.assertEqual(set(by), {"field_blue.tif", "field_green.tif", "field_red.tif", "field_red edge.tif", "IMG_0001_4.tif",
                                   "field_nir_old.tif", "field_thermal.tif", "field_rgb.tif", "dsm.tif"})
        for name, band in (("field_blue.tif", "blue"), ("field_green.tif", "green"), ("field_red.tif", "red"),
                           ("field_red edge.tif", "re"), ("field_nir_old.tif", "nir")):
            self.assertEqual(by[name]["band"], band, name); self.assertTrue(by[name]["use"], name)
        self.assertFalse(by["IMG_0001_4.tif"]["use"]); self.assertIsNone(by["IMG_0001_4.tif"]["band"])
        self.assertIn("not recognised", by["IMG_0001_4.tif"]["note"])
        self.assertFalse(by["field_thermal.tif"]["use"]); self.assertIn("thermal", by["field_thermal.tif"]["note"])
        self.assertFalse(by["field_rgb.tif"]["use"]); self.assertIn("3 bands", by["field_rgb.tif"]["note"])
        self.assertFalse(by["dsm.tif"]["use"])

    def test_run_imports_only_the_ticked_files(self):
        ticked = {"blue": os.path.join(self.dir, "field_blue.tif"), "green": os.path.join(self.dir, "field_green.tif"),
                  "red": os.path.join(self.dir, "field_red.tif"), "re": os.path.join(self.dir, "field_red edge.tif"),
                  "nir": os.path.join(self.dir, "IMG_0001_4.tif")}
        cfg = {"source_mode": "folder", "folder_path": self.dir, "folder_bands": ticked}
        self.assertEqual(describe_image(cfg)["bands"], ["blue", "green", "red", "re", "nir"])
        out = os.path.join(self.dir, "run")
        result = NativePPEEngine().run(dict(cfg, output_dir=out, work_dir=out))
        self.assertEqual(result["band_detection"], "band files ticked in the folder")
        self.assertEqual(sorted(result["source_bands"]), sorted(ticked))
        mask = NativePPEEngine().apply({"vote_path": result["vote_path"], "threshold": 0.5, "output_dir": out})
        self.assertAlmostEqual(mask["canopy_pct"], 25.0, delta=1.0)

    def test_without_ticks_the_file_names_decide(self):
        # Without the dialog PPE goes by file names: here it would take the old NIR file and ignore
        # IMG_0001_4.tif, which is the right one. Ticking the files is how the user prevents that.
        cfg = {"source_mode": "folder", "folder_path": self.dir}
        engine = NativePPEEngine()
        engine._map_bands(cfg)
        self.assertEqual(os.path.basename(engine.sources["nir"][0]), "field_nir_old.tif")

if __name__ == "__main__":
    unittest.main()


class FileNameTests(unittest.TestCase):
    """A camera name inside a file name must not decide the band: the band word nearest the end does."""

    def test_camera_name_does_not_win(self):
        from ppe_qgis.native_engine import infer_band
        stem = "Field_2025-08-08_MicaSense_RedEdge-P_120ft_orthomosaic_reflectance_"
        for word, band in (("blue", "blue"), ("green", "green"), ("red", "red"), ("red edge", "re"), ("rededge", "re"), ("nir", "nir")):
            self.assertEqual(infer_band(stem + word + ".tif"), band, word)
        self.assertIsNone(infer_band(stem + "LWIR_thermal.tif"))

    def test_common_camera_names_still_read(self):
        from ppe_qgis.native_engine import infer_band
        for name, band in (("DJI_0010_MS_RE.TIF", "re"), ("DJI_0010_MS_G.TIF", "green"), ("sequoia_REG.tif", "re"),
                           ("sequoia_GRE.tif", "green"), ("ortho_Near-Infrared.tif", "nir"), ("band_717nm.tif", "re"),
                           ("Kritis_reflectance_red edge.tif", "re"), ("IMG_0001_4.tif", None)):
            self.assertEqual(infer_band(name), band, name)
