"""Tests for the PPE framework as restored in 3.7.0 and hardened in 3.7.1:
spread-ranked layers, one per spectral group, split by Otsu, CDF, k-means and multi-Otsu."""
import os
import tempfile
import unittest

import numpy as np
from osgeo import gdal

from ppe_qgis.native_engine import (
    MAX_LAYERS, METHOD_NAMES, NON_VOTING, VI_GROUPS, VI_META, NativePPEEngine, cdf_threshold,
    compute_vi, display_channels, kmeans_threshold, multi_otsu, otsu, single_class_check, unit_free_spread,
)

LEAF = {"blue": 0.03, "green": 0.08, "red": 0.04, "re": 0.25, "nir": 0.50}
SOIL = {"blue": 0.10, "green": 0.14, "red": 0.18, "re": 0.22, "nir": 0.26}
LABEL = {"blue": "Blue", "green": "Green", "red": "Red", "re": "RedEdge", "nir": "NIR"}


def scene(plant, noise=0.004, seed=1, scale=1.0):
    rng = np.random.default_rng(seed)
    out = {}
    for name in LEAF:
        values = np.where(plant, LEAF[name], SOIL[name]).astype(np.float32)
        out[name] = ((values + rng.normal(0, noise, plant.shape)).clip(0.0005, 1) * scale).astype(np.float32)
    return out


def write(path, bands, names=("blue", "green", "red", "re", "nir")):
    first = bands[names[0]]
    ds = gdal.GetDriverByName("GTiff").Create(path, first.shape[1], first.shape[0], len(names), gdal.GDT_Float32)
    for i, name in enumerate(names, 1):
        ds.GetRasterBand(i).WriteArray(bands[name]); ds.GetRasterBand(i).SetDescription(LABEL[name])
    ds = None


def group_of(name):
    return next(group for group, members in VI_GROUPS.items() if name in members)


class FrameworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp.cleanup()

    def run_engine(self, bands, names=("blue", "green", "red", "re", "nir"), threshold=0.5, **cfg):
        path = os.path.join(self.temp.name, "img.tif"); write(path, bands, names)
        out = os.path.join(self.temp.name, "out")
        result = NativePPEEngine().run(dict({"source_mode": "stacked", "stacked_path": path, "output_dir": out, "work_dir": out + "/work"}, **cfg))
        applied = NativePPEEngine().apply({"vote_path": result["vote_path"], "threshold": threshold, "output_dir": out})
        return result, gdal.Open(applied["mask_path"]).ReadAsArray(), applied

    # ---- step 1: candidate layers and spread ------------------------------------------
    def test_candidates_are_ratio_indices_in_separate_groups(self):
        members = [name for group in VI_GROUPS.values() for name in group]
        self.assertEqual(len(members), len(set(members)))              # one group each
        self.assertFalse(set(members) & NON_VOTING)
        for name in ("exg", "dvi", "tvi", "mcari", "nirv", "savi", "evi"):
            self.assertIn(name, NON_VOTING)
        self.assertEqual(VI_META["rgri"][1], -1)                       # high red/green means soil
        leaf = {k: np.array([v], np.float32) for k, v in LEAF.items()}
        rescaled = {k: v * 0.1 for k, v in leaf.items()}
        for name in members:                                           # no units: same value on any reflectance scale
            self.assertAlmostEqual(float(compute_vi(name, leaf)[0]), float(compute_vi(name, rescaled)[0]), places=4)

    def test_spread_has_no_units(self):
        rng = np.random.default_rng(3)
        two_groups = np.concatenate([rng.normal(0.2, 0.03, 6000), rng.normal(0.8, 0.03, 4000)])
        long_tail = np.concatenate([rng.normal(1.5, 0.1, 9000), rng.normal(8.0, 2.0, 1000)])
        self.assertAlmostEqual(unit_free_spread(two_groups), unit_free_spread(two_groups * 1000.0), places=6)
        # the raw interquartile range would rank these the other way round
        self.assertGreater(np.subtract(*np.percentile(long_tail * 50, (75, 25))), np.subtract(*np.percentile(two_groups, (75, 25))))
        self.assertGreater(unit_free_spread(two_groups), unit_free_spread(long_tail * 50))

    # ---- step 2: the four methods -----------------------------------------------------
    def test_four_methods_split_two_groups(self):
        rng = np.random.default_rng(5)
        x = np.concatenate([rng.normal(0.2, 0.04, 7000), rng.normal(0.8, 0.05, 3000)]).astype(np.float32)
        o = otsu(x); k = kmeans_threshold(x, o); c = cdf_threshold(x, np.random.RandomState(42)); m = multi_otsu(x)
        for t in (o, k):                                               # both fall in the gap between the groups
            self.assertGreater((x[:7000] < t).mean(), 0.99)
            self.assertGreater((x[7000:] >= t).mean(), 0.99)
        self.assertTrue(0.2 < c < 0.8)
        self.assertTrue(x.min() < m[0] < m[1] < x.max())
        self.assertEqual(kmeans_threshold(x, o), kmeans_threshold(x, o))    # repeatable, no random restarts
        self.assertTrue(np.isnan(kmeans_threshold(x[:50], o)))

    # ---- the whole engine -------------------------------------------------------------
    def test_three_layers_from_three_groups_and_twelve_votes(self):
        plant = np.zeros((160, 160), bool); plant[:, 40:100] = True
        result, mask, applied = self.run_engine(scene(plant))
        chosen = result["chosen_layers"]
        self.assertEqual(len(chosen), MAX_LAYERS)
        self.assertEqual(len({group_of(name) for name in chosen}), MAX_LAYERS)
        ranked = [item["layer"] for item in result["spread_ranking"]]
        self.assertEqual(chosen, ranked[:MAX_LAYERS])                  # the highest-spread groups vote
        spreads = [item["spread"] for item in result["spread_ranking"]]
        self.assertEqual(spreads, sorted(spreads, reverse=True))
        for detail in result["layer_details"]:
            self.assertEqual(tuple(detail["methods"]), METHOD_NAMES)
        self.assertEqual(result["n_voters"], MAX_LAYERS * len(METHOD_NAMES))
        from ppe_qgis.version import PLUGIN_VERSION; self.assertEqual(result["ppe_version"], "native-" + PLUGIN_VERSION)
        self.assertAlmostEqual(applied["canopy_pct"], 100 * plant.mean(), delta=1.5)
        self.assertGreater((mask[:, 42:98] == 1).mean(), 0.99)
        self.assertLess((mask[:, 102:] == 1).mean(), 0.01)

    def test_low_reflectance_scale_gives_the_same_mask(self):
        plant = np.zeros((120, 120), bool); plant[30:90, 20:70] = True
        a = self.run_engine(scene(plant, noise=0.002), shadow_guard=False)
        b = self.run_engine(scene(plant, noise=0.002, scale=0.1), shadow_guard=False)
        self.assertEqual(a[0]["chosen_layers"], b[0]["chosen_layers"])
        self.assertAlmostEqual(a[2]["canopy_pct"], b[2]["canopy_pct"], delta=0.5)
        self.assertAlmostEqual(a[2]["canopy_pct"], 100 * plant.mean(), delta=1.0)

    def test_one_class_check_only_warns(self):
        soil = {k: v.ravel() for k, v in scene(np.zeros((60, 60), bool)).items()}
        leaf = {k: v.ravel() for k, v in scene(np.ones((60, 60), bool)).items()}
        self.assertEqual(single_class_check(soil)[0], "soil")
        self.assertEqual(single_class_check(leaf)[0], "plant")
        # a dense crop with a little soil between the rows is NOT one class
        # (3.6.0 and 3.7.0 called such a field all canopy and set every pixel to plant)
        dense = np.ones((100, 100), bool); dense[:, ::10] = False
        self.assertIsNone(single_class_check({k: v.ravel() for k, v in scene(dense).items()})[0])
        result, mask, applied = self.run_engine(scene(dense), shadow_guard=False)
        self.assertEqual(result["one_class_warning"], "")
        self.assertAlmostEqual(applied["canopy_pct"], 90.0, delta=2.0)
        self.assertLess((mask[:, ::10] == 1).mean(), 0.02)
        # an image that really is bare soil gets a warning, and the mask is left as the vote made it
        result, mask, applied = self.run_engine(scene(np.zeros((120, 120), bool)))
        self.assertEqual(result["one_class_warning"], "soil")
        self.assertEqual(result["single_class"], "")
        self.assertGreater(applied["canopy_pct"], 0.0)

    # ---- 3.7.1: stable CDF knee, local shift, optional fifth method ----------------------
    def test_cdf_knee_does_not_depend_on_the_sample(self):
        rng = np.random.default_rng(11)
        big = np.concatenate([rng.normal(0.25, 0.05, 30000), rng.normal(0.75, 0.08, 70000), rng.uniform(-3, 4, 40)]).astype(np.float32)
        small = big[::7]
        a, b = cdf_threshold(big), cdf_threshold(small)
        flat = lambda v: np.atleast_1d(np.array(v, float))
        self.assertEqual(flat(a).size, flat(b).size)
        self.assertTrue(np.allclose(flat(a), flat(b), atol=0.04))     # a few wild pixels and a thinner sample change nothing
        self.assertTrue(np.all((flat(a) > 0.2) & (flat(a) < 0.8)))
        self.assertEqual(cdf_threshold(big), cdf_threshold(big, np.random.RandomState(1), 5000))   # old arguments are accepted and ignored

    def test_cdf_with_two_knees_returns_a_band(self):
        rng = np.random.default_rng(12)
        x = np.concatenate([rng.normal(0.2, 0.03, 50000), rng.normal(0.8, 0.03, 50000)]).astype(np.float32)
        low, high = cdf_threshold(x)
        self.assertTrue(0.2 < low < 0.5 < high < 0.8)

    def test_local_shift_recovers_pale_plants(self):
        """Left half: vigorous plants. Right half: pale plants on the same soil. Thresholds
        fitted to the whole field sit between soil and the vigorous plants and cut the pale
        ones; shifted locally they follow each half."""
        plant = np.zeros((400, 800), bool); plant[:, 0:400:8] = plant[:, 1:400:8] = plant[:, 2:400:8] = plant[:, 3:400:8] = True
        plant[:, 400::8] = plant[:, 401::8] = True
        pale = np.zeros_like(plant); pale[:, 400:] = True
        bands = scene(plant, noise=0.004)
        for name in bands:      # pale plants: a 35 % leaf, 65 % soil spectrum
            mixed = 0.35 * LEAF[name] + 0.65 * SOIL[name]
            bands[name] = np.where(plant & pale, mixed + (bands[name] - LEAF[name]), bands[name]).astype(np.float32)
        kept = {}
        for mode in ("global", "adaptive"):
            result, mask, applied = self.run_engine(bands, shadow_guard=False, biofilm_guard=False, adaptation_mode=mode, tile_pixels=200)
            far = plant & pale; far[:, :600] = False          # away from the border between the two halves
            kept[mode] = (mask[far] == 1).mean()
            self.assertGreater((mask[plant & ~pale] == 1).mean(), 0.99)
            self.assertLess((mask[~plant] == 1).mean(), 0.02)
        self.assertLess(kept["global"], 0.5)
        self.assertGreater(kept["adaptive"], 0.9)

    def test_half_mixture_is_an_optional_fifth_method(self):
        plant = np.zeros((160, 160), bool); plant[:, 40:100] = True
        result, mask, applied = self.run_engine(scene(plant), half_mixture=True)
        for detail in result["layer_details"]:
            self.assertEqual(tuple(detail["methods"]), METHOD_NAMES + ("halfmix",))
        self.assertEqual(result["n_voters"], MAX_LAYERS * 5)
        self.assertAlmostEqual(applied["canopy_pct"], 100 * plant.mean(), delta=1.5)

    def test_layers_can_be_named_by_a_script(self):
        plant = np.zeros((120, 120), bool); plant[:, 30:80] = True
        result, mask, applied = self.run_engine(scene(plant), layers=["ndvi", "ndre"])
        self.assertEqual(result["chosen_layers"], ["ndvi", "ndre"])
        with self.assertRaises(ValueError):
            self.run_engine(scene(plant), names=("red", "green", "blue"), layers=["ndvi"])

    def test_rgb_image_uses_visible_indices_only(self):
        plant = np.zeros((120, 120), bool); plant[:, 30:80] = True
        result, mask, applied = self.run_engine(scene(plant), names=("red", "green", "blue"))
        self.assertTrue(1 <= len(result["chosen_layers"]) <= MAX_LAYERS)
        for name in result["chosen_layers"]:
            self.assertEqual(group_of(name), "visible")
        self.assertFalse(result["biofilm_guard"])                      # no red edge: algae guard is skipped
        self.assertAlmostEqual(applied["canopy_pct"], 100 * plant.mean(), delta=1.5)

    def test_four_band_image_without_red_edge(self):
        plant = np.zeros((120, 120), bool); plant[:, 30:80] = True
        result, mask, applied = self.run_engine(scene(plant), names=("blue", "green", "red", "nir"))
        self.assertEqual({group_of(name) for name in result["chosen_layers"]}, {"nir_red", "nir_green", "visible"})
        self.assertAlmostEqual(applied["canopy_pct"], 100 * plant.mean(), delta=1.5)


class DisplayTests(unittest.TestCase):
    """3.7.2: the image is shown as soon as it is chosen."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp.cleanup()

    def test_what_is_shown_for_each_band_set(self):
        self.assertEqual(display_channels(["blue", "green", "red", "re", "nir"]), ("true colour", ("red", "green", "blue")))
        self.assertEqual(display_channels(["green", "red", "nir"])[1], ("nir", "red", "green"))       # no blue: false colour
        self.assertEqual(display_channels(["red", "nir"])[1], ("nir", "nir", "nir"))                 # plants bright in NIR
        self.assertEqual(display_channels(["red", "re"])[1], ("re", "re", "re"))
        self.assertEqual(display_channels(["green"])[1], ("green", "green", "green"))

    def test_preview_finds_bands_and_stretch_without_running(self):
        plant = np.zeros((120, 120), bool); plant[:, 30:80] = True
        bands = scene(plant); path = os.path.join(self.temp.name, "img.tif"); write(path, bands, ("green", "red", "re", "nir"))
        work = os.path.join(self.temp.name, "work")
        result = NativePPEEngine().preview({"source_mode": "stacked", "stacked_path": path, "work_dir": work})
        self.assertEqual(result["display_mode"], "false colour (NIR, red, green)")
        self.assertEqual(result["rgb_source_bands"], [4, 2, 1])                # NIR, red, green in this file
        self.assertEqual(result["rgb_sources"], [path] * 3)
        self.assertFalse(result["true_rgb"])
        for (low, high), name in zip(result["display_stretch"], ("nir", "red", "green")):
            self.assertTrue(low < high)
            self.assertTrue(bands[name].min() - 0.01 <= low and high <= bands[name].max() + 0.01)
        self.assertFalse(os.path.exists(os.path.join(work, "vote_fraction.tif")))
        self.assertEqual([f for f in os.listdir(result["run_dir"]) if f.endswith(".tif")], [])     # nothing heavy is written

    def test_run_reports_the_same_display(self):
        plant = np.zeros((120, 120), bool); plant[:, 30:80] = True
        path = os.path.join(self.temp.name, "img.tif"); write(path, scene(plant), ("red", "nir"))
        out = os.path.join(self.temp.name, "out")
        result = NativePPEEngine().run({"source_mode": "stacked", "stacked_path": path, "output_dir": out, "work_dir": out})
        self.assertEqual(result["display_mode"], "NIR band in grey")
        self.assertEqual(result["rgb_bands"], ["nir"])


if __name__ == "__main__":
    unittest.main()


class TrueColourStretchTests(unittest.TestCase):
    """True colour keeps the real colour balance: one stretch for red, green and blue together."""

    def test_true_colour_shares_one_stretch(self):
        from ppe_qgis.native_engine import display_stretch
        rng = np.random.default_rng(0)
        values = {"red": rng.uniform(0.01, 0.25, 5000), "green": rng.uniform(0.02, 0.17, 5000), "blue": rng.uniform(0.01, 0.10, 5000)}
        s = display_stretch(["red", "green", "blue"], values)
        self.assertEqual(len(set(s)), 1)
        self.assertEqual(s[0][0], 0.0)
        self.assertAlmostEqual(s[0][1], float(np.percentile(np.concatenate(list(values.values())), 99.5)))

    def test_false_colour_keeps_a_stretch_per_band(self):
        from ppe_qgis.native_engine import display_stretch
        rng = np.random.default_rng(1)
        values = {"nir": rng.uniform(0.2, 0.6, 5000), "red": rng.uniform(0.01, 0.2, 5000), "green": rng.uniform(0.02, 0.15, 5000)}
        s = display_stretch(["nir", "red", "green"], values)
        self.assertEqual(len(set(s)), 3)
        self.assertGreater(s[0][1], s[1][1])
