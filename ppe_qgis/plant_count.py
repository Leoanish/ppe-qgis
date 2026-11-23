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
import json
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
STATUS_PLANT, STATUS_CHECK, STATUS_WEED, STATUS_ADDED = "plant", "check", "weed?", "added"   # point status; weed? is not counted
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


ROW_MIN_CM, ROW_MAX_CM = 15.0, 150.0   # row spacings searched (any row crop: cereals to wide-row crops)
ROW_MIN_CONTRAST = 1.8                 # rows are clear when their signal is this many times the background (other spacings/directions);
                                       # a 4 m tile needs half the field's row signal. Otherwise nothing is marked as weed there.


def _plant_pixel_sample(mask_path, n=2000000, seed=0):
    """Pixel coordinates of the plant pixels of the mask on a coarse grid (about 2 million cells), at most n."""
    ds = safe_gdal_open(mask_path); b = ds.GetRasterBand(1); W, H = ds.RasterXSize, ds.RasterYSize
    step = max(1, int(math.sqrt(W * H / 2e6)))             # read a coarse grid (about 2 million pixels)
    a = b.ReadAsArray(0, 0, W, H, buf_xsize=max(1, W // step), buf_ysize=max(1, H // step))
    ys, xs = np.nonzero(a == 1)
    if len(xs) > n:
        k = np.random.default_rng(seed).choice(len(xs), n, replace=False); xs, ys = xs[k], ys[k]
    return xs * (W / a.shape[1]) + 0.5, ys * (H / a.shape[0]) + 0.5


ROW_TILE_CM = 400.0                    # row positions are fitted per 4 m tile: plots can be shifted against each other


def _tiles(x, y, tile):
    tx = np.floor(x / tile).astype(np.int64); ty = np.floor(y / tile).astype(np.int64)
    _u, inv = np.unique(tx * 1000003 + ty, return_inverse=True)
    return inv, int(inv.max()) + 1 if len(inv) else 0


def _fold(sx, sy, tid, nt, angles, spacings):
    """Row strength for every direction x spacing: inside each tile, plant pixels folded by the spacing
    pile up at one phase when direction and spacing are right; strength = share-weighted mean over tiles."""
    out = np.empty((len(angles), len(spacings))); n = len(sx)
    for k, a in enumerate(angles):
        t = math.radians(a); d = sx * math.cos(t) + sy * math.sin(t)
        ph = 2 * math.pi * d[:, None] / spacings[None, :]
        c, s_ = np.cos(ph), np.sin(ph)
        re = np.zeros((nt, len(spacings))); im = np.zeros((nt, len(spacings)))
        np.add.at(re, tid, c); np.add.at(im, tid, s_)
        out[k] = np.hypot(re, im).sum(axis=0) / n
    return out


def _rows(px, py, weight, pixel_cm, spacing_cm=None, sample=None):
    """Crop rows, found on their own from the plant pixels of the mask: one direction and one spacing
    (ROW_MIN_CM to ROW_MAX_CM) for the image, row positions fitted per 4 m tile. Points farther than
    ROW_BAND x spacing from a row centre line are possible weeds. Where rows are not clear (field or tile),
    nothing is marked. A given spacing_cm skips the spacing search. Returns (on_row, info)."""
    n = len(px)
    if sample is None or len(sample[0]) < 200:
        return np.ones(n, bool), {"rows": 0, "note": "too few plant pixels to find rows; nothing marked as weed"}
    ax_, ay_ = sample; tile = ROW_TILE_CM / pixel_cm
    sx, sy = ax_, ay_
    if len(sx) > 20000:
        k = np.random.default_rng(1).choice(len(sx), 20000, replace=False); sx, sy = sx[k], sy[k]
    tid, nt = _tiles(sx, sy, tile)
    if spacing_cm:
        spacings = np.array([float(spacing_cm) / pixel_cm])
    else:
        spacings = np.exp(np.linspace(math.log(max(ROW_MIN_CM / pixel_cm, 3.0)), math.log(max(ROW_MAX_CM / pixel_cm, 4.0)), 60))
    angles = np.arange(0, 180, 3.0)
    R = _fold(sx, sy, tid, nt, angles, spacings); ka, ks = np.unravel_index(int(np.argmax(R)), R.shape)
    background = float(np.median(R))                      # strength of directions/spacings that are not rows
    a_f = np.arange(angles[ka] - 3, angles[ka] + 3.01, 0.25)
    s_f = spacings if len(spacings) == 1 else np.linspace(spacings[max(0, ks - 1)], spacings[min(len(spacings) - 1, ks + 1)], 21)
    Rf = _fold(sx, sy, tid, nt, a_f, s_f); ka, ks = np.unravel_index(int(np.argmax(Rf)), Rf.shape)
    a0, sp = float(a_f[ka]), float(s_f[ks]); strength = float(Rf[ka, ks])
    # Thin rows also line up at half (a third of) their spacing: take the widest spacing that fits as well.
    if spacing_cm is None:
        for mult in (3, 2):
            wide = sp * mult
            if wide <= ROW_MAX_CM / pixel_cm:
                rw = float(_fold(sx, sy, tid, nt, np.array([a0]), np.array([wide]))[0, 0])
                if rw >= 0.8 * strength:
                    sp, strength = wide, rw; break
    contrast = strength / max(background, 1e-6)
    info = {"strength": strength, "contrast": contrast, "angle_deg": a0, "spacing_px": sp, "spacing_cm": sp * pixel_cm}
    if contrast < ROW_MIN_CONTRAST or (spacing_cm is None and ks in (0, len(s_f) - 1) and sp in (spacings[0], spacings[-1])):
        info.update(rows=0, note="no clear rows (row signal {:.1f} x background); nothing marked as weed".format(contrast))
        return np.ones(n, bool), info
    # phase and strength of each tile (all sampled plant pixels), then each point against its own tile's rows
    sx, sy = ax_, ay_; tid, nt = _tiles(sx, sy, tile)
    t = math.radians(a0); ph_s = 2 * math.pi * (sx * math.cos(t) + sy * math.sin(t)) / sp
    re = np.bincount(tid, np.cos(ph_s), nt); im = np.bincount(tid, np.sin(ph_s), nt); cnt = np.bincount(tid, minlength=nt)
    tile_r = np.hypot(re, im) / np.maximum(cnt, 1)
    tile_phase = np.arctan2(im, re); tile_ok = (cnt >= 30) & (tile_r >= 0.5 * strength) & (tile_r >= 3.0 / np.sqrt(np.maximum(cnt, 1)))
    key = np.floor(sx / tile).astype(np.int64) * 1000003 + np.floor(sy / tile).astype(np.int64)
    ukey = np.zeros(nt, np.int64); ukey[tid] = key
    pk = np.floor(px / tile).astype(np.int64) * 1000003 + np.floor(py / tile).astype(np.int64)
    order = np.argsort(ukey); pos = np.searchsorted(ukey[order], pk); pos = np.clip(pos, 0, nt - 1)
    pt = np.where(ukey[order][pos] == pk, order[pos], -1)
    ph_p = 2 * math.pi * (px * math.cos(t) + py * math.sin(t)) / sp
    on_row = np.ones(n, bool); has = pt >= 0
    judged = np.zeros(n, bool); judged[has] = tile_ok[pt[has]]
    off = np.angle(np.exp(1j * (ph_p[judged] - tile_phase[pt[judged]]))) * sp / (2 * math.pi)
    on_row[judged] = np.abs(off) <= ROW_BAND * sp
    info.update(rows=1, tiles_with_rows=float(tile_ok.mean()), note="rows found {:.0f} cm apart (row signal {:.1f} x background); {} plants between rows marked as possible weeds".format(
        sp * pixel_cm, contrast, int((~on_row).sum())))
    return on_row, info


def count_plants(cfg, progress=None, log=None):
    """Count plants in cfg['mask_path'] (finalized PPE mask, 1 plant, 0 soil, 255 nodata).

    cfg: output_dir, vote_path (PPE vote raster: enables the sure-core check), min_plant_cm2 (default 1.0), row_spacing_cm (optional: keep plants on the rows), aoi_path / aoi_layer / aoi_id_field
    (optional plots). Writes ppe_plants.gpkg (one point per plant), ppe_count_summary.csv and, with
    plots, ppe_plot_counts.csv. Returns a dict with the counts and paths.
    """
    progress = progress or (lambda p, m: None); log = log or (lambda m: None); started = time.time()
    out_dir = cfg["output_dir"]; os.makedirs(out_dir, exist_ok=True)
    area_used = bool(cfg.get("area_wkts") or cfg.get("area_path"))
    if area_used:                       # 4.6.4: count only inside the area (the mask outside it becomes no data)
        progress(2, "Cutting the mask to the area")
        cfg = dict(cfg, mask_path=_mask_in_area(cfg, out_dir))
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
    if (cfg.get("find_rows") or cfg.get("row_spacing_cm")) and n_all:
        progress(80, "Finding crop rows")
        pxc = (xs - gt[0]) / gt[1]; pyc = (ys - gt[3]) / gt[5]
        pixel_cm = math.sqrt(pixel_m2) * 100 if pixel_m2 else None
        if pixel_cm:
            on_row, rows_info = _rows(pxc, pyc, areas, pixel_cm, cfg.get("row_spacing_cm"), _plant_pixel_sample(cfg["mask_path"]))
        else:
            rows_info = {"note": "the image is not in metres; rows not used"}
        log("Rows: " + rows_info["note"] + ".")
    to_cm2 = (pixel_m2 * 1e4) if pixel_m2 else None
    progress(85, "Writing the plant points")
    gpkg = os.path.join(out_dir, "ppe_plants.gpkg")
    if os.path.exists(gpkg): gdal.GetDriverByName("GPKG").Delete(gpkg)
    out = ogr.GetDriverByName("GPKG").CreateDataSource(gpkg)
    pl = out.CreateLayer("plants", srs if wkt else None, ogr.wkbPoint)
    for name, kind in (("plant_id", ogr.OFTInteger), ("area_cm2" if to_cm2 else "area_px", ogr.OFTReal), ("status", ogr.OFTString), ("on_row", ogr.OFTInteger)):
        pl.CreateField(ogr.FieldDefn(name, kind))
    pl.StartTransaction()
    for i in range(n_all):
        f = ogr.Feature(pl.GetLayerDefn()); p = ogr.Geometry(ogr.wkbPoint); p.AddPoint_2D(float(xs[i]), float(ys[i])); f.SetGeometry(p)
        f.SetField(0, i + 1); f.SetField(1, float(areas[i] * to_cm2) if to_cm2 else float(areas[i]))
        f.SetField(2, STATUS_WEED if not on_row[i] else (STATUS_CHECK if big[i] else STATUS_PLANT)); f.SetField(3, int(on_row[i])); pl.CreateFeature(f)
    pl.CommitTransaction(); out = None
    field_m2 = None
    if pixel_m2:
        band = ds.GetRasterBand(1); valid = 0
        for y in range(0, ds.RasterYSize, 2048):
            valid += int((band.ReadAsArray(0, y, ds.RasterXSize, min(2048, ds.RasterYSize - y)) != 255).sum())
        field_m2 = valid * pixel_m2
    info = {"mask_path": cfg["mask_path"], "field_m2": field_m2, "median_plant_cm2": (median * to_cm2) if to_cm2 else None,
            "min_plant_cm2": min_cm2 if pixel_m2 else None, "join_cm": join_cm, "rows": rows_info,
            "no_core_left_out": no_core if core is not None else None,
            "aoi_path": cfg.get("aoi_path") or "", "aoi_layer": cfg.get("aoi_layer") or "", "aoi_id_field": cfg.get("aoi_id_field") or "",
            "count_area": os.path.join(out_dir, "ppe_count_area.gpkg") if area_used else ""}
    with open(os.path.join(out_dir, "ppe_count_info.json"), "w", encoding="utf-8") as fh:
        json.dump(info, fh, indent=1)
    if cfg.get("aoi_path"): progress(92, "Counting per plot")
    summary = summarize_points(gpkg, log)
    summary["seconds"] = time.time() - started
    progress(100, "Counted {} plants".format(summary["plants"]))
    return summary


def read_points(points_path):
    """(x, y, status) of every point in ppe_plants.gpkg."""
    ds = ogr.Open(points_path)
    if ds is None: raise ValueError("The plant points file is missing: {}".format(points_path))
    lay = ds.GetLayer(); out = []
    for f in lay:
        g = f.GetGeometryRef()
        if g is None: continue
        out.append((g.GetX(), g.GetY(), f.GetField("status") or STATUS_PLANT))
    return out, lay.GetSpatialRef().ExportToWkt() if lay.GetSpatialRef() else ""


def summarize_points(points_path, log=None):
    """Totals (and counts per plot) from the points as they are now, after any corrections by the user.
    Counted: plant, check and added; not counted: weed?. Rewrites ppe_count_summary.csv and ppe_plot_counts.csv."""
    log = log or (lambda m: None); out_dir = os.path.dirname(points_path)
    try:
        with open(os.path.join(out_dir, "ppe_count_info.json"), encoding="utf-8") as fh: info = json.load(fh)
    except (OSError, ValueError):
        info = {}
    pts, wkt = read_points(points_path)
    counted = [(x, y, st) for x, y, st in pts if st != STATUS_WEED]
    n = {k: sum(1 for p in pts if p[2] == k) for k in (STATUS_PLANT, STATUS_CHECK, STATUS_ADDED, STATUS_WEED)}
    plants = len(counted); field_m2 = info.get("field_m2")
    summary = {"plants": plants, "flagged_big": n[STATUS_CHECK], "added_by_user": n[STATUS_ADDED], "weeds_not_counted": n[STATUS_WEED],
               "left_out_between_rows": n[STATUS_WEED], "no_core_left_out": info.get("no_core_left_out"),
               "median_plant_cm2": info.get("median_plant_cm2"), "min_plant_cm2": info.get("min_plant_cm2"),
               "plants_per_m2": (plants / field_m2) if field_m2 else None, "rows": info.get("rows"), "points_path": points_path, "note": EARLY_NOTE}
    path = os.path.join(out_dir, "ppe_count_summary.csv")
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(["plants", "flagged_check", "added_by_user", "weeds_not_counted", "median_plant_cm2", "smallest_plant_cm2", "plants_per_m2", "note"])
        w.writerow([plants, n[STATUS_CHECK], n[STATUS_ADDED], n[STATUS_WEED], _r(summary["median_plant_cm2"]), _r(summary["min_plant_cm2"]), _r(summary["plants_per_m2"], 3), EARLY_NOTE])
    summary["summary_path"] = path
    if info.get("aoi_path") and os.path.isfile(info["aoi_path"]):
        xs = np.array([p[0] for p in counted]); ys = np.array([p[1] for p in counted]); big = np.array([p[2] == STATUS_CHECK for p in counted])
        summary.update(_plot_counts(info, wkt, xs, ys, big, out_dir, log))
    return summary


def _r(v, d=2):
    return "" if v is None else round(float(v), d)


def _plot_counts(cfg, wkt, xs, ys, big, out_dir, log):
    vector, layer, info = open_plot_file(cfg["aoi_path"], cfg.get("aoi_layer") or None)
    transform = _plot_transform(layer, wkt); id_field = cfg.get("aoi_id_field") or ""
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


def _mask_in_area(cfg, out_dir):
    """A copy of the mask with everything outside the area set to no data (255): ppe_mask_in_area.tif.
    The area is kept as ppe_count_area.gpkg."""
    from .label_points import _area_raster
    area_path = _area_raster(cfg, out_dir, "ppe_count_area")
    src = safe_gdal_open(cfg["mask_path"]); area = gdal.Open(area_path); w, h = src.RasterXSize, src.RasterYSize
    path = os.path.join(out_dir, "ppe_mask_in_area.tif")
    if os.path.exists(path): os.remove(path)
    out = gdal.GetDriverByName("GTiff").Create(path, w, h, 1, gdal.GDT_Byte, options=["COMPRESS=DEFLATE", "TILED=YES"])
    out.SetGeoTransform(src.GetGeoTransform()); out.SetProjection(src.GetProjection()); out.GetRasterBand(1).SetNoDataValue(255)
    for y in range(0, h, 1024):
        n = min(1024, h - y); m = src.GetRasterBand(1).ReadAsArray(0, y, w, n); a = area.GetRasterBand(1).ReadAsArray(0, y, w, n)
        out.GetRasterBand(1).WriteArray(np.where(a > 0, m, 255).astype(np.uint8), 0, y)
    out.FlushCache(); out = None; area = None
    if os.path.exists(area_path): os.remove(area_path)
    return path


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
