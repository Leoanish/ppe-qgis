"""3.7.5 robustness rules: sensor band names, thermal bands, shaded leaves, camera colour guard."""
import os
import tempfile
import unittest
import numpy as np
from ppe_qgis.native_engine import NativePPEEngine, infer_band, is_thermal_name


class BandNameTests(unittest.TestCase):
    def test_sensor_names(self):
        cases = {
            # MicaSense / generic
            "Blue": "blue", "Green": "green", "Red": "red", "Red Edge": "re", "NIR": "nir", "Near Infrared": "nir",
            # Parrot Sequoia
            "GRE": "green", "RED": "red", "REG": "re", "NIR": "nir",
            # DJI multispectral
            "MS_G": "green", "MS_R": "red", "MS_RE": "re", "MS_NIR": "nir",
            # wavelength names
            "475nm": "blue", "560nm": "green", "668nm": "red", "717nm": "re", "842nm": "nir",
        }
        for name, band in cases.items():
            self.assertEqual(infer_band(name), band, name)

    def test_thermal_and_products_are_not_reflectance(self):
        for name in ("LWIR", "Thermal", "TIR", "temperature_C", "radiometric", "dsm", "ndvi"):
            self.assertIsNone(infer_band(name), name)
        self.assertTrue(is_thermal_name("LWIR"))
        self.assertFalse(is_thermal_name("Red Edge"))


def _scene(rng, n):
    soil = {"red": rng.normal(.10, .01, n), "green": rng.normal(.09, .01, n), "blue": rng.normal(.08, .01, n),
            "re": rng.normal(.12, .01, n), "nir": rng.normal(.16, .01, n)}
    sun = {"red": rng.normal(.04, .005, n), "green": rng.normal(.09, .01, n), "blue": rng.normal(.03, .005, n),
           "re": rng.normal(.20, .02, n), "nir": rng.normal(.45, .03, n)}
    return soil, sun


class ShadedLeafTests(unittest.TestCase):
    def test_shaded_leaf_kept_dark_soil_rejected(self):
        rng = np.random.RandomState(1); n = 400
        soil, sun = _scene(rng, n)
        shade_leaf = {k: v*0.25 for k, v in sun.items()}      # same leaf, a quarter of the light
        dark_soil = {k: v*0.25 for k, v in soil.items()}
        bands = {k: np.concatenate([soil[k], sun[k], shade_leaf[k], dark_soil[k]]).astype(np.float32).reshape(40, 40)
                 for k in soil}
        valid = np.ones((40, 40), bool)
        guard = NativePPEEngine()._fit_shadow_guard(bands, valid)
        self.assertIsNotNone(guard)
        self.assertIsNotNone(guard.get("ndvi_keep"))
        rejected = NativePPEEngine._shadow_guard_mask(bands, guard).ravel()
        self.assertLess(rejected[2*n:3*n].mean(), 0.05)          # shaded leaves stay plant
        self.assertLess(rejected[n:2*n].mean(), 0.01)            # sunlit leaves untouched


class ColourGuardTests(unittest.TestCase):
    def test_grey_white_black_objects_rejected_green_kept(self):
        rng = np.random.RandomState(3); n = 500
        soil = np.stack([rng.normal(130, 6, n), rng.normal(124, 6, n), rng.normal(114, 6, n)])   # warm soil
        plant = np.stack([rng.normal(100, 6, n), rng.normal(112, 6, n), rng.normal(80, 6, n)])   # yellow-green
        sample = {k: np.concatenate([soil[i], plant[i]]).astype(np.float32) for i, k in enumerate(("red", "green", "blue"))}
        with np.errstate(all="ignore"):
            vari = (sample["green"]-sample["red"])/(sample["green"]+sample["red"]-sample["blue"])
        split = float(np.median(vari))
        guard = NativePPEEngine._fit_colour_guard(sample, {"vari": vari}, [("vari", 1, [("otsu", split)])])
        self.assertIsNotNone(guard)
        self.assertGreater(guard["plant_excess"], 0.02)
        objects = {"red": np.array([187., 45., 120.], np.float32), "green": np.array([190., 47., 120.], np.float32),
                   "blue": np.array([199., 50., 120.], np.float32)}            # bluish-white truck, black bed, grey tarp
        self.assertTrue(NativePPEEngine._colour_guard_mask(objects, guard).all())
        kept = ~NativePPEEngine._colour_guard_mask({k: v[n:] for k, v in sample.items()}, guard)
        self.assertGreater(kept.mean(), 0.95)


if __name__ == "__main__":
    unittest.main()


class ImageTypeTests(unittest.TestCase):
    def _stack(self, folder, names):
        from osgeo import gdal
        path = os.path.join(folder, "s.tif"); d = gdal.GetDriverByName("GTiff").Create(path, 8, 8, 3, gdal.GDT_Float32)
        for i in range(3):
            b = d.GetRasterBand(i+1); b.WriteArray(np.full((8, 8), 0.1*(i+1), np.float32))
            if names: b.SetDescription(names[i])
        d = None; return path

    def test_nir_re_red_is_multispectral_not_rgb(self):
        from ppe_qgis.native_engine import describe_image
        with tempfile.TemporaryDirectory() as tmp:
            info = describe_image({"source_mode": "stacked", "stacked_path": self._stack(tmp, ["NIR", "Red edge", "Red"])})
            self.assertEqual(info["kind"], "ms"); self.assertEqual(info["bands"], ["red", "re", "nir"])
        with tempfile.TemporaryDirectory() as tmp:
            for b in ("nir", "re", "red"):
                open(os.path.join(tmp, "field_%s.tif" % b), "wb").close()
            self.assertEqual(describe_image({"source_mode": "folder", "folder_path": tmp})["kind"], "ms")

    def test_unnamed_three_bands_ask_for_order(self):
        from ppe_qgis.native_engine import describe_image
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(describe_image({"source_mode": "stacked", "stacked_path": self._stack(tmp, None)})["kind"], "unnamed3")

    def test_unnamed_order_nir_re_red(self):
        from ppe_qgis.native_engine import NativePPEEngine
        with tempfile.TemporaryDirectory() as tmp:
            path = self._stack(tmp, None); e = NativePPEEngine()
            e._map_bands({"source_mode": "stacked", "stacked_path": path, "ortho_type": "auto", "rgb_order": "nir_re_red"})
            self.assertEqual({k: v[1] for k, v in e.sources.items()}, {"nir": 1, "re": 2, "red": 3})
