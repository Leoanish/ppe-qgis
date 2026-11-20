"""Plant counter (4.3): count the plants in a finalized PPE plant mask.

Pieces smaller than the smallest plant are dropped; pieces closer than 1 cm (setting) are joined, so
the leaves of one plant split by a thin stem or petiole count once (PhenoBench seedlings: plants
counted exactly once 72 -> 88 %, split plants 21 -> 2 %). One plant = one joined group of at least the
smallest plant size with a sure core: >= 5 pixels where every PPE vote says plant. The core makes the count
robust to the threshold: soil spots let in by a low threshold have no core (PhenoBench seedling image
at threshold 0.50: 474 groups -> 13 plants; hand count 12). Groups much bigger than a typical plant are flagged "check" (they may be touching plants)
but still count as one: on PhenoBench, splitting them by size overcounted badly, so PPE does not guess.
Plants that touch (closed canopy) cannot be separated this way: count early in the season.
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

BIG_FACTOR = 2.5          # a group > 2.5 x the 75th-percentile plant is flagged "check" (may be several touching plants).
                          # The 75th percentile, not the median: small weeds pull the median down and made single crop plants look big.
CORE_VOTE = 0.999         # a plant has a core where every PPE vote says plant ...
CORE_PX = 5               # ... of at least 5 pixels (soil spots let through by a low threshold have none)
JOIN_CM = 1.0             # pieces closer than this are one plant (leaves split by a thin stem or petiole)
ROW_BAND = 0.2            # on a row = within 0.2 x row spacing of the row centre line
EARLY_NOTE = ("Counting works best before plants touch (early season). Groups flagged 'check' may be "
              "several touching plants; they count as one.")


def _plant_groups(mask_path, vote_path, min_px, join_px, hole_px, progress, log):
    """Plants of a 0/1 mask, GDAL only. 1) Pieces smaller than the smallest plant are dropped (they
    must not chain plants together). 2) Pieces closer than join_px are joined: leaves of one plant
    split by a thin stem or petiole become one plant. 3) Each joined group is one candidate plant:
    its plant pixels, sure-core pixels and centre (mean of its plant pixels, map coordinates).
    Returns (dataset, area_px, x, y, core_px or None)."""
    ds = safe_gdal_open(mask_path)
    if ds is None: raise ValueError("The finalized plant mask is missing or unreadable.")
    W, H = ds.RasterXSize, ds.RasterYSize; gt = ds.GetGeoTransform(); wkt = ds.GetProjection(); band = ds.GetRasterBand(1)
    mem = gdal.GetDriverByName("MEM"); STEP = 1024

    def new(dtype=gdal.GDT_Byte):
        r = mem.Create("", W, H, 1, dtype); r.SetGeoTransform(gt); r.SetProjection(wkt); return r
    plant = new(); pb = plant.GetRasterBand(1)
    for y in range(0, H, STEP):
        h = min(STEP, H - y); pb.WriteArray((band.ReadAsArray(0, y, W, h) == 1).astype(np.uint8), 0, y)
    # 1) drop specks. The sieve also fills small holes; '& original' puts the holes back.
    sieved = new(); sb = sieved.GetRasterBand(1)
    gdal.SieveFilter(pb, None, sb, max(2, int(math.ceil(min_px))), 8)
    for y in range(0, H, STEP):
        h = min(STEP, H - y); pb.WriteArray(pb.ReadAsArray(0, y, W, h) & sb.ReadAsArray(0, y, W, h), 0, y)
    progress(25, "Joining leaves of one plant")
    # 2) join pieces: everything within join_px / 2 of a plant pixel.
    joined = new(); jb = joined.GetRasterBand(1)
    if join_px >= 1:
        r = join_px / 2.0; prox = new(); xb = prox.GetRasterBand(1)
        gdal.ComputeProximity(pb, xb, ["VALUES=1", "DISTUNITS=PIXEL", "MAXDIST={}".format(int(math.ceil(r)) + 1), "NODATA=255"])
        for y in range(0, H, STEP):
            h = min(STEP, H - y); jb.WriteArray((xb.ReadAsArray(0, y, W, h) <= r).astype(np.uint8), 0, y)
        prox = None
    else:
        for y in range(0, H, STEP):
            h = min(STEP, H - y); jb.WriteArray(pb.ReadAsArray(0, y, W, h), 0, y)
    # Small soil holes inside a group are filled before tracing: same groups, ~10x faster on dense canopy.
    if hole_px:
        soil = new(); ob = soil.GetRasterBand(1)
        for y in range(0, H, STEP):
            h = min(STEP, H - y); ob.WriteArray(1 - jb.ReadAsArray(0, y, W, h), 0, y)
        gdal.SieveFilter(ob, None, ob, int(hole_px), 4)
        for y in range(0, H, STEP):
            h = min(STEP, H - y); jb.WriteArray(jb.ReadAsArray(0, y, W, h) | (1 - ob.ReadAsArray(0, y, W, h)).astype(np.uint8), 0, y)
        soil = None
    progress(40, "Finding plants")
    src = ogr.GetDriverByName("Memory").CreateDataSource("groups"); layer = src.CreateLayer("g", None, ogr.wkbPolygon)
    layer.CreateField(ogr.FieldDefn("v", ogr.OFTInteger))
    gdal.Polygonize(jb, jb, layer, 0, ["8CONNECTED=8"])
    n = layer.GetFeatureCount(); layer.CreateField(ogr.FieldDefn("gid", ogr.OFTInteger)); layer.ResetReading(); k = 0
    for f in layer:
        k += 1; f.SetField("gid", k); layer.SetFeature(f)
    ids = new(gdal.GDT_Int32); ib = ids.GetRasterBand(1)
    gdal.RasterizeLayer(ids, [1], layer, options=["ATTRIBUTE=gid"])
    progress(60, "Measuring plants")
    vote = safe_gdal_open(vote_path) if vote_path else None
    if vote is not None and (vote.RasterXSize, vote.RasterYSize) != (W, H):
        log("The vote raster does not match the mask: counting without the sure-core check."); vote = None
    elif vote_path and vote is None:
        log("The PPE vote raster is missing: counting without the sure-core check.")
    area = np.zeros(n + 1); sx = np.zeros(n + 1); sy = np.zeros(n + 1); core = np.zeros(n + 1) if vote is not None else None
    cols = np.arange(W, dtype=np.float64)
    for y in range(0, H, STEP):
        h = min(STEP, H - y); g = ib.ReadAsArray(0, y, W, h); p = (pb.ReadAsArray(0, y, W, h) == 1) & (g > 0)
        if not p.any(): continue
        gi = g[p]; yy, xx = np.nonzero(p)
        area += np.bincount(gi, minlength=n + 1)[:n + 1]
        sx += np.bincount(gi, weights=cols[xx] + 0.5, minlength=n + 1)[:n + 1]
        sy += np.bincount(gi, weights=yy + y + 0.5, minlength=n + 1)[:n + 1]
        if core is not None:
            v = vote.GetRasterBand(1).ReadAsArray(0, y, W, h); sure = p & (v >= CORE_VOTE) & (v <= 1.0)
            if sure.any(): core += np.bincount(g[sure], minlength=n + 1)[:n + 1]
    sel = area[1:] > 0; a = area[1:][sel]; cx = sx[1:][sel] / a; cy = sy[1:][sel] / a
    X = gt[0] + cx * gt[1] + cy * gt[2]; Y = gt[3] + cx * gt[4] + cy * gt[5]
    return ds, a, X, Y, (core[1:][sel] if core is not None else None)


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
    join_cm = float(cfg.get("join_cm", JOIN_CM))
    join_px = join_cm / (math.sqrt(pixel_m2) * 100) if pixel_m2 else float(cfg.get("join_px", 0))
    if join_px >= 1: log("Leaves closer than {:g} cm ({:.1f} pixels) are joined into one plant.".format(join_cm, join_px))
    progress(10, "Finding plants")
    ds, areas, xs, ys, core_n = _plant_groups(cfg["mask_path"], cfg.get("vote_path"), min_px, join_px, hole_px, progress, log)
    gt = ds.GetGeoTransform()
    srs = osr.SpatialReference(); wkt = ds.GetProjection()
    if wkt: srs.ImportFromWkt(wkt)
    progress(72, "Measuring plants")
    keep0 = areas >= min_px; no_core = 0
    if core_n is not None:
        no_core = int((keep0 & (core_n < CORE_PX)).sum()); keep0 &= core_n >= CORE_PX
        log("{} groups had no sure core (every vote plant) and were not counted.".format(no_core))
    areas, xs, ys = areas[keep0], xs[keep0], ys[keep0]; core = core_n
    n_all = len(areas)
    median = float(np.median(areas)) if n_all else 0.0
    big = areas > BIG_FACTOR * float(np.percentile(areas, 75)) if n_all >= 4 else np.zeros(n_all, bool)
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
    pl = out.CreateLayer("plants", srs if wkt else None, ogr.wkbPoint)
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
