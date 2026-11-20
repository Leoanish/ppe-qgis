"""Plant counter (4.3): count the plants in a finalized PPE plant mask.

One plant = one connected group of plant pixels (8-connected) that is at least the smallest plant
size and has a sure core: >= 5 pixels where every PPE vote says plant. The core makes the count
robust to the threshold: soil spots let in by a low threshold have no core (PhenoBench seedling image
at threshold 0.50: 474 groups -> 13 plants; hand count 12). Groups much bigger than a typical plant are flagged "check" (they may be touching plants)
but still count as one: on PhenoBench, splitting them by size overcounted badly, so PPE does not guess.
Optional: with the row spacing typed in, keep only plants on the crop rows (straight, parallel rows;
weeds between rows are left out).

Only numpy and GDAL/OGR are used, so it runs in any QGIS. The grouping is GDAL's Polygonize (C code).
"""
import csv
import math
import os
import time

import numpy as np
from osgeo import gdal, ogr, osr

from .native_engine import _pixel_area_m2, _plot_geometry, _plot_transform, open_plot_file, safe_gdal_open

BIG_FACTOR = 3.0          # a group > 3 x the median plant is flagged "check"
CORE_VOTE = 0.999         # a plant has a core where every PPE vote says plant ...
CORE_PX = 5               # ... of at least 5 pixels (soil spots let through by a low threshold have none)
ROW_BAND = 0.2            # on a row = within 0.2 x row spacing of the row centre line
EARLY_NOTE = ("Counting works best before plants touch (early season). Groups flagged 'check' may be "
              "several touching plants; they count as one.")


def _groups(mask_path, progress=None, fill_holes_px=0):
    """Connected plant groups of a 0/1 mask: list of (area_px, cx, cy) in pixel coordinates and the
    map polygons layer (kept in memory)."""
    ds = safe_gdal_open(mask_path)
    if ds is None:
        raise ValueError("The finalized plant mask is missing or unreadable.")
    band = ds.GetRasterBand(1)
    mem = ogr.GetDriverByName("Memory").CreateDataSource("plants")
    srs = osr.SpatialReference(); wkt = ds.GetProjection()
    if wkt: srs.ImportFromWkt(wkt)
    layer = mem.CreateLayer("groups", srs if wkt else None, ogr.wkbPolygon)
    layer.CreateField(ogr.FieldDefn("v", ogr.OFTInteger)); layer.CreateField(ogr.FieldDefn("gid", ogr.OFTInteger))
    # Only plant pixels (value 1) are traced: a 0/1 copy serves as its own mask band.
    plant = gdal.GetDriverByName("MEM").Create("", ds.RasterXSize, ds.RasterYSize, 1, gdal.GDT_Byte)
    plant.SetGeoTransform(ds.GetGeoTransform()); plant.SetProjection(wkt)
    pb = plant.GetRasterBand(1)
    soil = gdal.GetDriverByName("MEM").Create("", ds.RasterXSize, ds.RasterYSize, 1, gdal.GDT_Byte) if fill_holes_px else None
    for y in range(0, ds.RasterYSize, 1024):
        h = min(1024, ds.RasterYSize - y); a = (band.ReadAsArray(0, y, ds.RasterXSize, h) == 1).astype(np.uint8)
        pb.WriteArray(a, 0, y)
        if soil is not None: soil.GetRasterBand(1).WriteArray(1 - a, 0, y)
    if soil is not None:
        # Small soil holes inside a plant become plant: the count is the same (a hole is inside one
        # plant) but tracing is ~10x faster on dense canopy, where shapes have thousands of holes.
        sb = soil.GetRasterBand(1); gdal.SieveFilter(sb, None, sb, int(fill_holes_px), 4)
        for y in range(0, ds.RasterYSize, 1024):
            h = min(1024, ds.RasterYSize - y)
            a = pb.ReadAsArray(0, y, ds.RasterXSize, h) | (1 - sb.ReadAsArray(0, y, ds.RasterXSize, h)).astype(np.uint8)
            pb.WriteArray(a, 0, y)
        soil = None
    gdal.Polygonize(pb, pb, layer, 0, ["8CONNECTED=8"],
                    callback=(lambda f, m, d: (progress(int(10 + 60 * f), "Finding plants") or 1) if progress else 1))
    return ds, mem, layer


def _core_counts(ds, layer, vote_path, log):
    """Sure-core pixels (vote >= CORE_VOTE) in each group, indexed by feature id; None without votes."""
    vote = safe_gdal_open(vote_path) if vote_path else None
    if vote is None:
        if vote_path: log("The PPE vote raster is missing: counting without the sure-core check.")
        return None
    if (vote.RasterXSize, vote.RasterYSize) != (ds.RasterXSize, ds.RasterYSize):
        log("The vote raster does not match the mask: counting without the sure-core check."); return None
    n = layer.GetFeatureCount(); layer.ResetReading()
    for f in layer:
        f.SetField("gid", f.GetFID() + 1); layer.SetFeature(f)
    ids = gdal.GetDriverByName("MEM").Create("", ds.RasterXSize, ds.RasterYSize, 1, gdal.GDT_Int32)
    ids.SetGeoTransform(ds.GetGeoTransform()); ids.SetProjection(ds.GetProjection())
    gdal.RasterizeLayer(ids, [1], layer, options=["ATTRIBUTE=gid"])
    counts = np.zeros(n + 2, np.int64); ib = ids.GetRasterBand(1); vb = vote.GetRasterBand(1)
    for y in range(0, ds.RasterYSize, 1024):
        h = min(1024, ds.RasterYSize - y); g = ib.ReadAsArray(0, y, ds.RasterXSize, h); v = vb.ReadAsArray(0, y, ds.RasterXSize, h)
        sure = (g > 0) & (v >= CORE_VOTE) & (v <= 1.0)
        if sure.any(): counts += np.bincount(g[sure], minlength=n + 2)[:n + 2]
    return counts[1:]          # by feature id (gid - 1)


def _rows(px, py, weight, spacing_px):
    """Crop rows from plant centres, with the row spacing known (typed by the user).

    For each direction, plant centres are folded by the row spacing; on the right direction they pile
    up at one phase (the row centre). Returns (keep, info): keep = within ROW_BAND x spacing of a row."""
    n = len(px)
    if n < 10 or spacing_px < 3:
        return np.ones(n, bool), {"rows": 0, "note": "too few plants to find rows; all plants kept"}
    w = weight / weight.sum()
    def strength(a):
        t = math.radians(a); ph = 2 * math.pi * (px * math.cos(t) + py * math.sin(t)) / spacing_px
        z = (w * np.exp(1j * ph)).sum(); return abs(z), np.angle(z), ph
    angles = np.arange(0, 180, 0.5); r = [strength(a)[0] for a in angles]; a0 = float(angles[int(np.argmax(r))])
    fine = np.arange(a0 - 0.5, a0 + 0.51, 0.05); rf = [strength(a)[0] for a in fine]; a0 = float(fine[int(np.argmax(rf))])
    R, phase, ph = strength(a0)
    if R < 0.3:
        return np.ones(n, bool), {"rows": 0, "strength": float(R), "note": "no clear rows at this spacing; all plants kept"}
    off = np.angle(np.exp(1j * (ph - phase))) * spacing_px / (2 * math.pi)
    keep = np.abs(off) <= ROW_BAND * spacing_px
    return keep, {"strength": float(R), "angle_deg": a0, "spacing_px": float(spacing_px),
                  "note": "rows found (strength {:.2f}); {} plants between rows left out".format(R, int((~keep).sum()))}


def count_plants(cfg, progress=None, log=None):
    """Count plants in cfg['mask_path'] (finalized PPE mask, 1 plant, 0 soil, 255 nodata).

    cfg: output_dir, vote_path (PPE vote raster: enables the sure-core check), min_plant_cm2 (default 1.0), row_spacing_cm (optional: keep plants on the rows), aoi_path / aoi_layer / aoi_id_field
    (optional plots). Writes ppe_plants.gpkg (one point per plant), ppe_count_summary.csv and, with
    plots, ppe_plot_counts.csv. Returns a dict with the counts and paths.
    """
    progress = progress or (lambda p, m: None); log = log or (lambda m: None); started = time.time()
    out_dir = cfg["output_dir"]; os.makedirs(out_dir, exist_ok=True)
    progress(5, "Reading the plant mask")
    probe = safe_gdal_open(cfg["mask_path"])
    if probe is None: raise ValueError("The finalized plant mask is missing or unreadable.")
    pixel_m2 = _pixel_area_m2(probe); probe = None
    min_cm2 = float(cfg.get("min_plant_cm2", 1.0))
    if pixel_m2:
        min_px = max(2.0, min_cm2 / (pixel_m2 * 1e4))
    else:
        min_px = float(cfg.get("min_plant_px", 4)); log("The image is not in metres; smallest plant = {:g} pixels.".format(min_px))
    hole_px = int(cfg.get("fill_holes_px", max(1000, 50 * min_px)))
    ds, mem, layer = _groups(cfg["mask_path"], progress, hole_px)
    gt = ds.GetGeoTransform(); px_area = abs(gt[1] * gt[5]) or 1.0
    progress(72, "Measuring plants")
    core = _core_counts(ds, layer, cfg.get("vote_path"), log)
    areas, xs, ys = [], [], []; no_core = 0
    layer.ResetReading()
    for f in layer:
        g = f.GetGeometryRef()
        if g is None: continue
        a = g.GetArea() / px_area
        if a < min_px: continue
        if core is not None and core[f.GetFID()] < CORE_PX: no_core += 1; continue
        c = g.Centroid(); areas.append(a); xs.append(c.GetX()); ys.append(c.GetY())
    areas = np.array(areas); xs = np.array(xs); ys = np.array(ys)
    if core is not None: log("{} groups had no sure core (every vote plant) and were not counted.".format(no_core))
    n_all = len(areas)
    median = float(np.median(areas)) if n_all else 0.0
    big = areas > BIG_FACTOR * median if n_all else np.zeros(0, bool)
    rows_info = None; on_row = np.ones(n_all, bool)
    if cfg.get("row_spacing_cm") and n_all:
        progress(80, "Finding crop rows")
        pxc = (xs - gt[0]) / gt[1]; pyc = (ys - gt[3]) / gt[5]
        pixel_cm = math.sqrt(pixel_m2) * 100 if pixel_m2 else None
        if pixel_cm:
            on_row, rows_info = _rows(pxc, pyc, areas, float(cfg["row_spacing_cm"]) / pixel_cm)
        else:
            rows_info = {"note": "the image is not in metres; rows not used"}
        log("Rows: " + rows_info["note"] + ".")
    keep = on_row
    to_cm2 = (pixel_m2 * 1e4) if pixel_m2 else None
    progress(85, "Writing the plant points")
    gpkg = os.path.join(out_dir, "ppe_plants.gpkg")
    if os.path.exists(gpkg): gdal.GetDriverByName("GPKG").Delete(gpkg)
    out = ogr.GetDriverByName("GPKG").CreateDataSource(gpkg)
    pl = out.CreateLayer("plants", layer.GetSpatialRef(), ogr.wkbPoint)
    for name, kind in (("plant_id", ogr.OFTInteger), ("area_cm2" if to_cm2 else "area_px", ogr.OFTReal), ("check", ogr.OFTString), ("on_row", ogr.OFTInteger)):
        pl.CreateField(ogr.FieldDefn(name, kind))
    pl.StartTransaction(); pid = 0
    for i in range(n_all):
        if not keep[i]: continue
        pid += 1; f = ogr.Feature(pl.GetLayerDefn()); p = ogr.Geometry(ogr.wkbPoint); p.AddPoint_2D(float(xs[i]), float(ys[i])); f.SetGeometry(p)
        f.SetField(0, pid); f.SetField(1, float(areas[i] * to_cm2) if to_cm2 else float(areas[i]))
        f.SetField(2, "big: may be several plants" if big[i] else ""); f.SetField(3, int(on_row[i])); pl.CreateFeature(f)
    pl.CommitTransaction(); out = None
    plants = int(keep.sum()); flagged = int((big & keep).sum())
    field_m2 = None
    if pixel_m2:
        band = ds.GetRasterBand(1); valid = 0
        for y in range(0, ds.RasterYSize, 2048):
            valid += int((band.ReadAsArray(0, y, ds.RasterXSize, min(2048, ds.RasterYSize - y)) != 255).sum())
        field_m2 = valid * pixel_m2
    summary = {"plants": plants, "flagged_big": flagged, "no_core_left_out": no_core if core is not None else None, "left_out_between_rows": int(n_all - plants),
               "median_plant_cm2": (median * to_cm2) if to_cm2 else None, "min_plant_cm2": min_cm2 if pixel_m2 else None,
               "plants_per_m2": (plants / field_m2) if field_m2 else None, "rows": rows_info,
               "points_path": gpkg, "note": EARLY_NOTE}
    with open(os.path.join(out_dir, "ppe_count_summary.csv"), "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(["plants", "flagged_big", "left_out_between_rows", "median_plant_cm2", "smallest_plant_cm2", "plants_per_m2", "note"])
        w.writerow([plants, flagged, n_all - plants, _r(summary["median_plant_cm2"]), _r(summary["min_plant_cm2"]), _r(summary["plants_per_m2"], 3), EARLY_NOTE])
    summary["summary_path"] = os.path.join(out_dir, "ppe_count_summary.csv")
    if cfg.get("aoi_path"):
        progress(92, "Counting per plot")
        summary.update(_plot_counts(cfg, ds, xs[keep], ys[keep], big[keep], out_dir, log))
    summary["seconds"] = time.time() - started
    progress(100, "Counted {} plants".format(plants))
    return summary


def _r(v, d=2):
    return "" if v is None else round(float(v), d)


def _plot_counts(cfg, ds, xs, ys, big, out_dir, log):
    vector, layer, info = open_plot_file(cfg["aoi_path"], cfg.get("aoi_layer") or None)
    transform = _plot_transform(layer, ds.GetProjection()); id_field = cfg.get("aoi_id_field") or ""
    # Points into a memory layer with a spatial index-like filter (OGR filters by envelope first).
    mem = ogr.GetDriverByName("Memory").CreateDataSource("pts"); pts = mem.CreateLayer("p", None, ogr.wkbPoint)
    pts.CreateField(ogr.FieldDefn("big", ogr.OFTInteger))
    for x, y, b in zip(xs, ys, big):
        f = ogr.Feature(pts.GetLayerDefn()); g = ogr.Geometry(ogr.wkbPoint); g.AddPoint_2D(float(x), float(y)); f.SetGeometry(g); f.SetField(0, int(b)); pts.CreateFeature(f)
    rows = []; layer.ResetReading(); k = 0
    for feature in layer:
        geom = _plot_geometry(feature, transform)
        if geom is None: continue
        k += 1; pid = feature.GetField(id_field) if id_field else feature.GetFID()
        pts.SetSpatialFilter(geom); n = 0; nb = 0
        for p in pts:
            n += 1; nb += p.GetField(0)
        area = geom.GetArea()
        rows.append([pid, n, nb, round(area, 3), round(n / area, 3) if area > 0 else ""])
    pts.SetSpatialFilter(None)
    path = os.path.join(out_dir, "ppe_plot_counts.csv")
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(["plot_id", "plants", "flagged_big", "plot_area_m2", "plants_per_m2"]); w.writerows(rows)
    log("Counted plants in {} plots.".format(k))
    return {"plot_counts_path": path, "plots": k}


def count_batch(cfg, progress=None, log=None):
    """Count every finalized mask of a batch. cfg: count_items [{name, mask_path}], output_dir (the
    batch folder), plus the count_plants settings. Each image's points go to <mask folder>/ppe_count;
    one table for all images: ppe_batch_counts.csv."""
    progress = progress or (lambda p, m: None); log = log or (lambda m: None)
    items = [i for i in cfg.get("count_items") or [] if i.get("mask_path") and os.path.isfile(i["mask_path"])]
    if not items: raise ValueError("No finalized batch masks to count: write the batch masks first (Batch tab).")
    rows = []; started = time.time(); n = len(items)
    for k, it in enumerate(items):
        sub = dict(cfg, mask_path=it["mask_path"], vote_path=it.get("vote_path") or "", output_dir=os.path.join(os.path.dirname(it["mask_path"]), "ppe_count"), aoi_path=cfg.get("aoi_path") or "")
        r = count_plants(sub, progress=lambda p, m, k=k: progress(int(100 * (k + p / 100.0) / n), "{} ({}/{}): {}".format(it["name"], k + 1, n, m)), log=log)
        rows.append([it["name"], r["plants"], r["flagged_big"], r["left_out_between_rows"], _r(r["median_plant_cm2"]), _r(r["plants_per_m2"], 3), r["points_path"]])
        log("Count {}/{}: {} plants in {}.".format(k + 1, n, r["plants"], it["name"]))
    path = os.path.join(cfg["output_dir"], "ppe_batch_counts.csv")
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(["image", "plants", "flagged_big", "left_out_between_rows", "median_plant_cm2", "plants_per_m2", "points_path"]); w.writerows(rows)
    progress(100, "Counted {} images".format(n))
    return {"batch_counts_csv": path, "rows": rows, "plants": sum(r[1] for r in rows), "images": n, "seconds": time.time() - started, "note": EARLY_NOTE}
