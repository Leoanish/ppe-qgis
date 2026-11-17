"""Plot boundaries (3.8.0): every common vector format is read, side files and zips lead to the
Shapefile, non-polygon files are refused, and the export holds canopy cover and full statistics."""
import csv
import os
import shutil
import tempfile
import unittest
import zipfile

import numpy as np
from osgeo import gdal, ogr, osr

from ppe_qgis.native_engine import (
    STAT_NAMES, NativePPEEngine, guess_plot_id_field, open_plot_file, plots_over_image,
)

LEAF = {"blue": 0.03, "green": 0.08, "red": 0.04, "re": 0.25, "nir": 0.50}
SOIL = {"blue": 0.10, "green": 0.14, "red": 0.18, "re": 0.22, "nir": 0.26}
NAMES = ("blue", "green", "red", "re", "nir")
X0, Y0, PIX = 300000.0, 3500000.0, 0.05          # UTM 17N, 5 cm pixels
SIZE = 200


def utm():
    srs = osr.SpatialReference(); srs.ImportFromEPSG(32617); return srs


def write_image(path):
    """Left half of every 50-pixel block is plant, so each plot (a whole block) is 50 % canopy."""
    rng = np.random.default_rng(4)
    plant = (np.arange(SIZE)[None, :] % 50) < 25
    plant = np.repeat(plant, SIZE, 0)
    ds = gdal.GetDriverByName("GTiff").Create(path, SIZE, SIZE, 5, gdal.GDT_Float32)
    ds.SetGeoTransform((X0, PIX, 0, Y0, 0, -PIX)); ds.SetProjection(utm().ExportToWkt())
    for i, name in enumerate(NAMES, 1):
        values = np.where(plant, LEAF[name], SOIL[name]) + rng.normal(0, 0.004, plant.shape)
        band = ds.GetRasterBand(i); band.WriteArray(values.astype(np.float32)); band.SetDescription(name)
    ds = None


def plot_polygons():
    """Four 50 x 50-pixel plots in the top-left corner, as (plot_id, ring in UTM)."""
    out = []
    for k, (r, c) in enumerate(((0, 0), (0, 1), (1, 0), (1, 1))):
        x = X0 + c * 50 * PIX; y = Y0 - r * 50 * PIX
        inset = 0.5 * PIX                         # stay inside the block so only its pixels are touched
        ring = [(x + inset, y - inset), (x + 50 * PIX - inset, y - inset), (x + 50 * PIX - inset, y - 50 * PIX + inset),
                (x + inset, y - 50 * PIX + inset), (x + inset, y - inset)]
        out.append((101 + k, ring))
    return out


def write_vector(path, driver, srs_out=None, layers=None, z=False):
    """Write the plots (and optional extra layers) with any OGR driver; srs_out reprojects them."""
    ds = ogr.GetDriverByName(driver).CreateDataSource(path)
    target = srs_out or utm()
    transform = None
    if srs_out is not None:
        a = utm(); b = srs_out.Clone()
        a.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER); b.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        transform = osr.CoordinateTransformation(a, b)
    for name, kind in (layers or [("plots", "polygon")]):
        layer = ds.CreateLayer(name, srs=target, geom_type=ogr.wkbPoint if kind == "point" else (ogr.wkbPolygon25D if z else ogr.wkbPolygon))
        layer.CreateField(ogr.FieldDefn("plot_id", ogr.OFTInteger))
        for plot_id, ring in plot_polygons():
            feature = ogr.Feature(layer.GetLayerDefn()); feature.SetField("plot_id", plot_id)
            if kind == "point":
                geometry = ogr.CreateGeometryFromWkt("POINT ({} {})".format(*ring[0]))
            else:
                geometry = ogr.CreateGeometryFromWkt("POLYGON (({}))".format(", ".join(
                    "{} {}{}".format(x, y, " 5" if z else "") for x, y in ring)))
            if transform: geometry.Transform(transform)
            feature.SetGeometry(geometry); layer.CreateFeature(feature)
    ds = None


class PlotFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="ppe_plots_")
        cls.image = os.path.join(cls.dir, "field.tif"); write_image(cls.image)
        shp_dir = os.path.join(cls.dir, "my plots"); os.makedirs(shp_dir)
        cls.shp = os.path.join(shp_dir, "plots 2025.shp"); write_vector(cls.shp, "ESRI Shapefile")
        cls.zip = os.path.join(cls.dir, "plots.zip")
        with zipfile.ZipFile(cls.zip, "w") as z:
            for f in os.listdir(shp_dir): z.write(os.path.join(shp_dir, f), "inner folder/" + f)
            z.writestr("__MACOSX/inner folder/._plots 2025.shp", b"junk")
        wgs = osr.SpatialReference(); wgs.ImportFromEPSG(4326); wgs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)  # longitude, latitude as in real files
        cls.geojson = os.path.join(cls.dir, "plots.geojson"); write_vector(cls.geojson, "GeoJSON", srs_out=wgs)
        cls.kml = os.path.join(cls.dir, "plots.kml"); write_vector(cls.kml, "KML", srs_out=wgs)
        cls.gpkg = os.path.join(cls.dir, "plots.gpkg")
        write_vector(cls.gpkg, "GPKG", layers=[("centres", "point"), ("plots", "polygon"), ("plots_copy", "polygon")])
        cls.z3d = os.path.join(cls.dir, "plots3d.shp"); write_vector(cls.z3d, "ESRI Shapefile", z=True)
        cls.points = os.path.join(cls.dir, "points.geojson"); write_vector(cls.points, "GeoJSON", layers=[("p", "point")])
        cls.noprj = os.path.join(cls.dir, "noprj.shp"); write_vector(cls.noprj, "ESRI Shapefile"); os.remove(cls.noprj[:-4] + ".prj")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_every_format_opens_with_four_polygons(self):
        for path in (self.shp, self.zip, self.geojson, self.kml, self.gpkg, self.z3d, self.noprj,
                     self.shp[:-4] + ".dbf", self.shp[:-4] + ".prj", self.shp[:-4] + ".shx"):
            _, _, info = open_plot_file(path)
            self.assertEqual(info["features"], 4, path)
            over, total, _ = plots_over_image(path, self.image, info["layer"])
            self.assertEqual((over, total), (4, 4), path)

    def test_zip_leads_to_the_shapefile_and_ignores_mac_junk(self):
        _, _, info = open_plot_file(self.zip)
        self.assertTrue(info["source"].startswith("/vsizip/") and info["source"].endswith("inner folder/plots 2025.shp"))
        self.assertIn("inside the zip", info["note"])

    def test_side_file_leads_to_the_shapefile(self):
        _, _, info = open_plot_file(self.shp[:-4] + ".dbf")
        self.assertTrue(info["source"].endswith("plots 2025.shp")); self.assertIn("belongs to", info["note"])

    def test_geopackage_offers_only_polygon_layers(self):
        _, _, info = open_plot_file(self.gpkg)
        self.assertEqual(info["layers"], ["plots", "plots_copy"]); self.assertEqual(info["layer"], "plots")
        self.assertIn("|layername=plots", info["uri"])
        _, _, other = open_plot_file(self.gpkg, "plots_copy"); self.assertEqual(other["layer"], "plots_copy")

    def test_points_are_refused_with_a_clear_message(self):
        with self.assertRaises(ValueError) as caught:
            open_plot_file(self.points)
        self.assertIn("no polygons", str(caught.exception))

    def test_missing_shapefile_is_explained(self):
        lonely = os.path.join(self.dir, "lonely.dbf"); open(lonely, "w").close()
        with self.assertRaises(ValueError) as caught:
            open_plot_file(lonely)
        self.assertIn("same folder", str(caught.exception))

    def test_no_crs_is_noted(self):
        _, _, note = plots_over_image(self.noprj, self.image)
        self.assertIn("no coordinate system", note)

    def test_id_field_guess(self):
        self.assertEqual(guess_plot_id_field(["Name", "description", "plot"]), "plot")
        self.assertEqual(guess_plot_id_field(["FID_1", "Plot_ID"]), "Plot_ID")
        self.assertEqual(guess_plot_id_field(["a", "b"]), "")


class PlotStatisticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        PlotFileTests.setUpClass(); cls.files = PlotFileTests
        out = os.path.join(cls.files.dir, "run")
        engine = NativePPEEngine()
        cls.result = engine.run({"source_mode": "stacked", "stacked_path": cls.files.image, "output_dir": out, "work_dir": out})
        cls.mask = NativePPEEngine().apply({"vote_path": cls.result["vote_path"], "threshold": 0.5, "output_dir": out})["mask_path"]

    @classmethod
    def tearDownClass(cls):
        PlotFileTests.tearDownClass()

    def __getattr__(self, name):          # the plot files made by PlotFileTests (shp, zip, kml ...)
        return getattr(PlotFileTests, name)

    def export(self, path, layer=""):
        out = os.path.join(self.files.dir, "export_" + os.path.basename(path).replace(".", "_") + layer)
        result = NativePPEEngine().export_aoi_statistics({"source_mode": "stacked", "stacked_path": self.files.image,
                                                          "mask_path": self.mask, "export_dir": out, "aoi_path": path,
                                                          "aoi_id_field": "plot_id", "aoi_layer": layer})
        with open(result["canopy_csv"], newline="") as handle: canopy = list(csv.DictReader(handle))
        with open(result["aoi_csv"], newline="") as handle: stats = list(csv.DictReader(handle))
        return result, canopy, stats

    def test_canopy_and_statistics(self):
        result, canopy, stats = self.export(self.shp)
        self.assertEqual(result["plot_count"], 4)
        self.assertEqual([row["plot_id"] for row in canopy], ["101", "102", "103", "104"])
        for row in canopy:
            pixels, plant = int(row["plot_pixels"]), int(row["plant_pixels"])
            self.assertEqual(pixels, 2500)                       # 50 x 50 pixels, all touched
            self.assertAlmostEqual(float(row["canopy_pct"]), 100.0 * plant / pixels)
            self.assertAlmostEqual(float(row["canopy_pct"]), 50.0, delta=1.0)
            self.assertAlmostEqual(float(row["plot_area_m2"]), 2500 * PIX * PIX)
        for name in ("canopy_pct", "plot_area_m2", "plant_area_m2") + STAT_NAMES:
            self.assertIn(name, stats[0])
        ndvi = [row for row in stats if row["variable"] == "ndvi"][0]
        values = [float(ndvi[k]) for k in ("min", "p10", "q25", "median", "q75", "p90", "max")]
        self.assertEqual(values, sorted(values))
        self.assertAlmostEqual(float(ndvi["mean"]), (0.50 - 0.04) / (0.50 + 0.04), delta=0.02)

    def test_formats_give_the_same_numbers(self):
        _, reference, ref_stats = self.export(self.shp)
        key = lambda rows: [(r["plot_id"], r["plot_pixels"], r["plant_pixels"]) for r in rows]
        for path in (self.zip, self.geojson, self.kml, self.gpkg, self.z3d):
            _, canopy, stats = self.export(path)
            self.assertEqual(key(canopy), key(reference), path)
            self.assertEqual([r["mean"] for r in stats], [r["mean"] for r in ref_stats], path)


if __name__ == "__main__":
    unittest.main()
