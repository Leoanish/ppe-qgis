"""Crop areas (3.9.0): outlines become a crop raster, every area gets its own threshold, the vote is
unchanged, overlaps go to the crop listed first, and the plot export says which crop each plot is in."""
import csv
import json
import os
import shutil
import tempfile
import unittest

import numpy as np
from osgeo import gdal, osr

from ppe_qgis.native_engine import NODATA, NativePPEEngine

X0, Y0, PIX, SIZE = 300000.0, 3500000.0, 0.1, 100


def utm():
    srs = osr.SpatialReference(); srs.ImportFromEPSG(32617); return srs


def box(c0, c1, r0, r1):
    """WKT of a rectangle covering columns c0..c1 and rows r0..r1 (pixel edges)."""
    x0, x1 = X0 + c0 * PIX, X0 + c1 * PIX
    y0, y1 = Y0 - r1 * PIX, Y0 - r0 * PIX
    return "POLYGON(({0} {2}, {1} {2}, {1} {3}, {0} {3}, {0} {2}))".format(x0, x1, y0, y1)


class CropAreaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="ppe_crops_")
        # a vote raster: every column holds one vote value, from 0 (left) to 0.99 (right)
        votes = np.tile(np.round(np.arange(SIZE) / SIZE, 2), (SIZE, 1)).astype(np.float32)
        votes[0, 0] = NODATA
        cls.vote = os.path.join(cls.dir, "vote_fraction.tif")
        ds = gdal.GetDriverByName("GTiff").Create(cls.vote, SIZE, SIZE, 1, gdal.GDT_Float32)
        ds.SetGeoTransform((X0, PIX, 0, Y0, 0, -PIX)); ds.SetProjection(utm().ExportToWkt())
        ds.GetRasterBand(1).WriteArray(votes); ds.GetRasterBand(1).SetNoDataValue(NODATA); ds = None
        cls.votes = votes
        # crop A: top half; crop B: bottom half, overlapping crop A in rows 40-50
        cls.areas = [{"name": "corn", "wkt": box(0, SIZE, 0, 50)}, {"name": "soybean", "wkt": box(0, SIZE, 40, SIZE)}]
        cls.zones = NativePPEEngine().prepare_crop_areas({"vote_path": cls.vote, "crop_areas": cls.areas, "output_dir": cls.dir})

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_crop_raster_and_overlap(self):
        z = gdal.Open(self.zones["zone_path"]).ReadAsArray()
        self.assertTrue((z[:50] == 1).all())          # corn, listed first, wins rows 40-50
        self.assertTrue((z[50:] == 2).all())
        self.assertEqual(self.zones["zone_names"], ["corn", "soybean"])
        self.assertEqual(self.zones["zone_pixels"], [0, 50 * SIZE - 1, 50 * SIZE])

    def test_preview_holds_vote_plus_ten_times_area(self):
        p = gdal.Open(self.zones["preview_path"]).ReadAsArray()
        z = gdal.Open(self.zones["zone_path"]).ReadAsArray()
        ok = self.votes != NODATA
        np.testing.assert_allclose(p[ok], self.votes[ok] + 10 * z[ok], atol=1e-5)
        self.assertEqual(p[0, 0], NODATA)

    def test_curves_match_the_votes(self):
        curve = dict(self.zones["zone_curves"][2])           # soybean, rows 50-99
        self.assertAlmostEqual(curve[0.5], 100 * np.mean(self.votes[50:] >= 0.45), places=6)

    def test_each_area_gets_its_own_threshold(self):
        out = os.path.join(self.dir, "apply")
        result = NativePPEEngine().apply({"vote_path": self.vote, "threshold": 0.5, "output_dir": out,
                                          "zone_path": self.zones["zone_path"], "zone_thresholds": [0.5, 0.3, 0.8],
                                          "zone_names": ["corn", "soybean"], "field_info": {"growth_stage": "Closed canopy"}})
        m = gdal.Open(result["mask_path"]).ReadAsArray()
        ok = self.votes != NODATA
        expected = np.where(np.arange(SIZE)[:, None] < 50, self.votes >= 0.25, self.votes >= 0.75)
        self.assertTrue((m[ok] == expected[ok]).all())
        self.assertEqual(m[0, 0], 255)
        self.assertEqual([c["crop"] for c in result["crops"]], ["corn", "soybean"])
        self.assertAlmostEqual(result["crops"][1]["canopy_pct"], 100 * np.mean(self.votes[50:] >= 0.75))
        with open(result["run_record"]) as handle: record = json.load(handle)
        self.assertEqual(record["field_info"]["growth_stage"], "Closed canopy")
        self.assertEqual([c["threshold"] for c in record["crops"]], [0.3, 0.8])

    def test_without_areas_the_mask_is_unchanged(self):
        out = os.path.join(self.dir, "plain")
        a = NativePPEEngine().apply({"vote_path": self.vote, "threshold": 0.5, "output_dir": out})
        b = NativePPEEngine().apply({"vote_path": self.vote, "threshold": 0.5, "output_dir": out + "_same",
                                     "zone_path": self.zones["zone_path"], "zone_thresholds": [0.5, 0.5, 0.5]})
        np.testing.assert_array_equal(gdal.Open(a["mask_path"]).ReadAsArray(), gdal.Open(b["mask_path"]).ReadAsArray())

    def test_no_area_is_refused(self):
        with self.assertRaises(ValueError):
            NativePPEEngine().prepare_crop_areas({"vote_path": self.vote, "crop_areas": [], "output_dir": self.dir})


if __name__ == "__main__":
    unittest.main()
