#!/usr/bin/env python3
"""PPE companion: train, use and check a fast plant/soil segmentation model on PPE training data.

Needs Python with numpy, GDAL and PyTorch (pip install torch). Runs on an NVIDIA GPU, an Apple
GPU (M1/M2/M3...) or the CPU, whichever is available. It is not run inside QGIS.

  python ppe_train.py train   <training data folder> [more folders ...] [--out model.pt] [--iters 600]
  python ppe_train.py update  <model.pt> <new training data folder> <old folder> [...]   (new + old images)
  python ppe_train.py predict <model.pt> <image.tif or band folder> [--out plant_mask.tif]
  python ppe_train.py check   <model.pt> <image.tif or band folder> <PPE run folder of that image>

Training data folders come from the Training data tab of the PPE plugin. Images are read with PPE's
own band detection and scaling, so a new flight of the same camera needs no preparation.
'check' compares the model with PPE's sure pixels on a new flight: when the model misses PPE's sure
plants or calls PPE's sure soil plant more than it did on its own validation tiles, update it with
the new images plus some old ones (updating with new images only makes it forget the old ones).
"""
import argparse
import json
import os
import sys
import time

import numpy as np
from osgeo import gdal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))      # the folder that holds ppe_qgis
gdal.UseExceptions()


def _torch():
    try:
        import torch
    except ImportError:
        sys.exit("PyTorch is needed: pip install torch")
    return torch


def device(torch):
    if torch.cuda.is_available():
        return torch.device("cuda")
    if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def unet(torch, n_in, c=16):
    nn, F = torch.nn, torch.nn.functional

    def blk(i, o):
        return nn.Sequential(nn.Conv2d(i, o, 3, padding=1), nn.BatchNorm2d(o), nn.ReLU(inplace=True),
                             nn.Conv2d(o, o, 3, padding=1), nn.BatchNorm2d(o), nn.ReLU(inplace=True))

    class UNet(nn.Module):
        def __init__(s):
            super().__init__()
            s.e1, s.e2, s.e3, s.e4 = blk(n_in, c), blk(c, 2 * c), blk(2 * c, 4 * c), blk(4 * c, 8 * c)
            s.u3, s.d3 = nn.ConvTranspose2d(8 * c, 4 * c, 2, 2), blk(8 * c, 4 * c)
            s.u2, s.d2 = nn.ConvTranspose2d(4 * c, 2 * c, 2, 2), blk(4 * c, 2 * c)
            s.u1, s.d1 = nn.ConvTranspose2d(2 * c, c, 2, 2), blk(2 * c, c)
            s.out = nn.Conv2d(c, 1, 1)

        def forward(s, x):
            e1 = s.e1(x)
            e2 = s.e2(F.max_pool2d(e1, 2))
            e3 = s.e3(F.max_pool2d(e2, 2))
            e4 = s.e4(F.max_pool2d(e3, 2))
            d3 = s.d3(torch.cat([s.u3(e4), e3], 1))
            d2 = s.d2(torch.cat([s.u2(d3), e2], 1))
            d1 = s.d1(torch.cat([s.u1(d2), e1], 1))
            return s.out(d1)
    return UNet()


def norm(x, ranges):
    lo = np.array([r[0] for r in ranges], np.float32)[:, None, None]
    hi = np.array([r[1] for r in ranges], np.float32)[:, None, None]
    return np.clip((x - lo) / np.maximum(hi - lo, 1e-6), -0.5, 1.5).astype(np.float32)


def half(x, scale):
    """Average-pool by `scale` (images) - the fast model works at half resolution."""
    if scale == 1:
        return x
    c, h, w = x.shape
    h2, w2 = h // scale, w // scale
    return x[:, :h2 * scale, :w2 * scale].reshape(c, h2, scale, w2, scale).mean(axis=(2, 4))


def load_folder(folder, ranges, scale, split):
    m = json.load(open(os.path.join(folder, "manifest.json")))
    vs = {i["name"]: i.get("value_scale", 1.0) for i in m.get("images", []) if isinstance(i, dict)}
    X, Y = [], []
    for t in m["tiles"]:
        if split and t["split"] != split:
            continue
        x = gdal.Open(os.path.join(folder, "images", t["tile"] + ".tif")).ReadAsArray().astype(np.float32)
        y = gdal.Open(os.path.join(folder, "labels", t["tile"] + ".tif")).ReadAsArray()
        if x.ndim == 2:
            x = x[None]
        x = x / vs.get(t["image"], 1.0)
        X.append(half(norm(x, ranges), scale))
        Y.append(y[scale // 2::scale, scale // 2::scale])
    return m, X, Y


def agreement(p, y):
    """Watchdog numbers: share of PPE's sure plant the model calls plant; share of PPE's sure soil it calls plant."""
    sp = p[y == 1].mean() if (y == 1).any() else float("nan")
    ss = p[y == 0].mean() if (y == 0).any() else float("nan")
    return float(sp), float(ss)


def fit(torch, net, X, Y, iters, dev, log_every=100):
    F = torch.nn.functional
    opt = torch.optim.AdamW(net.parameters(), 2e-3, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, 2e-3, total_steps=iters)
    plant_tiles = [i for i, y in enumerate(Y) if (y == 1).any()] or list(range(len(Y)))
    net.train()
    t0 = time.time()
    for it in range(iters):
        xs, ys = [], []
        for _ in range(8):
            i = int(np.random.choice(plant_tiles)) if np.random.rand() < 0.5 else np.random.randint(len(X))
            x, y = X[i], Y[i]
            if np.random.rand() < 0.5:
                x, y = x[:, :, ::-1], y[:, ::-1]
            k = np.random.randint(4)
            x, y = np.rot90(x, k, (1, 2)), np.rot90(y, k)
            xs.append(np.ascontiguousarray(x))
            ys.append(np.ascontiguousarray(y))
        x = torch.from_numpy(np.stack(xs)).to(dev)
        y = torch.from_numpy(np.stack(ys).astype(np.int64)).to(dev)
        logit = net(x)[:, 0]
        m = y != 255
        yt = (y == 1).float()
        p = torch.sigmoid(logit)
        if not m.any():
            continue
        loss = F.binary_cross_entropy_with_logits(logit[m], yt[m]) + 1 - (2 * (p * yt * m).sum() + 1) / ((p * m).sum() + (yt * m).sum() + 1)
        opt.zero_grad()
        loss.backward()
        opt.step()
        sched.step()
        if it % log_every == 0 or it == iters - 1:
            print("  step {:4d}/{}  loss {:.3f}  {:.0f} s".format(it + 1, iters, loss.item(), time.time() - t0), flush=True)
    return time.time() - t0


def predict_array(torch, net, x, dev, scale, tile=512):
    """Plant probability of a (bands, H, W) normalized array, in overlapping windows."""
    net.eval()
    xs = half(x, scale)
    C, H, W = xs.shape
    out = np.zeros((H, W), np.float32)
    pad = 16
    with torch.no_grad():
        for y0 in range(0, H, tile):
            for x0 in range(0, W, tile):
                a, b = max(0, y0 - pad), max(0, x0 - pad)
                c, d = min(H, y0 + tile + pad), min(W, x0 + tile + pad)
                win = xs[:, a:c, b:d]
                h, w = win.shape[1:]
                ph, pw = (-h) % 8, (-w) % 8
                win = np.pad(win, ((0, 0), (0, ph), (0, pw)), mode="reflect" if min(h, w) > max(ph, pw) else "edge")
                p = torch.sigmoid(net(torch.from_numpy(np.ascontiguousarray(win))[None].to(dev)))[0, 0].float().cpu().numpy()[:h, :w]
                out[y0:min(H, y0 + tile), x0:min(W, x0 + tile)] = p[y0 - a:y0 - a + min(tile, H - y0), x0 - b:x0 - b + min(tile, W - x0)]
    if scale > 1:                           # back to the full grid (edge rows repeated when the size is odd)
        H0, W0 = x.shape[1:]
        out = np.repeat(np.repeat(out, scale, 0), scale, 1)[:H0, :W0]
        out = np.pad(out, ((0, H0 - out.shape[0]), (0, W0 - out.shape[1])), mode="edge")
    return out


def read_image(path, bands):
    """The image with PPE's band detection and scaling, bands in the model's order."""
    from ppe_qgis.native_engine import NativePPEEngine
    eng = NativePPEEngine(log=lambda m: None)
    src = {"source_mode": "folder", "folder_path": path} if os.path.isdir(path) else {"source_mode": "stacked", "stacked_path": path}
    eng._resolve(dict(src, output_dir=os.path.dirname(os.path.abspath(path)), work_dir=os.path.join(os.path.dirname(os.path.abspath(path)), "ppe_work")))
    missing = [b for b in bands if b not in eng.sources]
    if missing:
        sys.exit("The image has no {} band(s); the model needs {}.".format(", ".join(missing), ", ".join(bands)))
    got = eng._read_bands(bands)
    return eng, np.nan_to_num(np.stack([got[b] for b in bands]).astype(np.float32), nan=0.0)


def save_model(torch, net, path, meta):
    torch.save({"state": {k: v.cpu() for k, v in net.state_dict().items()}, **meta}, path)
    print("Model saved:", path)


def load_model(torch, path, dev):
    ck = torch.load(path, map_location="cpu", weights_only=True)
    net = unet(torch, len(ck["bands"]))
    net.load_state_dict(ck["state"])
    return net.to(dev), ck


def cmd_train(a, init=None):
    torch = _torch()
    dev = device(torch)
    torch.manual_seed(0)
    np.random.seed(0)
    first = json.load(open(os.path.join(a.folders[0], "manifest.json")))
    bands = init["bands"] if init else first["bands"]
    ranges = init["ranges"] if init else first["band_ranges_p1_p99"]
    scale = a.scale
    X, Y, VX, VY = [], [], [], []
    for k, f in enumerate(a.folders):
        m, x, y = load_folder(f, ranges, scale, "train")
        if m["bands"] != bands:
            sys.exit("{} has bands {}, the model uses {}.".format(f, m["bands"], bands))
        if k > 0 and a.old_share < 1 and len(x):          # update: a sample of the old images
            keep = np.random.rand(len(x)) < a.old_share
            x = [v for v, s in zip(x, keep) if s]
            y = [v for v, s in zip(y, keep) if s]
        X += x
        Y += y
        _m, vx, vy = load_folder(f, ranges, scale, "val")
        VX += vx
        VY += vy
    print("Training on {} tiles ({} validation) with {} at 1/{} resolution, {} steps, bands {}.".format(len(X), len(VX), dev.type, scale, a.iters, ", ".join(bands)))
    net = unet(torch, len(bands)).to(dev)
    if init:
        net.load_state_dict(init["state"])
    secs = fit(torch, net, X, Y, a.iters, dev)
    # Baseline for 'check': how the model agrees with PPE's sure pixels on its own validation tiles.
    sp, ss = [], []
    for x, y in zip(VX, VY):
        p = predict_array(torch, net, x, dev, 1) >= 0.5
        s1, s0 = agreement(p, y)
        sp.append(s1)
        ss.append(s0)
    base = {"sure_plant_agreement": float(np.nanmean(sp)) if sp else None, "plant_on_sure_soil": float(np.nanmean(ss)) if ss else None}
    print("Trained in {:.0f} s. On validation tiles: {:.1f} % of PPE's sure plant found, {:.2f} % of PPE's sure soil called plant.".format(
        secs, 100 * (base["sure_plant_agreement"] or 0), 100 * (base["plant_on_sure_soil"] or 0)))
    save_model(torch, net, a.out, {"bands": bands, "ranges": ranges, "scale": scale, "baseline": base, "trained_on": [os.path.abspath(f) for f in a.folders],
                                   "iters": a.iters, "seconds": secs, "ppe": "4.3.0"})


def cmd_predict(a):
    torch = _torch()
    dev = device(torch)
    net, ck = load_model(torch, a.model, dev)
    eng, x = read_image(a.image, ck["bands"])
    t = time.time()
    p = predict_array(torch, net, norm(x, ck["ranges"]), dev, ck["scale"])
    out = a.out or os.path.splitext(os.path.abspath(a.image.rstrip("/")))[0] + "_model_plant_mask.tif"
    ds = gdal.GetDriverByName("GTiff").Create(out, p.shape[1], p.shape[0], 1, gdal.GDT_Byte, options=["COMPRESS=DEFLATE", "TILED=YES"])
    ds.SetGeoTransform(eng.template.GetGeoTransform())
    ds.SetProjection(eng.template.GetProjection())
    b = ds.GetRasterBand(1)
    b.WriteArray((p >= 0.5).astype(np.uint8))
    b.SetNoDataValue(255)
    ds = None
    print("Plant mask (1 plant, 0 soil) written in {:.1f} s: {}  -  canopy {:.1f} %".format(time.time() - t, out, 100 * float((p >= 0.5).mean())))
    return p


def cmd_check(a):
    from ppe_qgis.training_data import label_array
    torch = _torch()
    dev = device(torch)
    net, ck = load_model(torch, a.model, dev)
    eng, x = read_image(a.image, ck["bands"])
    p = predict_array(torch, net, norm(x, ck["ranges"]), dev, ck["scale"]) >= 0.5
    vote = gdal.Open(os.path.join(a.run, "vote_fraction.tif")).ReadAsArray().astype(np.float32)
    y = label_array(vote, gdal.Open(os.path.join(a.run, "ppe_findings.tif")).ReadAsArray())
    sp, ss = agreement(p, y)
    base = ck.get("baseline") or {}
    bp, bs = base.get("sure_plant_agreement"), base.get("plant_on_sure_soil")
    print("New flight: {:.1f} % of PPE's sure plant found (at training {}), {:.2f} % of PPE's sure soil called plant (at training {}).".format(
        100 * sp, "{:.1f} %".format(100 * bp) if bp is not None else "?", 100 * ss, "{:.2f} %".format(100 * bs) if bs is not None else "?"))
    drop = bp is not None and sp < bp - 0.05
    extra = bs is not None and ss > 2 * bs + 0.005
    if drop or extra:
        print("CHECK: the model has drifted ({}). Update it: make training data from the new images on the PPE Training data tab, then\n"
              "  python ppe_train.py update {} <new training data folder> <old training data folder(s)>".format(
                  "; ".join(w for w in ("it misses plants PPE is sure of" if drop else "", "it calls soil plant" if extra else "") if w), a.model))
        return 1
    print("OK: the model agrees with PPE as well as when it was trained.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("train")
    t.add_argument("folders", nargs="+")
    t.add_argument("--out", default="ppe_model.pt")
    t.add_argument("--iters", type=int, default=600)
    t.add_argument("--scale", type=int, default=2)
    u = sub.add_parser("update")
    u.add_argument("model")
    u.add_argument("folders", nargs="+", help="new training data folder first, then old ones")
    u.add_argument("--out")
    u.add_argument("--iters", type=int, default=200)
    u.add_argument("--old-share", type=float, default=0.5, dest="old_share")
    p = sub.add_parser("predict")
    p.add_argument("model")
    p.add_argument("image")
    p.add_argument("--out")
    c = sub.add_parser("check")
    c.add_argument("model")
    c.add_argument("image")
    c.add_argument("run")
    a = ap.parse_args(argv)
    if a.cmd == "train":
        a.old_share = 1.0
        return cmd_train(a)
    if a.cmd == "update":
        torch = _torch()
        ck = torch.load(a.model, map_location="cpu", weights_only=True)
        a.scale = ck["scale"]
        a.out = a.out or os.path.splitext(a.model)[0] + "_updated.pt"
        return cmd_train(a, init=ck)
    if a.cmd == "predict":
        cmd_predict(a)
        return 0
    if a.cmd == "check":
        return cmd_check(a)


if __name__ == "__main__":
    sys.exit(main())
