"""Create labels (4.6): plant and soil label points for a machine-learning model, from PPE's own result.

PPE is unchanged: the points come from what the finalized mask calls plant and what it calls soil.
Only clear pixels can become a point:
    plant  = in the plant mask, every vote sure (vote >= 0.9), no PPE finding (pale or dry parts,
             leaf edges, shade, unclear, faint sparse spots), and not on the edge of the plant
    soil   = not in the plant mask, vote <= 0.1, no PPE finding, and not on the edge of the soil
Everything else (uncertain pixels near the threshold, plant parts PPE flags, no data) is never sampled.

Where the points go is chosen by conditioned Latin hypercube sampling (cLHS, Minasny and McBratney 2006),
separately for plant and for soil: the points spread over the whole range of every band, NDVI and the
position in the field, so the labels cover the whole distribution and not just the easy pixels.

An optional area (polygons drawn on the map or a polygon file) limits everything to the inside: no
point and no buffer pixel outside it, so field edges, headlands or weedy corners can be left out.

After the user checks the points on the map, each point can grow into a buffer: a disc of the chosen
radius, cut to the pixels of the same class (the same clear-pixel rule), so a buffer never crosses into
the other class. Every pixel in a buffer gets the point's label. A point the user moved to the other
class (or added) keeps just its own pixel, because PPE's mask disagrees with it there.
"""
import csv
import json
import math
import os
import time

import numpy as np
from osgeo import gdal, ogr, osr

from .native_engine import FINDING_NAMES, LEVEL_SURE, NODATA, NativePPEEngine, safe_gdal_open

PLANT, SOIL = 1, 0
NAMES = {PLANT: "plant", SOIL: "soil"}
CANDIDATES = 20000           # cLHS chooses from this many clear pixels per class (spread over the image)
EDGE_PX = 1                  # a clear pixel needs this many pixels of its own class all round
MAX_BUFFER_CM = 50.0
STEP_CM = 0.5                # buffer slider step
BLOCK = 512
LAYER = "points"
STATUS_PROPOSED, STATUS_CHANGED, STATUS_ADDED = "proposed", "changed", "added"


# ---- clear pixels ------------------------------------------------------------------------------
def _erode(m, n):
    """Pixels of m whose whole (2n+1) square is m (edges of the array count as outside)."""
    if n <= 0: return m
    p = np.pad(m, n, constant_values=False); h, w = m.shape; out = np.ones_like(m)
    for dy in range(-n, n + 1):
        for dx in range(-n, n + 1):
            out &= p[n + dy:n + dy + h, n + dx:n + dx + w]
    return out


def clear_classes(mask, vote, findings, edge=EDGE_PX, area=None):
    """(plant, soil) boolean arrays of clear pixels; see the module text. mask: 1 plant, 0 soil, 255
    no data; vote: vote fraction; findings: PPE finding codes (0 = none) or None; area: True inside
    the chosen area (None = everywhere)."""
    valid = (mask != 255) & np.isfinite(vote) & (vote != NODATA) & (vote >= 0) & (vote <= 1)
    if area is not None: valid &= area
    clean = valid if findings is None else valid & (findings == 0)
    plant = clean & (mask == 1) & (vote >= LEVEL_SURE)
    soil = clean & (mask == 0) & (vote <= 1.0 - LEVEL_SURE)
    return _erode(plant, edge), _erode(soil, edge)


class _Rasters:
    """The mask, vote and findings of one PPE result, read by window."""

    def __init__(self, mask_path, vote_path, findings_path="", area_path=""):
        self.mask = safe_gdal_open(mask_path); self.vote = safe_gdal_open(vote_path)
        self.area = safe_gdal_open(area_path) if area_path and os.path.isfile(area_path) else None
        self.findings = safe_gdal_open(findings_path) if findings_path and os.path.isfile(findings_path) else None
        self.w, self.h = self.mask.RasterXSize, self.mask.RasterYSize
        for ds in (self.vote, self.findings, self.area):
            if ds is not None and (ds.RasterXSize != self.w or ds.RasterYSize != self.h):
                raise ValueError("The plant mask and the PPE vote do not have the same size. Finalize the mask again.")
        self.gt = self.mask.GetGeoTransform(); self.wkt = self.mask.GetProjection()
        self.pixel_cm = 100.0 * math.sqrt(abs(self.gt[1] * self.gt[5] - self.gt[2] * self.gt[4]))

    def class_mask(self, x, y, w, h, label, edge=EDGE_PX):
        """All pixels of PPE's mask class (plant 1 / soil 0) in a window, at least `edge` pixels from the
        other class and from no data (4.6.7, buffer mode 'class')."""
        x0, y0 = max(0, x - edge), max(0, y - edge); x1, y1 = min(self.w, x + w + edge), min(self.h, y + h + edge)
        m = self.mask.GetRasterBand(1).ReadAsArray(x0, y0, x1 - x0, y1 - y0)
        c = (m == (1 if label == PLANT else 0))
        if self.area is not None: c &= self.area.GetRasterBand(1).ReadAsArray(x0, y0, x1 - x0, y1 - y0).astype(bool)
        c = _erode(c, edge)
        return c[y - y0:y - y0 + h, x - x0:x - x0 + w]

    def classes(self, x, y, w, h, edge=EDGE_PX):
        """Clear plant and soil in a window; reads a margin around it so the edge rule is right."""
        x0, y0 = max(0, x - edge), max(0, y - edge); x1, y1 = min(self.w, x + w + edge), min(self.h, y + h + edge)
        m = self.mask.GetRasterBand(1).ReadAsArray(x0, y0, x1 - x0, y1 - y0)
        v = self.vote.GetRasterBand(1).ReadAsArray(x0, y0, x1 - x0, y1 - y0).astype(np.float32)
        f = self.findings.GetRasterBand(1).ReadAsArray(x0, y0, x1 - x0, y1 - y0) if self.findings is not None else None
        a = self.area.GetRasterBand(1).ReadAsArray(x0, y0, x1 - x0, y1 - y0).astype(bool) if self.area is not None else None
        p, s = clear_classes(m, v, f, edge, a)
        sl = (slice(y - y0, y - y0 + h), slice(x - x0, x - x0 + w))
        return p[sl], s[sl]


def _area_raster(cfg, out_dir, stem="ppe_labels_area"):
    """The chosen area as a raster aligned with the mask (1 inside), or '' for the whole image.
    cfg area_wkts: polygons in the image CRS (drawn on the map), or area_path: a polygon file in any CRS.
    The polygons are also kept as <stem>.gpkg."""
    wkts = [w for w in (cfg.get("area_wkts") or []) if w]; src = cfg.get("area_path") or ""
    raster = os.path.join(out_dir, stem + ".tif"); vec = os.path.join(out_dir, stem + ".gpkg")
    if os.path.exists(raster): os.remove(raster)
    if not wkts and not src: return ""
    if os.path.exists(vec): gdal.GetDriverByName("GPKG").Delete(vec)
    mask = safe_gdal_open(cfg["mask_path"]); wkt = mask.GetProjection(); srs = osr.SpatialReference(); srs.ImportFromWkt(wkt)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    geoms = [ogr.CreateGeometryFromWkt(w) for w in wkts]
    if src:
        ds = ogr.Open(src)
        if ds is None: raise ValueError("Cannot open the area file: {}".format(src))
        L = ds.GetLayer(0); s0 = L.GetSpatialRef(); tr = None
        if s0 is not None:
            s0.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER); tr = osr.CoordinateTransformation(s0, srs)
        for f in L:
            g = f.GetGeometryRef()
            if g is None: continue
            g = g.Clone()
            if tr is not None: g.Transform(tr)
            if g.GetDimension() == 2: geoms.append(g)
    if not geoms: raise ValueError("The area has no polygons.")
    vds = ogr.GetDriverByName("GPKG").CreateDataSource(vec); VL = vds.CreateLayer("area", srs, ogr.wkbMultiPolygon)
    for g in geoms:
        f = ogr.Feature(VL.GetLayerDefn()); f.SetGeometry(ogr.ForceToMultiPolygon(g)); VL.CreateFeature(f)
    out = gdal.GetDriverByName("GTiff").Create(raster, mask.RasterXSize, mask.RasterYSize, 1, gdal.GDT_Byte, options=["COMPRESS=DEFLATE", "TILED=YES", "SPARSE_OK=TRUE"])
    out.SetGeoTransform(mask.GetGeoTransform()); out.SetProjection(wkt); out.GetRasterBand(1).Fill(0)
    gdal.RasterizeLayer(out, [1], VL, burn_values=[1]); out.FlushCache()
    inside = 0
    for y in range(0, mask.RasterYSize, 2048): inside += int(out.GetRasterBand(1).ReadAsArray(0, y, mask.RasterXSize, min(2048, mask.RasterYSize - y)).sum())
    out = None; vds = None
    if inside == 0: raise ValueError("The area does not overlap the image.")
    return raster


# ---- cLHS ----------------------------------------------------------------------------------------
def clhs(X, n, iterations=None, seed=0, weights=(1.0, 1.0)):
    """Indices of n rows of X (candidates x covariates) chosen by conditioned Latin hypercube sampling:
    each covariate is cut into n equal-count strata and the sample should hold one point per stratum
    (O1), with the same correlation between covariates as all candidates (O3). Simulated annealing
    with incremental updates, so thousands of points take a few seconds."""
    X = np.asarray(X, np.float64); N, k = X.shape
    if n >= N: return np.arange(N)
    rng = np.random.default_rng(seed)
    sd = X.std(0); sd[sd == 0] = 1; Z = (X - X.mean(0)) / sd
    edges = [np.unique(np.quantile(Z[:, j], np.linspace(0, 1, n + 1)[1:-1])) for j in range(k)]
    S = np.stack([np.searchsorted(edges[j], Z[:, j], side="right") for j in range(k)], 1)       # stratum of each candidate
    nstr = np.array([len(e) + 1 for e in edges]); target = np.array([N / ns for ns in nstr]) * n / N  # points per stratum (1 when unique)
    C_all = np.corrcoef(Z, rowvar=False) if k > 1 else np.ones((1, 1))
    sample = rng.choice(N, n, replace=False); inside = np.zeros(N, bool); inside[sample] = True
    counts = [np.bincount(S[sample, j], minlength=nstr[j]).astype(np.float64) for j in range(k)]
    s1 = Z[sample].sum(0); s2 = Z[sample].T @ Z[sample]

    def corr_term(s1, s2):
        if k == 1: return 0.0
        m = s1 / n; cov = s2 / n - np.outer(m, m); d = np.sqrt(np.clip(np.diag(cov), 1e-12, None))
        return float(np.abs(cov / np.outer(d, d) - C_all).sum())

    o1 = sum(float(np.abs(counts[j] - target[j]).sum()) for j in range(k)); o3 = corr_term(s1, s2)
    obj = weights[0] * o1 + weights[1] * o3
    iterations = int(iterations or min(20000, max(3000, 20 * n))); T = 1.0; cool = 0.95; best = (obj, sample.copy())
    pool = np.flatnonzero(~inside)                 # candidates not in the sample
    for it in range(iterations):
        if it % 50 == 0 and it: T *= cool
        pos = rng.integers(n)
        if rng.random() < 0.3:                     # targeted: drop the point that sits in the most crowded strata
            crowd = np.zeros(n)
            for j in range(k): crowd += counts[j][S[sample, j]] - target[j]
            pos = int(np.argmax(crowd + rng.random(n) * 1e-6))
        old = sample[pos]
        q = rng.integers(pool.size); new = pool[q]
        d1 = 0.0
        for j in range(k):
            a, b = S[old, j], S[new, j]
            if a == b: continue
            c = counts[j]; t = target[j]
            d1 += abs(c[a] - 1 - t) - abs(c[a] - t) + abs(c[b] + 1 - t) - abs(c[b] - t)
        zo, zn = Z[old], Z[new]
        n1 = s1 - zo + zn; n2 = s2 - np.outer(zo, zo) + np.outer(zn, zn); n3 = corr_term(n1, n2)
        delta = weights[0] * d1 + weights[1] * (n3 - o3)
        if delta <= 0 or rng.random() < math.exp(-delta / max(T, 1e-9)):
            for j in range(k):
                counts[j][S[old, j]] -= 1; counts[j][S[new, j]] += 1
            s1, s2, o3 = n1, n2, n3; obj += delta; sample[pos] = new; pool[q] = old
            if obj < best[0]: best = (obj, sample.copy())
    return np.sort(best[1])


# ---- create the points ---------------------------------------------------------------------------
def _engine(cfg):
    e = NativePPEEngine(log=lambda m: None)
    e._resolve(dict(cfg, output_dir=cfg.get("output_dir") or cfg.get("work_dir") or "", work_dir=cfg.get("work_dir") or cfg.get("output_dir") or ""))
    return e


def _features(engine, names, x, y, w, h):
    got = engine._read_bands(names, x=x, y=y, w=w, h=h)
    return {b: np.asarray(got[b], np.float64) for b in names}


def _ndvi(b):
    if "nir" in b and "red" in b:
        s = b["nir"] + b["red"]
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(s != 0, (b["nir"] - b["red"]) / s, np.nan)
    return None


def create_points(cfg, progress=lambda p, m: None, log=lambda m: None, cancelled=lambda: False):
    """cfg: mask_path, vote_path, findings_path, the image (as for a run), n_points, plant_share (0-1),
    output_dir, seed. Writes <output_dir>/ppe_labels.gpkg (layer 'points') and ppe_labels_info.json."""
    t0 = time.time(); out_dir = cfg["output_dir"]; os.makedirs(out_dir, exist_ok=True)
    area_path = _area_raster(cfg, out_dir)
    R = _Rasters(cfg["mask_path"], cfg["vote_path"], cfg.get("findings_path", ""), area_path)
    n_total = int(cfg.get("n_points", 500)); share = float(cfg.get("plant_share", 0.5)); seed = int(cfg.get("seed", 20261123))
    want = {PLANT: int(round(n_total * share)), SOIL: n_total - int(round(n_total * share))}
    # pass 1: how many clear pixels of each class
    count = {PLANT: 0, SOIL: 0}
    for y in range(0, R.h, BLOCK):
        p, s = R.classes(0, y, R.w, min(BLOCK, R.h - y)); count[PLANT] += int(p.sum()); count[SOIL] += int(s.sum())
        progress(int(30 * (y + BLOCK) / R.h), "Finding clear plant and soil pixels")
        if cancelled(): raise RuntimeError("Cancelled.")
    for c in (PLANT, SOIL):
        if count[c] == 0: raise ValueError("No clear {} pixels{}: check the finalized mask (tab 3){}.".format(
            NAMES[c], " inside the area" if area_path else "", " or draw a bigger area" if area_path else ""))
    # pass 2: an even random spread of candidates, with their band values
    e = _engine(cfg); bands = [b for b in ("blue", "green", "red", "re", "nir") if b in e.sources]
    rng = np.random.default_rng(seed); prob = {c: min(1.0, max(CANDIDATES, 3 * want[c]) / count[c]) for c in (PLANT, SOIL)}
    cand = {PLANT: [], SOIL: []}
    for y in range(0, R.h, BLOCK):
        h = min(BLOCK, R.h - y); p, s = R.classes(0, y, R.w, h); b = None
        for c, m in ((PLANT, p), (SOIL, s)):
            rr, cc = np.nonzero(m)
            if rr.size == 0: continue
            keep = rng.random(rr.size) < prob[c]; rr, cc = rr[keep], cc[keep]
            if rr.size == 0: continue
            if b is None: b = _features(e, bands, 0, y, R.w, h)
            vals = np.stack([b[n][rr, cc] for n in bands], 1)
            nd = _ndvi({n: b[n][rr, cc] for n in bands})
            cand[c].append(np.column_stack([cc, rr + y, vals] + ([nd] if nd is not None else [])))
        progress(30 + int(30 * (y + h) / R.h), "Reading the bands of clear pixels")
        if cancelled(): raise RuntimeError("Cancelled.")
    cols = ["px", "py"] + bands + (["ndvi"] if "nir" in bands and "red" in bands else [])
    points = []
    for c in (PLANT, SOIL):
        A = np.concatenate(cand[c]) if cand[c] else np.zeros((0, len(cols)))
        A = A[np.all(np.isfinite(A), 1)]
        if len(A) == 0: raise ValueError("No clear {} pixels with valid band values.".format(NAMES[c]))
        n = min(want[c], len(A))
        if n < want[c]: log("Only {} clear {} pixels to choose from: {} {} points.".format(len(A), NAMES[c], n, NAMES[c]))
        progress(60 if c == PLANT else 78, "Choosing {} {} points (cLHS)".format(n, NAMES[c]))
        cov = A[:, 2:]; xy = A[:, :2]
        idx = clhs(np.column_stack([cov, xy]), n, seed=seed + c)          # spread over bands, NDVI and position
        for i in idx: points.append((c, A[i]))
    path = os.path.join(out_dir, "ppe_labels.gpkg"); _write_points(path, points, cols, R, cfg)
    hist = _buffer_histograms(R, points, out_dir)
    info = {"points_path": path, "plant_points": sum(1 for c, _ in points if c == PLANT), "soil_points": sum(1 for c, _ in points if c == SOIL),
            "clear_plant_pixels": count[PLANT], "clear_soil_pixels": count[SOIL], "pixel_cm": R.pixel_cm, "bands": bands,
            "mask_path": cfg["mask_path"], "vote_path": cfg["vote_path"], "findings_path": cfg.get("findings_path", ""), "area_path": area_path,
            "area_polygons": len(cfg.get("area_wkts") or []) or (1 if cfg.get("area_path") else 0),
            "image": {k: cfg[k] for k in ("source_mode", "stacked_path", "folder_path", "folder_bands", "bands", "ortho_type", "rgb_order", "hsi_min_wavelength", "hsi_max_wavelength", "hsi_interval", "hsi_grid_confirmed") if k in cfg},
            "max_buffer_cm": MAX_BUFFER_CM, "step_cm": STEP_CM, "buffer_pixels": hist, "seed": seed, "seconds": round(time.time() - t0, 1),
            "rule": "plant: in the mask, vote >= {:.1f}, no finding, {} px from the edge; soil: outside the mask, vote <= {:.1f}, no finding, {} px from the edge".format(
                LEVEL_SURE, EDGE_PX, 1 - LEVEL_SURE, EDGE_PX)}
    with open(os.path.join(out_dir, "ppe_labels_info.json"), "w", encoding="utf-8") as f: json.dump(info, f)
    progress(100, "Points ready")
    log("Created {} plant and {} soil points in {:.0f} s (cLHS over {} candidates).".format(info["plant_points"], info["soil_points"], info["seconds"],
                                                                                          sum(len(np.concatenate(v)) if v else 0 for v in cand.values())))
    return info


def run_and_create(cfg, progress=lambda p, m: None, log=lambda m: None, cancelled=lambda: False):
    """One click (4.6): run PPE on the image, finalize the mask at PPE's suggested threshold, then
    create the points. cfg: the image and output_dir (as for a run), n_points, plant_share, labels_dir,
    area_wkts / area_path."""
    def part(a, b):
        return lambda p, m="": progress(int(a + (b - a) * max(0, min(100, p)) / 100), m)
    e = NativePPEEngine(part(0, 55), log, lambda *a: None, cancelled)
    r = e.run(dict(cfg, action="run", work_dir=cfg.get("work_dir") or os.path.join(cfg["output_dir"], "ppe_work")))
    if cancelled(): raise RuntimeError("Cancelled.")
    threshold = float(r["threshold"]); c = dict(r.get("config") or {}); c.update(r); c.update(output_dir=cfg["output_dir"], threshold=threshold, action="apply")
    a = NativePPEEngine(part(55, 62), log, lambda *x: None, cancelled).apply(c)
    log("PPE mask finalized at its suggested threshold {:.2f} ({:.1f} % plant).".format(threshold, a.get("canopy_pct", 0)))
    out = cfg.get("labels_dir") or os.path.join(cfg["output_dir"], "ppe_labels")
    info = create_points(dict(cfg, mask_path=a["mask_path"], vote_path=r["vote_path"], findings_path=r.get("findings_path", ""), output_dir=out),
                         part(62, 100), log, cancelled)
    info.update(threshold=threshold, canopy_pct=a.get("canopy_pct"), vote_path=r["vote_path"], mask_path=a["mask_path"])
    return info


def _write_points(path, points, cols, R, cfg):
    if os.path.exists(path): gdal.GetDriverByName("GPKG").Delete(path)
    srs = osr.SpatialReference(); srs.ImportFromWkt(R.wkt)
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(path); L = ds.CreateLayer(LAYER, srs, ogr.wkbPoint)
    for name, t in (("id", ogr.OFTInteger), ("label", ogr.OFTString), ("ppe_label", ogr.OFTString), ("status", ogr.OFTString),
                    ("px", ogr.OFTInteger), ("py", ogr.OFTInteger)) + tuple((c, ogr.OFTReal) for c in cols[2:]):
        L.CreateField(ogr.FieldDefn(name, t))
    gt = R.gt; ds.StartTransaction()
    for i, (c, a) in enumerate(points, 1):
        f = ogr.Feature(L.GetLayerDefn()); x, y = int(a[0]), int(a[1])
        g = ogr.Geometry(ogr.wkbPoint); g.AddPoint_2D(gt[0] + (x + .5) * gt[1] + (y + .5) * gt[2], gt[3] + (x + .5) * gt[4] + (y + .5) * gt[5]); f.SetGeometry(g)
        f.SetField("id", i); f.SetField("label", NAMES[c]); f.SetField("ppe_label", NAMES[c]); f.SetField("status", STATUS_PROPOSED)
        f.SetField("px", x); f.SetField("py", y)
        for j, name in enumerate(cols[2:], 2): f.SetField(name, float(a[j]))
        L.CreateFeature(f)
    ds.CommitTransaction(); ds = None


# ---- buffers -------------------------------------------------------------------------------------
def _radius_px(R, cm): return cm / R.pixel_cm


def _disc(r_px):
    n = int(math.floor(r_px)); yy, xx = np.mgrid[-n:n + 1, -n:n + 1]; return yy, xx, np.hypot(yy, xx)


BUFFER_MODES = ("sure", "class")    # sure: clear pixels only (most accurate); class: every pixel of PPE's class (fuller)


def _geodesic(allowed, sy, sx, rmax):
    """Distance from (sy, sx) to every allowed pixel along paths through allowed pixels only
    (8 neighbours; 1 straight, sqrt 2 diagonal); inf where it cannot be reached within rmax."""
    d = np.full(allowed.shape, np.inf); d[sy, sx] = 0.0; h, w = allowed.shape
    steps = [(dy, dx, math.hypot(dy, dx)) for dy in (-1, 0, 1) for dx in (-1, 0, 1) if dy or dx]
    for _ in range(int(3 * rmax) + 4):
        p = np.pad(d, 1, constant_values=np.inf); new = d.copy()
        for dy, dx, c in steps:
            np.minimum(new, p[1 + dy:1 + dy + h, 1 + dx:1 + dx + w] + c, out=new)
        new[~allowed] = np.inf; new[sy, sx] = 0.0; new[new > rmax] = np.inf
        if np.array_equal(new, d): break
        d = new
    return d


def _same_class(R, x, y, label, r_px, mode="sure"):
    """The pixels a buffer may take in around a point, their distance to it and whether PPE agrees
    with the point's label at the point itself. 4.6.7: the distance is measured along paths through
    those pixels, so a buffer grows out from its point, stays in one piece and follows the leaf (or
    the soil strip) instead of picking up bits of other leaves inside the disc. mode 'sure': clear
    pixels only; 'class': every pixel of PPE's class, 1 px from the other class."""
    n = int(math.floor(r_px)) + 1; x0, y0 = max(0, x - n), max(0, y - n); x1, y1 = min(R.w, x + n + 1), min(R.h, y + n + 1)
    if mode == "class": m = R.class_mask(x0, y0, x1 - x0, y1 - y0, label)
    else:
        p, s = R.classes(x0, y0, x1 - x0, y1 - y0); m = p if label == PLANT else s
    agrees = bool(m[y - y0, x - x0])
    if agrees: d = _geodesic(m, y - y0, x - x0, r_px)
    else: d = np.full(m.shape, np.inf); d[y - y0, x - x0] = 0.0
    return m & np.isfinite(d), d, x0, y0, agrees


SOIL_OFFSET = 1000.0      # buffer distance raster: plant pixels hold their distance in cm, soil pixels 1000 + it


def buffer_raster_path(out_dir, mode):
    return os.path.join(out_dir, "ppe_label_buffer_distance_{}.tif".format(mode))


def _buffer_histograms(R, points, out_dir=None, agrees_only=None):
    """For the slider (4.6.8): per point and buffer mode, how many pixels the buffer takes in at each
    radius step, and (with out_dir) one raster per mode holding, for every pixel a buffer can reach
    within 50 cm, its distance to its point along the buffer's path (cm; soil + 1000; nearest point
    wins). The map shows the buffers of any radius by colouring this raster, so they follow the slider
    at once. points: [(label, (px, py, ...))]; agrees_only: per point False = keep its own pixel only."""
    r_max = _radius_px(R, MAX_BUFFER_CM); steps = int(round(MAX_BUFFER_CM / STEP_CM)) + 1; out = {}
    for mode in BUFFER_MODES:
        rows = []; rast = band = None
        if out_dir:
            path = buffer_raster_path(out_dir, mode)
            if os.path.exists(path): os.remove(path)
            rast = gdal.GetDriverByName("GTiff").Create(path, R.w, R.h, 1, gdal.GDT_Float32, options=["COMPRESS=DEFLATE", "TILED=YES", "SPARSE_OK=TRUE"])
            rast.SetGeoTransform(R.gt); rast.SetProjection(R.wkt); band = rast.GetRasterBand(1); band.SetNoDataValue(-1)
        for k, (c, a) in enumerate(points):
            x, y = int(a[0]), int(a[1])
            if agrees_only is not None and not agrees_only[k]:
                m = np.ones((1, 1), bool); d = np.zeros((1, 1)); x0, y0 = x, y
            else:
                m, d, x0, y0, _ag = _same_class(R, x, y, c, r_max, mode)
            dist_cm = d[m] * R.pixel_cm; rows.append(np.searchsorted(np.sort(dist_cm), np.arange(steps) * STEP_CM, side="right").tolist())
            if band is not None:
                h, w = m.shape; win = band.ReadAsArray(x0, y0, w, h)
                val = np.where(m, d * R.pixel_cm + (0.0 if c == PLANT else SOIL_OFFSET), -1.0)
                cur = np.where(win < 0, np.inf, win % SOIL_OFFSET); new = np.where(m, d * R.pixel_cm, np.inf)
                win = np.where(new < cur, val, win)          # the nearer point's buffer wins
                band.WriteArray(win.astype(np.float32), x0, y0)
        if rast is not None: rast.FlushCache(); rast = None
        out[mode] = rows
    return out


def rebuild_buffers(cfg, progress=lambda p, m: None, log=lambda m: None, cancelled=lambda: False):
    """After the user checked points (4.6.8): rebuild the buffer distance rasters and slider counts from
    the points as they are now. Points switched to the other class or added keep only their own pixel.
    cfg: points_path. Returns the updated info."""
    work_dir = os.path.dirname(cfg["points_path"]); info_path = os.path.join(work_dir, "ppe_labels_info.json")
    with open(info_path, encoding="utf-8") as f: info = json.load(f)
    R = _Rasters(info["mask_path"], info["vote_path"], info.get("findings_path", ""), info.get("area_path", ""))
    pts = read_points(cfg["points_path"]); inv = gdal.InvGeoTransform(R.gt); points = []; agrees = []
    for p in pts:
        x, y = p["px"], p["py"]
        if x is None or y is None: x, y = (int(v) for v in gdal.ApplyGeoTransform(inv, p["X"], p["Y"]))
        if not (0 <= x < R.w and 0 <= y < R.h): continue
        points.append((p["label"], (x, y))); agrees.append(p["status"] != STATUS_ADDED and p["label"] == (PLANT if p["ppe_label"] == "plant" else SOIL))
    progress(10, "Updating the buffers")
    info["buffer_pixels"] = _buffer_histograms(R, points, work_dir, agrees); info["buffer_ids"] = [p["id"] for p in pts]
    with open(info_path, "w", encoding="utf-8") as f: json.dump(info, f)
    progress(100, "Buffers updated")
    return dict(info, points_path=cfg["points_path"], rebuilt=True)


def read_points(path):
    """The points as they are now (after the user's checks): list of dicts."""
    ds = ogr.Open(path); L = ds.GetLayerByName(LAYER) or ds.GetLayer(0); out = []
    for f in L:
        lab = (f.GetField("label") or "").strip().lower()
        if lab not in ("plant", "soil"): continue
        g = f.GetGeometryRef()
        out.append({"id": f.GetField("id"), "label": PLANT if lab == "plant" else SOIL, "ppe_label": (f.GetField("ppe_label") or ""),
                    "status": f.GetField("status") or "", "px": f.GetField("px"), "py": f.GetField("py"), "X": g.GetX(), "Y": g.GetY()})
    return out


def buffer_preview(info, points, radius_cm, max_px=0, mode="sure"):
    """Labelled pixels for a radius, from the counts made with the points (instant, for the slider).
    Points the user changed or added count their own pixel only. Overlaps are counted twice here."""
    hist = info.get("buffer_pixels") or []
    if isinstance(hist, dict): hist = hist.get(mode) or hist.get("sure") or []
    ids = info.get("buffer_ids")
    if ids is not None:                                # rebuilt after checks: one row per current point, in this order
        step = int(round(min(radius_cm, MAX_BUFFER_CM) / STEP_CM)); tot = {PLANT: 0, SOIL: 0}; pos = {i: k for k, i in enumerate(ids)}
        for p in points:
            k = pos.get(p["id"]); n = hist[k][step] if k is not None and k < len(hist) else 1
            tot[p["label"]] += min(n, max_px) if max_px else n
        return {"plant": tot[PLANT], "soil": tot[SOIL]}
    step = int(round(min(radius_cm, MAX_BUFFER_CM) / STEP_CM)); tot = {PLANT: 0, SOIL: 0}
    for p in points:
        i = (p["id"] or 0) - 1
        if 0 <= i < len(hist) and p["label"] == (PLANT if p["ppe_label"] == "plant" else SOIL):
            n = hist[i][step] if radius_cm > 0 else 1
        else:
            n = 1
        tot[p["label"]] += min(n, max_px) if max_px else n
    return {"plant": tot[PLANT], "soil": tot[SOIL]}


def write_labels(cfg, progress=lambda p, m: None, log=lambda m: None, cancelled=lambda: False):
    """Grow each checked point into its buffer and write the labels. cfg: points_path, radius_cm,
    max_px (0 = no cap), and the info of create_points (mask, vote, findings, image).
    Writes next to the points: ppe_label_buffers.gpkg (one polygon per point), ppe_label_raster.tif
    (1 plant, 0 soil, 255 no label), ppe_label_pixels.csv (every labelled pixel with its band values).
    cfg preview=True (4.6.6, for the slider): only the buffer polygons, to ppe_label_buffers_preview.gpkg in
    the working folder, without bands, raster or table, so the map follows the slider in a moment."""
    t0 = time.time(); work_dir = os.path.dirname(cfg["points_path"]); preview = bool(cfg.get("preview"))
    with open(os.path.join(work_dir, "ppe_labels_info.json"), encoding="utf-8") as f: info = json.load(f)
    out_dir = (cfg.get("export_dir") if not preview else None) or work_dir; os.makedirs(out_dir, exist_ok=True)
    R = _Rasters(info["mask_path"], info["vote_path"], info.get("findings_path", ""), info.get("area_path", "")); pts = read_points(cfg["points_path"])
    if not pts: raise ValueError("No points with a plant or soil label in {}.".format(cfg["points_path"]))
    radius_cm = max(0.0, min(MAX_BUFFER_CM, float(cfg.get("radius_cm", 0)))); max_px = int(cfg.get("max_px", 0) or 0)
    mode = cfg.get("buffer_mode") if cfg.get("buffer_mode") in BUFFER_MODES else "sure"
    r_px = _radius_px(R, radius_cm); gt = R.gt
    img = dict(info.get("image") or {}); img.update({k: cfg[k] for k in ("source_mode", "stacked_path", "folder_path", "folder_bands", "bands") if k in cfg})
    e = None if preview else _engine(img); bands = [] if preview else [b for b in ("blue", "green", "red", "re", "nir") if b in e.sources]
    raster_path = os.path.join(out_dir, "ppe_label_raster.tif"); csv_path = os.path.join(out_dir, "ppe_label_pixels.csv") if not preview else os.devnull
    poly_path = os.path.join(out_dir, "ppe_label_buffers_preview.gpkg" if preview else "ppe_label_buffers.gpkg"); rb = None; rast = None
    if not preview:
        rast = gdal.GetDriverByName("GTiff").Create(raster_path, R.w, R.h, 1, gdal.GDT_Byte, options=["COMPRESS=DEFLATE", "TILED=YES", "SPARSE_OK=TRUE"])
        rast.SetGeoTransform(gt); rast.SetProjection(R.wkt); rb = rast.GetRasterBand(1); rb.SetNoDataValue(255); rb.Fill(255)
    if os.path.exists(poly_path): gdal.GetDriverByName("GPKG").Delete(poly_path)
    srs = osr.SpatialReference(); srs.ImportFromWkt(R.wkt)
    pds = ogr.GetDriverByName("GPKG").CreateDataSource(poly_path); PL = pds.CreateLayer("buffers", srs, ogr.wkbMultiPolygon)
    for name, t in (("id", ogr.OFTInteger), ("label", ogr.OFTString), ("pixels", ogr.OFTInteger), ("point_only", ogr.OFTInteger)):
        PL.CreateField(ogr.FieldDefn(name, t))
    taken = set(); totals = {PLANT: 0, SOIL: 0}; point_only = 0; pds.StartTransaction()
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        wcsv = csv.writer(fh); ndvi_col = "nir" in bands and "red" in bands
        wcsv.writerow(["point_id", "label", "label_value", "px", "py", "x", "y"] + bands + (["ndvi"] if ndvi_col else []))
        for k, p in enumerate(pts):
            if cancelled(): raise RuntimeError("Cancelled.")
            x, y = int(p["px"]) if p["px"] is not None else None, int(p["py"]) if p["py"] is not None else None
            if x is None or y is None:            # a point the user added: find its pixel
                inv = gdal.InvGeoTransform(gt); x, y = (int(v) for v in gdal.ApplyGeoTransform(inv, p["X"], p["Y"]))
            if not (0 <= x < R.w and 0 <= y < R.h): continue
            m, d, x0, y0, agrees = _same_class(R, x, y, p["label"], max(r_px, 0.5), mode)
            if agrees and radius_cm > 0:
                sel = m & (d <= r_px)
                if max_px and sel.sum() > max_px:
                    keep = np.argsort(np.where(sel, d, np.inf), axis=None)[:max_px]; sel = np.zeros_like(sel); sel.flat[keep] = True
            else:
                sel = np.zeros_like(m); sel[y - y0, x - x0] = True; point_only += int(not agrees)
            rr, cc = np.nonzero(sel); flat = (rr + y0) * R.w + (cc + x0)
            new = np.array([f not in taken for f in flat.tolist()], bool)
            rr, cc, flat = rr[new], cc[new], flat[new]; taken.update(flat.tolist())
            if rr.size == 0: continue
            if not preview:
                win = rb.ReadAsArray(x0, y0, sel.shape[1], sel.shape[0]); win[rr, cc] = p["label"]; rb.WriteArray(win, x0, y0)
                vals = _features(e, bands, x0, y0, sel.shape[1], sel.shape[0])
                gx = gt[0] + (cc + x0 + .5) * gt[1] + (rr + y0 + .5) * gt[2]; gy = gt[3] + (cc + x0 + .5) * gt[4] + (rr + y0 + .5) * gt[5]
                bv = {b: vals[b][rr, cc] for b in bands}; nd = _ndvi(bv) if ndvi_col else None
                wcsv.writerows(zip([p["id"]] * rr.size, [NAMES[p["label"]]] * rr.size, [p["label"]] * rr.size, (cc + x0).tolist(), (rr + y0).tolist(),
                                   np.round(gx, 4).tolist(), np.round(gy, 4).tolist(), *[np.round(bv[b], 6).tolist() for b in bands],
                                   *([np.round(nd, 4).tolist()] if nd is not None else [])))
            totals[p["label"]] += int(rr.size)
            PL.CreateFeature(_polygon(PL, sel, rr, cc, x0, y0, gt, R.wkt, p, int(rr.size), int(not agrees)))
            if k % 25 == 0: progress(int(100 * (k + 1) / len(pts)), "Growing buffers")
    pds.CommitTransaction(); pds = None
    if rast is not None: rast.FlushCache(); rast = None
    result = {"points": len(pts), "plant_pixels": totals[PLANT], "soil_pixels": totals[SOIL], "radius_cm": radius_cm, "max_px": max_px,
              "point_only": point_only, "buffer_mode": mode, "raster_path": raster_path, "csv_path": csv_path, "buffers_path": poly_path, "seconds": round(time.time() - t0, 1),
              "preview": preview}
    if preview:
        progress(100, "Buffers shown"); return result
    if os.path.abspath(out_dir) != os.path.abspath(work_dir):          # export: the checked points (and the area) go along
        for name, layer in (("ppe_labels.gpkg", LAYER), ("ppe_labels_area.gpkg", "area")):
            src = os.path.join(work_dir, name); dst = os.path.join(out_dir, name)
            if not os.path.isfile(src): continue
            if os.path.exists(dst): gdal.GetDriverByName("GPKG").Delete(dst)
            gdal.VectorTranslate(dst, src, format="GPKG", layers=[layer])   # reads through SQLite, so edits still in the journal come along
        with open(os.path.join(out_dir, "ppe_labels_info.json"), "w", encoding="utf-8") as f: json.dump(info, f)
    result["export_dir"] = out_dir
    with open(os.path.join(out_dir, "ppe_label_buffers_info.json"), "w", encoding="utf-8") as f: json.dump(result, f)
    log("Labels: {:,} plant and {:,} soil pixels from {} points (buffer {:.1f} cm){}.".format(
        totals[PLANT], totals[SOIL], len(pts), radius_cm, "; {} points where PPE disagrees with your label keep only their own pixel".format(point_only) if point_only else ""))
    progress(100, "Labels written"); return result


def _polygon(L, sel, rr, cc, x0, y0, gt, wkt, p, n, only):
    mem = gdal.GetDriverByName("MEM").Create("", sel.shape[1], sel.shape[0], 1, gdal.GDT_Byte)
    mem.SetGeoTransform((gt[0] + x0 * gt[1] + y0 * gt[2], gt[1], gt[2], gt[3] + x0 * gt[4] + y0 * gt[5], gt[4], gt[5])); mem.SetProjection(wkt)
    a = np.zeros(sel.shape, np.uint8); a[rr, cc] = 1; b = mem.GetRasterBand(1); b.WriteArray(a)
    vds = ogr.GetDriverByName("Memory").CreateDataSource(""); vl = vds.CreateLayer("p", None, ogr.wkbPolygon); vl.CreateField(ogr.FieldDefn("v", ogr.OFTInteger))
    gdal.Polygonize(b, b, vl, 0, [], callback=None)
    geom = ogr.Geometry(ogr.wkbMultiPolygon)
    for f in vl: geom.AddGeometry(f.GetGeometryRef().Clone())
    f = ogr.Feature(L.GetLayerDefn()); f.SetGeometry(geom)
    f.SetField("id", p["id"] or 0); f.SetField("label", NAMES[p["label"]]); f.SetField("pixels", n); f.SetField("point_only", only)
    return f
