#!/usr/bin/env python3
"""check_ui_golden.py - compare the terminal layout with a golden output.

    python tools/check_ui_golden.py            # compare
    python tools/check_ui_golden.py --update   # rebuild the reference (only for intended layout changes)

Running the program twice and diffing the output does not work: it is full of
values that change on every run (fps, byte rate, free space, timestamps).
Instead, fixed data is passed to the layout functions in `rig.py`, which only
format text (no input, no hardware, no state), giving fully deterministic output.

This does not check the flow (which step asks what, in which order); that
needs one pass on the device.

When adding a screen, add a case to `cases()`, run `--update`, and record why
the reference changed. A reference that changes silently is no reference.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
GOLDEN = HERE / "tools" / "golden_ui.txt"
sys.path.insert(0, str(HERE))

import rig                                                       # noqa: E402


# ---------------------------------------------------------------- fixed data
# These numbers are fake and fixed on purpose; real measurements vary.
CFG = {
    "capture": {"resolution": [1920, 1080], "exposure_us": 3300,
                "gain": 1.0, "white_balance_k": 4600},
    "warmup_s": 6.0, "default_duration_s": 30.0,
    "auto_exposure": False,
    "ae_min_exposure_us": 1000, "ae_max_exposure_us": 8000,
    "expected_cameras": ["cam_A", "cam_B", "cam_C", "cam_D"],
    "dest_root": "captures", "tmp_root": "/dev/shm/rig",
    "settle_reminder_s": 30,
}

CAMS = [
    {"name": "cam_A", "node": "/dev/video2", "controller": "xhci-hcd.0"},
    {"name": "cam_B", "node": "/dev/video6", "controller": "xhci-hcd.0"},
    {"name": "cam_C", "node": "/dev/video0", "controller": "xhci-hcd.1"},
    {"name": "cam_D", "node": "/dev/video4", "controller": "xhci-hcd.1"},
]

WARM = {f"cam_{c}": {"fps": 30.0, "bytes_per_second": 8000000.0,
                     "frames": 180, "bytes": 48000000}
        for c in "ABCD"}

AE = {"scene_stops": 3.1, "window_stops": 2.0, "loss_pct": 0.82}


class FakeSrc:
    def __init__(self, name):
        self.name = name


SRCS = [FakeSrc(f"cam_{c}") for c in "ABCD"]


def cases():
    """(title, generated lines). Add new screens here."""
    yield "camera list", rig.cams_lines(CAMS)
    yield "camera list (none found)", rig.cams_lines([])
    yield "main menu (manual exposure)", rig.main_menu_lines(CFG, SRCS, WARM, None)
    yield "main menu (auto exposure)", rig.main_menu_lines(CFG, SRCS, WARM, AE)
    yield "parameters", rig.params_lines(CFG)
    cfg2 = {**CFG, "auto_exposure": True, "expected_cameras": []}
    yield "parameters (auto exposure on, no expected cameras)", rig.params_lines(cfg2)


def normalise(text):
    """Replace the two things unrelated to layout with markers.

        install path   differs between machines; one reference must work everywhere
        core version   changes often and has nothing to do with the layout

    Do not add more markers; each one checks a little less.
    """
    from core import CORE_VERSION, CORE_DATE
    return (text.replace(str(rig.HERE), "<HERE>")
                .replace("\\", "/")
                .replace(f"v{CORE_VERSION} ({CORE_DATE})", "v<VER> (<DATE>)"))


def build():
    out = []
    for title, lines in cases():
        out.append(f"=== {title} ===")
        out.extend(lines)
        out.append("")
    return normalise("\n".join(out).replace("\r\n", "\n")) + "\n"


def main():
    text = build()
    if "--update" in sys.argv:
        GOLDEN.write_text(text, encoding="utf-8")
        print(f"  Reference rebuilt: {GOLDEN.relative_to(HERE)}"
              f" ({len(text.splitlines())} lines)")
        return 0
    if not GOLDEN.exists():
        print(f"  x Reference {GOLDEN.relative_to(HERE)} not found. "
              f"Run with --update first")
        return 1
    want = GOLDEN.read_text(encoding="utf-8")
    if text == want:
        print(f"  OK terminal layout matches the reference ({len(text.splitlines())} lines).")
        return 0
    import difflib
    print("  x The terminal layout changed:\n")
    for ln in difflib.unified_diff(want.splitlines(), text.splitlines(),
                                   "reference", "now", lineterm="", n=1):
        print("   " + ln)
    print("\n     If intended, run --update to rebuild the reference.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
