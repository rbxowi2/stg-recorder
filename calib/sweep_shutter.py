#!/usr/bin/env python3
"""Run on the Pi: same static scene, fixed gain, dense shutter steps; record pixel percentiles.

Only the percentile table is copied back (tens of KB), not the images.
Percentiles are all that is needed: in a static scene, the same percentile
corresponds to the same scene luminance.
"""
import json
import sys

import numpy as np
from pathlib import Path

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
import rig

OUT = str(Path(__file__).resolve().parent.parent / "sweep.json")

# 1/3-stop steps from 300 to 32000 us (6.7 stops), rounded to 100 us
LADDER = sorted({int(round(300 * 2 ** (k / 3) / 100) * 100) for k in range(22)})
LADDER = [t for t in LADDER if 100 <= t <= 32000]
PCT = np.concatenate([np.arange(0.2, 5, 0.2), np.arange(5, 95.5, 0.5),
                      np.arange(95, 99.9, 0.2)])
GAIN = 1.0
REPEAT = 1
REF = 4800          # reference shutter, revisited every few steps
EVERY = 3           # shuffled order with periodic revisits: otherwise scene drift over time
                    # would look like non-linearity (a monotonic sweep cannot tell them apart)

cfg = rig.load_cfg()
cams = rig.detect()
pub = rig.StdoutPublisher()
srcs = rig.open_all(cfg, cams, pub)
rig.warmup_all(srcs, cfg, pub)

print(f"  {len(LADDER)} steps: {LADDER}", flush=True)
out = {"ladder": LADDER, "pct": PCT.tolist(), "gain_setting": GAIN,
       "cameras": [s.cam["name"] for s in srcs], "data": {}, "rep": {}}

import random
order = LADDER[:]
random.Random(7).shuffle(order)
plan = []
for i, t in enumerate(order):
    if i % EVERY == 0:
        plan.append(("ref", REF))
    plan.append(("m", t))
plan.append(("ref", REF))
out["drift"] = []

for i, (kind, t) in enumerate(plan):
    for s in srcs:
        s.set_exposure(t, GAIN)
    rec = {}
    for s in srcs:
        g = s.gray(skip=12)
        if g is None:
            continue
        rec[s.cam["name"]] = np.percentile(g.reshape(-1), PCT).tolist()
    if kind == "ref":
        out["drift"].append({"step": i, "v": rec})
    else:
        for k, v in rec.items():
            out["data"].setdefault(k, {})[str(t)] = v
    print(f"    {i+1}/{len(plan)}  {kind:3s} {t:6d} us", flush=True)

for s in srcs:
    s.close()
json.dump(out, open(OUT, "w"))
print(f"  -> {OUT}", flush=True)
