"""Native NumPy/GDAL implementation of PPE's proven classic Method 1 workflow."""

import csv
import json
import math, threading
import os
import re
import time
from datetime import datetime

import numpy as np
from osgeo import gdal, ogr, osr

gdal.UseExceptions()

NODATA = -9999.0
CANONICAL = ("blue", "green", "red", "re", "nir")
SENSORS = {
    "micasense_rededge": CANONICAL,
    "dji_p4m": CANONICAL,
    "altum": ("blue", "green", "red", "nir", "re"),
}
HSI_WINDOWS = {
    "blue": (450.0, 520.0), "green": (520.0, 600.0),
    "red": (630.0, 690.0), "re": (700.0, 750.0), "nir": (780.0, 900.0),
}

# name: (required bands, polarity). Formulas are centralized in compute_vi().
VI_META = {
    "ndvi": (("nir", "red"), 1), "gndvi": (("nir", "green"), 1),
    "ndre": (("nir", "re"), 1), "savi": (("nir", "red"), 1),
    "osavi": (("nir", "red"), 1), "msavi": (("nir", "red"), 1),
    "evi": (("nir", "red", "blue"), 1), "evi2": (("nir", "red"), 1),
    "cigreen": (("nir", "green"), 1), "cire": (("nir", "re"), 1),
    "clre": (("nir", "re"), 1), "vari": (("red", "green", "blue"), 1),
    "exg": (("red", "green", "blue"), 1), "ngrdi": (("red", "green"), 1),
    "nirv": (("nir", "red"), 1), "rvi": (("nir", "red"), 1),
    "msr": (("nir", "red"), 1), "rdvi": (("nir", "red"), 1),
    "dvi": (("nir", "re"), 1),
    "psri": (("red", "green", "re"), -1), "tvi": (("nir", "red", "green"), 1),
    "mcari": (("re", "red", "green"), 1), "tcari": (("re", "red", "green"), 1),
    "gli": (("red", "green", "blue"), 1), "brightness": (("red", "green", "blue"), -1),
    "sr": (("nir", "red"), 1), "ndwi": (("green", "nir"), -1),
    "grvi": (("green", "red"), 1), "rgri": (("red", "green"), -1),
    "arvi": (("nir", "red", "blue"), 1),
    "ndrer": (("re", "red"), 1),
}

# The PPE framework (as first designed, restored in 3.7.0):
#   1. candidate layers are ranked by how widely their values are spread over the field;
#   2. the top layers are each split into soil and plant by four methods
#      (Otsu, CDF knee, k-means, multi-Otsu);
#   3. every layer x method result is one vote, and the vote fraction is thresholded.
#
# Three repairs keep step 1 honest.
#   * Spread is measured without units (see unit_free_spread), so an index that simply runs
#     on bigger numbers cannot win.
#   * Only normalised-difference indices are candidates. They have no units, so they keep
#     their value on any reflectance scale; difference indices (ExG, DVI, TVI, MCARI, TCARI,
#     NIRv, RDVI) and indices with a fixed constant (SAVI, OSAVI, MSAVI, EVI, EVI2) do not.
#     Simple ratios (SR/RVI, MSR, CIre, CIgreen, GRVI, RGRI) rank pixels in exactly the same
#     order as their normalised-difference twin but pile most pixels at the low end with a
#     long tail, and thresholds on that tail cut weak plants (on the Tifton test field Otsu
#     on RVI kept 39 % of the low-vigour plant pixels, Otsu on NDVI kept 100 %).
#   * Indices built from the same pair of bands carry the same information, so they are
#     grouped and only the highest-spread member of a group can be chosen. That replaces the
#     old correlation filter and guarantees the chosen layers look at different parts of the
#     spectrum.
# Every other index is still calculated and exported; it just never votes.
VI_GROUPS = {
    "nir_red": ("ndvi", "arvi"),
    "nir_red_edge": ("ndre",),
    "nir_green": ("gndvi",),
    "red_edge_red": ("ndrer",),
    "visible": ("gli", "ngrdi", "vari"),
}
VI_DOMAINS = VI_GROUPS          # older name, kept so existing scripts keep working
MAX_LAYERS = 3                  # the classic PPE design: the three highest-spread layers vote
METHOD_NAMES = ("otsu", "cdf", "kmeans", "multiotsu")
ALGAE_GUARD_SHARE = 0.25        # see _fit_algae_guard
WEAK_GREEN = 0.02               # colour guard: below this plants are barely greener than soil
NON_VOTING = frozenset(VI_META) - frozenset(name for members in VI_GROUPS.values() for name in members)


def _ratio(a, b):
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        return np.divide(a, b, out=np.full_like(a, np.nan, dtype=np.float32), where=np.abs(b) > 1e-12)


def compute_vi(name, b):
    n, r, g = b.get("nir"), b.get("red"), b.get("green")
    u, e = b.get("blue"), b.get("re")
    with np.errstate(invalid="ignore", over="ignore"):
        return {
            "ndvi": lambda: _ratio(n-r, n+r), "gndvi": lambda: _ratio(n-g, n+g),
            "ndre": lambda: _ratio(n-e, n+e), "savi": lambda: _ratio(1.5*(n-r), n+r+0.5),
            "osavi": lambda: _ratio(1.16*(n-r), n+r+0.16),
            "msavi": lambda: (2*n+1-np.sqrt(np.maximum(0, (2*n+1)**2-8*(n-r))))/2,
            "evi": lambda: 2.5*_ratio(n-r, n+6*r-7.5*u+1),
            "evi2": lambda: 2.5*_ratio(n-r, n+2.4*r+1),
            "cigreen": lambda: _ratio(n, g)-1, "cire": lambda: _ratio(n, e)-1,
            "clre": lambda: _ratio(n, e)-1, "vari": lambda: _ratio(g-r, g+r-u),
            "exg": lambda: 2*g-r-u, "ngrdi": lambda: _ratio(g-r, g+r),
            "nirv": lambda: n*_ratio(n-r, n+r), "rvi": lambda: _ratio(n, r),
            "msr": lambda: _ratio(_ratio(n, r)-1, np.sqrt(np.maximum(0, _ratio(n, r)+1))),
            "rdvi": lambda: _ratio(n-r, np.sqrt(np.maximum(0, n+r))), "dvi": lambda: n-e,
            "cvi": lambda: _ratio(n, e)*_ratio(r, g), "psri": lambda: _ratio(r-g, e),
            "tvi": lambda: .5*(120*(n-g)-200*(r-g)),
            "mcari": lambda: ((e-r)-.2*(e-g))*_ratio(e, r),
            "tcari": lambda: 3*((e-r)-.2*(e-g))*_ratio(e, r),
            "gli": lambda: _ratio(2*g-r-u, 2*g+r+u),
            "brightness": lambda: (r+g+u)/3, "sr": lambda: _ratio(n, r),
            "ndwi": lambda: _ratio(g-n, g+n), "grvi": lambda: _ratio(g, r),
            "rgri": lambda: _ratio(r, g), "arvi": lambda: _ratio(n-(2*r-u), n+(2*r-u)),
            "ndrer": lambda: _ratio(e-r, e+r),
        }[name]().astype(np.float32, copy=False)


THERMAL_WORDS = ("thermal", "lwir", "tir", "temp", "temperature", "celsius", "kelvin", "radiometric")


def is_thermal_name(name):
    """True for a thermal band or file (FLIR, Altum LWIR, H20T...). Thermal bands measure
    temperature, not reflectance, so PPE never uses them as a colour band."""
    tokens = re.split(r"[^a-z0-9]+", os.path.basename(str(name)).lower())
    return any(token in THERMAL_WORDS for token in tokens)


def infer_band(filename):
    """Which band a file or band name refers to: blue, green, red, re (red edge) or nir.

    Recognises plain words (blue, green, red, red edge, nir) as written by Pix4D, Agisoft and
    most pipelines, and the short names cameras use: Parrot Sequoia (GRE, RED, REG, NIR),
    DJI multispectral (MS_G, MS_R, MS_RE, MS_NIR), MicaSense (Blue, Green, Red, Red edge, NIR).
    A wavelength in the name (e.g. "band_717nm") is used when no word matches. Thermal bands
    and anything unrecognised return None, so they are left out.
    """
    base = os.path.splitext(os.path.basename(str(filename)))[0].lower()
    if not base or is_thermal_name(base):
        return None
    tokens = [t for t in re.split(r"[^a-z0-9]+", base) if t]
    joined = "_" + "_".join(tokens) + "_"
    # Every place a band word occurs, with where it ends. When a name holds several (the camera
    # "MicaSense RedEdge-P" in "..._RedEdge-P_..._blue.tif"), the one nearest the end is the band:
    # pipelines add the band last. Equal positions keep the order nir, re, green, blue, red.
    patterns = (
        ("nir", r"near_?infra_?red|_(?:nir|nir1|nir2|nearir)_|nir"),
        ("re", r"rededge|_red_edge_|_(?:re|reg|rde|edge)_"),
        ("green", r"_(?:green|grn|gre)_|green|_ms_g_"),
        ("blue", r"_(?:blue|blu)_|blue|_ms_b_"),
        ("red", r"_(?:red|rd)_|_ms_r_"),
    )
    found = []
    for rank, (slot, pattern) in enumerate(patterns):
        compiled = re.compile(pattern)
        # try every start position, so matches that overlap (the "_" between words) all count
        ends = [m.end() for m in (compiled.match(joined, i) for i in range(len(joined))) if m]
        if ends:
            found.append((max(ends), -rank, slot))
    if found:
        return max(found)[2]
    hit = re.search(r"(?<!\d)(\d{3,4})\s*nm", base)
    if hit:
        wl = float(hit.group(1))
        for slot, (lo, hi) in (("blue", (420, 510)), ("green", (510, 590)), ("red", (620, 690)), ("re", (690, 760)), ("nir", (760, 1000))):
            if lo <= wl < hi: return slot
    return None


# Band order for a 3-band file whose bands carry no names, colour tags or wavelengths.
THREE_BAND_ORDERS = {
    "rgb": ("red", "green", "blue"),
    "bgr": ("blue", "green", "red"),
    "nir_re_red": ("nir", "re", "red"),
    "nir_red_green": ("nir", "red", "green"),
}
BAND_LABELS = {"blue": "blue", "green": "green", "red": "red", "re": "red edge", "nir": "NIR"}


def scan_band_folder(folder):
    """Every GeoTIFF in a band folder, with the band PPE would read from it and why a file would
    not be used. Reads file names and metadata only (no pixels). The panel shows this list so the
    user can tick the files that really are bands.

    Returns {"files": [{"path", "file", "band", "use", "note", "size"}], "other_files": n} where
    band is blue/green/red/re/nir or None, use is PPE's proposal, size is (columns, rows).
    """
    files = []; other = 0; taken = {}; grid = None
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if not os.path.isfile(path) or name.startswith("."):
            continue
        if not name.lower().endswith((".tif", ".tiff")):
            other += 1; continue
        entry = {"path": path, "file": name, "band": None, "use": False, "note": "", "size": None}
        try:
            ds = gdal.Open(path)
        except Exception:
            ds = None
        if ds is None:
            entry["note"] = "cannot be opened as a raster"; files.append(entry); continue
        entry["size"] = (ds.RasterXSize, ds.RasterYSize)
        if is_thermal_name(name):
            entry["note"] = "thermal band: not used for plant and soil"; files.append(entry); continue
        if ds.RasterCount != 1:
            entry["note"] = "holds {} bands: a stacked file, not one band".format(ds.RasterCount); files.append(entry); continue
        band = infer_band(name); how = "from the file name"
        if band is None:
            band = next(iter(named_band_map(ds)), None); how = "from the band name inside the file"
        if band is None:
            wavelength = dataset_wavelengths(ds, {"ortho_type": "ms"})[0]
            if wavelength is not None:
                for slot, (lo, hi) in (("blue", (420, 510)), ("green", (510, 590)), ("red", (620, 690)), ("re", (690, 760)), ("nir", (760, 1000))):
                    if lo <= wavelength < hi: band = slot; how = "from its wavelength ({:g} nm)".format(wavelength)
        entry["band"] = band
        if band is None:
            entry["note"] = "band not recognised: choose it if this is a band file"
        elif band in taken:
            entry["note"] = "a second {} file ({} is already used)".format(BAND_LABELS.get(band, band), taken[band])
        elif grid is not None and entry["size"] != grid:
            entry["note"] = "{} x {} pixels, unlike the other band files".format(*entry["size"])
        else:
            entry["use"] = True; entry["note"] = "{} {}".format(BAND_LABELS.get(band, band), how)
            taken[band] = name; grid = grid or entry["size"]
        files.append(entry)
    return {"files": files, "other_files": other}


def describe_image(cfg):
    """What kind of image the chosen source is, from band names, colour tags, wavelengths and
    file names only (no pixels are read). Used by the panel to say what was found.

    Returns {"kind", "bands", "count", "named"}: kind is "rgb", "ms", "hsi", "limited",
    "unnamed3" (three bands with no names), "unnamed" or "none"; bands are the canonical
    band names found, in blue-to-NIR order.
    """
    mode = cfg.get("source_mode", "stacked"); found = []; count = 0; named = True
    try:
        if mode == "stacked":
            ds = gdal.Open(cfg.get("stacked_path", ""))
            if ds is None: return {"kind": "none", "bands": [], "count": 0, "named": False}
            count = ds.RasterCount
            if count > 15: return {"kind": "hsi", "bands": [], "count": count, "named": True}
            found = list(named_band_map(ds))
            if len(found) < min(3, count):
                waves = dataset_wavelengths(ds, {"ortho_type": "ms"})
                slots = []
                for w in waves:
                    if w is None: continue
                    for slot, (lo, hi) in (("blue", (420, 510)), ("green", (510, 590)), ("red", (620, 690)), ("re", (690, 760)), ("nir", (760, 1000))):
                        if lo <= w < hi and slot not in slots: slots.append(slot)
                if len(slots) > len(found): found = slots
            if len(found) < min(3, count):
                named = False
                if count == 3: return {"kind": "unnamed3", "bands": [], "count": 3, "named": False}
                if count == 5: found = list(CANONICAL)          # read in the standard blue..NIR order
                else: return {"kind": "unnamed", "bands": [], "count": count, "named": False}
        elif mode == "folder" and cfg.get("folder_bands"):
            found = [k for k, v in cfg["folder_bands"].items() if v]; count = len(found)
        elif mode == "folder":
            folder = cfg.get("folder_path", "")
            for name in sorted(os.listdir(folder)):
                if not name.lower().endswith((".tif", ".tiff")): continue
                band = infer_band(name)
                if band is None:
                    ds = gdal.Open(os.path.join(folder, name))
                    if ds is not None and ds.RasterCount == 1:
                        band = next(iter(named_band_map(ds)), None)
                if band and band not in found: found.append(band)
            count = len(found)
        else:
            found = [k for k, v in (cfg.get("bands") or {}).items() if v]; count = len(found)
    except Exception:
        return {"kind": "none", "bands": [], "count": count, "named": False}
    found = [b for b in CANONICAL if b in found]
    if not found: kind = "none"
    elif "nir" in found or "re" in found: kind = "ms"
    elif set(found) >= {"red", "green", "blue"}: kind = "rgb"
    else: kind = "limited"
    return {"kind": kind, "bands": found, "count": count, "named": named}


def _wavelength_number(value):
    hit = re.search(r"(?<!\d)(\d{3,4}(?:\.\d+)?)(?:\s*nm)?", str(value), re.I)
    if hit: return float(hit.group(1))
    hit = re.search(r"(?<!\d)(0?\.\d+)(?:\s*(?:um|µm))", str(value), re.I)
    return float(hit.group(1))*1000.0 if hit else None


def dataset_wavelengths(ds, cfg=None):
    """Read per-band wavelengths from descriptions/metadata or an explicit HSI grid."""
    cfg = cfg or {}
    values = [None]*ds.RasterCount
    for index in range(ds.RasterCount):
        band = ds.GetRasterBand(index+1); candidates = [band.GetDescription()]
        candidates.extend(value for key, value in band.GetMetadata().items() if "wavelength" in key.lower() or "band_name" in key.lower())
        for value in candidates:
            wavelength = _wavelength_number(value)
            if wavelength is not None: values[index] = wavelength; break
    for domain in ("ENVI", "", "IMAGERY"):
        metadata = ds.GetMetadata(domain) or {}
        for key, value in metadata.items():
            if "wavelength" not in key.lower(): continue
            parsed = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", str(value))]
            if len(parsed) == ds.RasterCount:
                if max(parsed) < 10: parsed = [x*1000 for x in parsed]
                values = [values[i] if values[i] is not None else parsed[i] for i in range(ds.RasterCount)]

    # Explicit HSI grid is ONLY processed for hyperspectral imagery (ortho_type == 'hsi' or >15 bands)
    kind = cfg.get("ortho_type", "auto")
    is_hsi = (kind == "hsi") or (kind == "auto" and ds.RasterCount > 15)
    if is_hsi:
        start = float(cfg.get("hsi_min_wavelength", 0) or 0)
        end = float(cfg.get("hsi_max_wavelength", 0) or 0)
        interval = float(cfg.get("hsi_interval", 0) or 0)
        if cfg.get("hsi_grid_confirmed"):
            if start < 300 or end <= start or interval <= 0:
                raise ValueError("The HSI wavelength grid requires valid minimum, maximum, and interval values.")
            calculated_end = start + (ds.RasterCount-1)*interval
            if abs(calculated_end-end) > max(.1, interval*.5):
                raise ValueError("The HSI wavelength grid does not match {} bands: its calculated last band is {:.3f} nm, not {:.3f} nm.".format(ds.RasterCount, calculated_end, end))
            return [start+i*interval for i in range(ds.RasterCount)]
        if sum(x is not None for x in values) < 3:
            if start >= 300 and interval > 0 and end > start:
                calculated_end = start+(ds.RasterCount-1)*interval
                if abs(calculated_end-end) <= max(.1, interval*.5):
                    values = [start+i*interval for i in range(ds.RasterCount)]
    return values


def named_band_map(ds):
    """Resolve canonical names from explicit GDAL colors and band descriptions."""
    colors = {gdal.GCI_RedBand: "red", gdal.GCI_GreenBand: "green", gdal.GCI_BlueBand: "blue"}
    found = {}
    for index in range(1, ds.RasterCount+1):
        band = ds.GetRasterBand(index); name = colors.get(band.GetColorInterpretation())
        if name is None: name = infer_band(band.GetDescription() or "")
        if name and name not in found: found[name] = index
    return found


def safe_gdal_open(path):
    """Open a raster dataset with actionable diagnostics for cloud-provider (OneDrive/iCloud) dehydration locks."""
    if not path or not os.path.exists(path):
        raise ValueError("Raster path does not exist: {}".format(path))
    try:
        with open(path, "rb") as handle:
            _ = handle.read(16)
    except (TimeoutError, OSError) as err:
        if getattr(err, "errno", None) == 60 or "timed out" in str(err).lower():
            fname = os.path.basename(path)
            parent = os.path.dirname(os.path.abspath(path))
            raise RuntimeError(
                "Cannot open '{}': Operation timed out.\n\n"
                "Root Cause: This file is an online-only cloud placeholder managed by OneDrive or iCloud Files On-Demand, "
                "and macOS timed out waiting for the file download.\n\n"
                "How to fix in 10 seconds:\n"
                "1. Open Finder and go to: {}\n"
                "2. Right-click on '{}' (or the folder).\n"
                "3. Click 'Always Keep on This Device' (wait until the solid checkmark finishes downloading).\n"
                "4. Re-run PPE.".format(fname, parent, fname)
            ) from None
        raise
    ds = gdal.Open(path)
    if ds is None:
        fname = os.path.basename(path)
        raise RuntimeError(
            "GDAL could not open '{}'. If this file is stored in OneDrive or iCloud, "
            "please right-click it in Finder and select 'Always Keep on This Device' to ensure all bytes are stored locally.".format(fname)
        )
    return ds


def otsu(x, bins=256):
    x = x[np.isfinite(x)]
    if x.size < 10: return np.nan
    lo, hi = float(x.min()), float(x.max())
    if hi-lo < 1e-12: return lo
    h, edges = np.histogram(x, bins=bins, range=(lo, hi))
    p = h.astype(float)/max(1, h.sum()); w1 = np.cumsum(p); w2 = 1-w1
    mid = (edges[:-1]+edges[1:])/2; mu = np.cumsum(p*mid); mt = np.sum(p*mid)
    score = (mt*w1-mu)**2/(w1*w2+1e-12)
    return float(mid[int(np.argmax(score))])


def unit_free_spread(x):
    """Spread of a layer without units: interquartile range as a share of the 1-99 % range.

    The original PPE ranked layers by their raw interquartile range. That favours any index
    that runs on large numbers (TVI is in the tens, NDVI below one). Dividing by the layer's
    own range removes the units: a layer whose values pile up at one end with a long tail
    scores low, a layer that spreads soil and plants well apart scores high (0 to 1).
    """
    x = x[np.isfinite(x)]
    if x.size < 20: return 0.0
    p1, p25, p75, p99 = np.percentile(x, (1, 25, 75, 99))
    return float((p75 - p25) / (p99 - p1)) if p99 - p1 > 1e-12 else 0.0


def cdf_threshold(x, rng=None, limit=None):
    """Knee of the cumulative distribution.

    Sort the values and draw them against their rank (both scaled 0 to 1). A layer with one
    dominant class makes a curve that hugs one side of the diagonal, and the point farthest
    from the diagonal is where that dominant class ends: the knee. That is the classic PPE
    CDF threshold.

    Two things made the classic version change its answer from run to run on the same image
    (on the IH test field the knee of one layer was 0.207 with one sample and 0.633 with
    another, moving canopy by 6 points):
      * it used the absolute minimum and maximum, so a few extreme pixels moved the diagonal;
        the lowest and highest 0.5 % are now left out;
      * when the curve crosses the diagonal there are two knees, one on each side, and it took
        whichever was a hair larger. When the smaller knee is at least half as strong as the
        larger, both are now returned as (low, high) and pixels between them get half a vote,
        the same way multi-Otsu treats its middle class.
    Returns a number, a (low, high) pair, or nan. The rng and limit arguments are ignored;
    they are kept so older scripts still run.
    """
    x = x[np.isfinite(x)]
    if x.size < 30: return np.nan
    lo, hi = np.percentile(x, (0.5, 99.5))
    x = np.sort(x[(x >= lo) & (x <= hi)]); span = float(x[-1] - x[0]) if x.size else 0.0
    if x.size < 30 or span <= 0: return np.nan
    rank = (np.arange(x.size) + 0.5) / x.size
    gap = (x - x[0]) / span - rank
    below, above = int(np.argmin(gap)), int(np.argmax(gap))
    d_below, d_above = -float(gap[below]), float(gap[above])
    weak, strong = min(d_below, d_above), max(d_below, d_above)
    if strong <= 0: return np.nan
    if weak >= 0.5 * strong and x[above] != x[below]:
        return float(min(x[above], x[below])), float(max(x[above], x[below]))
    return float(x[below] if d_below >= d_above else x[above])


def kmeans_threshold(x, split, k=10, limit=100000):
    """k-means threshold of the classic PPE: group the values into k clusters, call a cluster
    plant when its centre is above the Otsu split, and return the value halfway between the
    highest soil centre and the lowest plant centre.

    Starting the centres at evenly spaced quantiles makes the result repeatable (no random
    restarts) and one-dimensional sorting keeps it fast.
    """
    x = x[np.isfinite(x)]
    if x.size < 100 or not np.isfinite(split): return np.nan
    if x.size > limit: x = x[:: int(math.ceil(x.size / float(limit)))]
    x = np.sort(x.astype(np.float64)); k = max(2, min(k, x.size // 20))
    centers = np.quantile(x, (np.arange(k) + 0.5) / k); csum = np.concatenate(([0.0], np.cumsum(x)))
    for _ in range(100):
        cuts = np.concatenate(([0], np.searchsorted(x, (centers[:-1] + centers[1:]) / 2.0), [x.size]))
        counts = np.diff(cuts); sums = np.diff(csum[cuts])
        updated = np.where(counts > 0, sums / np.maximum(counts, 1), centers); updated.sort()
        if np.max(np.abs(updated - centers)) < 1e-9: break
        centers = updated
    plant = centers > split
    if plant.all() or not plant.any(): return float(split)
    return float((centers[~plant].max() + centers[plant].min()) / 2.0)


def half_mix_threshold(name, bands, x, share=0.5):
    """Optional fifth method: the index value of a pixel that is half plant and half soil.

    Otsu and k-means put the cut between the two groups of values as they find them. When
    one class fills most of the field the groups are lopsided and the cut is pulled toward
    the big class: in a dense crop it lands inside the canopy and half-covered pixels are
    called soil. This method ignores how many pixels each class has. It takes the purest
    plant pixels (upper half of the plant side of the Otsu split) and the purest soil pixels
    (lower half of the soil side), mixes their typical band values 50:50, and returns the
    index value of that mixed spectrum.

    It is not part of the original four-method PPE design, so it only votes when the user
    switches it on. On the two real test fields it moved canopy toward the unmixing reference
    on both (IH: 51 to 58 %, reference 65; Tifton: 41 to 39 %, reference 37).
    """
    good = np.isfinite(x)
    if good.sum() < 60: return np.nan
    t = otsu(x[good])
    if not np.isfinite(t): return np.nan
    hi = good & (x >= t); lo = good & (x < t)
    if hi.sum() < 15 or lo.sum() < 15: return np.nan
    hi_cut = float(np.median(x[hi])); lo_cut = float(np.median(x[lo]))
    top = hi & (x >= hi_cut); bottom = lo & (x <= lo_cut)
    mix = {}
    for key, values in bands.items():
        a = np.nanmedian(values[top]); b = np.nanmedian(values[bottom])
        if not (np.isfinite(a) and np.isfinite(b)): return np.nan
        mix[key] = np.array([share*a + (1.0-share)*b], dtype=np.float32)
    value = float(compute_vi(name, mix)[0])
    return value if np.isfinite(value) and min(lo_cut, hi_cut) <= value <= max(lo_cut, hi_cut) else np.nan


# NDVI (or, without NIR, the green-red difference NGRDI) below / above which a whole class
# cannot be vegetation / bare soil. Both are ratios, so the limits hold at any reflectance scale.
ONE_CLASS_LIMITS = {"ndvi": (0.30, 0.50), "ngrdi": (0.00, 0.10)}


def single_class_check(bands):
    """Warn when an image looks like all soil or all canopy.

    Every automatic threshold splits its input in two, even when only one class is there, so
    a bare field comes out about half 'plant'. This looks at the extremes of a referee index:
    if even the greenest 1 % of pixels look like soil the image is probably all soil, and if
    even the least green 1 % look like canopy it is probably all canopy.

    It only produces a warning. Versions 3.6.0 and 3.7.0 overrode the mask with it, using a
    looser rule, and turned a normal dense field (IH, 6 June 2023) into 100 % canopy.
    Returns "soil", "plant" or None, with the referee name and the two percentiles.
    """
    name = "ndvi" if all(k in bands for k in ("nir", "red")) else ("ngrdi" if all(k in bands for k in ("red", "green")) else None)
    if name is None: return None, None, None, None
    x = compute_vi(name, bands); x = x[np.isfinite(x)]
    if x.size < 60: return None, name, None, None
    low, high = (float(v) for v in np.percentile(x, (1, 99)))
    soil_max, plant_min = ONE_CLASS_LIMITS[name]
    if high < soil_max: return "soil", name, low, high
    if low > plant_min: return "plant", name, low, high
    return None, name, low, high


def multi_otsu(x, bins=256):
    x = x[np.isfinite(x)]
    if x.size < 80 or float(np.ptp(x)) < 1e-10: return None
    h, edges = np.histogram(x, bins=bins); p = h.astype(float)/h.sum()
    m = (edges[:-1]+edges[1:])/2; wp = np.cumsum(p); mp = np.cumsum(p*m); total = mp[-1]
    best = (-np.inf, 0, 0)
    for i in range(bins-2):
        w0 = wp[i]
        if w0 < .02: continue
        mu0 = mp[i]/w0
        js = np.arange(i+1, bins-1); w1 = wp[js]-wp[i]; w2 = 1-wp[js]
        good = (w1 >= .02) & (w2 >= .02)
        if not good.any(): continue
        mu1 = (mp[js]-mp[i])/np.maximum(w1, 1e-12); mu2 = (total-mp[js])/np.maximum(w2, 1e-12)
        vb = w0*(mu0-total)**2+w1*(mu1-total)**2+w2*(mu2-total)**2; vb[~good] = -np.inf
        q = int(np.argmax(vb))
        if vb[q] > best[0]: best = (float(vb[q]), i, int(js[q]))
    _, i, j = best; t1, t2 = float(edges[i+1]), float(edges[j+1])
    return None if best[0] <= 0 or t2-t1 < .01*np.ptp(x) else (t1, t2, max(t2-t1, float(np.std(x))))


def _smooth_grid2d(grid):
    """3 x 3 mean over the tile grid that ignores tiles with no value."""
    pad = np.pad(grid, 1, mode="constant", constant_values=np.nan)
    total = np.zeros(grid.shape, np.float64); count = np.zeros(grid.shape, np.float64)
    for dy in (0, 1, 2):
        for dx in (0, 1, 2):
            part = pad[dy:dy + grid.shape[0], dx:dx + grid.shape[1]]; good = np.isfinite(part)
            total += np.where(good, part, 0.0); count += good
    return np.where(count > 0, total / np.maximum(count, 1), 0.0).astype(np.float32)


SUGGEST_THRESHOLDS = np.round(np.arange(0.15, 0.951, 0.05), 2)   # candidate slider values
SUGGEST_MIN_PURE = 30        # pure plant / pure soil sample pixels needed for a tile's own spectra
SUGGEST_MAX_RESIDUAL = 0.25  # pixels a plant-soil mix explains this poorly are left out (algae, shade, objects)


def suggest_threshold(votes, bands, valid, grid_h, grid_w):
    """Suggested vote threshold: the slider value whose mask best matches "at least half of the
    pixel is plant".

    How much of each sample pixel is plant is estimated by mixing two spectra: the plant spectrum
    (median of pixels where every vote says plant) and the soil spectrum (median of pixels where no
    vote says plant). Both are taken per tile, so a pale plot is judged against its own pale plants,
    and fall back to the whole-field spectra where a tile has too few pure pixels. Pixels that are
    neither plant nor soil (a plant-soil mix explains them poorly) are left out. The suggestion is
    the threshold whose mask overlaps best (IoU) with the pixels that are at least half plant; the
    good range is every threshold within one IoU point of it.

    votes: 2-D vote fractions on the sample grid; bands: dict of 2-D reflectance arrays on the same
    grid; valid: 2-D bool. Returns a dict, or None when the image has too few pure pixels.
    """
    names = [k for k in CANONICAL if k in bands] or list(bands)
    X = np.stack([np.asarray(bands[k], dtype=np.float64) for k in names])
    v = np.asarray(votes, dtype=np.float64)
    ok = valid & np.isfinite(v) & np.isfinite(X).all(0)
    if ok.sum() < 500:
        return None
    pure_p = ok & (v >= 0.99); pure_s = ok & (v <= 0.01)
    if pure_p.sum() < SUGGEST_MIN_PURE or pure_s.sum() < SUGGEST_MIN_PURE:
        return None
    all_p = np.median(X[:, pure_p], 1); all_s = np.median(X[:, pure_s], 1)
    rows, cols = v.shape
    ys = np.linspace(0, rows, grid_h + 1).astype(int); xs = np.linspace(0, cols, grid_w + 1).astype(int)
    frac = np.full(v.shape, np.nan)
    for j in range(grid_h):
        for i in range(grid_w):
            sl = (slice(ys[j], ys[j + 1]), slice(xs[i], xs[i + 1]))
            o = ok[sl]
            if not o.any():
                continue
            T = X[(slice(None),) + sl]
            p = np.median(T[:, pure_p[sl]], 1) if pure_p[sl].sum() >= SUGGEST_MIN_PURE else all_p
            q = np.median(T[:, pure_s[sl]], 1) if pure_s[sl].sum() >= SUGGEST_MIN_PURE else all_s
            d = p - q; dd = float(d @ d)
            if not dd > 0:
                p, q = all_p, all_s; d = p - q; dd = float(d @ d)
                if not dd > 0:
                    continue
            f = np.clip(np.tensordot(d, T - q[:, None, None], 1) / dd, 0, 1)
            fit = q[:, None, None] + f[None] * d[:, None, None]
            resid = np.sqrt(((T - fit) ** 2).sum(0)) / np.sqrt(dd)
            frac[sl] = np.where(o & (resid <= SUGGEST_MAX_RESIDUAL), f, np.nan)
    use = np.isfinite(frac)
    if use.sum() < 500:
        return None
    half = frac[use] >= 0.5; vv = v[use]
    iou = np.array([((vv >= t - 0.05) & half).sum() / max(1, ((vv >= t - 0.05) | half).sum()) for t in SUGGEST_THRESHOLDS])
    best = int(np.argmax(iou)); good = SUGGEST_THRESHOLDS[iou >= iou[best] - 0.01]
    return {"threshold": float(SUGGEST_THRESHOLDS[best]), "low": float(good.min()), "high": float(good.max()),
            "agreement": float(100 * iou[best]), "half_plant_cover": float(100 * half.mean()),
            "pixels_used": float(use.sum() / ok.sum())}


def _bilinear_interp_block(grid, y, h, width, grid_h, grid_w, height, x_cache=None):
    """Bilinear interpolation from the tile grid to a block of full-resolution rows."""
    if x_cache is None:
        step_x = float(width) / max(1, grid_w)
        x_idx = np.clip((np.arange(width, dtype=np.float32) + 0.5) / step_x - 0.5, 0, grid_w - 1)
        x0 = np.floor(x_idx).astype(int)
        x1 = np.minimum(x0 + 1, grid_w - 1)
        fx = (x_idx - x0)[None, :]
    else:
        x0, x1, fx = x_cache
    step_y = float(height) / max(1, grid_h)
    y_idx = np.clip((np.arange(y, y + h, dtype=np.float32) + 0.5) / step_y - 0.5, 0, grid_h - 1)
    y0 = np.floor(y_idx).astype(int)
    y1 = np.minimum(y0 + 1, grid_h - 1)
    fy = (y_idx - y0)[:, None]
    top = (1.0 - fx) * grid[y0, :][:, x0] + fx * grid[y0, :][:, x1]
    bot = (1.0 - fx) * grid[y1, :][:, x0] + fx * grid[y1, :][:, x1]
    return ((1.0 - fy) * top + fy * bot).astype(np.float32)


# Pale plant: tassels, flowers, seed heads, pods and yellowing leaves. They reflect plant-level NIR
# (far more than soil) and have a plant-level NDVI, but lack a green leaf's deep red dip, so they
# get fewer plant votes and drop out of the mask as the threshold rises. PPE only reports them:
# the vote and the mask are never changed. Tested on Kritis (corn tassels 7.8 percent of the dense
# corn, soybean 0.01 percent, bare soil 0).
PALE_NDVI = (0.5, 0.82)          # plant-level, but below a green leaf's NDVI
PALE_GREEN_RED = 1.5             # a green leaf reflects about twice as much green as red
PALE_NIR_OVER_SOIL = 1.4         # NIR well above this image's bare soil...
PALE_NIR_OF_LEAF = 0.5           # ...and at least half of this image's green leaves


def fit_pale_plant(sample):
    """Reference levels for pale plant parts, measured on this image's own soil and leaves.
    Needs NIR, red and green; returns None when the image has no clear green leaves to compare with."""
    if not all(k in sample for k in ("nir", "red", "green")):
        return None
    nir, red, green = (np.asarray(sample[k], np.float64) for k in ("nir", "red", "green"))
    ok = np.isfinite(nir) & np.isfinite(red) & np.isfinite(green) & (nir > 0) & (red > 0)
    nir, red, green = nir[ok], red[ok], green[ok]
    if nir.size < 500:
        return None
    ndvi = (nir - red) / (nir + red)
    leaf = (ndvi >= 0.8) & (green / red >= 2.0)
    if leaf.sum() < 200:
        return None
    leaf_nir = float(np.median(nir[leaf]))
    soil = ndvi < 0.25
    soil_nir = float(np.median(nir[soil])) if soil.sum() >= 200 else None
    floor = PALE_NIR_OF_LEAF * leaf_nir
    if soil_nir is not None:
        floor = max(floor, PALE_NIR_OVER_SOIL * soil_nir)
    return {"nir_floor": floor, "leaf_nir": leaf_nir, "soil_nir": soil_nir,
            "ndvi_low": PALE_NDVI[0], "ndvi_high": PALE_NDVI[1], "green_red_max": PALE_GREEN_RED}


PALE_SOIL_NDVI = 0.60            # a pixel this low is bare soil or a leaf-soil mix
PALE_CLEAR_OF_SOIL_M = 0.15      # pale parts sit inside the canopy: no soil within 15 cm


def _near(mask, r):
    """True where a True pixel of `mask` lies within r pixels (a square window), via an integral image."""
    if r <= 0:
        return mask.copy()
    m = np.pad(mask.astype(np.int32), r)
    c = np.pad(m.cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    k = 2 * r + 1; h, w = mask.shape
    return (c[k:k + h, k:k + w] - c[0:h, k:k + w] - c[k:k + h, 0:w] + c[0:h, 0:w]) > 0


def pale_plant_mask(bands, fit):
    nir, red, green = bands["nir"], bands["red"], bands["green"]
    with np.errstate(divide="ignore", invalid="ignore"):
        ndvi = (nir - red) / (nir + red)
        green_red = green / red
    return ((nir > fit["nir_floor"]) & (ndvi > fit["ndvi_low"]) & (ndvi < fit["ndvi_high"])
            & (green_red < fit["green_red_max"]))


def display_stretch(channel_names, values):
    """Low and high display value for each of the three channels shown on the map.

    True colour (red, green, blue): ONE stretch shared by the three bands, from zero reflectance to
    the 99.5th percentile of all three together. That keeps the real colour balance: stretching
    each band on its own (what PPE did before) turned brown soil grey-white and leaves cyan-green.
    False colour and single-band pictures keep a stretch per band (0.2 and 99 percentiles), the
    usual way to show them. `values` maps band name -> array of sampled values."""
    names = list(channel_names)
    clean = {n: np.asarray(values[n], np.float64)[np.isfinite(values[n]) & (np.asarray(values[n]) > 0)] for n in set(names)}
    if names == ["red", "green", "blue"] and all(clean[n].size for n in names):
        hi = float(np.percentile(np.concatenate([clean[n] for n in names]), 99.5))
        if np.isfinite(hi) and hi > 0:
            return [(0.0, hi)] * 3
    out = []
    for n in names:
        v = clean[n]
        lo, hi = (float(x) for x in np.percentile(v, (0.2, 99.0))) if v.size else (0.0, 1.0)
        if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo: lo, hi = 0.0, 1.0
        out.append((lo, hi))
    return out


# "What PPE found" (4.1): four kinds of pixel worth pointing out, decided by the image's own
# references (its green leaves and its sunlit bare soil) and by where the pixel sits. They never change
# the vote; the user may leave any of them out of the mask, and put them back.
FINDING_NAMES = {1: "plant parts that are not green", 2: "leaf edges over soil", 3: "shaded leaves", 4: "unclear"}
FINDING_COLUMNS = {1: "plant_parts_pct", 2: "leaf_edges_pct", 3: "shaded_leaves_pct", 4: "unclear_pct"}
FINDING_CLEAR_CANOPY = 0.90      # plant parts sit in closed canopy: at least 90 percent plant within 0.5 m
FINDING_CANOPY_M = 0.5
FINDING_PLANT_NDVI = 0.6         # below this a neighbour is soil or a leaf-soil mix
FINDING_CANOPY_NDVI = 0.7        # "plant" for the closed-canopy check (0.6 let V6 leaf edges through: IH 0.47 -> 0.07 percent)
FINDING_MIN_CONTRAST = 1.3       # leaves must reflect at least 1.3 x the NIR of sunlit soil, or nothing can be judged
FINDING_DARK_SHARE = 0.5         # dark = less than half as bright as this image's typical green leaf
                                 # (a fixed share, not a percentile: a percentile would call some pixels dark in every image)


def fit_findings(sample, pixel_m=None):
    """References for the findings, from the fitting sample (valid pixels only).
    Returns None when the image lacks NIR and red. Each part may be missing:
    `pale` (no clear green leaves or no green band) and `dark_threshold`/`soil_nir` (no RGB or no bare soil)."""
    if not all(k in sample for k in ("nir", "red")):
        return None
    nir = np.asarray(sample["nir"], np.float64); red = np.asarray(sample["red"], np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        ndvi = (nir - red) / (nir + red)
    fit = {"pale": fit_pale_plant(sample), "soil_nir": None, "dark_threshold": None}
    if all(k in sample for k in ("green", "blue")):
        bright = (red + np.asarray(sample["green"], np.float64) + np.asarray(sample["blue"], np.float64)) / 3.0
        soil = np.isfinite(ndvi) & (ndvi < 0.25)
        if soil.sum() >= 200:
            sunlit = soil & (bright >= np.median(bright[soil]))      # the brighter half of bare soil: soil in sun
            fit["soil_nir"] = float(np.median(nir[sunlit]))
        plantish = np.isfinite(ndvi) & (ndvi >= FINDING_PLANT_NDVI)
        if plantish.sum() >= 200:
            fit["dark_threshold"] = float(FINDING_DARK_SHARE * np.median(bright[plantish]))
    # Without a clear NIR difference between leaves and bare soil (an uncalibrated or rescaled band:
    # Tifton's "NIR" is 0.066 on leaves and 0.055 on soil) none of these checks means anything.
    leaf_nir = (fit["pale"] or {}).get("leaf_nir")
    soil_ref = fit["soil_nir"] or (fit["pale"] or {}).get("soil_nir")
    if not leaf_nir or (soil_ref and leaf_nir < FINDING_MIN_CONTRAST * soil_ref):
        return {"skipped": "leaves reflect {} NIR and sunlit soil {}: too little difference to tell these apart".format(
            "{:.3f}".format(leaf_nir) if leaf_nir else "no clear", "{:.3f}".format(soil_ref) if soil_ref else "unknown")}
    step = pixel_m or 0.02
    fit["soil_radius_px"] = int(min(20, max(2, round(PALE_CLEAR_OF_SOIL_M / step))))
    fit["canopy_radius_px"] = int(min(60, max(3, round(FINDING_CANOPY_M / step))))
    return fit


def classify_findings(bands, ok, fit, ndvi_halo, offset):
    """Codes for one block: 0 nothing to point out, 1 plant parts that are not green (tassels,
    flowers, heads), 2 leaf edges over soil, 3 shaded leaves, 4 unclear (dark, could be soil in shade).
    `ndvi_halo` is the NDVI of the block plus `offset` rows above and enough rows below for the
    neighbourhood checks."""
    h = next(iter(bands.values())).shape[0]
    code = np.zeros(ok.shape, np.uint8)
    nir, red = bands["nir"], bands["red"]
    with np.errstate(divide="ignore", invalid="ignore"):
        ndvi = (nir - red) / (nir + red)
    pale = np.zeros(ok.shape, bool)
    if fit.get("pale") and "green" in bands:
        pale = pale_plant_mask(bands, fit["pale"]) & ok
        soilish = np.isfinite(ndvi_halo) & (ndvi_halo < FINDING_PLANT_NDVI)
        near_soil = _near(soilish, fit["soil_radius_px"])[offset:offset + h]
        r = fit["canopy_radius_px"]
        plantish = (np.isfinite(ndvi_halo) & (ndvi_halo >= FINDING_CANOPY_NDVI)).astype(np.float64)
        m = np.pad(plantish, r, mode="edge"); c = np.pad(m.cumsum(0).cumsum(1), ((1, 0), (1, 0))); k = 2 * r + 1; hh, ww = plantish.shape
        cover = ((c[k:k + hh, k:k + ww] - c[0:hh, k:k + ww] - c[k:k + hh, 0:ww] + c[0:hh, 0:ww]) / float(k * k))[offset:offset + h]
        parts = pale & ~near_soil & (cover >= FINDING_CLEAR_CANOPY)
        code[parts] = 1
        code[pale & ~parts] = 2
    if fit.get("dark_threshold") is not None and fit.get("soil_nir") is not None and all(b in bands for b in ("green", "blue")):
        bright = (red + bands["green"] + bands["blue"]) / 3.0
        dark = ok & ~pale & (bright < fit["dark_threshold"]) & np.isfinite(ndvi) & (ndvi >= 0.3)
        shaded = dark & (nir > fit["soil_nir"]) & (ndvi >= 0.5)        # more NIR than soil in full sun: a leaf
        code[shaded] = 3
        code[dark & ~shaded] = 4
    code[~ok] = 255
    return code


def findings_histograms(findings_path, vote_path, levels=1200):
    """For each finding code, how many pixels have each vote value (votes rounded to 1/1200):
    from these the share of the plant in every category follows for any threshold."""
    f_ds = safe_gdal_open(findings_path); v_ds = safe_gdal_open(vote_path)
    width, height = v_ds.RasterXSize, v_ds.RasterYSize
    hist = np.zeros((5, levels + 1), np.int64)
    for y in range(0, height, 512):
        h = min(512, height - y)
        v = v_ds.GetRasterBand(1).ReadAsArray(0, y, width, h); f = f_ds.GetRasterBand(1).ReadAsArray(0, y, width, h)
        valid = (v != NODATA) & np.isfinite(v) & (v >= 0) & (v <= 1) & (f != 255)
        q = np.rint(v[valid] * levels).astype(np.intp); code = np.clip(f[valid].astype(np.intp), 0, 4)
        hist += np.bincount(code * (levels + 1) + q, minlength=5 * (levels + 1)).reshape(5, levels + 1)
    return hist.tolist()


def finding_shares(hist, threshold, levels=1200):
    """Percent of the plant (vote >= threshold - 0.05) in each finding code 1-4."""
    hist = np.asarray(hist); start = int(np.ceil(max(0.0, threshold - 0.05) * levels - 1e-9))
    plant = hist[:, start:].sum(axis=1); total = plant.sum()
    return {k: (100.0 * plant[k] / total if total else 0.0) for k in range(1, 5)}


def physical_memory_bytes():
    """Total RAM of the computer (macOS, Linux, Windows); 4 GB if it cannot be read."""
    try:
        if hasattr(os, "sysconf") and "SC_PAGE_SIZE" in os.sysconf_names and "SC_PHYS_PAGES" in os.sysconf_names:
            value = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
            if value > 0: return float(value)
        if os.name == "nt":
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                            ("available", ctypes.c_ulonglong), ("page_total", ctypes.c_ulonglong),
                            ("page_available", ctypes.c_ulonglong), ("virtual_total", ctypes.c_ulonglong),
                            ("virtual_available", ctypes.c_ulonglong), ("extended", ctypes.c_ulonglong)]
            status = MemoryStatus(); status.length = ctypes.sizeof(MemoryStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)): return float(status.total)
    except Exception:
        pass
    return 4.0 * 1024 ** 3


def _interp_columns(grid, width, grid_w):
    """First half of _bilinear_interp_block, done once per run: every grid row interpolated to full width."""
    step_x = float(width) / max(1, grid_w)
    x_idx = np.clip((np.arange(width, dtype=np.float32) + 0.5) / step_x - 0.5, 0, grid_w - 1)
    x0 = np.floor(x_idx).astype(int); x1 = np.minimum(x0 + 1, grid_w - 1); fx = (x_idx - x0)[None, :]
    return (1.0 - fx) * grid[:, x0] + fx * grid[:, x1]


def _interp_rows(columns, y, h, grid_h, height):
    """Second half: the same result as _bilinear_interp_block for rows y .. y+h."""
    step_y = float(height) / max(1, grid_h)
    y_idx = np.clip((np.arange(y, y + h, dtype=np.float32) + 0.5) / step_y - 0.5, 0, grid_h - 1)
    y0 = np.floor(y_idx).astype(int); y1 = np.minimum(y0 + 1, grid_h - 1); fy = (y_idx - y0)[:, None]
    return ((1.0 - fy) * columns[y0] + fy * columns[y1]).astype(np.float32)


def local_offsets(layer, valid, split, low, high, grid_h, grid_w, min_pixels=80):
    """How far the soil-plant split of each part of the field sits from the whole-field split.

    Plots differ: where plants are pale or small, both soil and plants sit lower on the index
    than in a vigorous plot, and thresholds fitted to the whole field cut through the pale
    plants. For each tile this fits Otsu to the tile's own pixels and returns the difference
    from the whole-field Otsu split. The engine subtracts that difference from the layer, so
    all four thresholds move up or down together.

    Safeguards: a tile is only refitted when it holds both clear soil (values below `low`)
    and something else, each at least 5 % of its pixels; a tile of bare soil or closed canopy
    keeps the field's thresholds (offset 0), because a local split would cut that single
    class in two. The local split may only move inside the band low..high (the 'mixed' class
    of the whole-field multi-Otsu), so soil-level values can never become plant. Tiles with
    too few pixels take the value of their neighbours.
    """
    rows, cols = layer.shape; out = np.full((grid_h, grid_w), np.nan, np.float32)
    ys = np.linspace(0, rows, grid_h + 1).astype(int); xs = np.linspace(0, cols, grid_w + 1).astype(int)
    for gy in range(grid_h):
        for gx in range(grid_w):
            block = layer[ys[gy]:ys[gy + 1], xs[gx]:xs[gx + 1]]
            values = block[valid[ys[gy]:ys[gy + 1], xs[gx]:xs[gx + 1]]]; values = values[np.isfinite(values)]
            if values.size < min_pixels: continue
            clear_soil = float((values < low).mean())
            if clear_soil < 0.05 or clear_soil > 0.95:
                out[gy, gx] = 0.0; continue
            local = otsu(values)
            out[gy, gx] = (min(max(local, low), high) - split) if np.isfinite(local) else 0.0
    return _smooth_grid2d(out)


def display_channels(bands):
    """What to show on the map for an image with these bands: (description, three band names).

    True colour when red, green and blue are all there. If one of them is missing but NIR,
    red and green exist, the standard false-colour picture (NIR shown as red, red as green,
    green as blue), in which plants are red and soil is grey-blue. Otherwise one band in
    grey, taking the band in which plants stand out most from soil: NIR, then red edge,
    then whatever there is.
    """
    bands = list(bands)
    if all(name in bands for name in ("red", "green", "blue")):
        return "true colour", ("red", "green", "blue")
    if all(name in bands for name in ("nir", "red", "green")):
        return "false colour (NIR, red, green)", ("nir", "red", "green")
    single = next((name for name in ("nir", "re", "red", "green", "blue") if name in bands), bands[0])
    label = {"nir": "NIR", "re": "red edge"}.get(single, single)
    return "{} band in grey".format(label), (single, single, single)


# ---- plot boundaries ------------------------------------------------------------------------
# Any polygon file GDAL reads: Shapefile (pick the .shp or any of its side files), a zipped
# Shapefile or GeoPackage, GeoPackage, GeoJSON, KML/KMZ, GML, FlatGeobuf, SpatiaLite, MapInfo.
PLOT_FILE_FILTER = ("Plot boundaries (*.shp *.zip *.gpkg *.geojson *.json *.kml *.kmz *.gml *.fgb *.sqlite *.tab "
                    "*.dbf *.shx *.prj *.cpg);;All files (*)")
SHAPEFILE_SIDE_FILES = (".dbf", ".shx", ".prj", ".cpg", ".sbn", ".sbx", ".qix", ".qmd", ".shp.xml")
VECTOR_IN_ARCHIVE = (".shp", ".gpkg", ".geojson", ".json", ".kml", ".gml", ".fgb", ".sqlite", ".tab")
POLYGON_TYPES = {ogr.wkbPolygon, ogr.wkbMultiPolygon, ogr.wkbCurvePolygon, ogr.wkbMultiSurface}
STAT_NAMES = ("pixel_count", "mean", "median", "sd", "cv_pct", "min", "p10", "q25", "q75", "p90", "max")


def _is_polygon_type(geom_type):
    return ogr.GT_Flatten(geom_type) in POLYGON_TYPES


def _plot_datasource_path(path):
    """The path GDAL should open for a plot file the user picked, and a note when it was changed."""
    path = os.path.abspath(os.path.expanduser(path or ""))
    lower = path.lower()
    for side in SHAPEFILE_SIDE_FILES:
        if lower.endswith(side):
            shp = path[: -len(side)] + ".shp"
            if not os.path.isfile(shp):
                shp = path[: -len(side)] + ".SHP"
            if os.path.isfile(shp):
                return shp, "using {} (the Shapefile that {} belongs to)".format(os.path.basename(shp), os.path.basename(path))
            raise ValueError("{} is part of a Shapefile, but {} is not beside it. A Shapefile needs its .shp, .shx and .dbf "
                             "files in the same folder.".format(os.path.basename(path), os.path.basename(path[: -len(side)] + ".shp")))
    if lower.endswith(".zip"):
        root = "/vsizip/" + path
        listed = gdal.ReadDirRecursive(root) or []
        inside = [name for name in listed if name.lower().endswith(VECTOR_IN_ARCHIVE) and "__macosx" not in name.lower()
                  and not os.path.basename(name).startswith("._")]
        if not inside:
            raise ValueError("No plot file found inside {} (looked for .shp, .gpkg, .geojson, .kml and similar).".format(os.path.basename(path)))
        inside.sort(key=lambda name: (VECTOR_IN_ARCHIVE.index(os.path.splitext(name.lower())[1]) if os.path.splitext(name.lower())[1] in VECTOR_IN_ARCHIVE else 99, name.count("/"), name))
        note = "using {} inside the zip file".format(inside[0])
        if len(inside) > 1:
            note += " ({} other vector files ignored)".format(len(inside) - 1)
        return root + "/" + inside[0], note
    return path, ""


def open_plot_file(path, layer_name=None):
    """Open a plot boundary file and pick its polygon layer.

    Returns (datasource, layer, info). info has: source (what GDAL opened), uri (for QGIS), layer,
    layers (all polygon layers), features (polygon features), fields, crs_wkt, crs_name, note.
    Raises ValueError with a plain message when the file cannot be used.
    """
    if not path:
        raise ValueError("Choose a plot boundary file.")
    if not (os.path.exists(path) or path.startswith("/vsi")):
        raise ValueError("Plot file not found: {}".format(path))
    source, note = _plot_datasource_path(path)
    datasource = ogr.Open(source)
    if datasource is None:
        raise ValueError("GDAL cannot read {} as a vector file.".format(os.path.basename(path)))
    polygon_layers = []
    for index in range(datasource.GetLayerCount()):
        layer = datasource.GetLayer(index)
        count = 0
        layer.ResetReading()
        for feature in layer:
            geometry = feature.GetGeometryRef()
            if geometry is not None and not geometry.IsEmpty() and _is_polygon_type(geometry.GetGeometryType()):
                count += 1
        layer.ResetReading()
        if count:
            polygon_layers.append((layer.GetName(), count))
    if not polygon_layers:
        kinds = sorted({ogr.GeometryTypeToName(datasource.GetLayer(i).GetGeomType()) for i in range(datasource.GetLayerCount())})
        raise ValueError("{} has no polygons (found: {}). Plot statistics need plot polygons, not points or lines."
                         .format(os.path.basename(path), ", ".join(kinds) or "no features"))
    names = [name for name, _ in polygon_layers]
    chosen = layer_name if layer_name in names else names[0]
    layer = datasource.GetLayerByName(chosen)
    definition = layer.GetLayerDefn()
    fields = [definition.GetFieldDefn(i).GetName() for i in range(definition.GetFieldCount())]
    srs = layer.GetSpatialRef()
    crs_name = ""
    if srs is not None:
        code = srs.GetAuthorityCode(None)
        crs_name = "{}{}".format(srs.GetName() or "", " (EPSG:{})".format(code) if code else "")
    uri = source if datasource.GetLayerCount() == 1 else "{}|layername={}".format(source, chosen)
    info = {"source": source, "uri": uri, "layer": chosen, "layers": names, "features": dict(polygon_layers)[chosen],
            "fields": fields, "crs_wkt": srs.ExportToWkt() if srs is not None else "", "crs_name": crs_name, "note": note}
    return datasource, layer, info


def guess_plot_id_field(fields):
    """The attribute most likely to hold plot IDs (plot_id, plot, id, name ...), or ''."""
    lower = {name.lower(): name for name in fields}
    for wanted in ("plot_id", "plotid", "plot", "plot_no", "plot_num", "plotnumber", "plot_name", "id", "name", "label", "fid_plot"):
        if wanted in lower:
            return lower[wanted]
    for name in fields:
        if "plot" in name.lower():
            return name
    return ""


def _plot_transform(layer, raster_wkt):
    """Coordinate transformation from the plot layer to the image, or None when they already match."""
    srs = layer.GetSpatialRef()
    if srs is None or not raster_wkt:
        return None
    target = osr.SpatialReference(); target.ImportFromWkt(raster_wkt)
    source = srs.Clone()
    if hasattr(source, "SetAxisMappingStrategy"):
        source.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER); target.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return None if source.IsSame(target) else osr.CoordinateTransformation(source, target)


def _plot_geometry(feature, transform):
    """A feature's polygon, flat (2-D), straight-edged and in image coordinates; None if it is not a polygon."""
    geometry = feature.GetGeometryRef()
    if geometry is None or geometry.IsEmpty() or not _is_polygon_type(geometry.GetGeometryType()):
        return None
    geometry = geometry.Clone()
    if geometry.HasCurveGeometry():
        geometry = geometry.GetLinearGeometry()
    geometry.FlattenTo2D()
    if transform is not None:
        geometry.Transform(transform)
    return geometry


def plots_over_image(path, image_path, layer_name=None):
    """How many plot polygons overlap the image: (overlapping, total, note)."""
    image = safe_gdal_open(image_path)
    datasource, layer, info = open_plot_file(path, layer_name)
    if image is None:
        return None, info["features"], "the image could not be read"
    gt = image.GetGeoTransform(); w, h = image.RasterXSize, image.RasterYSize
    xs = [gt[0], gt[0] + w * gt[1] + h * gt[2]]; ys = [gt[3], gt[3] + w * gt[4] + h * gt[5]]
    box = (min(xs), max(xs), min(ys), max(ys))
    raster_wkt = image.GetProjection()
    note = ""
    if layer.GetSpatialRef() is None:
        note = "the plot file has no coordinate system (for a Shapefile: no .prj file), so it is assumed to match the image"
    transform = _plot_transform(layer, raster_wkt)
    over = 0
    layer.ResetReading()
    for feature in layer:
        try:
            geometry = _plot_geometry(feature, transform)
        except Exception:
            geometry = None
        if geometry is None:
            continue
        e = geometry.GetEnvelope()
        if e[0] < box[1] and e[1] > box[0] and e[2] < box[3] and e[3] > box[2]:
            over += 1
    return over, info["features"], note


def _pixel_area_m2(dataset):
    """Area of one pixel in square metres, or None when the image is not in a projected (metric) CRS."""
    wkt = dataset.GetProjection()
    if not wkt:
        return None
    srs = osr.SpatialReference(); srs.ImportFromWkt(wkt)
    if not srs.IsProjected():
        return None
    gt = dataset.GetGeoTransform()
    return abs(gt[1] * gt[5] - gt[2] * gt[4]) * srs.GetLinearUnits() ** 2


class Cancelled(RuntimeError): pass


class NativePPEEngine:
    def __init__(self, progress=None, log=None, import_ready=None, cancelled=None):
        self.progress_cb = progress or (lambda p, m: None)
        self.log_cb = log or (lambda m: None)
        self.import_cb = import_ready or (lambda r: None)
        self.cancelled = cancelled or (lambda: False)
        self.sources = {}
        self.template = None
        self.hsi_proxy_info = []
        logical_cpus = max(1, os.cpu_count() or 1)
        self.thread_count = max(1, min(16, logical_cpus-1 if logical_cpus > 2 else logical_cpus))

    def progress(self, percent, message):
        if self.cancelled(): raise Cancelled("PPE processing was cancelled.")
        self.progress_cb(int(percent), message); self.log_cb(message)

    def _resolve(self, cfg, sample_n=500):
        """Find the bands, then read the fitting sample once."""
        self._map_bands(cfg)
        self._read_sample(sample_n)

    def _map_bands(self, cfg):
        """Work out which file and band holds blue, green, red, red edge and NIR. Reads no pixels
        except for hyperspectral cubes, where candidate bands are compared on a small sample."""
        self.thread_count = max(1, min(16, int(cfg.get("threads", self.thread_count) or self.thread_count)))
        self.log_cb("Automatic processing threads: {} logical workers.".format(self.thread_count))
        mode = cfg["source_mode"]
        detection = "user band assignments"
        if mode == "bands": paths = dict(cfg["bands"])
        elif mode == "folder" and cfg.get("folder_bands"):
            paths = {band: path for band, path in cfg["folder_bands"].items() if path}
            detection = "band files ticked in the folder"
            self.log_cb("Band files ticked by the user: {}".format(", ".join("{}={}".format(b, os.path.basename(p)) for b, p in paths.items())))
        elif mode == "folder":
            files = [os.path.join(cfg["folder_path"], p) for p in sorted(os.listdir(cfg["folder_path"])) if p.lower().endswith((".tif", ".tiff"))]
            paths = {}; ignored = []
            for path in files:
                name = infer_band(path)
                if name is None:
                    try:
                        ds = safe_gdal_open(path)
                    except Exception:
                        ds = None
                    if ds is not None and ds.RasterCount == 1:
                        explicit = named_band_map(ds); name = next(iter(explicit), None)
                        if name is None:
                            wavelength = dataset_wavelengths(ds, cfg)[0]
                            name = self._slot_for_wavelength(wavelength) if wavelength is not None else None
                if name is None: ignored.append(os.path.basename(path)); continue
                if name in paths: raise ValueError("Folder contains more than one candidate for the {} band: {} and {}".format(name, os.path.basename(paths[name]), os.path.basename(path)))
                paths[name] = path
            detection = "TIFF filename and embedded metadata"
            if ignored: self.log_cb("Ignored unrelated/unrecognized TIFF files: {}".format(", ".join(ignored)))
        else:
            path = cfg["stacked_path"]; ds = safe_gdal_open(path)
            if ds is None: raise ValueError("QGIS/GDAL could not open the stacked raster.")
            kind = cfg.get("ortho_type", "auto")
            is_hsi = kind == "hsi" or (kind == "auto" and ds.RasterCount > 15)
            explicit = named_band_map(ds)
            if is_hsi:
                wavelengths = dataset_wavelengths(ds, cfg)
                if sum(x is not None for x in wavelengths) != ds.RasterCount:
                    raise ValueError("Hyperspectral bands have incomplete wavelength metadata. Re-select the cube and provide minimum, maximum, and interval values.")
                mapping = self._wavelength_proxy_map(ds, wavelengths)
                paths = {name: (path, index) for name, index in mapping.items()}; detection = "correlation-aware HSI spectral proxies"
            elif len(explicit) >= min(3, ds.RasterCount):
                paths = {name: (path, index) for name, index in explicit.items()}; detection = "embedded color interpretation/band descriptions"
            elif kind == "rgb" or (kind == "auto" and ds.RasterCount == 3):
                key = cfg.get("rgb_order", "rgb"); order = THREE_BAND_ORDERS.get(key, THREE_BAND_ORDERS["rgb"])
                paths = {name: (path, i+1) for i, name in enumerate(order) if i < ds.RasterCount}
                detection = "unnamed 3-band file read as {} (band order chosen in the panel)".format(", ".join(BAND_LABELS[n] for n in order))
            elif kind == "ms" and ds.RasterCount == 5:
                order = CANONICAL
                paths = {name: (path, i+1) for i, name in enumerate(order)}; detection = "standard blue/green/red/red-edge/NIR order"
            else:
                wavelengths = dataset_wavelengths(ds, cfg)
                if sum(x is not None for x in wavelengths) >= 3:
                    mapping = self._wavelength_proxy_map(ds, wavelengths)
                    paths = {name: (path, index) for name, index in mapping.items()}; detection = "embedded/configured wavelengths"
                elif ds.RasterCount == 5:
                    order = CANONICAL
                    paths = {name: (path, i+1) for i, name in enumerate(order)}; detection = "standard blue/green/red/red-edge/NIR order"
                elif ds.RasterCount > 15:
                    raise ValueError("Hyperspectral bands have no usable wavelengths. Re-select the cube and supply minimum, maximum, and interval.")
                else:
                    raise ValueError("This unnamed {}-band stack has no color, name, or wavelength metadata, so its bands cannot be identified safely. Use named individual/folder TIFFs or add band descriptions; automatic guessing could produce a wrong mask.".format(ds.RasterCount))
        if len(paths) < 1: raise ValueError("No recognizable raster bands were found.")
        self.sources = {k: (v if isinstance(v, tuple) else (v, 1)) for k, v in paths.items() if k in CANONICAL}
        first = next(iter(self.sources.values())); self.template = safe_gdal_open(first[0]); self.width = self.template.RasterXSize; self.height = self.template.RasterYSize
        for name, (path, band) in self.sources.items():
            ds = safe_gdal_open(path)
            if ds is None or band > ds.RasterCount: raise ValueError("Could not open {} band: {}".format(name, path))
            if ds.RasterXSize != self.width or ds.RasterYSize != self.height: raise ValueError("Band rasters must have matching rows and columns. Align them in QGIS first.")
            if not np.allclose(ds.GetGeoTransform(), self.template.GetGeoTransform(), rtol=0, atol=1e-9) or ds.GetProjection() != self.template.GetProjection():
                raise ValueError("Band rasters must share the same CRS, extent, and pixel grid. Align them in QGIS first.")
        self.datasets = {k: safe_gdal_open(v[0]) for k, v in self.sources.items()}
        self.detection_method = detection
        self.log_cb("Band detection: {}.".format(detection))
        self.log_cb("Resolved mapping: {}".format(", ".join("{}=band {} ({})".format(name, band, os.path.basename(path)) for name, (path, band) in self.sources.items())))

    def _read_sample(self, sample_n):
        self.scale = 1.0
        # Every thinned read decompresses the whole file, so the fitting sample is read here
        # once and reused for scale detection, the display stretch and threshold fitting.
        values = self._sample_bands(sample_n)
        finite = [x[np.isfinite(x)] for x in values.values() if np.any(np.isfinite(x))]
        if not finite: raise ValueError("The selected imagery contains no readable numeric pixels.")
        mx = float(np.nanpercentile(np.concatenate(finite), 99))
        if mx > 10000:
            self.scale = 65535.0 if mx > 20000 else max(10000.0, float(mx / 0.95))
            self.log_cb("High/uncalibrated DN imagery detected (99th percentile {:.1f}); scaled by {:.1f}.".format(mx, self.scale))
        elif mx > 255:
            self.scale = 10000.0
        elif mx > 2:
            self.scale = 255.0
        else:
            self.scale = 1.0
        self._sample_cache = {k: v / self.scale for k, v in values.items()}

    def preview(self, cfg):
        """First step of the workflow: identify the bands and say how to show the image, so the
        user sees it on the map as soon as it is chosen. Nothing is written except, for separate
        band files, small VRT files that QGIS needs to draw them together.

        Returns the same keys the run reports for its display, plus `display_stretch`: the
        low and high value for each of the three display channels, in the units of the file
        (see display_stretch: one shared stretch for true colour; the same rule the run uses later).
        """
        self._map_bands(cfg)
        bands = list(self.sources)
        mode, channels = display_channels(bands)
        # The stretch comes from a grid of small full-resolution windows. A thinned read of
        # the whole image would have to decompress every tile of a compressed file and can take
        # many seconds; these windows touch only a few tiles, so the image appears at once.
        # A 10 x 10 grid of 32-pixel windows: spread widely enough to catch bare ground at the field
        # edges (6 x 6 larger windows missed it on Kritis and drew the image too bright and green).
        size = int(max(8, min(32, self.width // 10, self.height // 10))); cache = {}
        xs = np.linspace(0, self.width - size, 10).astype(int); ys = np.linspace(0, self.height - size, 10).astype(int)
        for name in channels:
            if name not in cache:
                path, band = self.sources[name]; rb = self.datasets[name].GetRasterBand(band); nodata = rb.GetNoDataValue(); parts = []
                for y0 in ys:
                    for x0 in xs:
                        block = rb.ReadAsArray(int(x0), int(y0), size, size)
                        if block is None: continue
                        block = block.astype(np.float32)
                        if nodata is not None and np.isfinite(nodata): block[block == nodata] = np.nan
                        parts.append(block[np.isfinite(block) & (block > 0)])
                cache[name] = np.concatenate(parts) if parts else np.array([], np.float32)
        # the same stretch rule as the display image the run writes (one stretch for true colour)
        stretch = display_stretch(channels, cache) if all(cache[n].size >= 50 for n in cache) else None   # else QGIS works out its own
        preview_dir = os.path.join(cfg.get("work_dir") or os.path.dirname(self.sources[channels[0]][0]), "ppe_preview")
        os.makedirs(preview_dir, exist_ok=True)
        available = [name for name, meta in VI_META.items() if all(x in bands for x in meta[0])]
        self.log_cb("Shown on the map as {}.".format(mode))
        return {"action": "preview", "rgb_path": "", "display_mode": mode, "display_stretch": stretch,
                "rgb_sources": [self.sources[name][0] for name in channels], "rgb_source_bands": [self.sources[name][1] for name in channels],
                "rgb_bands": list(dict.fromkeys(channels)), "true_rgb": mode == "true colour", "source_bands": bands,
                "available_vis": available, "band_detection": self.detection_method, "hsi_proxies": self.hsi_proxy_info,
                "run_dir": preview_dir, "width": self.width, "height": self.height,
                "preview_key": cfg.get("preview_key", "")}

    @staticmethod
    def _slot_for_wavelength(wavelength):
        matches = [(name, abs(wavelength-(lo+hi)/2)) for name, (lo, hi) in HSI_WINDOWS.items() if lo <= wavelength <= hi]
        return min(matches, key=lambda item: item[1])[0] if matches else None

    def _wavelength_proxy_map(self, ds, wavelengths):
        """Choose high-spread, mutually decorrelated bands in PPE's five MS windows."""
        mapping, selected_samples, info = {}, [], []
        ratio = ds.RasterXSize/max(1, ds.RasterYSize); bx=max(1,min(ds.RasterXSize,int(math.sqrt(8000*ratio)))); by=max(1,min(ds.RasterYSize,int(8000/bx)))
        candidates_by_window = {}
        for name, (lo, hi) in HSI_WINDOWS.items():
            candidates = [i for i, wavelength in enumerate(wavelengths, 1) if wavelength is not None and lo <= wavelength <= hi]
            if not candidates:
                if name in ("blue", "re"):
                    self.log_cb("HSI sensor range does not cover the {} window ({:.0f}–{:.0f} nm); continuing with available spectral windows.".format(name, lo, hi))
                    continue
                raise ValueError("The HSI wavelength range does not contain a {} proxy band ({:.0f}–{:.0f} nm).".format(name, lo, hi))
            scored = []
            for index in candidates:
                values = ds.GetRasterBand(index).ReadAsArray(buf_xsize=bx, buf_ysize=by).astype(float); nodata=ds.GetRasterBand(index).GetNoDataValue()
                if nodata is not None: values[values == nodata] = np.nan
                spread = np.nanpercentile(values, 75)-np.nanpercentile(values, 25)
                scored.append({"spread": float(spread), "center": abs(wavelengths[index-1]-(lo+hi)/2), "index": index, "values": values.ravel()})
            candidates_by_window[name] = sorted(scored, key=lambda x: (-x["spread"], x["center"]))
        for name in CANONICAL:
            if name not in candidates_by_window:
                continue
            ranked = candidates_by_window[name]
            for candidate in ranked:
                correlations = []
                for previous in selected_samples:
                    valid = np.isfinite(candidate["values"]) & np.isfinite(previous)
                    if valid.sum() < 20 or np.nanstd(candidate["values"][valid]) == 0 or np.nanstd(previous[valid]) == 0:
                        correlations.append(1.0)
                    else:
                        correlations.append(abs(float(np.corrcoef(candidate["values"][valid], previous[valid])[0, 1])))
                candidate["max_correlation"] = max(correlations) if correlations else 0.0
            acceptable = [candidate for candidate in ranked if candidate["max_correlation"] <= .85]
            chosen = acceptable[0] if acceptable else min(ranked, key=lambda x: (x["max_correlation"], -x["spread"], x["center"]))
            mapping[name] = chosen["index"]; selected_samples.append(chosen["values"])
            entry = {"name": name, "band": chosen["index"], "wavelength": float(wavelengths[chosen["index"]-1]), "iqr": chosen["spread"], "max_correlation": chosen["max_correlation"], "decorrelated": bool(acceptable)}
            info.append(entry)
            self.log_cb("HSI proxy {}: band {} at {:.2f} nm · IQR {:.4g} · max |r| {:.3f}{}.".format(name, entry["band"], entry["wavelength"], entry["iqr"], entry["max_correlation"], "" if acceptable else " (least-correlated fallback)"))
        self.hsi_proxy_info = info
        return mapping

    def _read_band(self, name, x=0, y=0, w=None, h=None, bx=None, by=None):
        w, h = w or self.width, h or self.height
        # A GDAL dataset must not be read from two threads at once: worker threads open their own.
        local = getattr(self, "_thread_local", None)
        if local is not None and threading.current_thread() is not threading.main_thread():
            if not hasattr(local, "datasets"): local.datasets = {}
            if name not in local.datasets: local.datasets[name] = safe_gdal_open(self.sources[name][0])
            ds = local.datasets[name]
        else:
            ds = self.datasets[name]
        band = ds.GetRasterBand(self.sources[name][1])
        a = band.ReadAsArray(x, y, w, h, buf_xsize=bx, buf_ysize=by).astype(np.float32)
        nd = band.GetNoDataValue()
        if nd is not None: a[a == nd] = np.nan
        return a/self.scale

    def _sample_bands(self, n):
        ratio = self.width/max(1, self.height); bx = min(self.width, max(1, int(math.sqrt(n*ratio)))); by = min(self.height, max(1, int(n/bx)))
        if len(self.sources) < 2 or self.thread_count < 2:
            return {name: self._read_band(name, bx=bx, by=by) for name in self.sources}
        # Each band is decompressed in its own thread (with its own GDAL dataset).
        from concurrent.futures import ThreadPoolExecutor
        self._thread_local = threading.local()
        try:
            with ThreadPoolExecutor(max_workers=min(self.thread_count, len(self.sources))) as pool:
                futures = {name: pool.submit(self._read_band, name, 0, 0, None, None, bx, by) for name in self.sources}
                return {name: f.result() for name, f in futures.items()}
        finally:
            self._thread_local = None

    def _valid(self, bands):
        valid = np.logical_and.reduce([np.isfinite(v) for v in bands.values()])
        ref = bands.get("nir", bands.get("red", next(iter(bands.values())))); valid &= (ref >= .0001) & (ref <= 1.5)
        alt = bands.get("red") if "nir" in bands else bands.get("green")
        if alt is not None: valid &= ~((ref > .85) & (np.abs(ref-alt) < .03))
        return valid

    def _create_output(self, path, dtype, nodata):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        opts = ["TILED=YES", "COMPRESS=DEFLATE", "BIGTIFF=IF_SAFER", "NUM_THREADS={}".format(self.thread_count)]
        out = gdal.GetDriverByName("GTiff").Create(path, self.width, self.height, 1, dtype, options=opts)
        out.SetGeoTransform(self.template.GetGeoTransform()); out.SetProjection(self.template.GetProjection()); out.GetRasterBand(1).SetNoDataValue(nodata)
        return out

    @staticmethod
    def _display_stretches(channel_names, sampled):
        """Display stretch of each channel from the fitting sample (see display_stretch)."""
        return display_stretch(channel_names, sampled)

    def _block_workers(self, block_rows, n_blocks):
        """How many row blocks to process at once: no more than the CPU threads PPE may use, and
        few enough that the blocks in memory stay within a quarter of the computer's RAM, so a
        small laptop runs safely (just with fewer workers) and QGIS keeps room to draw."""
        per_block = float(block_rows) * self.width * 4.0 * (len(self.sources) + 12)   # bands + votes, indices, guards (measured ~10)
        budget = 0.25 * physical_memory_bytes()
        by_memory = int(budget // max(per_block, 1.0)) - 1        # one more block waits in the queue
        return max(1, min(self.thread_count, n_blocks, by_memory, 8))

    def _display_writer(self, path, channel_names, sampled):
        """The display image of _write_rgb_display, written block by block during the vote pass,
        so the bands are read once for both instead of twice."""
        opts = ["TILED=YES", "COMPRESS=DEFLATE", "BIGTIFF=IF_SAFER", "NUM_THREADS={}".format(self.thread_count)]
        out = gdal.GetDriverByName("GTiff").Create(path, self.width, self.height, 3, gdal.GDT_Byte, options=opts)
        out.SetGeoTransform(self.template.GetGeoTransform()); out.SetProjection(self.template.GetProjection())
        stretches = self._display_stretches(channel_names, sampled)
        for index in range(1, 4):
            band = out.GetRasterBand(index); band.SetNoDataValue(0)
            band.SetColorInterpretation((gdal.GCI_RedBand, gdal.GCI_GreenBand, gdal.GCI_BlueBand)[index-1])
        class Writer:
            def convert(self, raw):
                planes = []
                for name, (lo, hi) in zip(channel_names, stretches):
                    values = raw[name]; valid = np.isfinite(values) & (values > 0)
                    scaled = (values - lo) * 254.0 / max(hi - lo, 1e-6) + 1.0
                    planes.append(np.where(valid, np.clip(scaled, 1, 255), 0).astype(np.uint8))
                return planes
            def write(self, planes, y):
                for index, plane in enumerate(planes, 1): out.GetRasterBand(index).WriteArray(plane, 0, y)
            def close(self):
                nonlocal out
                out.FlushCache(); out = None
        return Writer()

    def _write_rgb_display(self, path, channel_names, sampled):
        """Write a reliable, full-resolution, display-ready RGB GeoTIFF."""
        opts = ["TILED=YES", "COMPRESS=DEFLATE", "BIGTIFF=IF_SAFER", "NUM_THREADS={}".format(self.thread_count)]
        out = gdal.GetDriverByName("GTiff").Create(path, self.width, self.height, 3, gdal.GDT_Byte, options=opts)
        out.SetGeoTransform(self.template.GetGeoTransform()); out.SetProjection(self.template.GetProjection())
        stretches = []
        for name in channel_names:
            values = sampled[name]; values = values[np.isfinite(values) & (values > 0)]
            lo, hi = np.percentile(values, (0.2, 99.0)) if values.size else (0, 1)
            if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo: lo, hi = 0, 1
            stretches.append((float(lo), float(hi)))
        for y in range(0, self.height, 512):
            h = min(512, self.height-y)
            cache = {name: self._read_band(name, y=y, h=h) for name in set(channel_names)}
            for index, (name, stretch) in enumerate(zip(channel_names, stretches), 1):
                lo, hi = stretch; values = cache[name]
                # Scale valid pixels to [1, 255] to prevent dark green leaves from being crushed to 0 (pitch black)
                # and cleanly reserve byte 0 for NoData / transparent collar.
                valid = np.isfinite(values) & (values > 0)
                scaled = (values - lo) * 254.0 / max(hi - lo, 1e-6) + 1.0
                byte = np.where(valid, np.clip(scaled, 1, 255), 0).astype(np.uint8)
                band = out.GetRasterBand(index); band.WriteArray(byte, 0, y)
                band.SetNoDataValue(0)
                band.SetColorInterpretation((gdal.GCI_RedBand, gdal.GCI_GreenBand, gdal.GCI_BlueBand)[index-1])
        out.FlushCache(); out = None
        return path

    def run(self, cfg):
        started = time.time(); stamp = datetime.now().strftime("%Y%m%d_%H%M%S"); run_dir = os.path.join(cfg["work_dir"], "ppe_"+stamp); os.makedirs(run_dir, exist_ok=True)
        self.progress(3, "Validating and opening imagery"); self._resolve(cfg, sample_n=max(4000, int(cfg.get("sample_size", 100000))*3))
        bands = list(self.sources); available = [name for name, meta in VI_META.items() if all(x in bands for x in meta[0])]
        self.log_cb("Detected bands: {}".format(", ".join(bands)))
        self.log_cb("Available vegetation indices ({}): {}".format(len(available), ", ".join(available)))
        display_mode, channels = display_channels(bands); rgb = display_mode == "true colour"
        rgb_sources = [self.sources[x][0] for x in channels]; rgb_source_bands = [self.sources[x][1] for x in channels]
        self.progress(7, "Preparing the display image ({})".format(display_mode))
        display_sample = self._sample_cache; display_path = rgb_path = os.path.join(run_dir, "ppe_rgb_display.tif")
        imported = {"rgb_path": rgb_path, "rgb_sources": rgb_sources, "rgb_source_bands": rgb_source_bands, "rgb_bands": list(dict.fromkeys(channels)), "true_rgb": rgb, "display_mode": display_mode, "source_bands": bands, "available_vis": available, "band_detection": self.detection_method, "hsi_proxies": self.hsi_proxy_info, "run_dir": run_dir}
        # The display file is written during the vote pass; until then QGIS draws the source bands
        # with the same stretch (in the units of the files).
        early_stretch = [(lo * self.scale, hi * self.scale) for lo, hi in self._display_stretches(channels, display_sample)]
        self.import_cb(dict(imported, rgb_path="", display_stretch=early_stretch)); self.progress(12, "Sampling the full field and calculating possible vegetation indices")
        sample_2d = self._sample_cache
        valid_2d = self._valid(sample_2d)
        guard = self._fit_bright_guard(sample_2d, valid_2d) if cfg.get("bright_guard", False) else None
        if cfg.get("bright_guard", False) and guard is None:
            self.log_cb("Bright-soil safeguard skipped: RGB and a supported vegetation index are required.")
        shadow_guard = self._fit_shadow_guard(sample_2d, valid_2d) if cfg.get("shadow_guard", True) else None
        sample = {k: v[valid_2d] for k, v in sample_2d.items()}
        algae_guard = None
        if cfg.get("biofilm_guard", True):
            algae_guard = self._fit_algae_guard(sample)
        area_m2 = _pixel_area_m2(self.template)
        finding_fit = fit_findings(sample, math.sqrt(area_m2) if area_m2 else None)
        if finding_fit and finding_fit.get("skipped"):
            self.log_cb("What PPE found: not judged on this image ({}).".format(finding_fit["skipped"])); finding_fit = None
        pale_fit = (finding_fit or {}).get("pale")
        if finding_fit:
            self.log_cb("What PPE found, references: sunlit bare soil NIR {}; dark plant pixels below brightness {}; neighbourhood {} px (soil) and {} px (canopy).".format(
                "{:.3f}".format(finding_fit["soil_nir"]) if finding_fit["soil_nir"] is not None else "not found",
                "{:.4f}".format(finding_fit["dark_threshold"]) if finding_fit["dark_threshold"] is not None else "not available",
                finding_fit["soil_radius_px"], finding_fit["canopy_radius_px"]))
        if pale_fit:
            self.log_cb("Pale plant parts (tassels, flowers, heads, yellowing leaves): NIR above {:.3f} (leaves {:.3f}, soil {}), NDVI {:.2f}-{:.2f}, green/red below {:.1f}. Reported only; the mask is not changed.".format(
                pale_fit["nir_floor"], pale_fit["leaf_nir"], "{:.3f}".format(pale_fit["soil_nir"]) if pale_fit["soil_nir"] is not None else "not found",
                pale_fit["ndvi_low"], pale_fit["ndvi_high"], pale_fit["green_red_max"]))
        clean = {}
        for name in available:
            if name in NON_VOTING: continue
            x = np.asarray(compute_vi(name, sample), dtype=np.float32); x[~np.isfinite(x)] = np.nan; clean[name] = x
        if not clean: raise ValueError("The available band combination cannot produce a usable PPE candidate layer.")

        # Step 1 of the framework: rank by spread, keep the best layer of each spectral group,
        # and let the highest-spread groups vote.
        spreads = {name: unit_free_spread(x) for name, x in clean.items()}
        leaders = []
        for group, members in VI_GROUPS.items():
            have = [n for n in members if n in clean]
            if have:
                best = max(have, key=lambda n: (spreads[n], -members.index(n)))
                leaders.append((spreads[best], best, group))
        leaders.sort(key=lambda item: (-item[0], item[1]))
        max_layers = max(1, int(cfg.get("max_layers", MAX_LAYERS)))
        chosen = [name for _spread, name, _group in leaders[:max_layers]]
        forced = [str(n).lower().strip() for n in (cfg.get("layers") or [])]
        if forced:
            # "layers" lets a script name the voting layers itself; the panel never sets it.
            missing = [n for n in forced if n not in clean]
            if missing: raise ValueError("These layers cannot vote with the bands in this image: {}".format(", ".join(missing)))
            chosen = forced; max_layers = len(forced)
        if len(chosen) < max_layers:
            # Fewer spectral groups than layers (an RGB image has one): add the next
            # highest-spread indices unless they are a near copy of a layer already chosen.
            for name in sorted(clean, key=lambda n: (-spreads[n], n)):
                if len(chosen) >= max_layers: break
                if name not in chosen and all(self._correlation(clean[name], clean[c]) <= 0.95 for c in chosen):
                    chosen.append(name)
        self.log_cb("Spread ranking (best index of each spectral group): {}".format(
            ", ".join("{} {:.3f}".format(name, spread) for spread, name, _g in leaders)))
        self.progress(30, "Highest-spread layers selected: {}".format(", ".join(chosen)))

        # Thresholds are fitted to the whole field and then follow local conditions (see local_offsets).
        adaptive = str(cfg.get("adaptation_mode", "adaptive")).lower().strip() not in ("global", "off", "none", "false")
        tile = max(64, int(cfg.get("tile_pixels", 512)))
        grid_w = max(2, min(48, int(round(self.width / float(tile))))); grid_h = max(2, min(48, int(round(self.height / float(tile)))))
        self.log_cb("Thresholds: fitted to the whole field, then shifted locally on a {} x {} grid.".format(grid_w, grid_h) if adaptive else "Thresholds: one set for the whole field.")

        # Step 2: four thresholds on every chosen layer.
        half_mixture = bool(cfg.get("half_mixture", False))
        models = []; layer_details = []; offsets = {}
        for name in chosen:
            x = clean[name][np.isfinite(clean[name])]; polarity = VI_META.get(name, (None, 1))[1]
            o = otsu(x); c = cdf_threshold(x); k = kmeans_threshold(x, o)
            mo = multi_otsu(x) if polarity == 1 else None
            if not isinstance(c, tuple) and not np.isfinite(c): c = o
            methods = [("otsu", o), ("cdf", c), ("kmeans", k if np.isfinite(k) else o)]
            if mo: methods.append(("multiotsu", (mo[0], mo[1])))
            if half_mixture:
                # optional fifth method, off unless asked for (see half_mix_threshold)
                hmix = half_mix_threshold(name, sample, clean[name])
                methods.append(("halfmix", hmix if np.isfinite(hmix) else o))
            models.append((name, polarity, methods))
            shift_range = [0.0, 0.0]
            if adaptive and np.isfinite(o):
                if mo and mo[0] < o < mo[1]: low, high = mo[0], mo[1]
                else:
                    reach = 0.25 * float(np.percentile(x, 99) - np.percentile(x, 1)); low, high = o - reach, o + reach
                layer_2d = np.asarray(compute_vi(name, sample_2d), dtype=np.float32)
                offsets[name] = local_offsets(layer_2d, valid_2d, o, low, high, grid_h, grid_w)
                shift_range = [float(offsets[name].min()), float(offsets[name].max())]
            layer_details.append({"layer": name, "spread": spreads[name], "methods": [m[0] for m in methods],
                                  "thresholds": {m[0]: (list(m[1]) if isinstance(m[1], tuple) else m[1]) for m in methods},
                                  "local_shift_range": shift_range})
            self.log_cb("{}: spread {:.3f}; Otsu {:.4g}; CDF {}; k-means {:.4g}; multi-Otsu {}; local shift {:+.3f} to {:+.3f}".format(
                name, spreads[name], o, "{:.4g} / {:.4g}".format(*c) if isinstance(c, tuple) else "{:.4g}".format(c), methods[2][1],
                "{:.4g} / {:.4g}".format(mo[0], mo[1]) if mo else "not used", shift_range[0], shift_range[1]))

        # Colour guard (camera images with no NIR band): a pixel can only stay plant if it is
        # greener than the soil's own colour. Grey, white and black objects (vehicles, tarps,
        # residue, deep shade) can pass a visible index on brightness alone.
        colour_guard = None
        if cfg.get("colour_guard", True) and "nir" not in bands and all(b in sample for b in ("red", "green", "blue")):
            colour_guard = self._fit_colour_guard(sample, clean, models)
            if colour_guard:
                self.log_cb("Colour guard: soil colour R {:.3g} G {:.3g} B {:.3g}; plant pixels must be greener than soil.".format(*colour_guard["soil"]))
                if colour_guard["plant_excess"] < WEAK_GREEN:
                    self.log_cb("WARNING: plants and soil have almost the same colour in this image (excess green {:.3f}). It may hold little or no green vegetation, so check the mask before using it.".format(colour_guard["plant_excess"]))

        one_class, referee, low_end, high_end = single_class_check(sample)
        if one_class:
            self.log_cb("WARNING: this image looks like all {} ({} runs only from {:.2f} to {:.2f}). Automatic thresholds always split an image in two, so check the mask before using it.".format(
                "bare soil" if one_class == "soil" else "canopy", referee.upper(), low_end, high_end))
        single_class = None      # the mask is never overridden
        n_masks = sum(len(x[2]) for x in models); vote_path = os.path.join(run_dir, "vote_fraction.tif"); out = self._create_output(vote_path, gdal.GDT_Float32, NODATA)
        rejection_path = os.path.join(run_dir, "bright_neutral_rejection.tif") if guard else ""
        rejection_out = self._create_output(rejection_path, gdal.GDT_Byte, 255) if guard else None
        shadow_rejection_path = os.path.join(run_dir, "shadow_rejection.tif") if shadow_guard else ""
        shadow_rejection_out = self._create_output(shadow_rejection_path, gdal.GDT_Byte, 255) if shadow_guard else None
        biofilm_rejection_path = os.path.join(run_dir, "biofilm_rejection.tif") if algae_guard else ""
        biofilm_rejection_out = self._create_output(biofilm_rejection_path, gdal.GDT_Byte, 255) if algae_guard else None
        # What PPE found (4.1). A pale pixel is a plant part (tassel, flower, head) only inside closed
        # canopy; beside soil it is a leaf edge with a little soil in the pixel (IH corn at V6). Shaded
        # leaves keep more NIR than bare soil in full sun; dark pixels that do not are unclear.
        findings_path = os.path.join(run_dir, "ppe_findings.tif") if finding_fit else ""
        findings_out = self._create_output(findings_path, gdal.GDT_Byte, 255) if finding_fit else None
        pale_path = findings_path                      # code 1 = plant parts (the 3.9/4.0 pale-plant layer)
        rejected_count = shadow_rejected_count = biofilm_rejected_count = colour_rejected_count = valid_count = 0
        block_rows = 512

        # Step 3: every layer x method result is one equal vote.
        columns = {name: _interp_columns(offsets[name], self.width, grid_w) for name in offsets}
        display = self._display_writer(display_path, channels, display_sample)

        def block_votes(y):
            h = min(block_rows, self.height-y); raw = {n: self._read_band(n, y=y, h=h) for n in self.sources}; ok = self._valid(raw); total = np.zeros((h,self.width), np.float32); wsum = 0.
            rgb = display.convert(raw)
            for name, polarity, methods in models:
                x = raw[name] if name in raw else compute_vi(name, raw)
                if name in columns:
                    x = x - _interp_rows(columns[name], y, h, grid_h, self.height)
                for method, param in methods:
                    if isinstance(param, tuple):
                        vote = np.where(x > param[1], 1.0, np.where(x > param[0], 0.5, 0.0)).astype(np.float32)
                    else:
                        vote = (x >= param).astype(np.float32)
                    if polarity < 0: vote = 1.0 - vote
                    total += vote; wsum += 1.0
            result = total/max(wsum, 1e-12)
            masks = {}
            if guard:
                masks["bright"] = self._bright_guard_mask(raw, guard) & ok
            if shadow_guard:
                masks["shadow"] = self._shadow_guard_mask(raw, shadow_guard) & ok
            if colour_guard:
                masks["colour"] = self._colour_guard_mask(raw, colour_guard) & ok
            if algae_guard:
                masks["algae"] = self._algae_guard_mask(raw, algae_guard) & ok
            counts = {}
            for key in ("bright", "shadow", "colour", "algae"):
                if key in masks:
                    counts[key] = int((masks[key] & (result >= 0.45)).sum()); result[masks[key]] = 0.0
            nvalid = int(ok.sum())
            findings = None
            if finding_fit:
                r = max(finding_fit["soil_radius_px"], finding_fit["canopy_radius_px"]); top = max(0, y - r); bottom = min(self.height, y + h + r)
                nir_h = self._read_band("nir", y=top, h=bottom - top); red_h = self._read_band("red", y=top, h=bottom - top)
                with np.errstate(divide="ignore", invalid="ignore"):
                    ndvi_h = (nir_h - red_h) / (nir_h + red_h)
                findings = classify_findings(raw, ok, finding_fit, ndvi_h, y - top)
            result[~ok] = NODATA
            return y, h, result, ok, masks, counts, nvalid, rgb, findings

        from concurrent.futures import ThreadPoolExecutor
        self._thread_local = threading.local()
        starts = list(range(0, self.height, block_rows))
        workers = self._block_workers(block_rows, len(starts))
        self.log_cb("Full-resolution pass: {} blocks of {} rows on {} worker thread(s).".format(len(starts), block_rows, workers))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pending = []; nxt = 0
            def submit():
                nonlocal nxt
                if nxt < len(starts): pending.append(pool.submit(block_votes, starts[nxt])); nxt += 1
            for _ in range(workers + 1): submit()
            while pending:
                y, h, result, ok, masks, counts, nvalid, rgb, findings = pending.pop(0).result(); submit()
                if findings is not None:
                    findings_out.GetRasterBand(1).WriteArray(findings, 0, y)
                display.write(rgb, y)
                rejected_count += counts.get("bright", 0); shadow_rejected_count += counts.get("shadow", 0)
                colour_rejected_count += counts.get("colour", 0); biofilm_rejected_count += counts.get("algae", 0)
                for key, target in (("bright", rejection_out), ("shadow", shadow_rejection_out), ("algae", biofilm_rejection_out)):
                    if key in masks and target is not None:
                        target.GetRasterBand(1).WriteArray(np.where(ok, masks[key].astype(np.uint8), 255).astype(np.uint8), 0, y)
                valid_count += nvalid
                out.GetRasterBand(1).WriteArray(result, 0, y)
                self.progress(40+int(45*(y+h)/self.height), "Combining {} full-resolution layer×method votes".format(n_masks))
        display.close(); self._thread_local = None
        out.FlushCache(); out = None
        if rejection_out is not None: rejection_out.FlushCache(); rejection_out = None
        if shadow_rejection_out is not None: shadow_rejection_out.FlushCache(); shadow_rejection_out = None
        if biofilm_rejection_out is not None: biofilm_rejection_out.FlushCache(); biofilm_rejection_out = None
        if findings_out is not None: findings_out.FlushCache(); findings_out = None
        findings_hist = findings_histograms(findings_path, vote_path) if finding_fit else None
        pale_pct, pale_lost_pct = self._log_findings(findings_hist)
        rejected_pct = 100.0*rejected_count/max(1, valid_count) if guard else 0.0
        shadow_rejected_pct = 100.0*shadow_rejected_count/max(1, valid_count) if shadow_guard else 0.0
        biofilm_rejected_pct = 100.0*biofilm_rejected_count/max(1, valid_count) if algae_guard else 0.0
        if guard: self.log_cb("Bright-soil guard changed {:.2f}% of valid pixels from plant to soil.".format(rejected_pct))
        if shadow_guard: self.log_cb("Shadow guard changed {:.2f}% of valid pixels from plant to soil.".format(shadow_rejected_pct))
        colour_rejected_pct = 100.0*colour_rejected_count/max(1, valid_count) if colour_guard else 0.0
        if colour_guard: self.log_cb("Colour guard changed {:.2f}% of valid pixels from plant to soil.".format(colour_rejected_pct))
        if algae_guard: self.log_cb("Algae guard changed {:.2f}% of valid pixels from plant to soil.".format(biofilm_rejected_pct))
        curve_path = os.path.join(run_dir, "threshold_curve.csv"); self._write_curve(vote_path, curve_path)
        # Suggested starting threshold, from the votes on the same sample grid as the fitting sample.
        suggestion = None; no_suggestion_reason = "too few pixels that are clearly all plant and all soil"
        if "nir" not in sample_2d:
            # Without NIR, plant and soil differ too little in colour for a reliable plant share per pixel
            # (tested on camera RGB fields: the estimate swung from far too much to far too little plant).
            no_suggestion_reason = "it needs a NIR band; camera (RGB) images start at the standard 0.50"
        elif cfg.get("suggest_threshold", True):
            rows, cols = next(iter(sample_2d.values())).shape
            vds = safe_gdal_open(vote_path)
            sample_votes = vds.GetRasterBand(1).ReadAsArray(buf_xsize=cols, buf_ysize=rows).astype(np.float64); vds = None
            sample_votes[(sample_votes == NODATA) | (sample_votes < 0) | (sample_votes > 1)] = np.nan
            suggestion = suggest_threshold(sample_votes, sample_2d, valid_2d, grid_h, grid_w)
        if suggestion:
            self.log_cb("Suggested threshold {:.2f} (good range {:.2f}-{:.2f}): best match to pixels that are at least half plant.".format(
                suggestion["threshold"], suggestion["low"], suggestion["high"]))
        else:
            self.log_cb("No threshold suggestion ({}); starting at 0.50.".format(no_suggestion_reason))
        start_threshold = suggestion["threshold"] if suggestion else 0.5
        result = dict(imported, vote_path=vote_path, curve_path=curve_path, threshold=start_threshold, suggestion=suggestion or {}, suggestion_note="" if suggestion else no_suggestion_reason, selected_layers=chosen, chosen_layers=chosen, layer_details=layer_details, n_voters=n_masks, n_method_masks=n_masks, canopy_regime="classic", adaptation_mode="adaptive" if adaptive else "global", half_mixture=half_mixture, spread_ranking=[{"layer": name, "group": group, "spread": spread} for spread, name, group in leaders], mixed_class_vote=.5, bright_guard=bool(guard), bright_rejection_path=rejection_path, bright_rejected_pct=rejected_pct, shadow_guard=bool(shadow_guard), shadow_rejection_path=shadow_rejection_path, shadow_rejected_pct=shadow_rejected_pct, biofilm_guard=bool(algae_guard), algae_guard_fit=algae_guard or {}, biofilm_rejection_path=biofilm_rejection_path, biofilm_rejected_pct=biofilm_rejected_pct, colour_guard=bool(colour_guard), colour_rejected_pct=colour_rejected_pct, weak_green_warning=bool(colour_guard and colour_guard["plant_excess"] < WEAK_GREEN), single_class="", one_class_warning=one_class or "", pale_plant_path=pale_path, pale_plant_pct=pale_pct, pale_plant_lost_pct=pale_lost_pct, pale_plant_fit=pale_fit or {}, findings_path=findings_path, findings_hist=findings_hist, findings_fit={k: v for k, v in (finding_fit or {}).items() if k != "pale"}, ppe_version="native-3.9.0", processing_threads=self.thread_count, output_dir=cfg["output_dir"], timings={"total": time.time()-started})
        with open(os.path.join(run_dir, "run_summary.json"), "w", encoding="utf-8") as handle: json.dump(result, handle, indent=2)
        self.progress(100, "Plant vote raster and threshold curve are ready"); return result

    @staticmethod
    def _correlation(a, b):
        good = np.isfinite(a) & np.isfinite(b)
        if good.sum() < 3 or np.std(a[good]) == 0 or np.std(b[good]) == 0: return 0.0
        value = np.corrcoef(a[good], b[good])[0, 1]
        return abs(float(value)) if np.isfinite(value) else 0.0

    @staticmethod
    def _fit_colour_guard(sample, clean, models):
        """Soil colour for the colour guard, taken from the pixels the first voting layer's
        Otsu split calls soil."""
        if not models: return None
        name, polarity, methods = models[0]; split = methods[0][1]
        x = clean[name]
        if not np.isfinite(split): return None
        soil_side = (x < split) if polarity > 0 else (x > split)
        r, g, b = sample["red"], sample["green"], sample["blue"]
        good = np.isfinite(x) & np.isfinite(r) & np.isfinite(g) & np.isfinite(b)
        soil = good & soil_side; plant = good & ~soil_side
        if soil.sum() < 100 or plant.sum() < 100: return None
        ref = [float(np.median(v[soil])) for v in (r, g, b)]
        if min(ref) <= 0: return None
        guard = {"soil": ref}
        guard["plant_excess"] = float(np.median(NativePPEEngine._balanced_excess_green(sample, guard)[plant]))
        return guard

    @staticmethod
    def _balanced_excess_green(bands, guard):
        r = bands["red"]/guard["soil"][0]; g = bands["green"]/guard["soil"][1]; b = bands["blue"]/guard["soil"][2]
        return (2*g - r - b)/np.maximum(r + g + b, 1e-6)

    @staticmethod
    def _colour_guard_mask(bands, guard):
        with np.errstate(all="ignore"):
            return ~(NativePPEEngine._balanced_excess_green(bands, guard) > 0)

    def _fit_bright_guard(self, bands, valid):
        if not all(name in bands for name in ("red", "green", "blue")) or not np.any(valid): return None
        red, green, blue = bands["red"], bands["green"], bands["blue"]
        brightness = (red+green+blue)/3.0
        chroma = np.maximum(np.maximum(red, green), blue)-np.minimum(np.minimum(red, green), blue)
        evidence_names = [name for name in ("ndvi", "ndre") if name in VI_META and all(b in bands for b in VI_META[name][0])]
        if not evidence_names:
            evidence_names = [name for name in ("ngrdi", "exg") if all(b in bands for b in VI_META[name][0])]
        evidence = {}
        for name in evidence_names:
            values = compute_vi(name, bands); threshold = otsu(values[valid])
            if np.isfinite(threshold): evidence[name] = threshold
        if not evidence: return None
        fitted = {"brightness": float(np.nanpercentile(brightness[valid], 90)), "chroma": float(np.nanpercentile(chroma[valid], 25)), "evidence": evidence}
        self.log_cb("Bright-soil safeguard: brightness ≥ {:.4g}, RGB chroma ≤ {:.4g}, and no {} support.".format(fitted["brightness"], fitted["chroma"], "/".join(evidence)))
        return fitted

    @staticmethod
    def _bright_guard_mask(bands, guard):
        red, green, blue = bands["red"], bands["green"], bands["blue"]
        brightness = (red+green+blue)/3.0
        chroma = np.maximum(np.maximum(red, green), blue)-np.minimum(np.minimum(red, green), blue)
        vegetation = np.zeros(brightness.shape, dtype=bool)
        for name, threshold in guard["evidence"].items(): vegetation |= compute_vi(name, bands) >= threshold
        return (brightness >= guard["brightness"]) & (chroma <= guard["chroma"]) & ~vegetation

    def _fit_shadow_guard(self, bands, valid):
        """Fit adaptive threshold for unilluminated shadows and black sensor pixels."""
        if not np.any(valid): return None
        if all(name in bands for name in ("red", "green", "blue")):
            brightness = (bands["red"] + bands["green"] + bands["blue"]) / 3.0
        elif "red" in bands:
            brightness = bands["red"]
        else:
            brightness = next(iter(bands.values()))

        b_valid = brightness[valid]
        b_valid = b_valid[np.isfinite(b_valid)]
        if not b_valid.size: return None
        b_thresh = float(np.nanpercentile(b_valid, 15))

        has_nir = "nir" in bands and "red" in bands
        nirv_floor = None
        if has_nir:
            nir = bands["nir"]
            red = bands["red"]
            denom = np.maximum(nir + red, 1e-6)
            ndvi = (nir - red) / denom
            nirv = (nir * ndvi)[valid]
            nirv = nirv[np.isfinite(nirv)]
            if nirv.size >= 10:
                o_nirv = otsu(nirv)
                if np.isfinite(o_nirv) and o_nirv > 0:
                    nirv_floor = float(0.60 * o_nirv)
            if nirv_floor is None and nirv.size > 0:
                p50 = float(np.nanpercentile(nirv, 50))
                if p50 > 0: nirv_floor = float(0.50 * p50)

        # A leaf in shade keeps a leaf's NDVI (a ratio is unchanged when all bands dim together);
        # a dark gap or shaded soil does not. So a dark pixel whose NDVI reaches the plant class
        # of the field (upper multi-Otsu cut) is kept as a leaf. Without this, a closed canopy
        # (Kritis, 8 Aug 2025) lost a quarter of its shaded leaves.
        ndvi_keep = None
        if has_nir:
            nd = ((bands["nir"] - bands["red"]) / np.maximum(bands["nir"] + bands["red"], 1e-6))[valid]
            nd = nd[np.isfinite(nd)]
            if nd.size >= 100:
                cuts = multi_otsu(nd); split = otsu(nd)
                if cuts and np.isfinite(split):
                    ndvi_keep = float(max(cuts[1], split))
        # With a red-edge band the shaded leaf must also show a leaf's red-edge contrast
        # (NDRE at least its Otsu split), which dark soil and gaps do not.
        ndre_keep = None
        if ndvi_keep is not None and "re" in bands:
            ne = ((bands["nir"] - bands["re"]) / np.maximum(bands["nir"] + bands["re"], 1e-6))[valid]
            ne = ne[np.isfinite(ne)]
            if ne.size >= 100:
                split = otsu(ne)
                ndre_keep = float(split) if np.isfinite(split) else None
        fitted = {
            "b_thresh": b_thresh,
            "has_nir": has_nir,
            "nirv_floor": nirv_floor,
            "ndvi_keep": ndvi_keep,
            "ndre_keep": ndre_keep,
        }
        if nirv_floor is not None:
            self.log_cb("Shadow guard: dark means brightness < {:.5g}; a dark pixel is kept as plant if NIRv ≥ {:.5g}{}.".format(
                b_thresh, nirv_floor, " or NDVI ≥ {:.3f} (a shaded leaf)".format(ndvi_keep) if ndvi_keep is not None else ""))
        else:
            self.log_cb("Shadow & black-pixel safeguard: brightness < {:.5g}, requiring visible greenness.".format(b_thresh))
        return fitted

    @staticmethod
    def _shadow_guard_mask(bands, guard):
        """Identify dark shadow and black pixels that lack vegetation characteristics."""
        if all(name in bands for name in ("red", "green", "blue")):
            red, green, blue = bands["red"], bands["green"], bands["blue"]
            brightness = (red + green + blue) / 3.0
            gr = green / np.maximum(red, 1e-6)
            exg = 2.0 * green - red - blue
        elif "red" in bands:
            red = bands["red"]
            brightness = red
            green = red
            gr = np.ones_like(red)
            exg = np.zeros_like(red)
        else:
            brightness = next(iter(bands.values()))
            red = green = brightness
            gr = np.ones_like(brightness)
            exg = np.zeros_like(brightness)

        is_dark = brightness < guard["b_thresh"]

        if guard.get("has_nir") and "nir" in bands and "red" in bands and guard.get("nirv_floor") is not None:
            nir = bands["nir"]
            denom = np.maximum(nir + red, 1e-6)
            ndvi = (nir - red) / denom
            nirv = nir * ndvi
            # Living vegetation in shade reflects NIR skylight (NIRv >= floor) or exhibits greenness with moderate NIRv
            is_veg = (nirv >= guard["nirv_floor"]) | ((gr >= 1.6) & (nirv >= 0.5 * guard["nirv_floor"]))
            if guard.get("ndvi_keep") is not None:
                leaf_in_shade = ndvi >= guard["ndvi_keep"]
                if guard.get("ndre_keep") is not None and "re" in bands:
                    ndre = (nir - bands["re"]) / np.maximum(nir + bands["re"], 1e-6)
                    leaf_in_shade &= ndre >= guard["ndre_keep"]
                is_veg |= leaf_in_shade
        else:
            # RGB-only: require clear green excess or green/red contrast
            is_veg = (gr >= 1.25) | (exg > 0)

        return is_dark & ~is_veg

    def _fit_algae_guard(self, sample):
        """Fit the algae guard: how much red-edge contrast do bare soil and plants have here?

        Algae, moss and biofilm on wet soil are green and absorb red light, so NDVI and the
        visible greenness indices can take them for plants. What they lack is the steep rise
        from the red-edge band to the NIR band that a leaf has: their NDRE stays close to bare
        soil. The guard measures the bare-soil level (median NDRE of the 5 % of pixels with the
        lowest NDVI) and the plant level (median NDRE of the pixels above the NDVI Otsu split),
        and later rejects plant candidates that show less than a quarter of the plant's rise.

        A quarter, not more: plants that are pale or short of nitrogen also have a weak
        red-edge rise. On the IH test field (6 June 2023) a floor at one third removed real
        plants in the sparse plots, while on the Tifton field (22 May 2024) algae sits below
        one quarter. It needs red, red-edge and NIR bands; without them it is skipped, because
        greenness or texture alone cannot tell algae from a small or shaded leaf.
        """
        if not all(name in sample for name in ("red", "re", "nir")):
            self.log_cb("Algae guard skipped: it needs red, red-edge and NIR bands.")
            return None
        ndvi = compute_vi("ndvi", sample); ndre = compute_vi("ndre", sample)
        good = np.isfinite(ndvi) & np.isfinite(ndre)
        if good.sum() < 400: return None
        ndvi = ndvi[good]; ndre = ndre[good]
        split = otsu(ndvi)
        if not np.isfinite(split): return None
        soil = ndre[ndvi <= np.percentile(ndvi, 5)]; plant = ndre[ndvi >= split]
        if soil.size < 20 or plant.size < 50: return None
        soil_mid = float(np.median(soil)); plant_mid = float(np.median(plant))
        if not soil_mid < plant_mid:
            self.log_cb("Algae guard skipped: red-edge contrast does not separate soil from plants in this image.")
            return None
        floor = soil_mid + ALGAE_GUARD_SHARE * (plant_mid - soil_mid)
        self.log_cb("Algae guard: a plant must have NDRE above {:.3f} (bare soil is typically {:.3f}, plants {:.3f}).".format(floor, soil_mid, plant_mid))
        return {"ndre_floor": floor, "soil_ndre_median": soil_mid, "plant_ndre_median": plant_mid}

    @staticmethod
    def _algae_guard_mask(bands, guard):
        """Pixels whose red-edge contrast is no higher than bare soil's."""
        return compute_vi("ndre", bands) < guard["ndre_floor"]

    def _write_curve(self, vote_path, curve_path):
        ds = safe_gdal_open(vote_path); ratio = self.width/max(1,self.height); bx=min(self.width,max(1,int(math.sqrt(300000*ratio)))); by=min(self.height,max(1,int(300000/bx))); x=ds.GetRasterBand(1).ReadAsArray(buf_xsize=bx,buf_ysize=by).astype(float); x=x[(x != NODATA)&np.isfinite(x)]
        with open(curve_path,"w",newline="",encoding="utf-8") as f:
            w=csv.writer(f); w.writerow(("plant_thresh","canopy_pct"))
            for t in np.arange(.15,.951,.01): w.writerow((round(float(t),2),float(100*np.mean(x >= max(0,t-.05)))))

    def apply(self, cfg):
        """Write the plant mask. With crop areas (cfg zone_path + zone_thresholds: index 0 for the rest
        of the image, then one per crop) each area gets its own threshold; the vote is unchanged."""
        self.thread_count=max(1,min(16,int(cfg.get("threads",self.thread_count) or self.thread_count))); vote_path=cfg["vote_path"]; ds=safe_gdal_open(vote_path); self.template=ds; self.width=ds.RasterXSize; self.height=ds.RasterYSize
        threshold=float(cfg["threshold"]); out_dir=cfg["output_dir"]; os.makedirs(out_dir,exist_ok=True)
        zone_path=cfg.get("zone_path") or ""; zone_t=[float(t) for t in (cfg.get("zone_thresholds") or [])]
        zones=safe_gdal_open(zone_path) if zone_path and len(zone_t) > 1 else None
        if zones is not None and (zones.RasterXSize != self.width or zones.RasterYSize != self.height): zones=None
        if zones is not None: zone_t[0]=threshold
        cut=np.maximum(0, np.array(zone_t if zones is not None else [threshold], dtype=float)-.05)
        mask_path=os.path.join(out_dir,"ppe_plant_mask_by_crop.tif" if zones is not None else "ppe_plant_mask_{:.2f}.tif".format(threshold))
        out=self._create_output(mask_path,gdal.GDT_Byte,255); n=len(cut); count=np.zeros(n,np.int64); valid_count=np.zeros(n,np.int64)
        clip=sorted({int(c) for c in (cfg.get("clip_codes") or []) if int(c) in FINDING_NAMES})
        findings=safe_gdal_open(cfg["findings_path"]) if clip and cfg.get("findings_path") and os.path.isfile(cfg["findings_path"]) else None
        if findings is not None and (findings.RasterXSize != self.width or findings.RasterYSize != self.height): findings=None
        if clip and findings is None: raise ValueError("The findings layer of this run is missing, so nothing can be left out of the mask. Run PPE again.")
        removed=np.zeros(5,np.int64)
        for y in range(0,self.height,512):
            h=min(512,self.height-y); x=ds.GetRasterBand(1).ReadAsArray(0,y,self.width,h); valid=(x != NODATA)&np.isfinite(x)
            z=zones.GetRasterBand(1).ReadAsArray(0,y,self.width,h).astype(np.intp) if zones is not None else np.zeros(x.shape,np.intp)
            z=np.clip(z,0,n-1); plant=valid&(x >= cut[z])
            if findings is not None:
                f=findings.GetRasterBand(1).ReadAsArray(0,y,self.width,h); out_of_mask=plant&np.isin(f,clip)
                removed += np.bincount(f[out_of_mask].astype(np.intp),minlength=5)[:5]; plant &= ~out_of_mask
            m=np.where(valid,plant.astype(np.uint8),255).astype(np.uint8); out.GetRasterBand(1).WriteArray(m,0,y)
            count += np.bincount(z[plant],minlength=n)[:n]; valid_count += np.bincount(z[valid],minlength=n)[:n]
            self.progress(int(100*(y+h)/self.height),"Writing native-resolution plant mask")
        out.FlushCache(); out=None
        result={"mask_path":mask_path,"mask_qgis_path":mask_path,"threshold":threshold,"canopy_pct":100*count.sum()/max(1,valid_count.sum()),
                "clip_codes":clip,"left_out":{FINDING_NAMES[k]:int(removed[k]) for k in clip},
                "left_out_pct":100.0*removed.sum()/max(1,valid_count.sum())}
        if clip:
            self.log_cb("Left out of the mask: " + ", ".join("{} ({:,} pixels)".format(FINDING_NAMES[k], int(removed[k])) for k in clip) + ".")
        if zones is not None:
            names=list(cfg.get("zone_names") or [])
            result["zone_path"]=zone_path; result["zone_thresholds"]=zone_t
            result["crops"]=[{"crop":(names[k-1] if k-1 < len(names) else "crop {}".format(k)),"threshold":zone_t[k],
                              "pixels":int(valid_count[k]),"canopy_pct":(100*count[k]/valid_count[k] if valid_count[k] else None)} for k in range(1,n)]
            result["rest_canopy_pct"]=100*count[0]/valid_count[0] if valid_count[0] else None
        record=self.write_run_record(cfg, result, out_dir)
        if record: result["run_record"]=record
        return result

    def _log_findings(self, hist):
        """Write the findings into the log; return (plant parts as percent of the plant at 0.30,
        percent of the plant parts that drop out of the mask at 0.60)."""
        if hist is None:
            return 0.0, 0.0
        shares = finding_shares(hist, 0.30)
        self.log_cb("What PPE found (percent of the plant at 0.30): " + "; ".join("{} {:.2f}%".format(FINDING_NAMES[k], shares[k]) for k in range(1, 5)) +
                    ". Information only; the mask is not changed unless you leave them out.")
        h = np.asarray(hist[1]); levels = h.size - 1
        low = h[int(np.ceil(0.25 * levels - 1e-9)):].sum(); high = h[int(np.ceil(0.55 * levels - 1e-9)):].sum()
        return float(shares[1]), float(100.0 * (low - high) / low) if low else 0.0

    def prepare_crop_areas(self, cfg):
        """Turn the crop outlines into (1) a crop-area raster on the image grid (0 = outside every
        outline, 1..n = the crops in the order listed; where outlines overlap the crop listed first
        wins), (2) a preview raster holding vote + 10 x crop number, so that one colour ramp can show a
        different threshold in every area, and (3) for every area a canopy-by-threshold curve from all
        of its pixels. The vote itself is not changed."""
        vote=safe_gdal_open(cfg["vote_path"])
        if vote is None: raise ValueError("The vote raster is missing; run PPE again.")
        self.template=vote; self.width=vote.RasterXSize; self.height=vote.RasterYSize
        areas=[a for a in (cfg.get("crop_areas") or []) if a.get("wkt")]
        if not areas: raise ValueError("Draw at least one crop area first.")
        out_dir=cfg.get("output_dir") or os.path.dirname(cfg["vote_path"]); os.makedirs(out_dir,exist_ok=True)
        zone_path=os.path.join(out_dir,"ppe_crop_areas.tif"); preview_path=os.path.join(out_dir,"ppe_vote_by_crop.tif")
        self.progress(5,"Drawing the crop areas on the image grid")
        zones=self._create_output(zone_path,gdal.GDT_Byte,255); zones.GetRasterBand(1).Fill(0); zones.GetRasterBand(1).SetNoDataValue(255)
        srs=osr.SpatialReference(); wkt=vote.GetProjection()
        if wkt: srs.ImportFromWkt(wkt)
        memory=ogr.GetDriverByName("Memory").CreateDataSource(""); layer=memory.CreateLayer("crops",srs=srs if wkt else None,geom_type=ogr.wkbMultiPolygon)
        layer.CreateField(ogr.FieldDefn("zone",ogr.OFTInteger))
        for k in range(len(areas),0,-1):                       # last first, so the crop listed first wins overlaps
            geometry=ogr.CreateGeometryFromWkt(areas[k-1]["wkt"])
            if geometry is None or geometry.IsEmpty(): continue
            feature=ogr.Feature(layer.GetLayerDefn()); feature.SetField("zone",k); feature.SetGeometry(geometry); layer.CreateFeature(feature)
        gdal.RasterizeLayer(zones,[1],layer,options=["ATTRIBUTE=zone"]); zones.FlushCache()
        preview=self._create_output(preview_path,gdal.GDT_Float32,NODATA)
        n=len(areas)+1; levels=1200                            # votes are multiples of 1/24 (1/30 with half-mixture)
        hist=np.zeros((n,levels+1),np.int64); overlap=0
        pale_ds=safe_gdal_open(cfg["pale_path"]) if cfg.get("pale_path") and os.path.isfile(cfg["pale_path"]) else None
        pale_n=np.zeros(n,np.int64); plant_n=np.zeros(n,np.int64)
        for y in range(0,self.height,512):
            h=min(512,self.height-y); v=vote.GetRasterBand(1).ReadAsArray(0,y,self.width,h); z=zones.GetRasterBand(1).ReadAsArray(0,y,self.width,h).astype(np.intp)
            valid=(v != NODATA)&np.isfinite(v)&(v >= 0)&(v <= 1); z=np.where(z == 255,0,z)
            preview.GetRasterBand(1).WriteArray(np.where(valid,v+10.0*z,NODATA).astype(np.float32),0,y)
            q=np.rint(v[valid]*levels).astype(np.intp); hist += np.bincount(z[valid]*(levels+1)+q,minlength=n*(levels+1)).reshape(n,levels+1)
            if pale_ds is not None:
                plant=valid&(v >= 0.25); p=pale_ds.GetRasterBand(1).ReadAsArray(0,y,self.width,h) == 1
                plant_n += np.bincount(z[plant],minlength=n); pale_n += np.bincount(z[plant&p],minlength=n)
            self.progress(10+int(85*(y+h)/self.height),"Preparing the crop preview")
        preview.FlushCache(); preview=None; zones=None
        values=np.arange(levels+1)/levels; thresholds=np.round(np.arange(.15,.951,.01),2); curves=[]
        for k in range(n):
            total=hist[k].sum(); above=[int(hist[k][values >= max(0,t-.05)-1e-9].sum()) for t in thresholds]
            curves.append([(float(t),(100.0*a/total if total else None)) for t,a in zip(thresholds,above)])
        names=[a.get("name") or "crop {}".format(i+1) for i,a in enumerate(areas)]
        self.progress(100,"Crop areas ready")
        return {"zone_path":zone_path,"preview_path":preview_path,"zone_names":names,"zone_pixels":[int(hist[k].sum()) for k in range(n)],
                "zone_curves":curves,
                "zone_pale_pct":[(float(100.0*pale_n[k]/plant_n[k]) if plant_n[k] else None) for k in range(n)] if pale_ds is not None else None}

    def write_run_record(self, cfg, result, out_dir):
        """Save what was run and chosen as ppe_run_record.json (the start of a data catalogue)."""
        try:
            record={"plugin_version":"3.9.0","written":datetime.now().isoformat(timespec="seconds"),
                    "source_mode":cfg.get("source_mode"),"source":cfg.get("stacked_path") or cfg.get("folder_path") or cfg.get("bands"),
                    "field_info":cfg.get("field_info") or {},"suggestion":cfg.get("suggestion") or {},
                    "final_threshold":result.get("threshold"),"canopy_pct":result.get("canopy_pct"),
                    "crops":result.get("crops") or [],"rest_of_image_canopy_pct":result.get("rest_canopy_pct"),
                    "chosen_layers":cfg.get("chosen_layers") or [],"mask_path":result.get("mask_path"),
                    "pale_plant_pct":cfg.get("pale_plant_pct"),"pale_plant_path":cfg.get("pale_path") or "",
                    "findings_at_threshold_pct":cfg.get("findings_shares") or {},"left_out_of_mask":list(result.get("left_out") or {}),
                    "left_out_pct":result.get("left_out_pct")}
            if self.template is not None:
                gt=self.template.GetGeoTransform(); record["image"]={"width":self.width,"height":self.height,"pixel_size":[gt[1],abs(gt[5])],
                                                                     "crs":(osr.SpatialReference(wkt=self.template.GetProjection()).GetName() if self.template.GetProjection() else "")}
            path=os.path.join(out_dir,"ppe_run_record.json")
            with open(path,"w",encoding="utf-8") as handle: json.dump(record,handle,indent=2,default=str)
            return path
        except Exception as err:
            self.log_cb("The run record could not be written: {}".format(err)); return ""

    @staticmethod
    def _safe_filename(value):
        value = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(value).strip()).strip("_").lower()
        return value or "band"

    def _check_mask_grid(self, mask):
        if mask.RasterXSize != self.width or mask.RasterYSize != self.height:
            raise ValueError("Finalized mask dimensions do not match the source imagery.")
        tolerance = max(1e-10, max(abs(x) for x in self.template.GetGeoTransform())*1e-12)
        if any(abs(a-b) > tolerance for a,b in zip(mask.GetGeoTransform(), self.template.GetGeoTransform())) or mask.GetProjection() != self.template.GetProjection():
            raise ValueError("Finalized mask CRS or pixel grid does not match the source imagery.")

    def export_plant_only(self, cfg):
        """Export original bands and/or all computable VIs with non-plant set to NoData."""
        started = time.time(); self.progress(2, "Validating source imagery and finalized mask"); self._resolve(cfg)
        mask = safe_gdal_open(cfg.get("mask_path", ""))
        if mask is None: raise ValueError("The finalized plant mask is missing or unreadable.")
        self._check_mask_grid(mask)
        export_root = os.path.abspath(os.path.expanduser(cfg["export_dir"])); os.makedirs(export_root, exist_ok=True)
        action = cfg.get("action", "export_all"); do_bands = action in ("export_bands", "export_all"); do_vis = action in ("export_vis", "export_all")
        outputs = {}; raw_entries = []; raw_metadata = {}
        if do_bands:
            raw_dir = os.path.join(export_root, "plant_only_bands"); os.makedirs(raw_dir, exist_ok=True)
            if cfg.get("source_mode") == "stacked":
                source_path = cfg["stacked_path"]; source = safe_gdal_open(source_path); canonical = {band:name for name, (path,band) in self.sources.items() if os.path.abspath(path) == os.path.abspath(source_path)}
                wavelengths = dataset_wavelengths(source, cfg) if cfg.get("ortho_type") == "hsi" or source.RasterCount > 15 else [None]*source.RasterCount
                for index in range(1, source.RasterCount+1):
                    description = source.GetRasterBand(index).GetDescription()
                    wavelength = wavelengths[index-1] if index <= len(wavelengths) else None
                    if wavelength is not None:
                        name = "band_{:03d}_{:g}nm".format(index, wavelength)
                    else:
                        name = canonical.get(index) or description or "band_{:03d}".format(index)
                    raw_entries.append((self._safe_filename(name), source, index))
            else:
                for name, (path,index) in self.sources.items(): raw_entries.append((name, self.datasets[name], index))
            used = set(); normalized = []
            for name, source, index in raw_entries:
                base=name; suffix=2
                while name in used: name="{}_{}".format(base,suffix); suffix+=1
                used.add(name); path=os.path.join(raw_dir,"{}_plant_only.tif".format(name)); out=self._create_output(path,gdal.GDT_Float32,NODATA); out.GetRasterBand(1).SetDescription(name); normalized.append((name,source,index,out,path))
                raw_metadata[name] = {"source_band": index, "source_description": source.GetRasterBand(index).GetDescription() or None, "wavelength_nm": (wavelengths[index-1] if cfg.get("source_mode") == "stacked" and index <= len(wavelengths) else None)}
            raw_entries = normalized
        vi_entries = []
        if do_vis:
            vi_dir = os.path.join(export_root, "plant_only_vis"); os.makedirs(vi_dir, exist_ok=True)
            available = [name for name, meta in VI_META.items() if all(band in self.sources for band in meta[0])]
            for name in available:
                path=os.path.join(vi_dir,"{}_plant_only.tif".format(name)); out=self._create_output(path,gdal.GDT_Float32,NODATA); out.GetRasterBand(1).SetDescription(name); vi_entries.append((name,out,path))
        total_products = len(raw_entries)+len(vi_entries)
        if total_products == 0: raise ValueError("No requested plant-only products can be exported from the detected bands.")
        for y in range(0,self.height,256):
            h=min(256,self.height-y); labels=mask.GetRasterBand(1).ReadAsArray(0,y,self.width,h); plant=labels==1
            if do_bands:
                for _name,source,index,out,_path in raw_entries:
                    values=source.GetRasterBand(index).ReadAsArray(0,y,self.width,h).astype(np.float32); source_nodata=source.GetRasterBand(index).GetNoDataValue(); good=plant&np.isfinite(values)
                    if source_nodata is not None: good &= values != source_nodata
                    out.GetRasterBand(1).WriteArray(np.where(good,values,NODATA).astype(np.float32),0,y)
            if do_vis:
                bands={name:self._read_band(name,y=y,h=h) for name in self.sources}; valid=self._valid(bands); keep=plant&valid
                for name,out,_path in vi_entries:
                    values=compute_vi(name,bands); out.GetRasterBand(1).WriteArray(np.where(keep&np.isfinite(values),values,NODATA).astype(np.float32),0,y)
            self.progress(5+int(92*(y+h)/self.height),"Exporting {} plant-only GeoTIFFs".format(total_products))
        for name,_source,_index,out,path in raw_entries: out.FlushCache(); outputs["band:"+name]=path
        for name,out,path in vi_entries: out.FlushCache(); outputs["vi:"+name]=path
        raw_entries=[]; vi_entries=[]
        manifest={"export_dir":export_root,"mask_path":cfg["mask_path"],"soil_encoding":"NoData ({})".format(NODATA),"raw_band_metadata":raw_metadata,"products":outputs,"export_count":len(outputs),"elapsed_seconds":time.time()-started}
        manifest_path=os.path.join(export_root,"ppe_plant_only_manifest.json")
        with open(manifest_path,"w",encoding="utf-8") as handle: json.dump(manifest,handle,indent=2)
        manifest["manifest_path"]=manifest_path; self.progress(100,"Plant-only GeoTIFF export complete"); return manifest

    @staticmethod
    def _summary(values):
        """Statistics of the plant pixels of one plot (percentiles as R's default, type 7)."""
        values = np.asarray(values, dtype=float); values = values[np.isfinite(values)]
        if values.size == 0:
            out = {name: None for name in STAT_NAMES}; out["pixel_count"] = 0; return out
        mean = float(np.mean(values)); sd = float(np.std(values, ddof=1)) if values.size > 1 else 0.0
        p = np.percentile(values, [0, 10, 25, 50, 75, 90, 100])
        return {"pixel_count": int(values.size), "mean": mean, "median": float(p[3]), "sd": sd,
                "cv_pct": (100.0*sd/abs(mean) if abs(mean) > 1e-12 else None),
                "min": float(p[0]), "p10": float(p[1]), "q25": float(p[2]), "q75": float(p[4]), "p90": float(p[5]), "max": float(p[6])}

    def _aoi_window(self, geometry):
        envelope = geometry.GetEnvelope()  # min x, max x, min y, max y
        inverse = gdal.InvGeoTransform(self.template.GetGeoTransform())
        corners = [gdal.ApplyGeoTransform(inverse, x, y) for x in (envelope[0], envelope[1]) for y in (envelope[2], envelope[3])]
        x0=max(0,int(math.floor(min(point[0] for point in corners)))); x1=min(self.width,int(math.ceil(max(point[0] for point in corners))))
        y0=max(0,int(math.floor(min(point[1] for point in corners)))); y1=min(self.height,int(math.ceil(max(point[1] for point in corners))))
        return x0, y0, max(0, x1-x0), max(0, y1-y0)

    def _rasterize_aoi(self, geometry, x, y, width, height):
        memory = gdal.GetDriverByName("MEM").Create("", width, height, 1, gdal.GDT_Byte)
        gt = self.template.GetGeoTransform(); origin = gdal.ApplyGeoTransform(gt, x, y)
        memory.SetGeoTransform((origin[0], gt[1], gt[2], origin[1], gt[4], gt[5])); memory.SetProjection(self.template.GetProjection())
        wkt = self.template.GetProjection(); spatial_ref = None
        if wkt:
            spatial_ref = osr.SpatialReference(); spatial_ref.ImportFromWkt(wkt)
        vector = ogr.GetDriverByName("Memory").CreateDataSource(""); layer = vector.CreateLayer("aoi", srs=spatial_ref, geom_type=geometry.GetGeometryType())
        feature = ogr.Feature(layer.GetLayerDefn()); feature.SetGeometry(geometry); layer.CreateFeature(feature)
        gdal.RasterizeLayer(memory, [1], layer, burn_values=[1], options=["ALL_TOUCHED=TRUE"])
        return memory.GetRasterBand(1).ReadAsArray().astype(bool)

    def export_aoi_statistics(self, cfg):
        """Canopy cover and statistics of the plant pixels in each plot polygon, without resampling.

        Writes two CSV files into the export folder:
          ppe_plot_canopy.csv      one row per plot: plot pixels, plant pixels, canopy %, areas;
          ppe_plot_statistics.csv  one row per plot and variable (band or index): the plot's canopy
                                   columns, then pixel count, mean, median, SD, CV %, min, P10,
                                   Q25, Q75, P90 and max of the plant pixels.
        A pixel belongs to a plot when the polygon touches it.
        """
        started=time.time(); self.progress(2,"Opening imagery, plant mask, and plot boundaries"); self._resolve(cfg)
        mask=safe_gdal_open(cfg.get("mask_path", ""))
        if mask is None: raise ValueError("The finalized plant mask is missing or unreadable.")
        self._check_mask_grid(mask)
        vector, layer, info = open_plot_file(cfg.get("aoi_path", ""), cfg.get("aoi_layer") or None)
        if info["note"]: self.log_cb("Plot file: {}.".format(info["note"]))
        self.log_cb("Plot layer '{}': {} polygons; CRS {}.".format(info["layer"], info["features"], info["crs_name"] or "none"))
        transform=_plot_transform(layer, self.template.GetProjection())
        if layer.GetSpatialRef() is None:
            self.log_cb("The plot file has no coordinate system; its coordinates are assumed to match the image.")
        pixel_area=_pixel_area_m2(self.template)
        zone_names=list(cfg.get("zone_names") or []); zone_t=list(cfg.get("zone_thresholds") or [])
        zones=safe_gdal_open(cfg.get("zone_path") or "") if zone_names else None
        if zones is not None and (zones.RasterXSize != self.width or zones.RasterYSize != self.height): zones=None
        pale=safe_gdal_open(cfg["pale_path"]) if cfg.get("pale_path") and os.path.isfile(cfg["pale_path"]) else None
        if pale is not None and (pale.RasterXSize != self.width or pale.RasterYSize != self.height): pale=None
        field_info=cfg.get("field_info") or {}
        info_columns={key:field_info.get(key,"") for key in ("growth_stage","planting_date","flight_date","irrigation","weeds")}
        is_hsi = cfg.get("ortho_type") == "hsi" or (cfg.get("source_mode") == "stacked" and gdal.Open(cfg["stacked_path"]).RasterCount > 15)
        raw=[]
        if cfg.get("source_mode") == "stacked":
            source=gdal.Open(cfg["stacked_path"]); wavelengths=dataset_wavelengths(source,cfg) if is_hsi else [None]*source.RasterCount
            canonical={band:name for name,(path,band) in self.sources.items() if os.path.abspath(path)==os.path.abspath(cfg["stacked_path"])}
            for index in range(1,source.RasterCount+1):
                wavelength=wavelengths[index-1] if index <= len(wavelengths) else None
                name=("band_{:03d}_{:g}nm".format(index,wavelength) if wavelength is not None else canonical.get(index) or source.GetRasterBand(index).GetDescription() or "band_{:03d}".format(index))
                raw.append((self._safe_filename(name),source,index,wavelength))
        else:
            for name,(path,index) in self.sources.items(): raw.append((name,self.datasets[name],index,None))
        vis=[] if is_hsi else [name for name,meta in VI_META.items() if all(band in self.sources for band in meta[0])]
        id_field=cfg.get("aoi_id_field") or ""
        rows=[]; canopy_rows=[]; total=max(1,info["features"]); plots=0; outside=0; position=0
        layer.ResetReading()
        for feature in layer:
            geometry=_plot_geometry(feature, transform)
            if geometry is None: continue
            position += 1
            plot_id=feature.GetField(id_field) if id_field and feature.GetFieldIndex(id_field)>=0 else feature.GetFID()
            if isinstance(plot_id,float) and plot_id.is_integer(): plot_id=int(plot_id)   # KML and GeoJSON often store 401 as 401.0
            base={"plot_id":plot_id,"feature_id":feature.GetFID(),"crop":"","threshold":(cfg.get("threshold") if cfg.get("threshold") is not None else ""),
                  "plot_pixels":0,"plant_pixels":0,"canopy_pct":None,"plot_area_m2":None,"plant_area_m2":None}
            x,y,width,height=self._aoi_window(geometry)
            if width < 1 or height < 1:
                outside += 1; canopy_rows.append(base)
                self.log_cb("Plot {} lies outside the image.".format(plot_id)); continue
            inside=self._rasterize_aoi(geometry,x,y,width,height)
            m=mask.GetRasterBand(1).ReadAsArray(x,y,width,height)
            valid=inside&((m==0)|(m==1)); keep=valid&(m==1)
            if zones is not None:                                   # the crop area holding most of the plot
                z=zones.GetRasterBand(1).ReadAsArray(x,y,width,height)[inside]; z=z[(z > 0)&(z <= len(zone_names))]
                if z.size:
                    k=int(np.bincount(z).argmax()); base["crop"]=zone_names[k-1]
                    if k < len(zone_t): base["threshold"]=zone_t[k]
            n_valid=int(valid.sum()); n_plant=int(keep.sum())
            if pale is not None:                                    # what PPE found in the plot, whatever the threshold
                f=pale.GetRasterBand(1).ReadAsArray(x,y,width,height)
                for code,column in FINDING_COLUMNS.items():
                    base[column]=100.0*int(((f==code)&valid).sum())/n_valid if n_valid else None
            base["left_out_of_mask"]="; ".join(cfg.get("left_out") or [])
            base.update(plot_pixels=n_valid, plant_pixels=n_plant, canopy_pct=(100.0*n_plant/n_valid if n_valid else None),
                        plot_area_m2=(n_valid*pixel_area if pixel_area else None), plant_area_m2=(n_plant*pixel_area if pixel_area else None))
            canopy_rows.append(base); plots += 1
            for name,source,index,wavelength in raw:
                values=source.GetRasterBand(index).ReadAsArray(x,y,width,height).astype(float); nodata=source.GetRasterBand(index).GetNoDataValue(); good=keep&np.isfinite(values)
                if nodata is not None: good &= values != nodata
                rows.append(dict(base,variable=name,variable_type="raw_band",source_band=index,wavelength_nm=wavelength,**self._summary(values[good])))
            if vis:
                bands={name:self._read_band(name,x=x,y=y,w=width,h=height) for name in self.sources}; vi_keep=keep&self._valid(bands)
                for name in vis:
                    values=compute_vi(name,bands)
                    rows.append(dict(base,variable=name,variable_type="vegetation_index",source_band=None,wavelength_nm=None,**self._summary(values[vi_keep&np.isfinite(values)])))
            self.progress(5+int(90*position/total),"Plot statistics: plot {} of {}".format(position,total))
        if not plots:
            raise ValueError("None of the {} plots overlap the image. Check that the plot file belongs to this field and has the "
                             "right coordinate system (for a Shapefile, its .prj file).".format(position))
        export_root=os.path.abspath(os.path.expanduser(cfg["export_dir"])); os.makedirs(export_root,exist_ok=True)
        for row in canopy_rows: row.update(info_columns)
        canopy_fields=("plot_id","feature_id","crop","threshold","plot_pixels","plant_pixels","canopy_pct","plot_area_m2","plant_area_m2")+(tuple(FINDING_COLUMNS.values()) if pale is not None else ())+("left_out_of_mask",)+tuple(info_columns)
        canopy_path=os.path.join(export_root,"ppe_plot_canopy.csv")
        with open(canopy_path,"w",newline="",encoding="utf-8") as handle:
            writer=csv.DictWriter(handle,fieldnames=canopy_fields); writer.writeheader(); writer.writerows(canopy_rows)
        path=os.path.join(export_root,"ppe_plot_statistics.csv")
        fields=canopy_fields[:9]+("variable","variable_type","source_band","wavelength_nm")+STAT_NAMES
        with open(path,"w",newline="",encoding="utf-8") as handle:
            writer=csv.DictWriter(handle,fieldnames=fields,extrasaction="ignore"); writer.writeheader(); writer.writerows(rows)
        record=cfg.get("run_record") or ""
        if record and os.path.isfile(record):
            import shutil
            shutil.copyfile(record,os.path.join(export_root,"ppe_run_record.json"))
        if outside: self.log_cb("{} of {} plots lie outside the image; they are listed in ppe_plot_canopy.csv with 0 pixels.".format(outside, position))
        self.progress(100,"Plot statistics export complete")
        return {"aoi_csv":path,"canopy_csv":canopy_path,"export_dir":export_root,"plot_count":plots,"plots_outside":outside,"row_count":len(rows),"elapsed_seconds":time.time()-started}
