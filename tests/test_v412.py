"""4.12.0: plain error messages, map overviews, the background weed job and Batch with weeds."""
import json
import os
import shutil
import tempfile
import unittest

import numpy as np
from osgeo import gdal

from ppe_qgis.errors import friendly
from ppe_qgis.native_engine import NativePPEEngine, build_overviews, findings_histograms
from ppe_qgis.plant_count import WEED_IN_ROW_CODE, mark_weeds
from test_v47 import rows_field, tif


class PlainErrors(unittest.TestCase):
    def test_known_errors_get_a_next_step_and_keep_the_details(self):
        m = friendly("Traceback ...\nOSError: [Errno 13] Permission denied: '/data/out'")
        self.assertIn("Choose a folder you can write to", m)
        self.assertIn("Details:", m)
        self.assertIn("out of memory", friendly("MemoryError: Unable to allocate 2.1 GiB"))
        self.assertIn("disk is full", friendly("OSError: [Errno 28] No space left on device"))

    def test_unknown_errors_are_kept_as_they_are(self):
        self.assertEqual(friendly("RuntimeError: something new"), "RuntimeError: something new")
        self.assertEqual(friendly(""), "")


class Overviews(unittest.TestCase):
    def test_large_outputs_get_overviews_small_ones_not(self):
        tmp = tempfile.mkdtemp()
        try:
            big = tif(os.path.join(tmp, "big.tif"), np.random.default_rng(0).random((1100, 1300)).astype(np.float32),
                      gdal.GDT_Float32)
            small = tif(os.path.join(tmp, "small.tif"), np.zeros((100, 100), np.uint8), gdal.GDT_Byte)
            self.assertTrue(build_overviews(big))
            ds = gdal.Open(big)
            self.assertGreaterEqual(ds.GetRasterBand(1).GetOverviewCount(), 2)
            ds = None
            self.assertFalse(build_overviews(small))
            self.assertTrue(build_overviews(self._copy(big, tmp), "AVERAGE", external=True))
            self.assertTrue(os.path.isfile(os.path.join(tmp, "big2.tif.ovr")))
            self.assertFalse(build_overviews(os.path.join(tmp, "missing.tif")))   # never fails a job
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @staticmethod
    def _copy(path, tmp):
        out = os.path.join(tmp, "big2.tif")
        gdal.Translate(out, path)
        return out


class BackgroundWeeds(unittest.TestCase):
    def test_find_weeds_writes_a_new_findings_file_and_leaves_the_old_one(self):
        tmp = tempfile.mkdtemp()
        try:
            m, _ = rows_field(row_weeds=((224, 45), (284, 135)))
            vote = tif(os.path.join(tmp, "vote_fraction.tif"), m.astype(np.float32), gdal.GDT_Float32)
            find = tif(os.path.join(tmp, "ppe_findings.tif"), np.zeros(m.shape, np.uint8), gdal.GDT_Byte, nodata=255)
            with open(os.path.join(tmp, "run_summary.json"), "w") as f:
                json.dump({"findings_path": find}, f)
            e = NativePPEEngine(log=lambda msg: None)
            # same settings as the unit tests of the counter (tiny plants on a small field)
            import ppe_qgis.plant_count as pc
            real = pc.mark_weeds
            pc.mark_weeds = lambda *a, **k: real(*a, **dict(k, cfg={"min_plant_cm2": 0.05, "join_cm": 0}))
            try:
                r = e.find_weeds({"run_dir": tmp, "vote_path": vote, "findings_path": find, "threshold": 0.5})
            finally:
                pc.mark_weeds = real
            self.assertNotEqual(r["findings_path"], find)
            self.assertEqual(int((gdal.Open(find).ReadAsArray() == WEED_IN_ROW_CODE).sum()), 0)    # old file unchanged
            new = gdal.Open(r["findings_path"]).ReadAsArray()
            self.assertEqual(new[45, 224], WEED_IN_ROW_CODE)
            self.assertEqual(r["weeds"]["in_row"], 2)
            self.assertEqual(np.asarray(r["findings_hist"]).tolist(),
                             np.asarray(findings_histograms(r["findings_path"], vote)).tolist())
            with open(os.path.join(tmp, "run_summary.json")) as f:
                self.assertEqual(json.load(f)["findings_path"], r["findings_path"])
            only = e.find_weeds({"run_dir": tmp, "vote_path": vote, "findings_path": find, "threshold": 0.5, "weeds": False})
            self.assertIsNone(only["weeds"])                     # overviews only
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
