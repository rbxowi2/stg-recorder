#!/usr/bin/env python3
"""Fit the camera response curve from two sweeps and write oecf.json.

    python calib/fit_oecf.py sweep.json gsweep.json [-o oecf.json]

Model   h(pixel value[k, p]) = s(p) + e(k)

s(p) is the scene luminance of percentile p and e(k) the exposure actually
delivered at step k. Both are free parameters, solved by alternating least
squares.

The model has one scale degree of freedom: multiplying h, s and e by the same
constant still satisfies it. It must therefore be anchored externally,
otherwise the result depends on the starting values (a different start once
changed the fitted shutter exponent from 0.95 to 1.55).

Anchor: during the shutter sweep the shutter is assumed linear (double the
time, double the photons; this is physics, not a fit), so e(k) is fixed to
log2(shutter) and only the curve is solved. What can be identified is how far
e departs from a straight line, which is checked separately from the residuals.

The gain sweep is the opposite: the curve is already fixed, so the gain
factor is determined by the data and is left free.

Measurement: run calib/sweep_shutter.py and calib/sweep_gain.py on the Pi in
front of a static scene, copy the two JSON files back and run this script.
"""
import argparse
import json
from pathlib import Path

import numpy as np

V_LO, V_HI = 10, 240      # pixel range used for fitting, slightly narrower than the real limits
MIN_N = 60                # steps with fewer valid samples are excluded
GRID = np.arange(0, 256, dtype=np.float64)


def pava(y, w):
    """Isotonic regression. The curve must be monotonic or inversion has multiple solutions."""
    y = np.asarray(y, float).copy()
    w = np.asarray(w, float).copy()
    lvl, lw, idx = [], [], []
    for i in range(len(y)):
        lvl.append(y[i]); lw.append(w[i]); idx.append(1)
        while len(lvl) > 1 and lvl[-2] > lvl[-1]:
            v2, w2, n2 = lvl.pop(), lw.pop(), idx.pop()
            v1, w1, n1 = lvl.pop(), lw.pop(), idx.pop()
            tw = w1 + w2
            lvl.append((v1 * w1 + v2 * w2) / max(tw, 1e-12))
            lw.append(tw); idx.append(n1 + n2)
    out, k = np.empty(len(y)), 0
    for v, n_ in zip(lvl, idx):
        out[k:k + n_] = v; k += n_
    return out


def fit(vals, logx, h0, iters=200, free_e=True):
    """vals[k, p] are pixel values, logx[k] = log2 of the setting.

    free_e=False: fix the delivered exposure to the setting (anchors the scale).
    free_e=True:  let it float (meaningful only once the curve is known).
    """
    ok = (vals >= V_LO) & (vals <= V_HI)
    keep = ok.sum(1) >= MIN_N
    vals, ok, logx = vals[keep], ok[keep], logx[keep]
    h, e = h0.copy(), logx.copy()
    for _ in range(iters):
        hv = np.interp(vals, GRID, h)
        with np.errstate(invalid="ignore"):
            s = np.nanmean(np.where(ok, hv - e[:, None], np.nan), 0)
            g = np.isfinite(s)
            if free_e:
                e = np.nanmean(np.where(ok & g, hv - s[None, :], np.nan), 1)
                e -= e.mean() - logx.mean()       # compare shape, not absolute level
        m = ok & g[None, :]
        tgt = (s[None, :] + e[:, None])[m]
        vv = vals[m]
        num = np.zeros(256); den = np.zeros(256)
        lo = np.clip(np.floor(vv).astype(int), 0, 255)
        hi = np.clip(lo + 1, 0, 255)
        fr = vv - lo
        np.add.at(num, lo, tgt * (1 - fr)); np.add.at(den, lo, 1 - fr)
        np.add.at(num, hi, tgt * fr);       np.add.at(den, hi, fr)
        seen = den > 0.5
        h = pava(np.interp(GRID, GRID[seen], num[seen] / den[seen]),
                 np.where(seen, den, 1e-3))
        h -= h[50]                                # output 50 -> relative exposure 1.0
    hv = np.interp(vals, GRID, h)
    with np.errstate(invalid="ignore"):
        s = np.nanmean(np.where(ok, hv - e[:, None], np.nan), 0)
    m = ok & np.isfinite(s)[None, :]
    return h, e, logx, (hv - (s[None, :] + e[:, None]))[m], seen


def drift_report(d, name):
    D = d.get("drift") or []
    if len(D) < 2:
        print(f"  ! {name} has no drift reference; results are not fully reliable")
        return
    pct = np.array(d["pct"])
    worst_med = worst_hi = 0.0
    for c in d["cameras"]:
        base = np.array(D[0]["v"][c])
        m = (base >= V_LO) & (base <= V_HI)
        hi = m & (pct >= 95)          # changing light sources (TV, monitors) show up first in the highlights
        for x in D[1:]:
            dv = np.array(x["v"][c]) - base
            worst_med = max(worst_med, abs(float(np.median(dv[m]))))
            if hi.any():
                worst_hi = max(worst_hi, abs(float(np.median(dv[hi]))))
    print(f"  {name} drift: median {worst_med:.1f}  highlights (p95+) {worst_hi:.1f} pixel values")
    if worst_med > 2.0 or worst_hi > 4.0:
        print("  ! The scene changed during the sweep; measure again, drift masquerades as non-linearity. "
              "Typical causes: TV / monitor / clouds outside the window")


ap = argparse.ArgumentParser()
ap.add_argument("shutter_sweep")
ap.add_argument("gain_sweep")
ap.add_argument("-o", "--out", default="oecf.json")
a = ap.parse_args()

ds = json.load(open(a.shutter_sweep, encoding="utf-8"))
dg = json.load(open(a.gain_sweep, encoding="utf-8"))
drift_report(ds, "shutter sweep")
drift_report(dg, "gain sweep")

# Starting point: a rough log-gamma guess; alternating least squares converges from it
h0 = 2.2 * np.log2(np.maximum(GRID, 1) / 50.0)

# -- shutter: curve + shutter exponent
lad = np.array(ds["ladder"], float)
curves, ps = {}, []
print("\nShutter sweep")
for c in ds["cameras"]:
    vals = np.array([ds["data"][c][str(int(t))] for t in lad])
    h, e, lx, res, seen = fit(vals, np.log2(lad), h0, free_e=False)
    curves[c] = h
    # With the curve fixed, free e to see how far it departs from a line (the identifiable part)
    _, e2, lx2, _, _ = fit(vals, np.log2(lad), h, iters=60, free_e=True)
    dev = e2 - np.polyval(np.polyfit(lx2, e2, 1), lx2)
    ps.append(float(np.abs(dev).max()))
    print(f"  {c}  residual RMS {res.std():.4f} stops   "
          f"max departure of delivered exposure from a line {np.abs(dev).max():.3f} stops")
hm = np.mean([curves[c] for c in ds["cameras"]], 0); hm -= hm[50]
spread = (np.max([curves[c] for c in ds["cameras"]], 0)
          - np.min([curves[c] for c in ds["cameras"]], 0))
print(f"  curve spread between cameras {spread[V_LO:V_HI+1].max():.3f} stops")
dev_max = float(np.max(ps))
print(f"  shutter log-linearity: max departure {dev_max:.3f} stops"
      + ("  ! too large, the shutter is not proportional" if dev_max > 0.15 else "  (linear)"))

# -- gain: solve the real factor of each step with the fitted curve
gl = np.array(dg["ladder"], float)
gains = {}
print("\nGain sweep")
for c in dg["cameras"]:
    vals = np.array([dg["data"][c][str(x)] for x in gl])
    _, e, lx, res, _ = fit(vals, np.log2(gl), hm, free_e=True)
    e = e - np.interp(0.0, lx, e)                 # relative to setting 1.0
    gains[c] = 2.0 ** e
    print(f"  {c}  residual RMS {res.std():.4f} stops")
used = gl[-len(next(iter(gains.values()))):]
gmean = np.mean([gains[c] for c in dg["cameras"]], 0)
print(f"  gain range {gmean.min():.3f}-{gmean.max():.3f} = "
      f"{np.log2(gmean.max()/gmean.min()):.2f} stops")

cur = 2.0 ** hm
lo = int(GRID[seen].min()); hi = int(GRID[seen].max())
out = {
    "measured": __import__("datetime").date.today().isoformat(),
    "cameras": ds["cameras"],
    "valid_output": [max(lo, V_LO), min(hi, V_HI)],
    "curve_mean": [round(float(x), 6) for x in cur],
    "per_camera_curve": {c: [round(float(x), 6) for x in 2.0 ** (curves[c] - curves[c][50])]
                         for c in ds["cameras"]},
    "shutter_linearity_dev_stops": round(dev_max, 4),
    "gain_setting": [float(x) for x in used],
    "gain_effective_mean": [round(float(x), 4) for x in gmean],
    "per_camera_gain": {c: [round(float(x), 4) for x in gains[c]] for c in dg["cameras"]},
}
Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
w = np.log2(cur[out["valid_output"][1]] / cur[out["valid_output"][0]])
print(f"\nvalid output {out['valid_output']}  single-shot window {w:.2f} stops")
print(f"-> {a.out}")
