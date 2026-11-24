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
EARLY_NOTE = ("For the early season only: count before the leaves of neighbouring plants touch or overlap; plants whose "
              "canopies overlap are counted as one. Groups flagged 'check' may be several touching plants; they count as one "
              "unless you split them.")


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
    for name, kind in (("plant_id", ogr.OFTInteger), ("area_cm2" if to_cm2 else "area_px", ogr.OFTReal), ("status", ogr.OFTString), ("on_row", ogr.OFTInteger),
                       ("split_of", ogr.OFTInteger)):
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
    if cfg.get("auto_split", True) and rows_info and rows_info.get("rows"):
        progress(88, "Splitting touching plants by the plant spacing")
        auto_split(gpkg, cfg.get("plant_spacing_cm"), log)
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
    Counted: plant, check (as one plant), split and added; not counted: weed?. Rewrites ppe_count_summary.csv and ppe_plot_counts.csv."""
    log = log or (lambda m: None); out_dir = os.path.dirname(points_path)
    try:
        with open(os.path.join(out_dir, "ppe_count_info.json"), encoding="utf-8") as fh: info = json.load(fh)
    except (OSError, ValueError):
        info = {}
    pts, wkt = read_points(points_path)
    counted = [(x, y, st) for x, y, st in pts if st != STATUS_WEED]
    n = {k: sum(1 for p in pts if p[2] == k) for k in (STATUS_PLANT, STATUS_CHECK, STATUS_ADDED, STATUS_WEED, "split", "auto split")}
    plants = len(counted); field_m2 = info.get("field_m2")
    summary = {"plants": plants, "flagged_big": n[STATUS_CHECK], "added_by_user": n[STATUS_ADDED], "weeds_not_counted": n[STATUS_WEED],
               "plain_plants": n[STATUS_PLANT], "split_by_user": n["split"], "split_by_ppe": n["auto split"],
               "plant_spacing_cm": info.get("plant_spacing_cm"), "auto_split": info.get("auto_split"),
               "left_out_between_rows": n[STATUS_WEED], "no_core_left_out": info.get("no_core_left_out"),
               "median_plant_cm2": info.get("median_plant_cm2"), "min_plant_cm2": info.get("min_plant_cm2"),
               "plants_per_m2": (plants / field_m2) if field_m2 else None, "rows": info.get("rows"), "points_path": points_path, "note": EARLY_NOTE}
    path = os.path.join(out_dir, "ppe_count_summary.csv")
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(["plants_counted", "of_which_plant", "of_which_check_counted_as_one", "of_which_split_by_ppe", "of_which_split_by_user",
                                        "of_which_added_by_user", "weeds_not_counted", "median_plant_cm2", "smallest_plant_cm2", "plant_spacing_cm",
                                        "split_longer_than_cm", "plants_per_m2", "note"])
        w.writerow([plants, n[STATUS_PLANT], n[STATUS_CHECK], n["auto split"], n["split"], n[STATUS_ADDED], n[STATUS_WEED], _r(summary["median_plant_cm2"]),
                    _r(summary["min_plant_cm2"]), _r(info.get("plant_spacing_cm"), 1),
                    _r(1.5 * info["plant_spacing_cm"] if info.get("plant_spacing_cm") else None, 1), _r(summary["plants_per_m2"], 3), EARLY_NOTE])
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


# ---- splitting touching plants (4.6.6) -----------------------------------------------------------
# A 'check' point is a group much bigger than the larger plants: most likely several plants touching.
# Splitting by size alone made counts worse (big single plants were split too), so the user decides how
# many plants a group holds and PPE finds that many lobes. On PhenoBench 'check' groups with 2+ plants
# (31 groups, 120 plants), splitting with the right number raised the plants with exactly one point
# from 26 to 53 %; neither size nor shape guessed the number well (best: 10 of 42 exact).
STATUS_SPLIT = "split"


def _dilate(m, r=1):
    p = np.pad(m, r); h, w = m.shape; out = np.zeros_like(m)
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            out |= p[r + dy:r + dy + h, r + dx:r + dx + w]
    return out


def group_at(points_path, x, y, area_cm2=None):
    """The plant pixels of the group under a point (map coordinates), as PPE grouped them: plant
    pixels connected through gaps up to the join distance. Returns (mask window bool array, x0, y0,
    geotransform, pixel_cm, info)."""
    with open(os.path.join(os.path.dirname(points_path), "ppe_count_info.json"), encoding="utf-8") as fh: info = json.load(fh)
    return _group_window(info, x, y, area_cm2)


def _group_window(info, x, y, area_cm2=None, ds=None):
    ds = ds or safe_gdal_open(info["mask_path"]); gt = ds.GetGeoTransform(); W, H = ds.RasterXSize, ds.RasterYSize
    pixel_cm = 100.0 * math.sqrt(abs(gt[1] * gt[5])) if _pixel_area_m2(ds) else 1.0
    a_px = (area_cm2 / pixel_cm ** 2) if area_cm2 else 400.0
    half = int(min(2000, max(40, 3.0 * math.sqrt(a_px / math.pi))))
    cx = int((x - gt[0]) / gt[1]); cy = int((y - gt[3]) / gt[5])
    x0, y0 = max(0, cx - half), max(0, cy - half); x1, y1 = min(W, cx + half + 1), min(H, cy + half + 1)
    plant = ds.GetRasterBand(1).ReadAsArray(x0, y0, x1 - x0, y1 - y0) == 1
    if not plant.any(): return None
    join_px = max(0, int(round((info.get("join_cm") or 0) / pixel_cm / 2.0)))
    allowed = _dilate(plant, join_px) if join_px else plant.copy()
    py, px = np.nonzero(plant); k = int(np.argmin((px - (cx - x0)) ** 2 + (py - (cy - y0)) ** 2))   # plant pixel nearest the point
    seed = np.zeros_like(plant); seed[py[k], px[k]] = True
    for _ in range(4 * half):                       # flood fill through the joined area
        grown = _dilate(seed) & allowed
        if (grown == seed).all(): break
        seed = grown
    return seed & plant, x0, y0, gt, pixel_cm, info


def suggest_split(points_path, x, y, area_cm2):
    """A rough number of plants: the group's size over the median plant, at least 2. Not used by the
    split tool (too often wrong where plant sizes vary); kept for scripts."""
    with open(os.path.join(os.path.dirname(points_path), "ppe_count_info.json"), encoding="utf-8") as fh: info = json.load(fh)
    med = info.get("median_plant_cm2") or 0
    return max(2, int(round(area_cm2 / med))) if med and area_cm2 else 2


def split_group(points_path, x, y, n, area_cm2=None):
    """Split the group under a point into n plants. Lobe centres are the n pixels farthest inside the
    group (distance to the nearest non-plant pixel), each at least a typical plant's radius from the
    others; every pixel then goes to its nearest centre and the centres move to the middle of their
    pixels (a few k-means steps). Returns [(x, y, area_cm2)] in map coordinates, or [] if nothing found."""
    g = group_at(points_path, x, y, area_cm2)
    return _split_window(g, n)


def _split_window(g, n):
    if g is None or n < 2: return []
    m, x0, y0, gt, pixel_cm, info = g
    py, px = np.nonzero(m)
    if len(px) < n: return []
    mem = gdal.GetDriverByName("MEM").Create("", m.shape[1], m.shape[0], 1, gdal.GDT_Byte); mem.GetRasterBand(1).WriteArray((~m).astype(np.uint8))
    dist = gdal.GetDriverByName("MEM").Create("", m.shape[1], m.shape[0], 1, gdal.GDT_Float32)
    gdal.ComputeProximity(mem.GetRasterBand(1), dist.GetRasterBand(1), ["VALUES=1", "DISTUNITS=PIXEL"])
    d = dist.GetRasterBand(1).ReadAsArray()[py, px]
    pts = np.column_stack([px, py]).astype(np.float64)
    sep = math.sqrt(len(px) / n / math.pi)          # a lobe's radius if the n plants were equal
    order = np.argsort(-d); seeds = []
    for i in order:                                 # deepest pixels first, kept apart
        if all((px[i] - px[j]) ** 2 + (py[i] - py[j]) ** 2 >= sep ** 2 for j in seeds): seeds.append(i)
        if len(seeds) == n: break
    while len(seeds) < n:                           # not enough separate lobes: farthest point from the chosen ones
        dd = np.min([(px - px[j]) ** 2 + (py - py[j]) ** 2 for j in seeds], axis=0); seeds.append(int(np.argmax(dd)))
    c = np.array([[px[j], py[j]] for j in seeds], np.float64)
    for _ in range(10):
        lab = np.argmin(((pts[:, None, :] - c[None, :, :]) ** 2).sum(2), 1)
        new = np.array([pts[lab == k].mean(0) if (lab == k).any() else c[k] for k in range(n)])
        if np.allclose(new, c): break
        c = new
    lab = np.argmin(((pts[:, None, :] - c[None, :, :]) ** 2).sum(2), 1)
    out = []
    for k in range(n):
        sel = lab == k
        if not sel.any(): continue
        cx, cy = pts[sel].mean(0) + (x0 + 0.5, y0 + 0.5)
        out.append((gt[0] + cx * gt[1] + cy * gt[2], gt[3] + cx * gt[4] + cy * gt[5], float(sel.sum()) * pixel_cm ** 2))
    return out


# ---- 4.7: split touching plants by the plant spacing in the row ------------------------------------------
# A 'check' group's length along the row over the in-row plant spacing is close to the number of plants in
# it. On PhenoBench (61 check groups; spacing measured from many plants per date, as in an orthomosaic)
# round(length / spacing) cut the wrong plants in check groups from 51 to 25 (34 to 10 in mid early
# season; 17 to 15 once canopies close). From a single 1 m image the spacing was too rough to help, so PPE
# measures it only from enough gaps, or the user types it. The n points are placed as in ✂ Split (deepest
# pixels, then k-means): true plants with exactly one point went from 33 to 63 % (starting the centres
# evenly along the row gave 59 %).
STATUS_AUTO = "auto split"          # split by PPE from the plant spacing; counted; the user can merge or split again
SPACING_MIN_GAPS = 20               # plant spacing is measured only from at least this many gaps between plants


def _ensure_split_field(path):
    ds = ogr.Open(path, 1); lay = ds.GetLayer()
    if lay.GetLayerDefn().GetFieldIndex("split_of") < 0:
        lay.CreateField(ogr.FieldDefn("split_of", ogr.OFTInteger))
    ds = None


def confident_range(areas):
    """The size range of a normal single plant in THIS image: median area x/÷ exp(2 sigma), sigma from the
    spread of the log areas (MAD), kept within x0.4 - x2.5 of the median. A uniform stand gets a tight range,
    an uneven one a wider range. Returns (median, low, high) or None with fewer than 3 plants."""
    a = np.array([v for v in areas if v is not None and v > 0], float)
    if len(a) < 3: return None
    med = float(np.median(a)); la = np.log(a)
    sig = max(0.15, 1.4826 * float(np.median(np.abs(la - np.median(la)))))
    return med, max(0.4 * med, med * math.exp(-2 * sig)), min(2.5 * med, med * math.exp(2 * sig))


def spacing_gaps(px, py, angle_deg, row_spacing_px, tile_px, use=None):
    """Gaps (pixels) between neighbouring plants along each row: plant centres in pixel coordinates are
    grouped into rows per 4 m tile (by the gaps across the rows) and sorted along the row; a gap is kept
    only when both neighbours are in `use` (all when None)."""
    px = np.asarray(px, float); py = np.asarray(py, float)
    use = np.ones(len(px), bool) if use is None else np.asarray(use, bool)
    if len(px) < 2: return []
    t = math.radians(angle_deg); d = px * math.cos(t) + py * math.sin(t); u = -px * math.sin(t) + py * math.cos(t)
    gaps = []
    tid = np.floor(px / tile_px) * 1000003 + np.floor(py / tile_px)
    for k in np.unique(tid):
        sel = np.nonzero(tid == k)[0]
        if len(sel) < 2: continue
        o = sel[np.argsort(d[sel])]; start = 0
        for i in range(1, len(o) + 1):
            if i == len(o) or d[o[i]] - d[o[i - 1]] > 0.5 * row_spacing_px:
                r = o[start:i]; r = r[np.argsort(u[r])]
                gaps += [float(u[b] - u[a]) for a, b in zip(r[:-1], r[1:]) if use[a] and use[b] and u[b] > u[a]]
                start = i
    return gaps


def spacing_from_gaps(gaps):
    """The plant spacing from gaps: the median gap, after dividing gaps over missing plants by their number
    of steps. None with fewer than SPACING_MIN_GAPS gaps."""
    g = np.array([v for v in gaps if v > 0], float)
    if len(g) < SPACING_MIN_GAPS: return None
    s0 = float(np.median(g)); return float(np.median(g / np.maximum(1, np.round(g / s0))))


def plant_spacing(px, py, angle_deg, row_spacing_px, tile_px, use=None):
    """In-row plant spacing (pixels). Returns (spacing, gaps used) or (None, gaps) when there are fewer
    than SPACING_MIN_GAPS gaps."""
    g = spacing_gaps(px, py, angle_deg, row_spacing_px, tile_px, use)
    return spacing_from_gaps(g), len(g)


def _read_features(path):
    ds = ogr.Open(path); lay = ds.GetLayer(); out = []
    names = [lay.GetLayerDefn().GetFieldDefn(i).GetName() for i in range(lay.GetLayerDefn().GetFieldCount())]
    for f in lay:
        g = f.GetGeometryRef()
        if g is None: continue
        r = {k: f.GetField(k) for k in names}; r["fid"] = f.GetFID(); r["x"] = g.GetX(); r["y"] = g.GetY(); out.append(r)
    return out


def auto_split(points_path, plant_spacing_cm=None, log=None):
    """Split every 'check' group into round(length along the row / plant spacing) plants (status
    'auto split', field split_of = the original plant_id). Needs rows (count with rows on); the spacing is
    measured from the plants on the rows unless given. Groups that come out as one plant stay 'check'.
    Writes plant_spacing_cm into ppe_count_info.json. Returns a dict with what was done."""
    log = log or (lambda m: None); out_dir = os.path.dirname(points_path)
    info_path = os.path.join(out_dir, "ppe_count_info.json")
    with open(info_path, encoding="utf-8") as fh: info = json.load(fh)
    rows = info.get("rows") or {}
    res = {"groups_split": 0, "points_added": 0, "plant_spacing_cm": None, "spacing_from": ""}
    if not rows.get("rows") or rows.get("angle_deg") is None:
        res["note"] = "no crop rows found, so touching plants were not split by spacing (split them with ✂ Split)"
        info["auto_split"] = res
        with open(info_path, "w", encoding="utf-8") as fh: json.dump(info, fh, indent=1)
        return res
    ds = safe_gdal_open(info["mask_path"]); gt = ds.GetGeoTransform()
    pixel_cm = 100.0 * math.sqrt(abs(gt[1] * gt[5])); a0 = float(rows["angle_deg"])
    feats = _read_features(points_path)
    if plant_spacing_cm:
        s_px = float(plant_spacing_cm) / pixel_cm; res["spacing_from"] = "given"
    else:
        # 4.7 (confident plants): the spacing is measured only between neighbouring CONFIDENT plants, single
        # plants of normal size for this image (`confident_range`); leaf pieces, plants cut at the image edge
        # and oversized plants make false gaps. On PhenoBench this moved the measured spacing from 15.7 to
        # 20.6 cm (true 21.2) on the latest date and cut the wrong plants in check patches from 32 to 22.
        row_pts = [f for f in feats if f.get("status") != STATUS_WEED]
        area_key = "area_cm2" if any(f.get("area_cm2") is not None for f in feats) else "area_px"
        rng = confident_range([f.get(area_key) for f in row_pts if f.get("status") == STATUS_PLANT])
        use = [f.get("status") == STATUS_PLANT and rng is not None and f.get(area_key) is not None and rng[1] <= f[area_key] <= rng[2]
               for f in row_pts]
        res["confident_plants"] = int(sum(use))
        if rng is not None:
            k = 1.0 if area_key == "area_cm2" else pixel_cm ** 2
            res["confident_range_cm2"] = [round(rng[1] * k, 1), round(rng[2] * k, 1)]
        s_px, n_gaps = plant_spacing([(f["x"] - gt[0]) / gt[1] for f in row_pts], [(f["y"] - gt[3]) / gt[5] for f in row_pts], a0,
                                     float(rows["spacing_cm"]) / pixel_cm, ROW_TILE_CM / pixel_cm, use)
        if s_px is None:
            res["note"] = ("too few confident plants in the rows to measure the plant spacing ({} gaps between them, {} needed); "
                           "type the spacing to split touching plants").format(n_gaps, SPACING_MIN_GAPS)
            info["auto_split"] = res
            with open(info_path, "w", encoding="utf-8") as fh: json.dump(info, fh, indent=1)
            return res
        res["spacing_from"] = "measured from {} gaps between {} confident plants, single plants of {}-{} cm2".format(
            n_gaps, res["confident_plants"], *[int(round(v)) for v in res.get("confident_range_cm2", [0, 0])])
    res["plant_spacing_cm"] = s_px * pixel_cm
    checks = [f for f in feats if f.get("status") == STATUS_CHECK]
    _ensure_split_field(points_path)
    out = ogr.Open(points_path, 1); lay = out.GetLayer()
    next_id = max([f.get("plant_id") or 0 for f in feats] or [0]) + 1
    area_field = "area_cm2" if lay.GetLayerDefn().GetFieldIndex("area_cm2") >= 0 else "area_px"
    t = math.radians(a0)
    for f in checks:
        g = _group_window(info, f["x"], f["y"], f.get(area_field) if area_field == "area_cm2" else None, ds)
        if g is None: continue
        m = g[0]; yy, xx = np.nonzero(m)
        if len(xx) < 2: continue
        u = -xx * math.sin(t) + yy * math.cos(t)
        length = float(np.percentile(u, 98) - np.percentile(u, 2))
        n = int(round(length / s_px))
        if n < 2: continue
        res.setdefault("patches", []).append({"plant_id": int(f["plant_id"]), "length_cm": round(length * pixel_cm, 1), "plants": n})
        parts = _split_window(g, n)
        if len(parts) < 2: continue
        lay.DeleteFeature(f["fid"])
        for x, y, a in parts:
            nf = ogr.Feature(lay.GetLayerDefn()); p = ogr.Geometry(ogr.wkbPoint); p.AddPoint_2D(float(x), float(y)); nf.SetGeometry(p)
            nf.SetField("plant_id", next_id); next_id += 1
            nf.SetField(area_field, float(a) if area_field == "area_cm2" else float(a) / pixel_cm ** 2)
            nf.SetField("status", STATUS_AUTO); nf.SetField("on_row", int(f.get("on_row") if f.get("on_row") is not None else 1))
            nf.SetField("split_of", int(f["plant_id"])); lay.CreateFeature(nf)
        res["groups_split"] += 1; res["points_added"] += len(parts) - 1
    out = None
    res["split_longer_than_cm"] = 1.5 * res["plant_spacing_cm"]
    res["note"] = split_note(res)
    log(res["note"] + ".")
    info["auto_split"] = res; info["plant_spacing_cm"] = res["plant_spacing_cm"]
    with open(info_path, "w", encoding="utf-8") as fh: json.dump(info, fh, indent=1)
    return res


def split_note(res):
    """Plain words: the plant spacing, the length above which a patch is split, and what was split."""
    s = res["plant_spacing_cm"]
    text = ("plant spacing in the row {:.1f} cm ({}). A patch longer than {:.1f} cm along the row (1.5 x spacing) is split, "
            "one plant per {:.1f} cm: up to {:.1f} cm = 2 plants, up to {:.1f} cm = 3 plants, and so on. ").format(
        s, res["spacing_from"], 1.5 * s, s, 2.5 * s, 3.5 * s)
    if not res["groups_split"]:
        return text + "No patch was that long, so nothing was split"
    parts = ", ".join("{} cm into {}".format(p["length_cm"], p["plants"]) for p in res.get("patches", [])[:6])
    return text + "{} patch{} split ({}{}): {} more plant{}; check them on the map".format(
        res["groups_split"], "" if res["groups_split"] == 1 else "es", parts, ", ..." if len(res.get("patches", [])) > 6 else "",
        res["points_added"], "" if res["points_added"] == 1 else "s")


def split_part(points_path, x, y, n, siblings, area_cm2=None):
    """Split ONE part of a patch that was already split (by PPE or by the user) into n plants: the patch's
    pixels are shared out to the nearest of its points (`siblings`, map coordinates, the clicked point
    among them) and only the clicked point's share is split. `area_cm2` is the whole patch's area."""
    g = group_at(points_path, x, y, area_cm2)
    if g is None or n < 2: return []
    m, x0, y0, gt, pixel_cm, info = g
    yy, xx = np.nonzero(m)
    if len(xx) < n: return []
    sib = np.array([((a - gt[0]) / gt[1] - x0 - 0.5, (b - gt[3]) / gt[5] - y0 - 0.5) for a, b in siblings], float)
    me = int(np.argmin((sib[:, 0] - ((x - gt[0]) / gt[1] - x0 - 0.5)) ** 2 + (sib[:, 1] - ((y - gt[3]) / gt[5] - y0 - 0.5)) ** 2))
    near = np.argmin((xx[:, None] - sib[None, :, 0]) ** 2 + (yy[:, None] - sib[None, :, 1]) ** 2, axis=1)
    sub = np.zeros_like(m); sub[yy[near == me], xx[near == me]] = True
    return _split_window((sub, x0, y0, gt, pixel_cm, info), n)


def merge_points(points):
    """One point for a list of points [(x, y, area)]: the area-weighted middle and the summed area."""
    a = np.array([max(p[2] or 0.0, 1e-9) for p in points]); x = np.array([p[0] for p in points]); y = np.array([p[1] for p in points])
    return float((x * a).sum() / a.sum()), float((y * a).sum() / a.sum()), float(sum(p[2] or 0.0 for p in points))
