"""Exposure algorithm. Mostly pure functions, platform-independent.

Seven steps: bracketed shots -> linearize -> scene histogram -> sliding window
-> split into shutter and gain by endpoint interpolation -> clamp and warn ->
apply.

`scene_log_exposure`, `place_window`, `compose` and `decide` do not touch the
cameras and can be re-run offline on existing captures. Only `auto_exposure()`
needs the Source interface (`set_exposure()` and `gray()`).
"""
from __future__ import annotations

import json
from pathlib import Path


# `oecf.json` is a physical property of the cameras, so it belongs to the
# platform layer. The platform sets this variable at startup (see rig.py).
OECF_PATH = None
_OECF = None


def load_oecf(path=None):
    """Load the camera response curve (OECF).

    It is a property of this camera model: measured once, then fixed.
    """
    global _OECF
    if _OECF is None:
        p = Path(path) if path else OECF_PATH
        if p is None:
            raise SystemExit("  x core.exposure.OECF_PATH is not set"
                             " - the platform layer must point to oecf.json")
        _OECF = json.loads(p.read_text(encoding="utf-8"))
    return _OECF


# Valid output range. This is the range that was actually measured; the curve
# has no support outside it.
V_LO, V_HI = 10, 236


def exposure_amount(t_us, g_effective, oecf):
    """Shutter and gain -> exposure amount E (in "microsecond equivalents").

    The shutter is linear (double the time, double the photons). Gain is not,
    so the gain setting must first be converted to its real factor with
    `gain_effective_for()`; the setting value must never be multiplied directly.

    Measured shutter log-linearity deviates at most 0.10 stops from a straight
    line (`shutter_linearity_dev_stops` in oecf.json), so the product is valid.
    """
    return float(t_us) * float(g_effective)


def gain_setting_for(effective, oecf):
    """Convert a desired gain factor back to the AnalogueGain setting.

    The gain setting is not a factor: a setting of 1.484 amplifies 3.71x
    (measured). The setting value must never be used as a factor.
    """
    import numpy as np
    xs = oecf["gain_effective_mean"]
    ys = oecf["gain_setting"]
    return float(np.interp(effective, xs, ys))


def gain_effective_for(setting, oecf):
    import numpy as np
    return float(np.interp(setting, oecf["gain_setting"],
                           oecf["gain_effective_mean"]))


def exposure_limits(cfg, oecf):
    """Range of exposure amount E (= shutter us x effective gain) and window width.

    Returns (E_min, E_max, window width in stops).
    """
    import math
    t_lo = float(cfg.get("ae_min_exposure_us", 2500))
    t_hi = float(cfg["ae_max_exposure_us"])
    g_lo = oecf["gain_effective_mean"][0]
    g_hi = oecf["gain_effective_mean"][-1]
    c = oecf["curve_mean"]
    width = math.log2(c[V_HI] / c[V_LO])
    return (exposure_amount(t_lo, g_lo, oecf),
            exposure_amount(t_hi, g_hi, oecf), width)


def compose(E, cfg, oecf):
    """Split an exposure amount E into exactly one shutter/gain pair.

    Endpoint interpolation: f=0 is the shortest shutter with the lowest gain,
    f=1 the longest shutter with the highest gain, and both move together with
    the same f. A given E therefore maps to a single combination, so cameras
    never diverge (one choosing a long shutter, another a high gain).

    Dim scenes land near f=1 (long shutter, high gain) and bright scenes near
    f=0 (short shutter, low gain), which is the desired behavior.
    """
    import math
    E_min, E_max, _ = exposure_limits(cfg, oecf)
    t_lo = float(cfg.get("ae_min_exposure_us", 2500))
    t_hi = float(cfg["ae_max_exposure_us"])
    g_lo = oecf["gain_effective_mean"][0]
    g_hi = oecf["gain_effective_mean"][-1]
    # E is still log-linear in f (the exponent only changes the slope), so f
    # can be solved directly.
    E = min(max(E, E_min), E_max)
    f = math.log2(E / E_min) / math.log2(E_max / E_min)
    t = t_lo * (t_hi / t_lo) ** f
    g_eff = g_lo * (g_hi / g_lo) ** f
    return int(round(t)), gain_setting_for(g_eff, oecf), g_eff, f


# --------------------------------------------------------------- pure functions
# The three functions below do not touch the cameras.

def scene_log_exposure(shots, curve):
    """Recover the scene's own luminance distribution from a bracket.

    The result is independent of the exposure settings.

    shots: [(grayscale array, exposure amount E of that shot), ...]

    For each pixel the shot closest to mid-gray is used, since it has the best
    signal-to-noise ratio and is away from both ends of the curve:

        scene luminance L = curve[pixel value] / E

    Returns a 1-D array of log2(L), only for pixels that fall inside the valid
    range in at least one shot.
    """
    import numpy as np
    c = np.asarray(curve, dtype=np.float64)
    best_d, best_L = None, None
    for px, E in shots:
        v = np.asarray(px).reshape(-1)
        d = np.abs(v.astype(np.int16) - 128).astype(np.float32)
        d[(v < V_LO) | (v > V_HI)] = np.inf          # both ends are unreliable
        L = c[v] / E
        if best_d is None:
            best_d, best_L = d, L
        else:
            take = d < best_d
            best_d = np.where(take, d, best_d)
            best_L = np.where(take, L, best_L)
    ok = np.isfinite(best_d)
    if not ok.any():
        return None
    return np.log2(best_L[ok])


def place_window(logL, width, step=0.05):
    """Slide a fixed-width exposure window over the scene distribution.

    Picks the position with the smallest combined over- and under-exposure.
    Both count equally; a centered window is the outcome of this rule for a
    symmetric distribution, not a separate rule.

    Returns (window lower edge in log2 L, loss %, over %, under %).
    """
    import numpy as np
    x = np.sort(logL)
    n = x.size
    lo, hi = x[0], x[-1]
    if hi - lo <= width:                              # scene fits in the window
        p = (lo + hi - width) / 2.0
        return float(p), 0.0, 0.0, 0.0
    grid = np.arange(lo, hi - width + step, step)
    under = np.searchsorted(x, grid, "left")          # count below p
    over = n - np.searchsorted(x, grid + width, "right")
    loss = under + over
    m = loss.min()
    tie = np.flatnonzero(loss == m)
    k = int(tie[tie.size // 2])                       # middle of ties, avoids jitter
    return (float(grid[k]), 100.0 * m / n,
            100.0 * over[k] / n, 100.0 * under[k] / n)


def decide(shots, cfg, oecf):
    """Bracket result -> suggested exposure. Pure function, re-runnable offline."""
    import math
    curve = oecf["curve_mean"]
    logL = scene_log_exposure(shots, curve)
    if logL is None:
        return None
    E_min, E_max, width = exposure_limits(cfg, oecf)
    p, loss, over, under = place_window(logL, width)
    # Align the window's lower edge to output value V_LO: E x L_lo = curve[V_LO]
    E = curve[V_LO] / (2.0 ** p)
    verdict = "ok"
    if E > E_max:
        verdict = "underlit"
    elif E < E_min:
        verdict = "overlit"
    t, g_set, g_eff, f = compose(E, cfg, oecf)
    return {"exposure_us": t, "gain": round(g_set, 3),
            "gain_effective": round(g_eff, 3),
            "mode": "window_apex", "f": round(f, 3),
            "scene_stops": round(float(logL.max() - logL.min()), 2),
            "window_stops": round(width, 2),
            "window_lo_log2L": round(p, 3),
            "E_wanted": round(E, 1),
            "E_used": round(exposure_amount(t, g_eff, oecf), 1),
            "loss_pct": round(loss, 2), "over_pct": round(over, 2),
            "under_pct": round(under, 2),
            "pixels": int(logL.size), "verdict": verdict}


def auto_exposure(sources, cfg, pub, apply=True):
    """The seven-step exposure procedure.

    1. bracket (gain fixed, shutter swept)   2. linearize with the response curve
    3. scene histogram   4. window with minimum loss   5. split into shutter + gain
    6. clamp and warn    7. apply to all cameras
    """
    import math
    oecf = load_oecf()
    br = [int(x) for x in cfg.get("ae_bracket_us", [500, 4000, 31300])]
    g_set = float(cfg.get("ae_bracket_gain", 1.0))
    g_eff = gain_effective_for(g_set, oecf)
    E_min, E_max, width = exposure_limits(cfg, oecf)

    pub.message(f"  Bracketed metering: shutter {'/'.join(str(x) for x in br)} us, "
                f"gain fixed at {g_set:.2f} (actual {g_eff:.2f}x)", oled=False)

    shots = []
    for t in br:
        for s in sources:
            s.set_exposure(t, g_set)
        got = 0
        for s in sources:
            px = s.gray()
            if px is not None:
                shots.append((px, exposure_amount(t, g_eff, oecf)))
                got += 1
        pub.message(f"    {t/1000:7.2f} ms  captured {got}/{len(sources)} cameras", oled=False)

    if not shots:
        pub.message("  ! Every metering capture failed; settings unchanged", oled=False)
        return None

    r = decide(shots, cfg, oecf)
    if r is None:
        pub.message("  ! No pixel fell in the valid range; settings unchanged", oled=False)
        return None

    if apply:
        for s in sources:
            s.set_exposure(r["exposure_us"], r["gain"])
        cfg["capture"]["exposure_us"] = r["exposure_us"]
        cfg["capture"]["gain"] = r["gain"]

    pub.message(f"  Scene dynamic range {r['scene_stops']:.1f} stops, "
                f"single-shot window {r['window_stops']:.1f} stops", oled=False)
    pub.message(f"  Window position: over {r['over_pct']:.2f}%  under {r['under_pct']:.2f}%"
                f"  total loss {r['loss_pct']:.2f}% (minimum)", oled=False)
    pub.message(f"  {'Applied' if apply else 'Suggested'} "
                f"{r['exposure_us']/1000:.2f} ms / gain {r['gain']:.3f}"
                f" (actual {r['gain_effective']:.2f}x, f={r['f']:.2f})", oled=False)
    pub.message(f"     Motion blur: an object moving 100 px/s smears "
                f"{100*r['exposure_us']/1e6:.2f} px", oled=False)

    if r["verdict"] == "underlit":
        pub.message(f"     ! Scene too dark: required exposure {r['E_wanted']:.0f} exceeds the "
                    f"camera maximum {E_max:.0f}. Add light; more gain only adds noise", oled=False)
    if r["verdict"] == "overlit":
        pub.message(f"     ! Scene too bright: required exposure {r['E_wanted']:.0f} is below the "
                    f"camera minimum {E_min:.0f}. Reduce light (ND filter or avoid the source)", oled=False)
    if r["scene_stops"] > r["window_stops"]:
        pub.message(f"     ! Scene is {r['scene_stops']-r['window_stops']:.1f} stops wider than "
                    f"the window; both ends cannot be kept, minimum-loss position used", oled=False)

    return r
