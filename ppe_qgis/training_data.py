"""Training data (4.3): image tiles + PPE labels for a fast segmentation model, no hand labelling.

Labels per pixel (the rule that trained the fast U-Net on PhenoBench to 93.9 IoU, hand labels 95.4):
    1   plant  = every PPE vote says plant (vote 1.0), not 'unclear' (code 4), groups of >= 30 pixels
    0   soil   = (almost) no vote says plant (vote <= 0.1), or 'unclear' (too dark to tell)
    255 unsure = everything else; models skip these pixels when they learn.
Image tiles keep every original band with its original values and data type, as GeoTIFF
(manifest.json gives value_scale per image: reflectance = value / value_scale). Train and validation tiles come from different images (or, for one image, different parts).
Use tools/ppe_train.py to train, predict and check a model.
"""
import json
import os
import time
from datetime import datetime

import numpy as np
from osgeo import gdal

from .native_engine import NativePPEEngine, load_batch_result, safe_gdal_open

PLANT_VOTE = 1.0
SOIL_VOTE = 0.1
MIN_PLANT_PX = 30
UNCLEAR = 4

README = """PPE training data
=================
images/<image>_<row>_<col>.tif   image tile: all bands, original values and data type, georeferenced
                                 (reflectance = value / value_scale of the image in manifest.json)
labels/<image>_<row>_<col>.tif   label tile: 1 plant, 0 soil, 255 unsure (ignore when training)
manifest.json                    tiles, train/val split, band names, band ranges for scaling

Labels come from PPE without hand labelling:
  plant  = every PPE vote says plant, not 'unclear', groups of at least 30 pixels
  soil   = no PPE vote says plant (vote <= 0.1), or 'unclear' (too dark to tell)
  unsure = the rest (models should skip these pixels)

Train a fast model:  python ppe_train.py train  <this folder>
Use it:              python ppe_train.py predict <model.pt> <image.tif>
Check it later:      python ppe_train.py check   <model.pt> <PPE output folder of a new flight>
"""


def label_array(vote, findings):
    """PPE label of each pixel (see the module text). vote: float vote fraction (-1 / NaN outside),
    findings: byte codes. Small plant groups (< MIN_PLANT_PX, 4-connected) become 255."""
    valid = np.isfinite(vote) & (vote >= 0) & (vote <= 1)
    y = np.full(vote.shape, 255, np.uint8)
    y[valid & ((vote <= SOIL_VOTE) | (findings == UNCLEAR))] = 0
    plant = valid & (vote >= PLANT_VOTE) & (findings != UNCLEAR)
    if plant.any():
        mem = gdal.GetDriverByName("MEM").Create("", plant.shape[1], plant.shape[0], 1, gdal.GDT_Byte)
        b = mem.GetRasterBand(1); b.WriteArray(plant.astype(np.uint8))
        gdal.SieveFilter(b, None, b, MIN_PLANT_PX, 4)
        plant &= b.ReadAsArray().astype(bool)          # specks dropped (sieve hole-filling undone by &)
    y[plant] = 1
    return y


def _write_tile(path, arr, gt, wkt, x0, y0, dtype, nodata=None):
    bands = 1 if arr.ndim == 2 else arr.shape[0]; h, w = arr.shape[-2:]
    ds = gdal.GetDriverByName("GTiff").Create(path, w, h, bands, dtype, options=["COMPRESS=DEFLATE", "PREDICTOR=3" if dtype in (gdal.GDT_Float32, gdal.GDT_Float64) else "PREDICTOR=2"])
    ds.SetGeoTransform((gt[0] + x0 * gt[1] + y0 * gt[2], gt[1], gt[2], gt[3] + x0 * gt[4] + y0 * gt[5], gt[4], gt[5])); ds.SetProjection(wkt)
    for i in range(bands):
        ds.GetRasterBand(i + 1).WriteArray(arr if arr.ndim == 2 else arr[i])
        if nodata is not None: ds.GetRasterBand(i + 1).SetNoDataValue(nodata)
    ds = None


def items_from_batch(manifest_path):
    """Training items from a PPE batch (ppe_batch.json): every image that ran."""
    with open(manifest_path, encoding="utf-8") as f:
        m = json.load(f)
    out = []
    for e in m.get("items", []):
        if e.get("status") != "done": continue
        r = load_batch_result(e["result_path"])
        src = {"source_mode": e["source_mode"], ("stacked_path" if e["source_mode"] == "stacked" else "folder_path"): e["source"]}
        out.append({"name": e["name"], "source": src, "vote_path": r["vote_path"], "findings_path": r["findings_path"]})
    return out


def export_training_tiles(cfg, progress=None, log=None, cancelled=None):
    """cfg: items [{name, source (PPE source config), vote_path, findings_path}], output_dir,
    tile (256), min_labelled (0.2 of the tile), val_fraction (0.2). Returns the manifest."""
    progress = progress or (lambda p, m: None); log = log or (lambda m: None); cancelled = cancelled or (lambda: False)
    started = time.time(); items = list(cfg["items"]); out = cfg["output_dir"]; tile = int(cfg.get("tile", 256))
    min_lab = float(cfg.get("min_labelled", 0.2)); val_frac = float(cfg.get("val_fraction", 0.2))
    os.makedirs(os.path.join(out, "images"), exist_ok=True); os.makedirs(os.path.join(out, "labels"), exist_ok=True)
    if not items: raise ValueError("No PPE results to make training data from: run PPE first.")
    # Split by image when there are several images; one image is split into a top and bottom part.
    n_val_images = int(round(len(items) * val_frac)) if len(items) >= 3 else 0
    val_names = {it["name"] for it in items[len(items) - n_val_images:]} if n_val_images else set()
    tiles = []; image_info = []; band_names = None; counts = {"plant": 0, "soil": 0, "unsure": 0}; samples = []
    for k, it in enumerate(items):
        if cancelled(): raise RuntimeError("Cancelled.")
        eng = NativePPEEngine(log=lambda m: None)
        eng._resolve(dict(it["source"], output_dir=out, work_dir=os.path.join(out, "_work")))
        names = list(eng.sources); band_names = band_names or names
        if names != band_names:
            log("{}: bands {} differ from the first image ({}); skipped.".format(it["name"], ", ".join(names), ", ".join(band_names))); continue
        vote_ds = safe_gdal_open(it["vote_path"]); find_ds = safe_gdal_open(it["findings_path"])
        if vote_ds is None or find_ds is None:
            log("{}: PPE outputs missing; skipped.".format(it["name"])); continue
        W, H = eng.width, eng.height; gt = eng.template.GetGeoTransform(); wkt = eng.template.GetProjection()
        # Tiles keep the source data type (8/16-bit stay integers: exact and small); value / value_scale
        # gives the reflectance PPE works with.
        src_type = eng.template.GetRasterBand(1).DataType
        int_type = src_type in (gdal.GDT_Byte, gdal.GDT_UInt16, gdal.GDT_Int16, gdal.GDT_UInt32, gdal.GDT_Int32)
        out_type = src_type if int_type else gdal.GDT_Float32
        image_info.append({"name": it["name"], "value_scale": float(eng.scale) if int_type else 1.0})
        rows = list(range(0, H - tile + 1, tile)); cols = list(range(0, W - tile + 1, tile))
        split_row = rows[int(len(rows) * (1 - val_frac))] if (not val_names and len(items) < 3 and len(rows) >= 5) else None
        for ri, y0 in enumerate(rows):
            progress(int(100 * (k + ri / max(1, len(rows))) / len(items)), "{}: cutting tiles".format(it["name"]))
            vote = vote_ds.GetRasterBand(1).ReadAsArray(0, y0, W, tile).astype(np.float32)
            nd = vote_ds.GetRasterBand(1).GetNoDataValue()
            if nd is not None: vote[vote == nd] = np.nan
            labels = label_array(vote, find_ds.GetRasterBand(1).ReadAsArray(0, y0, W, tile))
            img = None
            for x0 in cols:
                lab = labels[:, x0:x0 + tile]
                if (lab != 255).mean() < min_lab: continue
                if img is None:
                    bands = eng._read_bands(names, y=y0, h=tile)
                    img = np.stack([bands[n] for n in names]).astype(np.float32)
                im = img[:, :, x0:x0 + tile]
                if not np.isfinite(im).all(axis=0).mean() > 0.5: continue
                stem = "{}_{:05d}_{:05d}".format(it["name"], y0, x0)
                raw = np.round(np.nan_to_num(im, nan=0.0) * eng.scale) if int_type else np.nan_to_num(im, nan=0.0)
                _write_tile(os.path.join(out, "images", stem + ".tif"), raw, gt, wkt, x0, y0, out_type)
                _write_tile(os.path.join(out, "labels", stem + ".tif"), lab, gt, wkt, x0, y0, gdal.GDT_Byte, 255)
                part = "val" if (it["name"] in val_names or (split_row is not None and y0 >= split_row)) else "train"
                tiles.append({"tile": stem, "image": it["name"], "split": part})
                counts["plant"] += int((lab == 1).sum()); counts["soil"] += int((lab == 0).sum()); counts["unsure"] += int((lab == 255).sum())
                if len(samples) < 400: samples.append(im[:, ::8, ::8].reshape(len(names), -1))
        log("{}: {} tiles.".format(it["name"], sum(1 for t in tiles if t["image"] == it["name"])))
    if not tiles: raise ValueError("No tile had enough labelled pixels. Try a smaller tile size.")
    s = np.concatenate(samples, axis=1)
    ranges = [[float(np.nanpercentile(s[i], 1)), float(np.nanpercentile(s[i], 99))] for i in range(s.shape[0])]
    total = sum(counts.values())
    manifest = {"created": datetime.now().isoformat(timespec="seconds"), "ppe_version": "native-4.4.0", "tile": tile,
                "bands": band_names, "band_ranges_p1_p99": ranges, "labels": {"1": "plant", "0": "soil", "255": "unsure (ignore)"},
                "label_rule": {"plant_vote": PLANT_VOTE, "soil_vote": SOIL_VOTE, "min_plant_px": MIN_PLANT_PX, "unclear_as_soil": True},
                "pixel_shares": {k: round(v / total, 4) for k, v in counts.items()},
                "train": sum(1 for t in tiles if t["split"] == "train"), "val": sum(1 for t in tiles if t["split"] == "val"),
                "tiles": tiles, "images": image_info, "seconds": time.time() - started}
    if manifest["val"] == 0:
        for t in tiles[::5]: t["split"] = "val"
        manifest["train"] = sum(1 for t in tiles if t["split"] == "train"); manifest["val"] = sum(1 for t in tiles if t["split"] == "val")
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1)
    with open(os.path.join(out, "README.txt"), "w", encoding="utf-8") as f:
        f.write(README)
    manifest["output_dir"] = out
    progress(100, "{} training tiles written".format(len(tiles)))
    return manifest
