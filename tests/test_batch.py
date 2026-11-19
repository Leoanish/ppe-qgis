"""4.2: batch. Every image of a folder (single files and band folders) runs exactly as on its own;
all masks are then written at their own thresholds, with a summary table."""
import csv
import json
import os
import shutil
import tempfile
import unittest

import numpy as np
from osgeo import gdal

from ppe_qgis.native_engine import NativePPEEngine, list_batch_images, load_batch_result
from test_findings_levels import LEAF, SOIL, ORDER, SIZE, run_field


class BatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="ppe_batch_"); cls.inp = os.path.join(cls.root, "images"); os.makedirs(cls.inp)
        kind = np.zeros((SIZE, SIZE), int); kind[:, : SIZE // 3] = 1
        cls.single_root, cls.single = run_field([SOIL, LEAF], kind, seed=5)       # a band folder and its own run
        band_dir = os.path.join(cls.single_root, "bands")
        shutil.copytree(band_dir, os.path.join(cls.inp, "field_b"))              # image 1: a band folder
        stacked = os.path.join(cls.inp, "field_a.tif")                            # image 2: the same bands stacked in one file
        files = [os.path.join(band_dir, "field_{}.tif".format(k)) for k in ORDER]
        vrt = gdal.BuildVRT("/vsimem/stack.vrt", files, separate=True); gdal.Translate(stacked, vrt); vrt = None
        ds = gdal.Open(stacked, gdal.GA_Update)
        for i, k in enumerate(ORDER): ds.GetRasterBand(i + 1).SetDescription(k)
        ds = None
        with open(os.path.join(cls.inp, "notes.txt"), "w") as f: f.write("not an image")
        os.makedirs(os.path.join(cls.inp, "empty_folder"))
        cls.out = os.path.join(cls.root, "out"); cls.log = []
        cls.engine = NativePPEEngine(log=cls.log.append)
        cls.manifest = cls.engine.batch_run({"batch_folder": cls.inp, "output_dir": cls.out, "work_dir": os.path.join(cls.root, "work")})

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True); shutil.rmtree(cls.single_root, ignore_errors=True)

    def test_finds_files_and_band_folders_only(self):
        items = list_batch_images(self.inp)
        self.assertEqual([(i["name"], i["source_mode"]) for i in items], [("field_a", "stacked"), ("field_b", "folder")])

    def test_every_image_done_and_saved(self):
        self.assertEqual([e["status"] for e in self.manifest["items"]], ["done", "done"])
        with open(os.path.join(self.out, "ppe_batch.json")) as f: on_disk = json.load(f)
        self.assertEqual(len(on_disk["items"]), 2)
        r = load_batch_result(self.manifest["items"][1]["result_path"])
        self.assertTrue(os.path.isfile(r["vote_path"]) and os.path.isfile(r["confident_labels_path"]))

    def test_same_vote_as_a_run_of_its_own(self):
        r = load_batch_result(self.manifest["items"][1]["result_path"])     # field_b = the band folder run on its own
        a = gdal.Open(r["vote_path"]).ReadAsArray(); b = gdal.Open(self.single["vote_path"]).ReadAsArray()
        self.assertTrue(np.array_equal(a, b))

    def test_one_bad_image_does_not_stop_the_rest(self):
        bad = os.path.join(self.root, "bad"); os.makedirs(bad)
        shutil.copy(os.path.join(self.inp, "field_a.tif"), os.path.join(bad, "good.tif"))
        with open(os.path.join(bad, "broken.tif"), "wb") as f: f.write(b"not a tiff")
        m = NativePPEEngine().batch_run({"batch_folder": bad, "output_dir": os.path.join(self.root, "out_bad"), "work_dir": os.path.join(self.root, "work_bad")})
        self.assertEqual(sorted((e["name"], e["status"]) for e in m["items"]), [("broken", "failed"), ("good", "done")])

    def test_finalize_at_each_images_own_threshold(self):
        f = self.engine.batch_finalize({"manifest_path": self.manifest["manifest_path"],
                                        "choices": {"field_a": {"threshold": 0.8, "reviewed": True}}})
        with open(f["summary_csv"]) as fh: rows = {r["image"]: r for r in csv.DictReader(fh)}
        self.assertEqual(float(rows["field_a"]["threshold"]), 0.8); self.assertEqual(rows["field_a"]["reviewed"], "True")
        self.assertEqual(float(rows["field_b"]["threshold"]), round(self.manifest["items"][1]["threshold"], 3))
        for r in rows.values():
            self.assertTrue(os.path.isfile(r["mask_path"]))
            self.assertAlmostEqual(float(r["canopy_pct"]), 100.0 / 3, delta=3.0)   # a third of each image is leaves

    def test_empty_folder_says_so(self):
        with self.assertRaises(ValueError):
            NativePPEEngine().batch_run({"batch_folder": os.path.join(self.inp, "empty_folder"), "output_dir": os.path.join(self.root, "o2")})


if __name__ == "__main__":
    unittest.main()
