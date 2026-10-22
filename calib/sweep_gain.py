#!/usr/bin/env python3
"""Run on the Pi: same static scene, fixed shutter, dense gain steps; record pixel percentiles.

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

OUT = str(Path(__file__).resolve().parent.parent / "gsweep.json")

# gain settings to sweep
LADDER = [0.5,0.6,0.7,0.8,0.9,1.0,1.05,1.15,1.25,1.35,1.484]   # gain settings
PCT = np.concatenate([np.arange(0.2, 5, 0.2), np.arange(5, 95.5, 0.5),
                      np.arange(95, 99.9, 0.2)])
SHUTTER = 4800      # shutter fixed, only gain is swept
REPEAT = 1
REF = 1.0          # reference gain, revisited every few steps
EVERY = 3           # shuffled order with periodic revisits: otherwise scene drift over time
                    # would look like non-linearity (a monotonic sweep cannot tell them apart)

cfg = rig.load_cfg()
cams = rig.detect()
pub = rig.StdoutPublisher()
srcs = rig.open_all(cfg, cams, pub)
rig.warmup_all(srcs, cfg, pub)

print(f"  {len(LADDER)} steps: {LADDER}", flush=True)
out = {"ladder": LADDER, "pct": PCT.tolist(), "shutter": SHUTTER,
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
        s.set_exposure(SHUTTER, t)
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
    print(f"    {i+1}/{len(plan)}  {kind:3s} gain {t:.3f}", flush=True)

for s in srcs:
    s.close()
json.dump(out, open(OUT, "w"))
print(f"  -> {OUT}", flush=True)
