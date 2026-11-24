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


def _plant_groups(mask_path, vote_path, min_px, join_px, hole_px, progress, log, ids_path=None, keep=None):
    """Plants of a 0/1 mask, GDAL only. 1) Pieces smaller than the smallest plant are dropped (they
    must not chain plants together). 2) Pieces closer than join_px are joined: leaves of one plant
    split by a thin stem or petiole become one plant. 3) Each joined group is one candidate plant:
    its plant pixels, sure-core pixels and centre (mean of its plant pixels, map coordinates).
    Returns (dataset, area_px, x, y, core_px or None). With ids_path the group of every pixel is written
    there (0 = none) and keep["gid"] gets the group number of each returned plant."""
    ds = safe_gdal_open(mask_path)
    if ds is None:
        raise ValueError("The finalized plant mask is missing or unreadable.")
    W, H = ds.RasterXSize, ds.RasterYSize
    gt = ds.GetGeoTransform()
    wkt = ds.GetProjection()
    band = ds.GetRasterBand(1)
    mem = gdal.GetDriverByName("MEM")
    STEP = 1024

    def new(dtype=gdal.GDT_Byte):
        r = mem.Create("", W, H, 1, dtype)
        r.SetGeoTransform(gt)
        r.SetProjection(wkt)
        return r
    plant = new()
    pb = plant.GetRasterBand(1)
    for y in range(0, H, STEP):
        h = min(STEP, H - y)
        pb.WriteArray((band.ReadAsArray(0, y, W, h) == 1).astype(np.uint8), 0, y)
    # 1) drop specks. The sieve also fills small holes; '& original' puts the holes back.
    sieved = new()
    sb = sieved.GetRasterBand(1)
    gdal.SieveFilter(pb, None, sb, max(2, int(math.ceil(min_px))), 8)
    for y in range(0, H, STEP):
        h = min(STEP, H - y)
        pb.WriteArray(pb.ReadAsArray(0, y, W, h) & sb.ReadAsArray(0, y, W, h), 0, y)
    sb = None
    sieved = None                                # free each full-size raster as soon as it is used
    progress(25, "Joining leaves of one plant")
    # 2) join pieces: everything within join_px / 2 of a plant pixel.
    joined = new()
    jb = joined.GetRasterBand(1)
    if join_px >= 1:
        r = join_px / 2.0
        prox = new()
        xb = prox.GetRasterBand(1)
        gdal.ComputeProximity(pb, xb, ["VALUES=1", "DISTUNITS=PIXEL", "MAXDIST={}".format(int(math.ceil(r)) + 1), "NODATA=255"])
        for y in range(0, H, STEP):
            h = min(STEP, H - y)
            jb.WriteArray((xb.ReadAsArray(0, y, W, h) <= r).astype(np.uint8), 0, y)
        prox = None
    else:
        for y in range(0, H, STEP):
            h = min(STEP, H - y)
            jb.WriteArray(pb.ReadAsArray(0, y, W, h), 0, y)
    # Small soil holes inside a group are filled before tracing: same groups, ~10x faster on dense canopy.
    if hole_px:
        soil = new()
        ob = soil.GetRasterBand(1)
        for y in range(0, H, STEP):
            h = min(STEP, H - y)
            ob.WriteArray(1 - jb.ReadAsArray(0, y, W, h), 0, y)
        gdal.SieveFilter(ob, None, ob, int(hole_px), 4)
        for y in range(0, H, STEP):
            h = min(STEP, H - y)
            jb.WriteArray(jb.ReadAsArray(0, y, W, h) | (1 - ob.ReadAsArray(0, y, W, h)).astype(np.uint8), 0, y)
        soil = None
    progress(40, "Finding plants")
    src = ogr.GetDriverByName("Memory").CreateDataSource("groups")
    layer = src.CreateLayer("g", None, ogr.wkbPolygon)
    layer.CreateField(ogr.FieldDefn("v", ogr.OFTInteger))
    gdal.Polygonize(jb, jb, layer, 0, ["8CONNECTED=8"])
    n = layer.GetFeatureCount()
    layer.CreateField(ogr.FieldDefn("gid", ogr.OFTInteger))
    layer.ResetReading()
    k = 0
    for f in layer:
        k += 1
        f.SetField("gid", k)
        layer.SetFeature(f)
    jb = None
    joined = None
    ids = new(gdal.GDT_Int32)
    ib = ids.GetRasterBand(1)
    gdal.RasterizeLayer(ids, [1], layer, options=["ATTRIBUTE=gid"])
    layer = None
    src = None
    progress(60, "Measuring plants")
    vote = safe_gdal_open(vote_path) if vote_path else None
    if vote is not None and (vote.RasterXSize, vote.RasterYSize) != (W, H):
        log("The vote raster does not match the mask: counting without the sure-core check.")
        vote = None
    elif vote_path and vote is None:
        log("The PPE vote raster is missing: counting without the sure-core check.")
    area = np.zeros(n + 1)
    sx = np.zeros(n + 1)
    sy = np.zeros(n + 1)
    core = np.zeros(n + 1) if vote is not None else None
    cols = np.arange(W, dtype=np.float64)
    for y in range(0, H, STEP):
        h = min(STEP, H - y)
        g = ib.ReadAsArray(0, y, W, h)
        p = (pb.ReadAsArray(0, y, W, h) == 1) & (g > 0)
        if not p.any():
            continue
        gi = g[p]
        yy, xx = np.nonzero(p)
        area += np.bincount(gi, minlength=n + 1)[:n + 1]
        sx += np.bincount(gi, weights=cols[xx] + 0.5, minlength=n + 1)[:n + 1]
        sy += np.bincount(gi, weights=yy + y + 0.5, minlength=n + 1)[:n + 1]
        if core is not None:
            v = vote.GetRasterBand(1).ReadAsArray(0, y, W, h)
            sure = p & (v >= CORE_VOTE) & (v <= 1.0)
            if sure.any():
                core += np.bincount(g[sure], minlength=n + 1)[:n + 1]
    sel = area[1:] > 0
    a = area[1:][sel]
    cx = sx[1:][sel] / a
    cy = sy[1:][sel] / a
    if ids_path:
        gdal.GetDriverByName("GTiff").CreateCopy(ids_path, ids, options=["TILED=YES", "COMPRESS=DEFLATE", "BIGTIFF=IF_SAFER"])
        if keep is not None:
            keep["gid"] = np.nonzero(sel)[0] + 1
    X = gt[0] + cx * gt[1] + cy * gt[2]
    Y = gt[3] + cx * gt[4] + cy * gt[5]
    return ds, a, X, Y, (core[1:][sel] if core is not None else None)


ROW_MIN_CM, ROW_MAX_CM = 15.0, 150.0   # row spacings searched (any row crop: cereals to wide-row crops)
ROW_MIN_CONTRAST = 1.8                 # rows are clear when their signal is this many times the background (other spacings/directions);
# a 4 m tile needs half the field's row signal. Otherwise nothing is marked as weed there.


def _plant_pixel_sample(mask_path, n=2000000, seed=0):
    """Pixel coordinates of the plant pixels of the mask on a coarse grid (about 2 million cells), at most n."""
    ds = safe_gdal_open(mask_path)
    b = ds.GetRasterBand(1)
    W, H = ds.RasterXSize, ds.RasterYSize
    step = max(1, int(math.sqrt(W * H / 2e6)))             # read a coarse grid (about 2 million pixels)
    a = b.ReadAsArray(0, 0, W, H, buf_xsize=max(1, W // step), buf_ysize=max(1, H // step))
    ys, xs = np.nonzero(a == 1)
    if len(xs) > n:
        k = np.random.default_rng(seed).choice(len(xs), n, replace=False)
        xs, ys = xs[k], ys[k]
    return xs * (W / a.shape[1]) + 0.5, ys * (H / a.shape[0]) + 0.5


ROW_TILE_CM = 400.0                    # row positions are fitted per 4 m tile: plots can be shifted against each other


def _tiles(x, y, tile):
    tx = np.floor(x / tile).astype(np.int64)
    ty = np.floor(y / tile).astype(np.int64)
    _u, inv = np.unique(tx * 1000003 + ty, return_inverse=True)
    return inv, int(inv.max()) + 1 if len(inv) else 0


def _fold(sx, sy, tid, nt, angles, spacings):
    """Row strength for every direction x spacing: inside each tile, plant pixels folded by the spacing
    pile up at one phase when direction and spacing are right; strength = share-weighted mean over tiles."""
    out = np.empty((len(angles), len(spacings)))
    n = len(sx)
    ns = len(spacings)
    idx = (np.asarray(tid, np.int64)[:, None] * ns + np.arange(ns)[None, :]).ravel()   # tile x spacing bin of each value
    for k, a in enumerate(angles):
        t = math.radians(a)
        d = sx * math.cos(t) + sy * math.sin(t)
        ph = 2 * math.pi * d[:, None] / spacings[None, :]
        # bincount adds in the same order as np.add.at did: the same sums, about a third faster
        re = np.bincount(idx, np.cos(ph).ravel(), nt * ns).reshape(nt, ns)
        im = np.bincount(idx, np.sin(ph).ravel(), nt * ns).reshape(nt, ns)
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
    ax_, ay_ = sample
    tile = ROW_TILE_CM / pixel_cm
    sx, sy = ax_, ay_
    if len(sx) > 20000:
        k = np.random.default_rng(1).choice(len(sx), 20000, replace=False)
        sx, sy = sx[k], sy[k]
    tid, nt = _tiles(sx, sy, tile)
    if spacing_cm:
        spacings = np.array([float(spacing_cm) / pixel_cm])
    else:
        spacings = np.exp(np.linspace(math.log(max(ROW_MIN_CM / pixel_cm, 3.0)), math.log(max(ROW_MAX_CM / pixel_cm, 4.0)), 60))
    angles = np.arange(0, 180, 3.0)
    R = _fold(sx, sy, tid, nt, angles, spacings)
    ka, ks = np.unravel_index(int(np.argmax(R)), R.shape)
    background = float(np.median(R))                      # strength of directions/spacings that are not rows
    a_f = np.arange(angles[ka] - 3, angles[ka] + 3.01, 0.25)
    s_f = spacings if len(spacings) == 1 else np.linspace(spacings[max(0, ks - 1)], spacings[min(len(spacings) - 1, ks + 1)], 21)
    Rf = _fold(sx, sy, tid, nt, a_f, s_f)
    ka, ks = np.unravel_index(int(np.argmax(Rf)), Rf.shape)
    a0, sp = float(a_f[ka]), float(s_f[ks])
    strength = float(Rf[ka, ks])
    # Thin rows also line up at half (a third of) their spacing: take the widest spacing that fits as well.
    if spacing_cm is None:
        for mult in (3, 2):
            wide = sp * mult
            if wide <= ROW_MAX_CM / pixel_cm:
                rw = float(_fold(sx, sy, tid, nt, np.array([a0]), np.array([wide]))[0, 0])
                if rw >= 0.8 * strength:
                    sp, strength = wide, rw
                    break
    contrast = strength / max(background, 1e-6)
    info = {"strength": strength, "contrast": contrast, "angle_deg": a0, "spacing_px": sp, "spacing_cm": sp * pixel_cm}
    if contrast < ROW_MIN_CONTRAST or (spacing_cm is None and ks in (0, len(s_f) - 1) and sp in (spacings[0], spacings[-1])):
        info.update(rows=0, note="no clear rows (row signal {:.1f} x background); nothing marked as weed".format(contrast))
        return np.ones(n, bool), info
    # phase and strength of each tile (all sampled plant pixels), then each point against its own tile's rows
    sx, sy = ax_, ay_
    tid, nt = _tiles(sx, sy, tile)
    t = math.radians(a0)
    ph_s = 2 * math.pi * (sx * math.cos(t) + sy * math.sin(t)) / sp
    re = np.bincount(tid, np.cos(ph_s), nt)
    im = np.bincount(tid, np.sin(ph_s), nt)
    cnt = np.bincount(tid, minlength=nt)
    tile_r = np.hypot(re, im) / np.maximum(cnt, 1)
    tile_phase = np.arctan2(im, re)
    tile_ok = (cnt >= 30) & (tile_r >= 0.5 * strength) & (tile_r >= 3.0 / np.sqrt(np.maximum(cnt, 1)))
    key = np.floor(sx / tile).astype(np.int64) * 1000003 + np.floor(sy / tile).astype(np.int64)
    ukey = np.zeros(nt, np.int64)
    ukey[tid] = key
    pk = np.floor(px / tile).astype(np.int64) * 1000003 + np.floor(py / tile).astype(np.int64)
    order = np.argsort(ukey)
    pos = np.searchsorted(ukey[order], pk)
    pos = np.clip(pos, 0, nt - 1)
    pt = np.where(ukey[order][pos] == pk, order[pos], -1)
    ph_p = 2 * math.pi * (px * math.cos(t) + py * math.sin(t)) / sp
    on_row = np.ones(n, bool)
    has = pt >= 0
    judged = np.zeros(n, bool)
    judged[has] = tile_ok[pt[has]]
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
    progress = progress or (lambda p, m: None)
    log = log or (lambda m: None)
    started = time.time()
    out_dir = cfg["output_dir"]
    os.makedirs(out_dir, exist_ok=True)
    area_used = bool(cfg.get("area_wkts") or cfg.get("area_path"))
    source_mask = cfg["mask_path"]
    if cfg.get("keep_groups", True) and not cfg.get("groups_path"):      # 4.9.0: for the crop-only mask
        cfg = dict(cfg, groups_path=os.path.join(out_dir, "ppe_plant_groups.tif"))
    if area_used:                       # 4.6.4: count only inside the area (the mask outside it becomes no data)
        progress(2, "Cutting the mask to the area")
        cfg = dict(cfg, mask_path=_mask_in_area(cfg, out_dir))
    progress(5, "Reading the plant mask")
    probe = safe_gdal_open(cfg["mask_path"])
    if probe is None:
        raise ValueError("The finalized plant mask is missing or unreadable.")
    pixel_m2 = _pixel_area_m2(probe)
    probe = None
    min_cm2 = float(cfg.get("min_plant_cm2", 1.0))
    if pixel_m2:
        min_px = max(2.0, min_cm2 / (pixel_m2 * 1e4))
    else:
        min_px = float(cfg.get("min_plant_px", 4))
        log("The image is not in metres; smallest plant = {:g} pixels.".format(min_px))
    hole_px = int(cfg.get("fill_holes_px", max(1000, 50 * min_px)))
    join_cm = float(cfg.get("join_cm", JOIN_CM))
    join_px = join_cm / (math.sqrt(pixel_m2) * 100) if pixel_m2 else float(cfg.get("join_px", 0))
    if join_px >= 1:
        log("Leaves closer than {:g} cm ({:.1f} pixels) are joined into one plant.".format(join_cm, join_px))
    progress(10, "Finding plants")
    keep = {}
    ds, areas, xs, ys, core_n = _plant_groups(cfg["mask_path"], cfg.get("vote_path"), min_px, join_px, hole_px, progress, log,
                                              cfg.get("groups_path"), keep)
    gt = ds.GetGeoTransform()
    srs = osr.SpatialReference()
    wkt = ds.GetProjection()
    if wkt:
        srs.ImportFromWkt(wkt)
    progress(72, "Measuring plants")
    keep0 = areas >= min_px
    no_core = 0
    if core_n is not None:
        no_core = int((keep0 & (core_n < CORE_PX)).sum())
        keep0 &= core_n >= CORE_PX
        log("{} groups had no sure core (every vote plant) and were not counted.".format(no_core))
    areas, xs, ys = areas[keep0], xs[keep0], ys[keep0]
    core = core_n
    if cfg.get("groups_path") and "gid" in keep:          # plant_id i + 1 is group keep["gid"][i] of the groups raster
        np.save(os.path.join(out_dir, "ppe_group_ids.npy"), keep["gid"][keep0].astype(np.int32))
    n_all = len(areas)
    median = float(np.median(areas)) if n_all else 0.0
    big = areas > BIG_FACTOR * float(np.percentile(areas, 75)) if n_all >= 4 else np.zeros(n_all, bool)
    rows_info = None
    on_row = np.ones(n_all, bool)
    if (cfg.get("find_rows") or cfg.get("row_spacing_cm")) and n_all:
        progress(80, "Finding crop rows")
        pxc = (xs - gt[0]) / gt[1]
        pyc = (ys - gt[3]) / gt[5]
        pixel_cm = math.sqrt(pixel_m2) * 100 if pixel_m2 else None
        if pixel_cm:
            on_row, rows_info = _rows(pxc, pyc, areas, pixel_cm, cfg.get("row_spacing_cm"), _plant_pixel_sample(cfg["mask_path"]))
        else:
            rows_info = {"note": "the image is not in metres; rows not used"}
        log("Rows: " + rows_info["note"] + ".")
    to_cm2 = (pixel_m2 * 1e4) if pixel_m2 else None
    progress(85, "Writing the plant points")
    gpkg = os.path.join(out_dir, "ppe_plants.gpkg")
    if os.path.exists(gpkg):
        gdal.GetDriverByName("GPKG").Delete(gpkg)
    out = ogr.GetDriverByName("GPKG").CreateDataSource(gpkg)
    pl = out.CreateLayer("plants", srs if wkt else None, ogr.wkbPoint)
    for name, kind in (("plant_id", ogr.OFTInteger), ("area_cm2" if to_cm2 else "area_px", ogr.OFTReal), ("status", ogr.OFTString), ("on_row", ogr.OFTInteger),
                       ("split_of", ogr.OFTInteger)):
        pl.CreateField(ogr.FieldDefn(name, kind))
    pl.StartTransaction()
    for i in range(n_all):
        f = ogr.Feature(pl.GetLayerDefn())
        p = ogr.Geometry(ogr.wkbPoint)
        p.AddPoint_2D(float(xs[i]), float(ys[i]))
        f.SetGeometry(p)
        f.SetField(0, i + 1)
        f.SetField(1, float(areas[i] * to_cm2) if to_cm2 else float(areas[i]))
        f.SetField(2, STATUS_WEED if not on_row[i] else (STATUS_CHECK if big[i] else STATUS_PLANT))
        f.SetField(3, int(on_row[i]))
        pl.CreateFeature(f)
    pl.CommitTransaction()
    out = None
    field_m2 = None
    if pixel_m2:
        band = ds.GetRasterBand(1)
        valid = 0
        for y in range(0, ds.RasterYSize, 2048):
            valid += int((band.ReadAsArray(0, y, ds.RasterXSize, min(2048, ds.RasterYSize - y)) != 255).sum())
        field_m2 = valid * pixel_m2
    info = {"mask_path": cfg["mask_path"], "source_mask_path": source_mask, "groups_path": cfg.get("groups_path") or "", "field_m2": field_m2, "median_plant_cm2": (median * to_cm2) if to_cm2 else None,
            "min_plant_cm2": min_cm2 if pixel_m2 else None, "join_cm": join_cm, "rows": rows_info,
            "no_core_left_out": no_core if core is not None else None,
            "aoi_path": cfg.get("aoi_path") or "", "aoi_layer": cfg.get("aoi_layer") or "", "aoi_id_field": cfg.get("aoi_id_field") or "",
            "count_area": os.path.join(out_dir, "ppe_count_area.gpkg") if area_used else ""}
    with open(os.path.join(out_dir, "ppe_count_info.json"), "w", encoding="utf-8") as fh:
        json.dump(info, fh, indent=1)
    if cfg.get("auto_split", True) and rows_info and rows_info.get("rows"):
        progress(88, "Splitting touching plants by the plant spacing")
        auto_split(gpkg, cfg.get("plant_spacing_cm"), log)
    if cfg.get("row_weeds", True) and rows_info and rows_info.get("rows"):
        progress(90, "Looking for weeds inside the rows")
        flag_row_weeds(gpkg, log)
    if cfg.get("aoi_path"):
        progress(92, "Counting per plot")
    summary = summarize_points(gpkg, log)
    summary["seconds"] = time.time() - started
    progress(100, "Counted {} plants".format(summary["plants"]))
    return summary


def read_points(points_path):
    """(x, y, status) of every point in ppe_plants.gpkg."""
    ds = ogr.Open(points_path)
    if ds is None:
        raise ValueError("The plant points file is missing: {}".format(points_path))
    lay = ds.GetLayer()
    out = []
    k = lay.GetLayerDefn().GetFieldIndex("status")
    for f in lay:
        g = f.GetGeometryRef()
        if g is None:
            continue
        out.append((g.GetX(), g.GetY(), (f.GetField(k) if k >= 0 else None) or STATUS_PLANT))
    return out, lay.GetSpatialRef().ExportToWkt() if lay.GetSpatialRef() else ""


def _count_in_row_weeds(points_path):
    try:
        ds = ogr.Open(points_path)
        lay = ds.GetLayer()
        d = lay.GetLayerDefn()
        ks, kr = d.GetFieldIndex("status"), d.GetFieldIndex("weed_reason")
        if ks < 0 or kr < 0:
            return 0
        return sum(1 for f in lay if f.GetField(ks) == STATUS_WEED and str(f.GetField(kr) or "").startswith("in the row"))
    except Exception:
        return 0


def summarize_points(points_path, log=None):
    """Totals (and counts per plot) from the points as they are now, after any corrections by the user.
    Counted: plant, check (as one plant), split and added; not counted: weed?. Rewrites ppe_count_summary.csv and ppe_plot_counts.csv."""
    log = log or (lambda m: None)
    out_dir = os.path.dirname(points_path)
    try:
        with open(os.path.join(out_dir, "ppe_count_info.json"), encoding="utf-8") as fh:
            info = json.load(fh)
    except (OSError, ValueError):
        info = {}
    pts, wkt = read_points(points_path)
    counted = [(x, y, st) for x, y, st in pts if st != STATUS_WEED]
    n = {k: sum(1 for p in pts if p[2] == k) for k in (STATUS_PLANT, STATUS_CHECK, STATUS_ADDED, STATUS_WEED, "split", "auto split")}
    plants = len(counted)
    field_m2 = info.get("field_m2")
    summary = {"plants": plants, "flagged_big": n[STATUS_CHECK], "added_by_user": n[STATUS_ADDED], "weeds_not_counted": n[STATUS_WEED],
               "weeds_in_row": _count_in_row_weeds(points_path), "row_weeds": info.get("row_weeds"),
               "plain_plants": n[STATUS_PLANT], "split_by_user": n["split"], "split_by_ppe": n["auto split"],
               "plant_spacing_cm": info.get("plant_spacing_cm"), "auto_split": info.get("auto_split"),
               "left_out_between_rows": n[STATUS_WEED], "no_core_left_out": info.get("no_core_left_out"),
               "median_plant_cm2": info.get("median_plant_cm2"), "min_plant_cm2": info.get("min_plant_cm2"),
               "plants_per_m2": (plants / field_m2) if field_m2 else None, "rows": info.get("rows"), "points_path": points_path, "note": EARLY_NOTE}
    path = os.path.join(out_dir, "ppe_count_summary.csv")
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["plants_counted", "of_which_plant", "of_which_check_counted_as_one", "of_which_split_by_ppe", "of_which_split_by_user",
                    "of_which_added_by_user", "weeds_not_counted", "of_which_weeds_in_row", "median_plant_cm2", "smallest_plant_cm2", "plant_spacing_cm",
                    "split_longer_than_cm", "plants_per_m2", "note"])
        w.writerow([plants, n[STATUS_PLANT], n[STATUS_CHECK], n["auto split"], n["split"], n[STATUS_ADDED], n[STATUS_WEED], summary["weeds_in_row"], _r(summary["median_plant_cm2"]),
                    _r(summary["min_plant_cm2"]), _r(info.get("plant_spacing_cm"), 1),
                    _r(1.5 * info["plant_spacing_cm"] if info.get("plant_spacing_cm") else None, 1), _r(summary["plants_per_m2"], 3), EARLY_NOTE])
    summary["summary_path"] = path
    if info.get("aoi_path") and os.path.isfile(info["aoi_path"]):
        xs = np.array([p[0] for p in counted])
        ys = np.array([p[1] for p in counted])
        big = np.array([p[2] == STATUS_CHECK for p in counted])
        summary.update(_plot_counts(info, wkt, xs, ys, big, out_dir, log))
    return summary


def _r(v, d=2):
    return "" if v is None else round(float(v), d)


def _plot_counts(cfg, wkt, xs, ys, big, out_dir, log):
    vector, layer, info = open_plot_file(cfg["aoi_path"], cfg.get("aoi_layer") or None)
    transform = _plot_transform(layer, wkt)
    id_field = cfg.get("aoi_id_field") or ""
    # Points into a memory layer with a spatial index-like filter (OGR filters by envelope first).
    mem = ogr.GetDriverByName("Memory").CreateDataSource("pts")
    pts = mem.CreateLayer("p", None, ogr.wkbPoint)
    pts.CreateField(ogr.FieldDefn("big", ogr.OFTInteger))
    for x, y, b in zip(xs, ys, big):
        f = ogr.Feature(pts.GetLayerDefn())
        g = ogr.Geometry(ogr.wkbPoint)
        g.AddPoint_2D(float(x), float(y))
        f.SetGeometry(g)
        f.SetField(0, int(b))
        pts.CreateFeature(f)
    rows = []
    layer.ResetReading()
    k = 0
    for feature in layer:
        geom = _plot_geometry(feature, transform)
        if geom is None:
            continue
        k += 1
        pid = feature.GetField(id_field) if id_field else feature.GetFID()
        pts.SetSpatialFilter(geom)
        n = 0
        nb = 0
        for p in pts:
            n += 1
            nb += p.GetField(0)
        area = geom.GetArea()
        rows.append([pid, n, nb, round(area, 3), round(n / area, 3) if area > 0 else ""])
    pts.SetSpatialFilter(None)
    path = os.path.join(out_dir, "ppe_plot_counts.csv")
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["plot_id", "plants", "flagged_big", "plot_area_m2", "plants_per_m2"])
        w.writerows(rows)
    log("Counted plants in {} plots.".format(k))
    return {"plot_counts_path": path, "plots": k}


def _mask_in_area(cfg, out_dir):
    """A copy of the mask with everything outside the area set to no data (255): ppe_mask_in_area.tif.
    The area is kept as ppe_count_area.gpkg."""
    from .label_points import _area_raster
    area_path = _area_raster(cfg, out_dir, "ppe_count_area")
    src = safe_gdal_open(cfg["mask_path"])
    area = gdal.Open(area_path)
    w, h = src.RasterXSize, src.RasterYSize
    path = os.path.join(out_dir, "ppe_mask_in_area.tif")
    if os.path.exists(path):
        os.remove(path)
    out = gdal.GetDriverByName("GTiff").Create(path, w, h, 1, gdal.GDT_Byte, options=["COMPRESS=DEFLATE", "TILED=YES"])
    out.SetGeoTransform(src.GetGeoTransform())
    out.SetProjection(src.GetProjection())
    out.GetRasterBand(1).SetNoDataValue(255)
    for y in range(0, h, 1024):
        n = min(1024, h - y)
        m = src.GetRasterBand(1).ReadAsArray(0, y, w, n)
        a = area.GetRasterBand(1).ReadAsArray(0, y, w, n)
        out.GetRasterBand(1).WriteArray(np.where(a > 0, m, 255).astype(np.uint8), 0, y)
    out.FlushCache()
    out = None
    area = None
    if os.path.exists(area_path):
        os.remove(area_path)
    return path


def count_batch(cfg, progress=None, log=None):
    """Count every finalized mask of a batch. cfg: count_items [{name, mask_path}], output_dir (the
    batch folder), plus the count_plants settings. Each image's points go to <mask folder>/ppe_count;
    one table for all images: ppe_batch_counts.csv."""
    progress = progress or (lambda p, m: None)
    log = log or (lambda m: None)
    items = [i for i in cfg.get("count_items") or [] if i.get("mask_path") and os.path.isfile(i["mask_path"])]
    if not items:
        raise ValueError("No finalized batch masks to count: write the batch masks first (Batch tab).")
    rows = []
    started = time.time()
    n = len(items)
    for k, it in enumerate(items):
        sub = dict(cfg, mask_path=it["mask_path"], vote_path=it.get("vote_path") or "", output_dir=os.path.join(os.path.dirname(it["mask_path"]), "ppe_count"), aoi_path=cfg.get("aoi_path") or "")
        r = count_plants(sub, progress=lambda p, m, k=k: progress(int(100 * (k + p / 100.0) / n), "{} ({}/{}): {}".format(it["name"], k + 1, n, m)), log=log)
        rows.append([it["name"], r["plants"], r["flagged_big"], r["left_out_between_rows"], _r(r["median_plant_cm2"]), _r(r["plants_per_m2"], 3), r["points_path"]])
        log("Count {}/{}: {} plants in {}.".format(k + 1, n, r["plants"], it["name"]))
    path = os.path.join(cfg["output_dir"], "ppe_batch_counts.csv")
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["image", "plants", "flagged_big", "left_out_between_rows", "median_plant_cm2", "plants_per_m2", "points_path"])
        w.writerows(rows)
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
    p = np.pad(m, r)
    h, w = m.shape
    out = np.zeros_like(m)
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            out |= p[r + dy:r + dy + h, r + dx:r + dx + w]
    return out


def group_at(points_path, x, y, area_cm2=None):
    """The plant pixels of the group under a point (map coordinates), as PPE grouped them: plant
    pixels connected through gaps up to the join distance. Returns (mask window bool array, x0, y0,
    geotransform, pixel_cm, info)."""
    with open(os.path.join(os.path.dirname(points_path), "ppe_count_info.json"), encoding="utf-8") as fh:
        info = json.load(fh)
    return _group_window(info, x, y, area_cm2)


def _group_window(info, x, y, area_cm2=None, ds=None):
    ds = ds or safe_gdal_open(info["mask_path"])
    gt = ds.GetGeoTransform()
    W, H = ds.RasterXSize, ds.RasterYSize
    pixel_cm = 100.0 * math.sqrt(abs(gt[1] * gt[5])) if _pixel_area_m2(ds) else 1.0
    a_px = (area_cm2 / pixel_cm ** 2) if area_cm2 else 400.0
    half = int(min(2000, max(40, 3.0 * math.sqrt(a_px / math.pi))))
    cx = int((x - gt[0]) / gt[1])
    cy = int((y - gt[3]) / gt[5])
    x0, y0 = max(0, cx - half), max(0, cy - half)
    x1, y1 = min(W, cx + half + 1), min(H, cy + half + 1)
    plant = ds.GetRasterBand(1).ReadAsArray(x0, y0, x1 - x0, y1 - y0) == 1
    if not plant.any():
        return None
    join_px = max(0, int(round((info.get("join_cm") or 0) / pixel_cm / 2.0)))
    allowed = _dilate(plant, join_px) if join_px else plant.copy()
    py, px = np.nonzero(plant)
    k = int(np.argmin((px - (cx - x0)) ** 2 + (py - (cy - y0)) ** 2))   # plant pixel nearest the point
    seed = np.zeros_like(plant)
    seed[py[k], px[k]] = True
    for _ in range(4 * half):                       # flood fill through the joined area
        grown = _dilate(seed) & allowed
        if (grown == seed).all():
            break
        seed = grown
    return seed & plant, x0, y0, gt, pixel_cm, info


def suggest_split(points_path, x, y, area_cm2):
    """A rough number of plants: the group's size over the median plant, at least 2. Not used by the
    split tool (too often wrong where plant sizes vary); kept for scripts."""
    with open(os.path.join(os.path.dirname(points_path), "ppe_count_info.json"), encoding="utf-8") as fh:
        info = json.load(fh)
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
    if g is None or n < 2:
        return []
    m, x0, y0, gt, pixel_cm, info = g
    py, px = np.nonzero(m)
    if len(px) < n:
        return []
    mem = gdal.GetDriverByName("MEM").Create("", m.shape[1], m.shape[0], 1, gdal.GDT_Byte)
    mem.GetRasterBand(1).WriteArray((~m).astype(np.uint8))
    dist = gdal.GetDriverByName("MEM").Create("", m.shape[1], m.shape[0], 1, gdal.GDT_Float32)
    gdal.ComputeProximity(mem.GetRasterBand(1), dist.GetRasterBand(1), ["VALUES=1", "DISTUNITS=PIXEL"])
    d = dist.GetRasterBand(1).ReadAsArray()[py, px]
    pts = np.column_stack([px, py]).astype(np.float64)
    sep = math.sqrt(len(px) / n / math.pi)          # a lobe's radius if the n plants were equal
    order = np.argsort(-d)
    seeds = []
    for i in order:                                 # deepest pixels first, kept apart
        if all((px[i] - px[j]) ** 2 + (py[i] - py[j]) ** 2 >= sep ** 2 for j in seeds):
            seeds.append(i)
        if len(seeds) == n:
            break
    while len(seeds) < n:                           # not enough separate lobes: farthest point from the chosen ones
        dd = np.min([(px - px[j]) ** 2 + (py - py[j]) ** 2 for j in seeds], axis=0)
        seeds.append(int(np.argmax(dd)))
    c = np.array([[px[j], py[j]] for j in seeds], np.float64)
    for _ in range(10):
        lab = np.argmin(((pts[:, None, :] - c[None, :, :]) ** 2).sum(2), 1)
        new = np.array([pts[lab == k].mean(0) if (lab == k).any() else c[k] for k in range(n)])
        if np.allclose(new, c):
            break
        c = new
    lab = np.argmin(((pts[:, None, :] - c[None, :, :]) ** 2).sum(2), 1)
    out = []
    for k in range(n):
        sel = lab == k
        if not sel.any():
            continue
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
    ds = ogr.Open(path, 1)
    lay = ds.GetLayer()
    if lay.GetLayerDefn().GetFieldIndex("split_of") < 0:
        lay.CreateField(ogr.FieldDefn("split_of", ogr.OFTInteger))
    ds = None


def confident_range(areas):
    """The size range of a normal single plant in THIS image: median area x/÷ exp(2 sigma), sigma from the
    spread of the log areas (MAD), kept within x0.4 - x2.5 of the median. A uniform stand gets a tight range,
    an uneven one a wider range. Returns (median, low, high) or None with fewer than 3 plants."""
    a = np.array([v for v in areas if v is not None and v > 0], float)
    if len(a) < 3:
        return None
    med = float(np.median(a))
    la = np.log(a)
    sig = max(0.15, 1.4826 * float(np.median(np.abs(la - np.median(la)))))
    return med, max(0.4 * med, med * math.exp(-2 * sig)), min(2.5 * med, med * math.exp(2 * sig))


def spacing_gaps(px, py, angle_deg, row_spacing_px, tile_px, use=None):
    """Gaps (pixels) between neighbouring plants along each row: plant centres in pixel coordinates are
    grouped into rows per 4 m tile (by the gaps across the rows) and sorted along the row; a gap is kept
    only when both neighbours are in `use` (all when None)."""
    px = np.asarray(px, float)
    py = np.asarray(py, float)
    use = np.ones(len(px), bool) if use is None else np.asarray(use, bool)
    if len(px) < 2:
        return []
    t = math.radians(angle_deg)
    d = px * math.cos(t) + py * math.sin(t)
    u = -px * math.sin(t) + py * math.cos(t)
    gaps = []
    tid = np.floor(px / tile_px) * 1000003 + np.floor(py / tile_px)
    order = np.argsort(tid, kind="stable")             # one sort instead of one full scan per tile (same groups, same order)
    for sel in np.split(order, np.flatnonzero(np.diff(tid[order])) + 1):
        if len(sel) < 2:
            continue
        o = sel[np.argsort(d[sel])]
        start = 0
        for i in range(1, len(o) + 1):
            if i == len(o) or d[o[i]] - d[o[i - 1]] > 0.5 * row_spacing_px:
                r = o[start:i]
                r = r[np.argsort(u[r])]
                gaps += [float(u[b] - u[a]) for a, b in zip(r[:-1], r[1:]) if use[a] and use[b] and u[b] > u[a]]
                start = i
    return gaps


def spacing_from_gaps(gaps):
    """The plant spacing from gaps: the median gap, after dividing gaps over missing plants by their number
    of steps. None with fewer than SPACING_MIN_GAPS gaps."""
    g = np.array([v for v in gaps if v > 0], float)
    if len(g) < SPACING_MIN_GAPS:
        return None
    s0 = float(np.median(g))
    return float(np.median(g / np.maximum(1, np.round(g / s0))))


def plant_spacing(px, py, angle_deg, row_spacing_px, tile_px, use=None):
    """In-row plant spacing (pixels). Returns (spacing, gaps used) or (None, gaps) when there are fewer
    than SPACING_MIN_GAPS gaps."""
    g = spacing_gaps(px, py, angle_deg, row_spacing_px, tile_px, use)
    return spacing_from_gaps(g), len(g)


def _read_features(path):
    ds = ogr.Open(path)
    lay = ds.GetLayer()
    out = []
    names = [lay.GetLayerDefn().GetFieldDefn(i).GetName() for i in range(lay.GetLayerDefn().GetFieldCount())]
    idx = list(enumerate(names))
    for f in lay:                                        # fields read by position: GDAL looks a name up field by field
        g = f.GetGeometryRef()
        if g is None:
            continue
        r = {k: f.GetField(i) for i, k in idx}
        r["fid"] = f.GetFID()
        r["x"] = g.GetX()
        r["y"] = g.GetY()
        out.append(r)
    return out


def auto_split(points_path, plant_spacing_cm=None, log=None):
    """Split every 'check' group into round(length along the row / plant spacing) plants (status
    'auto split', field split_of = the original plant_id). Needs rows (count with rows on); the spacing is
    measured from the plants on the rows unless given. Groups that come out as one plant stay 'check'.
    Writes plant_spacing_cm into ppe_count_info.json. Returns a dict with what was done."""
    log = log or (lambda m: None)
    out_dir = os.path.dirname(points_path)
    info_path = os.path.join(out_dir, "ppe_count_info.json")
    with open(info_path, encoding="utf-8") as fh:
        info = json.load(fh)
    rows = info.get("rows") or {}
    res = {"groups_split": 0, "points_added": 0, "plant_spacing_cm": None, "spacing_from": ""}
    if not rows.get("rows") or rows.get("angle_deg") is None:
        res["note"] = "no crop rows found, so touching plants were not split by spacing (split them with ✂ Split)"
        info["auto_split"] = res
        with open(info_path, "w", encoding="utf-8") as fh:
            json.dump(info, fh, indent=1)
        return res
    ds = safe_gdal_open(info["mask_path"])
    gt = ds.GetGeoTransform()
    pixel_cm = 100.0 * math.sqrt(abs(gt[1] * gt[5]))
    a0 = float(rows["angle_deg"])
    feats = _read_features(points_path)
    if plant_spacing_cm:
        s_px = float(plant_spacing_cm) / pixel_cm
        res["spacing_from"] = "given"
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
            with open(info_path, "w", encoding="utf-8") as fh:
                json.dump(info, fh, indent=1)
            return res
        res["spacing_from"] = "measured from {} gaps between {} confident plants, single plants of {}-{} cm2".format(
            n_gaps, res["confident_plants"], *[int(round(v)) for v in res.get("confident_range_cm2", [0, 0])])
    res["plant_spacing_cm"] = s_px * pixel_cm
    checks = [f for f in feats if f.get("status") == STATUS_CHECK]
    _ensure_split_field(points_path)
    out = ogr.Open(points_path, 1)
    lay = out.GetLayer()
    next_id = max([f.get("plant_id") or 0 for f in feats] or [0]) + 1
    area_field = "area_cm2" if lay.GetLayerDefn().GetFieldIndex("area_cm2") >= 0 else "area_px"
    t = math.radians(a0)
    lay.StartTransaction()                # one commit for all points, not one per point
    for f in checks:
        g = _group_window(info, f["x"], f["y"], f.get(area_field) if area_field == "area_cm2" else None, ds)
        if g is None:
            continue
        m = g[0]
        yy, xx = np.nonzero(m)
        if len(xx) < 2:
            continue
        u = -xx * math.sin(t) + yy * math.cos(t)
        length = float(np.percentile(u, 98) - np.percentile(u, 2))
        n = int(round(length / s_px))
        if n < 2:
            continue
        res.setdefault("patches", []).append({"plant_id": int(f["plant_id"]), "length_cm": round(length * pixel_cm, 1), "plants": n})
        parts = _split_window(g, n)
        if len(parts) < 2:
            continue
        lay.DeleteFeature(f["fid"])
        for x, y, a in parts:
            nf = ogr.Feature(lay.GetLayerDefn())
            p = ogr.Geometry(ogr.wkbPoint)
            p.AddPoint_2D(float(x), float(y))
            nf.SetGeometry(p)
            nf.SetField("plant_id", next_id)
            next_id += 1
            nf.SetField(area_field, float(a) if area_field == "area_cm2" else float(a) / pixel_cm ** 2)
            nf.SetField("status", STATUS_AUTO)
            nf.SetField("on_row", int(f.get("on_row") if f.get("on_row") is not None else 1))
            nf.SetField("split_of", int(f["plant_id"]))
            lay.CreateFeature(nf)
        res["groups_split"] += 1
        res["points_added"] += len(parts) - 1
    lay.CommitTransaction()
    out = None
    res["split_longer_than_cm"] = 1.5 * res["plant_spacing_cm"]
    res["note"] = split_note(res)
    log(res["note"] + ".")
    info["auto_split"] = res
    info["plant_spacing_cm"] = res["plant_spacing_cm"]
    with open(info_path, "w", encoding="utf-8") as fh:
        json.dump(info, fh, indent=1)
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
    if g is None or n < 2:
        return []
    m, x0, y0, gt, pixel_cm, info = g
    yy, xx = np.nonzero(m)
    if len(xx) < n:
        return []
    sib = np.array([((a - gt[0]) / gt[1] - x0 - 0.5, (b - gt[3]) / gt[5] - y0 - 0.5) for a, b in siblings], float)
    me = int(np.argmin((sib[:, 0] - ((x - gt[0]) / gt[1] - x0 - 0.5)) ** 2 + (sib[:, 1] - ((y - gt[3]) / gt[5] - y0 - 0.5)) ** 2))
    near = np.argmin((xx[:, None] - sib[None, :, 0]) ** 2 + (yy[:, None] - sib[None, :, 1]) ** 2, axis=1)
    sub = np.zeros_like(m)
    sub[yy[near == me], xx[near == me]] = True
    return _split_window((sub, x0, y0, gt, pixel_cm, info), n)


def merge_points(points):
    """One point for a list of points [(x, y, area)]: the area-weighted middle and the summed area."""
    a = np.array([max(p[2] or 0.0, 1e-9) for p in points])
    x = np.array([p[0] for p in points])
    y = np.array([p[1] for p in points])
    return float((x * a).sum() / a.sum()), float((y * a).sum() / a.sum()), float(sum(p[2] or 0.0 for p in points))


# ---- 4.7: weeds inside the row ------------------------------------------------------------------------
# Crop plants stand about one plant spacing apart along the row; a weed or a stray leaf inside the row sits
# much closer to a plant. A 'plant' point closer than ROW_WEED_CLOSE x the plant spacing to its neighbour in
# the row, and smaller than a typical plant of the image (and than that neighbour), becomes 'weed?' (not
# counted; the user can switch it back). On PhenoBench (79 images, 541 sugar beet, 262 counted weed points)
# position + size removed about a third of the weed points and cut the per-image count error by a quarter
# to two fifths on every date, for about 4 % of the crop plants. Size alone, colour (RGB only there) and
# shape cost more crop plants than they removed weeds, so they are not used.
ROW_WEED_CLOSE = 0.5
WEED_IN_ROW = "in the row: too close to a plant and smaller than a typical plant"
WEED_BETWEEN = "between the rows"


def _ensure_field(path, name, kind):
    ds = ogr.Open(path, 1)
    lay = ds.GetLayer()
    if lay.GetLayerDefn().GetFieldIndex(name) < 0:
        fd = ogr.FieldDefn(name, kind)
        lay.CreateField(fd)
    ds = None


def flag_row_weeds(points_path, log=None, smaller_than_neighbour=True, close=ROW_WEED_CLOSE):
    """Mark small plants that sit too close to a neighbour in the row as 'weed?' (field weed_reason). Needs
    crop rows and the plant spacing (measured or given, see auto_split). Returns what was done."""
    log = log or (lambda m: None)
    out_dir = os.path.dirname(points_path)
    info_path = os.path.join(out_dir, "ppe_count_info.json")
    with open(info_path, encoding="utf-8") as fh:
        info = json.load(fh)
    rows = info.get("rows") or {}
    spacing = info.get("plant_spacing_cm")
    res = {"flagged": 0, "close_cm": None}
    _ensure_field(points_path, "weed_reason", ogr.OFTString)
    feats = _read_features(points_path)
    ds = ogr.Open(points_path, 1)
    lay = ds.GetLayer()
    lay.StartTransaction()   # one commit for all edits
    for f in feats:                                      # weeds found between the rows get their reason too
        if f.get("status") == STATUS_WEED and not f.get("weed_reason"):
            g = lay.GetFeature(f["fid"])
            g.SetField("weed_reason", WEED_BETWEEN)
            lay.SetFeature(g)
    if not rows.get("rows") or not spacing:
        lay.CommitTransaction()
        ds = None
        res["note"] = "weeds inside the rows not checked: that needs crop rows and the plant spacing"
        info["row_weeds"] = res
        with open(info_path, "w", encoding="utf-8") as fh:
            json.dump(info, fh, indent=1)
        return res
    gt = safe_gdal_open(info["mask_path"]).GetGeoTransform()
    pixel_cm = 100.0 * math.sqrt(abs(gt[1] * gt[5]))
    area_key = "area_cm2" if any(f.get("area_cm2") is not None for f in feats) else "area_px"
    k = 1.0 if area_key == "area_cm2" else pixel_cm ** 2
    pts = [f for f in feats if f.get("status") != STATUS_WEED]
    plain = [f.get(area_key) for f in pts if f.get("status") == STATUS_PLANT and f.get(area_key)]
    if len(plain) < 3:
        lay.CommitTransaction()
        ds = None
        res["note"] = "too few plants to check for weeds inside the rows"
        info["row_weeds"] = res
        with open(info_path, "w", encoding="utf-8") as fh:
            json.dump(info, fh, indent=1)
        return res
    typical = float(np.median(plain)) * k
    t = math.radians(float(rows["angle_deg"]))
    row_cm = float(rows["spacing_cm"])
    x = np.array([(f["x"] - gt[0]) / gt[1] * pixel_cm for f in pts])
    y = np.array([(f["y"] - gt[3]) / gt[5] * pixel_cm for f in pts])
    d = x * math.cos(t) + y * math.sin(t)
    u = -x * math.sin(t) + y * math.cos(t)
    a = np.array([(f.get(area_key) or 0.0) * k for f in pts])
    limit = close * float(spacing)
    flagged = []
    # Only neighbours closer than `limit` along the row can make a weed, so each plant looks only at the
    # plants in a window of +-limit along the row (sorted once): the same neighbour as a search of all
    # plants (nearest along the row, the first one on a tie), in about n log n instead of n squared.
    by_u = np.argsort(u, kind="stable")
    us = u[by_u]
    pad = limit * 1.001 + 1e-9
    for i, f in enumerate(pts):
        if f.get("status") != STATUS_PLANT or a[i] >= typical:
            continue
        lo, hi = np.searchsorted(us, u[i] - pad, "left"), np.searchsorted(us, u[i] + pad, "right")
        cand = np.sort(by_u[lo:hi])
        cand = cand[(np.abs(d[cand] - d[i]) < 0.5 * row_cm) & (cand != i)]
        if not len(cand):
            continue
        j = cand[np.argmin(np.abs(u[cand] - u[i]))]
        gap = abs(u[j] - u[i])
        if gap >= limit:
            continue
        if smaller_than_neighbour and a[i] >= a[j]:
            continue
        flagged.append((f, gap, pts[j].get("plant_id")))
    for f, gap, nb in flagged:
        g = lay.GetFeature(f["fid"])
        g.SetField("status", STATUS_WEED)
        g.SetField("weed_reason", "{}: {:.0f} cm from plant {} (plant spacing {:.0f} cm), {:.0f} cm2 (typical {:.0f} cm2)".format(
            "in the row", gap, nb, spacing, (f.get(area_key) or 0) * k, typical))
        lay.SetFeature(g)
    lay.CommitTransaction()
    ds = None
    res.update(flagged=len(flagged), close_cm=limit, typical_cm2=typical)
    res["note"] = ("{} small plant{} inside the rows marked weed? (not counted): closer than {:.1f} cm (half the plant spacing) to a "
                   "plant and smaller than a typical plant ({:.0f} cm2). Switch any that are crop back to plant").format(
        len(flagged), "" if len(flagged) == 1 else "s", limit, typical)
    log(res["note"] + ".")
    info["row_weeds"] = res
    with open(info_path, "w", encoding="utf-8") as fh:
        json.dump(info, fh, indent=1)
    return res


# 4.9.0: weeds as kinds of "What PPE found", so the plant mask can be made crop only. After a run the
# counter runs once on the mask at the suggested threshold (rows, plant spacing, weeds between and inside
# the rows, the same rules as the plant counter); the pixels of every weed? plant get code 7 (between the
# rows) or 8 (inside the row) in the findings raster. The user leaves either kind out of the mask (−) or
# keeps it; the vote is not changed. Needs crop rows; without them nothing is marked.
WEED_BETWEEN_CODE, WEED_IN_ROW_CODE = 7, 8
MAX_WEED_PIECE = 3.0      # review: a piece over 3 x a typical plant is crop joined to a weed, never marked weed as a whole


def mark_weeds(vote_path, findings_path, threshold, work_dir, log=None, progress=None, cfg=None, keep_dir=None):
    """Mark weed pixels in the findings raster (codes 7 and 8) from a count at `threshold`. Returns a summary.
    With keep_dir the count (pieces, points) and a copy of the findings before the weeds are kept there,
    so single pieces can be switched weed <-> crop later (toggle_weed_piece)."""
    import shutil
    log = log or (lambda m: None)
    cfg = dict(cfg or {})
    tmp = keep_dir or os.path.join(work_dir, "_ppe_weeds_tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    res = {"threshold": float(threshold), "between": 0, "in_row": 0, "pixels_between": 0, "pixels_in_row": 0, "pixels_pct": 0.0}
    try:
        v_ds = safe_gdal_open(vote_path)
        W, H = v_ds.RasterXSize, v_ds.RasterYSize
        cut = max(0.0, float(threshold) - 0.05)
        if not _pixel_area_m2(v_ds):
            res["note"] = "the image is not in metres, so rows and plant sizes cannot be measured"
            return res
        mpath = os.path.join(tmp, "mask.tif")
        m_ds = gdal.GetDriverByName("GTiff").Create(mpath, W, H, 1, gdal.GDT_Byte, ["TILED=YES", "COMPRESS=DEFLATE", "BIGTIFF=IF_SAFER"])
        m_ds.SetGeoTransform(v_ds.GetGeoTransform())
        m_ds.SetProjection(v_ds.GetProjection())
        m_ds.GetRasterBand(1).SetNoDataValue(255)
        valid_all = 0
        for y in range(0, H, 512):
            h = min(512, H - y)
            v = v_ds.GetRasterBand(1).ReadAsArray(0, y, W, h)
            ok = np.isfinite(v) & (v >= 0) & (v <= 1)
            valid_all += int(ok.sum())
            m_ds.GetRasterBand(1).WriteArray(np.where(ok, (v >= cut).astype(np.uint8), 255).astype(np.uint8), 0, y)
        m_ds = None
        c = count_plants(dict({"mask_path": mpath, "vote_path": vote_path, "output_dir": tmp, "find_rows": True,
                               "groups_path": os.path.join(tmp, "groups.tif")}, **cfg), progress=progress)
        rows = c.get("rows") or {}
        res.update(rows_found=bool(rows.get("rows")), plant_spacing_cm=c.get("plant_spacing_cm"), plants=c.get("plants"))
        if not rows.get("rows"):
            res["note"] = "no clear crop rows, so weeds cannot be told from crop plants"
            return res
        gid = np.load(os.path.join(tmp, "ppe_group_ids.npy"))
        code = np.zeros(int(gid.max()) + 1 if len(gid) else 1, np.uint8)
        feats = _read_features(c["points_path"])
        ak = "area_cm2" if any(f.get("area_cm2") is not None for f in feats) else "area_px"
        typical = float(np.median([f[ak] for f in feats if f.get("status") == STATUS_PLANT and f.get(ak)] or [np.inf]))
        res["typical_plant"] = typical
        px_cm2 = (_pixel_area_m2(v_ds) or 0) * 1e4
        res["typical_px"] = (typical / px_cm2 if ak == "area_cm2" and px_cm2 else typical) if np.isfinite(typical) else None
        for f in feats:
            pid = int(f.get("plant_id") or 0)
            if f.get("status") != STATUS_WEED or not (1 <= pid <= len(gid)):
                continue
            inrow = str(f.get("weed_reason") or "").startswith("in the row")
            # Between the rows, a piece at least as big as a typical plant is kept: on closed or merged canopy its
            # centre can fall between the rows (Tifton). The plant counter still shows it as weed? for review.
            if not inrow and not ((f.get(ak) or 0) < typical):
                res["kept_big"] = res.get("kept_big", 0) + 1
                continue
            code[gid[pid - 1]] = WEED_IN_ROW_CODE if inrow else WEED_BETWEEN_CODE
            res["in_row" if inrow else "between"] += 1
        if keep_dir:
            gdal.GetDriverByName("GTiff").CreateCopy(os.path.join(tmp, "findings_before_weeds.tif"), safe_gdal_open(findings_path),
                                                     options=["TILED=YES", "COMPRESS=DEFLATE", "BIGTIFF=IF_SAFER"])
            res.update(groups_path=os.path.join(tmp, "groups.tif"), points_path=c["points_path"], ids_path=os.path.join(tmp, "ppe_group_ids.npy"),
                       before_path=os.path.join(tmp, "findings_before_weeds.tif"), cut=cut)
        g_ds = safe_gdal_open(os.path.join(tmp, "groups.tif"))
        f_ds = gdal.Open(findings_path, gdal.GA_Update)
        m_ds = safe_gdal_open(mpath)
        # A piece cut by the edge of the image or of the field (no data) cannot be judged: cut short it looks
        # small and off the row. Such pieces are never marked (IH: all 17 first marks were at the field edge).
        edge = np.zeros(len(code), bool)
        for y in range(0, H, 512):
            y0, y1 = max(0, y - 1), min(H, y + 513)
            h = min(512, H - y)
            g = g_ds.GetRasterBand(1).ReadAsArray(0, y0, W, y1 - y0)
            nd = m_ds.GetRasterBand(1).ReadAsArray(0, y0, W, y1 - y0) == 255
            nd = np.pad(nd, 1, constant_values=True)                    # outside the image counts as no data
            near = nd[:-2, 1:-1] | nd[2:, 1:-1] | nd[1:-1, :-2] | nd[1:-1, 2:] | nd[1:-1, 1:-1]
            hit = np.unique(g[near & (g > 0)])
            edge[hit[hit < len(code)]] = True
        res["at_edge_not_marked"] = int((edge & (code > 0)).sum())
        code[edge] = 0
        for y in range(0, H, 512):
            h = min(512, H - y)
            g = g_ds.GetRasterBand(1).ReadAsArray(0, y, W, h)
            w = code[np.clip(g, 0, len(code) - 1)] * (m_ds.GetRasterBand(1).ReadAsArray(0, y, W, h) == 1)
            if not w.any():
                continue
            f = f_ds.GetRasterBand(1).ReadAsArray(0, y, W, h)
            f = np.where((w > 0) & (f != 255), w, f).astype(np.uint8)
            f_ds.GetRasterBand(1).WriteArray(f, 0, y)
            res["pixels_between"] += int((f == WEED_BETWEEN_CODE).sum())
            res["pixels_in_row"] += int((f == WEED_IN_ROW_CODE).sum())
        f_ds.FlushCache()
        f_ds = None
        g_ds = None
        m_ds = None
        res["between"] = int((code == WEED_BETWEEN_CODE).sum())
        res["in_row"] = int((code == WEED_IN_ROW_CODE).sum())
        res["pixels_pct"] = 100.0 * (res["pixels_between"] + res["pixels_in_row"]) / max(1, valid_all)
        res["note"] = ("{} weed{} between the rows and {} small plant{} crowding a crop plant inside the rows ({:.2f}% of the image) at {:.2f}"
                       ).format(res["between"], "" if res["between"] == 1 else "s", res["in_row"], "" if res["in_row"] == 1 else "s",
                                res["pixels_pct"], float(threshold))
        return res
    finally:
        if keep_dir:
            for name in ("mask.tif", "ppe_count_summary.csv"):
                try:
                    os.remove(os.path.join(tmp, name))
                except OSError:
                    pass
        else:
            shutil.rmtree(tmp, ignore_errors=True)


COUNTED = (STATUS_PLANT, STATUS_CHECK, STATUS_ADDED, "split", "auto split")


def crop_only_mask(points_path, leave_out_uncounted=False, log=None):
    """4.9.0: the plant mask without the pixels of the weeds, as the points are now (after the user's review).
    A plant piece of the count is left out when it had a point and none of its points is counted any more:
    every point on it is weed? (PPE's or switched by the user) or was removed. A piece with any counted
    point (plant, check, split, added) stays whole. With leave_out_uncounted, pieces the count never took
    as plants (smaller than the smallest plant, or with no pixel every vote calls plant) are left out too.
    Writes <mask>_crop_only.tif next to the points and returns what was done."""
    log = log or (lambda m: None)
    out_dir = os.path.dirname(points_path)
    with open(os.path.join(out_dir, "ppe_count_info.json"), encoding="utf-8") as fh:
        info = json.load(fh)
    gpath = info.get("groups_path") or ""
    ids_path = os.path.join(out_dir, "ppe_group_ids.npy")
    if not gpath or not os.path.isfile(gpath) or not os.path.isfile(ids_path):
        raise ValueError("This count has no plant pieces saved (it was made with an older PPE). Count the plants again, then make the crop-only mask.")
    src_path = info.get("source_mask_path") or info["mask_path"]
    g_ds = safe_gdal_open(gpath)
    m_ds = safe_gdal_open(src_path)
    if m_ds is None:
        raise ValueError("The plant mask of this count is missing: {}".format(src_path))
    W, H = g_ds.RasterXSize, g_ds.RasterYSize
    gt = g_ds.GetGeoTransform()
    if (m_ds.RasterXSize, m_ds.RasterYSize) != (W, H):
        raise ValueError("The plant mask does not match the counted pieces. Count the plants again.")
    gid = np.load(ids_path)
    n_g = int(gid.max()) + 1 if len(gid) else 1
    had = np.zeros(n_g, bool)
    had[gid] = True          # pieces the count made a point for
    counted = np.zeros(n_g, bool)
    feats = _read_features(points_path)
    for f in feats:
        if f.get("status") not in COUNTED:
            continue
        pid = int(f.get("plant_id") or 0)
        k = 0
        if 1 <= pid <= len(gid) and f.get("status") != STATUS_ADDED:
            k = int(gid[pid - 1])
        else:                                            # split and added points: the piece under the point
            c = int((f["x"] - gt[0]) / gt[1])
            r = int((f["y"] - gt[3]) / gt[5])
            if 0 <= c < W and 0 <= r < H:
                k = int(g_ds.GetRasterBand(1).ReadAsArray(c, r, 1, 1)[0, 0])
        if 0 < k < n_g:
            counted[k] = True
    drop = had & ~counted
    if leave_out_uncounted:
        drop |= ~had
    drop[0] = False
    stem = os.path.splitext(os.path.basename(src_path))[0]
    out_path = os.path.join(out_dir, stem + ("_crop_only_strict" if leave_out_uncounted else "_crop_only") + ".tif")
    out = gdal.GetDriverByName("GTiff").Create(out_path, W, H, 1, gdal.GDT_Byte, ["TILED=YES", "COMPRESS=DEFLATE", "BIGTIFF=IF_SAFER"])
    out.SetGeoTransform(m_ds.GetGeoTransform())
    out.SetProjection(m_ds.GetProjection())
    out.GetRasterBand(1).SetNoDataValue(255)
    before = removed = 0
    for y in range(0, H, 512):
        h = min(512, H - y)
        m = m_ds.GetRasterBand(1).ReadAsArray(0, y, W, h)
        g = g_ds.GetRasterBand(1).ReadAsArray(0, y, W, h)
        plant = m == 1
        cut = plant & drop[np.clip(g, 0, n_g - 1)]
        before += int(plant.sum())
        removed += int(cut.sum())
        m = m.copy()
        m[cut] = 0
        out.GetRasterBand(1).WriteArray(m, 0, y)
    out.FlushCache()
    out = None
    res = {"mask_path": out_path, "pieces_left_out": int(drop[1:].sum()), "pixels_left_out": removed, "plant_pixels_before": before,
           "left_out_pct": 100.0 * removed / max(1, before), "leave_out_uncounted": bool(leave_out_uncounted)}
    res["note"] = "Crop-only mask: {:,} pieces left out ({:,} pixels, {:.1f} % of the plant): the weeds{} as the points are now".format(
        res["pieces_left_out"], removed, res["left_out_pct"], " and the pieces too small to count" if leave_out_uncounted else "")
    log(res["note"] + ".")
    return res


def toggle_weed_piece(weeds, findings_path, vote_path, x, y, levels=1200):
    """Review one plant piece of 'What PPE found': the piece under map point (x, y) switches between weed
    (code 7 between the rows, 8 inside them) and crop (its codes from before the weeds are put back).
    `weeds` is the run's weeds summary (paths from mark_weeds with keep_dir). Returns
    {"state": "weed"|"crop"|None, "pixels": n, "code": c, "hist_delta": {code: {level: change}}, "message": text}."""
    g_ds = safe_gdal_open(weeds["groups_path"])
    gt = g_ds.GetGeoTransform()
    W, H = g_ds.RasterXSize, g_ds.RasterYSize
    col = int(math.floor((x - gt[0]) / gt[1]))
    row = int(math.floor((y - gt[3]) / gt[5]))
    if not (0 <= col < W and 0 <= row < H):
        return {"state": None, "message": "Outside the image."}
    gid = int(g_ds.GetRasterBand(1).ReadAsArray(col, row, 1, 1)[0, 0])
    if gid <= 0:
        return {"state": None, "message": "No plant piece here: click on the plant itself."}
    half = 128
    while True:                       # a window around the click that holds the whole piece
        c0, r0 = max(0, col - half), max(0, row - half)
        c1, r1 = min(W, col + half), min(H, row + half)
        g = g_ds.GetRasterBand(1).ReadAsArray(c0, r0, c1 - c0, r1 - r0)
        on = g == gid
        touches = (on[0].any() and r0 > 0) or (on[-1].any() and r1 < H) or (on[:, 0].any() and c0 > 0) or (on[:, -1].any() and c1 < W)
        if not touches:
            break
        half *= 2
    v_ds = safe_gdal_open(vote_path)
    v = v_ds.GetRasterBand(1).ReadAsArray(c0, r0, c1 - c0, r1 - r0)
    v_ds = None
    f_ds = gdal.Open(findings_path, gdal.GA_Update)
    f = f_ds.GetRasterBand(1).ReadAsArray(c0, r0, c1 - c0, r1 - r0)
    piece = on & np.isfinite(v) & (v >= float(weeds.get("cut", 0.0))) & (v <= 1) & (f != 255)
    if not piece.any():
        f_ds = None
        return {"state": None, "message": "This piece is below the threshold PPE looked for weeds at."}
    old = f[piece].astype(np.intp)
    if np.isin(old, (WEED_BETWEEN_CODE, WEED_IN_ROW_CODE)).any():      # weed -> crop: the codes from before
        b_ds = safe_gdal_open(weeds["before_path"])
        before = b_ds.GetRasterBand(1).ReadAsArray(c0, r0, c1 - c0, r1 - r0)
        b_ds = None
        new = before[piece].astype(np.intp)
        new[np.isin(new, (WEED_BETWEEN_CODE, WEED_IN_ROW_CODE))] = 0
        state = "crop"
    else:                                                              # crop -> weed: between or inside the rows
        big = weeds.get("typical_px")
        if big and piece.sum() > MAX_WEED_PIECE * big:
            f_ds = None
            return {"state": None, "message": "This piece is {:.0f} times a typical plant: it is joined to crop plants, so it cannot be marked a "
                                              "weed as a whole (that would take the crop out too). Leave it, or raise the threshold so it splits.".format(
                                                  piece.sum() / big)}
        code = WEED_IN_ROW_CODE
        try:
            ids = np.load(weeds["ids_path"])
            k = np.nonzero(ids == gid)[0]
            if len(k):
                for p in _read_features(weeds["points_path"]):
                    if int(p.get("plant_id") or 0) == int(k[0]) + 1:
                        code = WEED_IN_ROW_CODE if (p.get("on_row") is None or int(p.get("on_row"))) else WEED_BETWEEN_CODE
                        break
        except (OSError, ValueError, KeyError):
            pass
        new = np.full(old.shape, code, np.intp)
        state = "weed"
    out = f.copy()
    out[piece] = new.astype(np.uint8)
    f_ds.GetRasterBand(1).WriteArray(out, c0, r0)
    f_ds.FlushCache()
    f_ds = None
    q = np.rint(v[piece].astype(np.float64) * levels).astype(np.intp)
    delta = {}
    for codes, sign in ((old, -1), (new, 1)):
        for c in np.unique(codes):
            lv, n = np.unique(q[codes == c], return_counts=True)
            d = delta.setdefault(int(c), {})
            for a, b in zip(lv.tolist(), n.tolist()):
                d[a] = d.get(a, 0) + sign * b
    name = "weed" if state == "weed" else "crop"
    return {"state": state, "pixels": int(piece.sum()), "code": int(new[0]), "hist_delta": delta,
            "message": "Piece of {:,} pixels marked {}.".format(int(piece.sum()), name)}
