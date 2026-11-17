import os
import tempfile
import unittest
import numpy as np
from osgeo import gdal
from ppe_qgis.native_engine import NativePPEEngine


class ShadowGuardTests(unittest.TestCase):
    def test_shadow_guard_multispectral(self):
        engine = NativePPEEngine()
        rng = np.random.RandomState(42)
        n = 100
        # 50 soil pixels (low NIR, moderate red)
        red_soil = rng.normal(0.020, 0.003, 50).astype(np.float32)
        green_soil = rng.normal(0.018, 0.003, 50).astype(np.float32)
        blue_soil = rng.normal(0.015, 0.002, 50).astype(np.float32)
        re_soil = rng.normal(0.022, 0.003, 50).astype(np.float32)
        nir_soil = rng.normal(0.025, 0.004, 50).astype(np.float32)

        # 50 plant pixels (high NIR, low red)
        red_plant = rng.normal(0.008, 0.002, 50).astype(np.float32)
        green_plant = rng.normal(0.030, 0.004, 50).astype(np.float32)
        blue_plant = rng.normal(0.008, 0.002, 50).astype(np.float32)
        re_plant = rng.normal(0.045, 0.005, 50).astype(np.float32)
        nir_plant = rng.normal(0.120, 0.015, 50).astype(np.float32)

        bands = {
            "blue": np.concatenate([blue_soil, blue_plant]),
            "green": np.concatenate([green_soil, green_plant]),
            "red": np.concatenate([red_soil, red_plant]),
            "re": np.concatenate([re_soil, re_plant]),
            "nir": np.concatenate([nir_soil, nir_plant]),
        }
        valid = np.ones(n, dtype=bool)

        guard = engine._fit_shadow_guard(bands, valid)
        self.assertIsNotNone(guard)
        self.assertTrue(guard["has_nir"])
        self.assertIsNotNone(guard["nirv_floor"])
        self.assertGreater(guard["nirv_floor"], 0.0)

        # Test specific pixel profiles:
        # Pixel 0: Shaded plant (low brightness, high NIRv, Green > Red)
        # Pixel 1: Shaded soil / black pixel (low brightness, low NIR, Red >= Green)
        # Pixel 2: Pitch black sensor dropout (all zeros)
        test_bands = {
            "blue": np.array([0.005, 0.004, 0.0], dtype=np.float32),
            "green": np.array([0.015, 0.006, 0.0], dtype=np.float32),
            "red": np.array([0.006, 0.007, 0.0], dtype=np.float32),
            "re": np.array([0.025, 0.008, 0.0], dtype=np.float32),
            "nir": np.array([0.080, 0.010, 0.0], dtype=np.float32),
        }
        guard_test = {
            "b_thresh": 0.020,
            "has_nir": True,
            "nirv_floor": 0.015,
        }
        rejected = engine._shadow_guard_mask(test_bands, guard_test)
        # Shaded plant -> NOT rejected
        self.assertFalse(rejected[0])
        # Shaded soil -> REJECTED
        self.assertTrue(rejected[1])
        # Pitch black -> REJECTED
        self.assertTrue(rejected[2])

    def test_shadow_guard_rgb(self):
        engine = NativePPEEngine()
        bands = {
            "blue": np.array([0.01, 0.005, 0.008], dtype=np.float32),
            "green": np.array([0.05, 0.018, 0.007], dtype=np.float32),
            "red": np.array([0.02, 0.008, 0.009], dtype=np.float32),
        }

        guard = {
            "b_thresh": 0.025,
            "has_nir": False,
            "nirv_floor": None,
        }
        rejected = engine._shadow_guard_mask(bands, guard)

        # Pixel 0: Bright green leaf (brightness 0.027 >= 0.025) -> not rejected
        self.assertFalse(rejected[0])
        # Pixel 1: Shaded green leaf (brightness 0.010, Green/Red = 2.25 >= 1.25) -> not rejected
        self.assertFalse(rejected[1])
        # Pixel 2: Dark soil shadow (brightness 0.008, Green/Red = 0.77 < 1.25) -> REJECTED
        self.assertTrue(rejected[2])

    def test_write_rgb_display_nodata_and_stretch(self):
        engine = NativePPEEngine()
        engine.width = 10
        engine.height = 10
        engine.thread_count = 1
        engine.scale = 1.0

        with tempfile.TemporaryDirectory() as tmp:
            tmpl_path = f"{tmp}/tmpl.tif"
            tds = gdal.GetDriverByName("GTiff").Create(tmpl_path, 10, 10, 1, gdal.GDT_Float32)
            tds.SetGeoTransform((0, 1, 0, 0, 0, -1))
            engine.template = tds

            red_data = np.full((10, 10), 0.006, dtype=np.float32)
            red_data[0, :] = np.nan
            green_data = np.full((10, 10), 0.025, dtype=np.float32)
            green_data[0, :] = np.nan
            blue_data = np.full((10, 10), 0.008, dtype=np.float32)
            blue_data[0, :] = np.nan

            r_path = f"{tmp}/r.tif"
            g_path = f"{tmp}/g.tif"
            b_path = f"{tmp}/b.tif"
            for p, arr in [(r_path, red_data), (g_path, green_data), (b_path, blue_data)]:
                d = gdal.GetDriverByName("GTiff").Create(p, 10, 10, 1, gdal.GDT_Float32)
                d.GetRasterBand(1).WriteArray(arr)
                d = None

            engine.sources = {"red": (r_path, 1), "green": (g_path, 1), "blue": (b_path, 1)}
            engine.datasets = {k: gdal.Open(v[0]) for k, v in engine.sources.items()}

            sampled = {
                "red": red_data.ravel(),
                "green": green_data.ravel(),
                "blue": blue_data.ravel(),
            }

            out_rgb = f"{tmp}/display_rgb.tif"
            engine._write_rgb_display(out_rgb, ("red", "green", "blue"), sampled)

            out_ds = gdal.Open(out_rgb)
            self.assertEqual(out_ds.RasterCount, 3)
            # Band 1 nodata must be 0
            self.assertEqual(out_ds.GetRasterBand(1).GetNoDataValue(), 0)

            out_arr = out_ds.GetRasterBand(1).ReadAsArray()
            # Collar must be 0
            self.assertTrue(np.all(out_arr[0, :] == 0))
            # Valid field pixels must be in [1, 255]
            valid_pixels = out_arr[1:, :]
            self.assertTrue(np.all(valid_pixels >= 1))
            self.assertTrue(np.all(valid_pixels <= 255))
            tds = None
            out_ds = None
            engine.datasets = {}

    def test_rgb_unnamed_with_lingering_hsi_settings(self):
        """Ensure unnamed 3-band RGB imagery is correctly recognized even if HSI grid settings are lingering in config."""
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/rgb_undefined.tif"
            ds = gdal.GetDriverByName("GTiff").Create(path, 60, 60, 3, gdal.GDT_Byte)
            rng = np.random.RandomState(42)
            for b in range(1, 4):
                band = ds.GetRasterBand(b)
                band.WriteArray(rng.randint(30, 220, (60, 60), dtype=np.uint8))
                band.SetColorInterpretation(gdal.GCI_Undefined)
                band.SetDescription("")
            ds = None

            cfg = {
                "source_mode": "stacked",
                "stacked_path": path,
                "ortho_type": "auto",
                "rgb_order": "rgb",
                "hsi_min_wavelength": 400.0,
                "hsi_max_wavelength": 900.0,
                "hsi_interval": 10.0,
                "hsi_grid_confirmed": True,
                "sample_size": 2000,
                "output_dir": f"{tmp}/out",
                "work_dir": f"{tmp}/work",
            }
            res = NativePPEEngine().run(cfg)
            self.assertTrue(res["true_rgb"])
            self.assertEqual(len(res["chosen_layers"]), 3)
            self.assertTrue(os.path.isfile(res["vote_path"]))
            self.assertTrue(os.path.isfile(res["rgb_path"]))


if __name__ == "__main__":
    unittest.main()
