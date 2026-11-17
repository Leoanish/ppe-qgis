import os
import tempfile
import unittest

import numpy as np
from osgeo import gdal, ogr

from ppe_qgis.native_engine import CANONICAL, NativePPEEngine, dataset_wavelengths


class HSIProxyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "hsi.tif")
        driver = gdal.GetDriverByName("GTiff")
        ds = driver.Create(self.path, 40, 40, 46, gdal.GDT_Float32)
        rng = np.random.default_rng(42)
        common = rng.normal(0, 10, (40, 40)).astype("float32")
        windows = ((450, 520), (520, 600), (630, 690), (700, 750), (780, 900))
        for band in range(1, 47):
            wavelength = 450 + (band-1)*10
            in_window = any(lo <= wavelength <= hi for lo, hi in windows)
            # Even bands offer a lower-spread independent alternative to the
            # highest-spread correlated signal in each wavelength window.
            values = common + wavelength if in_window and band % 2 else rng.normal(wavelength, 6, (40, 40))
            ds.GetRasterBand(band).WriteArray(values.astype("float32"))
        ds = None

    def tearDown(self):
        self.temp.cleanup()

    def test_explicit_grid_and_correlation_aware_proxies(self):
        ds = gdal.Open(self.path)
        cfg = {"hsi_min_wavelength": 450, "hsi_max_wavelength": 900,
               "hsi_interval": 10, "hsi_grid_confirmed": True}
        wavelengths = dataset_wavelengths(ds, cfg)
        engine = NativePPEEngine()
        mapping = engine._wavelength_proxy_map(ds, wavelengths)
        self.assertEqual(set(mapping), set(CANONICAL))
        self.assertEqual(len(set(mapping.values())), 5)
        self.assertTrue(all(item["max_correlation"] <= .85 for item in engine.hsi_proxy_info[1:]))

    def test_inconsistent_grid_is_rejected(self):
        ds = gdal.Open(self.path)
        with self.assertRaisesRegex(ValueError, "calculated last band"):
            dataset_wavelengths(ds, {"hsi_min_wavelength": 450,
                                     "hsi_max_wavelength": 910,
                                     "hsi_interval": 10,
                                     "hsi_grid_confirmed": True})

    def test_multispectral_aoi_contains_raw_bands_and_indices(self):
        image = os.path.join(self.temp.name, "ms.tif")
        ds = gdal.GetDriverByName("GTiff").Create(image, 20, 20, 5, gdal.GDT_Float32)
        rng = np.random.default_rng(7)
        for index, name in enumerate(CANONICAL, 1):
            ds.GetRasterBand(index).SetDescription(name)
            ds.GetRasterBand(index).WriteArray(rng.uniform(.05, .8, (20, 20)).astype("float32"))
        ds = None
        mask_path = os.path.join(self.temp.name, "mask.tif")
        mask = gdal.GetDriverByName("GTiff").Create(mask_path, 20, 20, 1, gdal.GDT_Byte)
        mask.GetRasterBand(1).WriteArray(np.ones((20, 20), dtype="uint8")); mask = None
        aoi_path = os.path.join(self.temp.name, "ms_plots.geojson")
        vector = ogr.GetDriverByName("GeoJSON").CreateDataSource(aoi_path); layer = vector.CreateLayer("plots", geom_type=ogr.wkbPolygon)
        ring = ogr.Geometry(ogr.wkbLinearRing)
        for point in ((0, 0), (20, 0), (20, 20), (0, 20), (0, 0)): ring.AddPoint(*point)
        polygon = ogr.Geometry(ogr.wkbPolygon); polygon.AddGeometry(ring); feature = ogr.Feature(layer.GetLayerDefn()); feature.SetGeometry(polygon); layer.CreateFeature(feature); vector = None
        result = NativePPEEngine().export_aoi_statistics({"source_mode":"stacked", "stacked_path":image, "ortho_type":"ms", "mask_path":mask_path, "aoi_path":aoi_path, "aoi_id_field":"", "export_dir":os.path.join(self.temp.name,"ms_stats")})
        with open(result["aoi_csv"], encoding="utf-8") as handle: content = handle.read()
        self.assertIn("raw_band", content); self.assertIn("vegetation_index", content); self.assertIn("ndvi", content)

    def test_hsi_runs_through_the_standard_ensemble(self):
        output = os.path.join(self.temp.name, "output")
        work = os.path.join(self.temp.name, "work")
        result = NativePPEEngine().run({
            "source_mode": "stacked", "stacked_path": self.path,
            "ortho_type": "hsi", "hsi_min_wavelength": 450,
            "hsi_max_wavelength": 900, "hsi_interval": 10,
            "hsi_grid_confirmed": True, "sample_size": 4000,
            "bright_guard": False, "threads": 2, "output_dir": output, "work_dir": work,
        })
        self.assertEqual(result["band_detection"], "correlation-aware HSI spectral proxies")
        self.assertEqual(len(result["hsi_proxies"]), 5)
        self.assertEqual(result["processing_threads"], 2)
        self.assertTrue(os.path.isfile(result["rgb_path"]))
        self.assertTrue(os.path.isfile(result["vote_path"]))
        self.assertTrue(os.path.isfile(result["curve_path"]))
        applied = NativePPEEngine().apply({"vote_path": result["vote_path"],
                                           "threshold": .5, "output_dir": output})
        exported = NativePPEEngine().export_plant_only({
            "action": "export_bands", "source_mode": "stacked",
            "stacked_path": self.path, "ortho_type": "hsi",
            "hsi_min_wavelength": 450, "hsi_max_wavelength": 900,
            "hsi_interval": 10, "hsi_grid_confirmed": True,
            "mask_path": applied["mask_path"],
            "export_dir": os.path.join(self.temp.name, "exports"),
        })
        self.assertEqual(exported["export_count"], 46)
        self.assertEqual(len(exported["raw_band_metadata"]), 46)
        self.assertTrue(any("450nm" in name for name in exported["raw_band_metadata"]))
        aoi_path = os.path.join(self.temp.name, "plots.geojson")
        vector = ogr.GetDriverByName("GeoJSON").CreateDataSource(aoi_path)
        layer = vector.CreateLayer("plots", geom_type=ogr.wkbPolygon)
        layer.CreateField(ogr.FieldDefn("plot", ogr.OFTString))
        ring = ogr.Geometry(ogr.wkbLinearRing)
        for point in ((0, 0), (20, 0), (20, 20), (0, 20), (0, 0)): ring.AddPoint(*point)
        polygon = ogr.Geometry(ogr.wkbPolygon); polygon.AddGeometry(ring)
        feature = ogr.Feature(layer.GetLayerDefn()); feature.SetField("plot", "A1"); feature.SetGeometry(polygon); layer.CreateFeature(feature)
        vector = None
        statistics = NativePPEEngine().export_aoi_statistics({
            "source_mode": "stacked", "stacked_path": self.path,
            "ortho_type": "hsi", "hsi_min_wavelength": 450,
            "hsi_max_wavelength": 900, "hsi_interval": 10,
            "hsi_grid_confirmed": True, "mask_path": applied["mask_path"],
            "aoi_path": aoi_path, "aoi_id_field": "plot",
            "export_dir": os.path.join(self.temp.name, "aoi_exports"),
        })
        self.assertEqual(statistics["plot_count"], 1)
        self.assertEqual(statistics["row_count"], 46)
        self.assertTrue(os.path.isfile(statistics["aoi_csv"]))

    def test_hsi_missing_blue_runs_with_available_windows(self):
        # Create a cube starting at 530 nm (no blue window 450-520 nm)
        path = os.path.join(self.temp.name, "hsi_no_blue.tif")
        ds = gdal.GetDriverByName("GTiff").Create(path, 30, 30, 38, gdal.GDT_Float32)
        rng = np.random.default_rng(101)
        for band in range(1, 39):
            wavelength = 530 + (band-1)*10
            ds.GetRasterBand(band).WriteArray(rng.uniform(100, 500, (30, 30)).astype("float32"))
        ds = None
        output = os.path.join(self.temp.name, "output_no_blue")
        work = os.path.join(self.temp.name, "work_no_blue")
        result = NativePPEEngine().run({
            "source_mode": "stacked", "stacked_path": path,
            "ortho_type": "hsi", "hsi_min_wavelength": 530,
            "hsi_max_wavelength": 900, "hsi_interval": 10,
            "hsi_grid_confirmed": True, "sample_size": 2000,
            "bright_guard": False, "threads": 2, "output_dir": output, "work_dir": work,
        })
        self.assertTrue("blue" not in result["source_bands"])
        self.assertTrue(any(b in result["source_bands"] for b in ("green", "red", "re", "nir")))
        self.assertTrue(os.path.isfile(result["vote_path"]))

if __name__ == "__main__":
    unittest.main()
