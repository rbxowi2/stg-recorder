#!/usr/bin/env python3
"""rig.py - entry point of the recorder (Raspberry Pi 5 implementation).

    stg-recorder/       copy the whole folder to the target machine and run
      core/             platform-independent recording core
      pi/               Raspberry Pi 5 implementation: Picamera2, GPIO, OLED, I2S
      rig.py            this file: entry point and menus
      oecf.json         response curve of the cameras
      calib/            tools to re-measure the response curve

Porting to other hardware or cameras only requires replacing `pi/` and
`oecf.json`; `core/` and the data format stay the same.
Deployment and dependencies are described in README.md.

    python3 rig.py              interactive menu (over SSH)
    python3 rig.py --rec 30     record 30 seconds directly
    python3 rig.py --calib      go straight to calibration-set mode

There is no "field mode" flag. The OLED and the GPIO buttons are used when
present and silently skipped otherwise; keyboard and buttons work at the same
time, both are event sources for the same state machine.

Both modes share the same cameras and the same state machine:

    video  ->  <cam>/stream.mjpg + index.jsonl     start / stop
    calib  ->  <cam>/shot_NNNN.jpg                 one set per trigger

Output is written to /tmp (tmpfs) first and moved to captures/ next to this
file afterwards; the source is deleted only after every file is verified.
Folder names end with a timestamp.

Extension points (interfaces in `core/`, implementations in `pi/`):

    Trigger          -> ButtonTrigger in pi/buttons.py (stop recording with a button)
    StatusPublisher  -> OledRenderer in pi/oled_ui.py (screen)
    Source           -> AudioSource in pi/audio.py (I2S stereo microphones)

The MenuMachine in `core/menu.py` is the single answer to "what is happening
now"; the terminal, the OLED and the buttons all read it. The terminal jumps
with `goto()`, the buttons cycle with `handle()`: the state is shared, the
events are not.

All sources must use the same clock for ts_ns (libcamera SensorTimestamp is
CLOCK_MONOTONIC). For audio, ALSA must use SND_PCM_TSTAMP_TYPE_MONOTONIC.
This cannot be fixed after recording: offsets between clocks drift.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

# `core/` and `pi/` are siblings of this file, so the folder runs as copied.
# The directory is added to sys.path explicitly because the interpreter does
# not do so when this file is loaded by path from another tool.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import CORE_DATE, CORE_VERSION, exposure           # noqa: E402
from core.budget import Budget, free_bytes, probe_write_bps   # noqa: E402
from core.exposure import (auto_exposure, compose, decide,    # noqa: E402,F401
                           exposure_amount, exposure_limits,
                           gain_effective_for, gain_setting_for,
                           load_oecf, place_window, scene_log_exposure)
from core.menu import Action, Event, MenuMachine              # noqa: E402
from core.publishers import StatusPublisher, StdoutPublisher  # noqa: E402
from core.session import CLOCK, SCHEMA, Session, move_session  # noqa: E402,F401
from core.triggers import (EnterKeyTrigger, ImmediateTrigger,  # noqa: E402,F401
                           Trigger, drain_stdin, is_cancel)

from pi.camera import NAMES, VideoSource, detect, setup_camera  # noqa: E402,F401

try:                                   # unbuffered output so menus and progress show immediately
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

HERE = Path(__file__).resolve().parent
# oecf.json is a physical property of these cameras, so it belongs to the platform layer
exposure.OECF_PATH = HERE / "oecf.json"

DEFAULT_CFG = {
    "capture": {"resolution": [1920, 1080], "fps": 30,
                "exposure_us": 3300, "gain": 1.484, "buffer_count": 6},
    "warmup_s": 6,
    "default_duration_s": 10,
    # Recording is written to RAM (`/tmp` is tmpfs) and moved to `dest_root`
    # afterwards, even with an NVMe SSD that has far more average bandwidth
    # than needed. Dropped frames depend on worst-case latency, not average
    # bandwidth: an SSD can stall (garbage collection, exhausted SLC cache,
    # thermal throttling), and a stall longer than the buffer depth loses
    # frames that cannot be recovered. tmpfs does not stall.
    # The cost is that recording length is bounded by the tmpfs size
    # (4 GB is about 103 s); the OLED then shows `tmpfs` as the limit.
    "tmp_root": "/tmp",
    "dest_root": "captures",
    "settle_reminder_s": 30,
    # Automatic camera sleep: after this many idle seconds the cameras are
    # closed (only the cameras; the Pi and the OLED stay on). Running cameras
    # keep producing heat, which matters outdoors without power or cooling.
    # 0 = never sleep (the default); set a number of seconds to enable it.
    "sleep_after_s": 0,
    # Settling delay for calibration shots: wait this long after the button
    # press before shooting. The cameras do not expose at exactly the same
    # instant, so shooting while the rig still shakes corrupts the
    # extrinsics. Used only for the physical buttons (a keyboard over SSH
    # does not touch the rig).
    "calib_delay_s": 3.0,
    # Expected camera list. None = remember the cameras detected on the first
    # run. The number of cameras is not hard-coded.
    "expected_cameras": None,

    # Off by default. 3300 us / gain 1.484 is a verified setting.
    # Re-deriving exposure at every start can pick a worse value (6.6 ms was
    # observed, doubling motion blur) and adds about 20 s of probing.
    # Use "meter exp" from the menu when the scene or lighting changes.
    "auto_exposure": False,
    "lock_white_balance": False,  # off by default; locking made cameras less consistent, see lock_white_balance()
    # Bracketed metering parameters
    "ae_max_exposure_us": 8333,   # shutter upper limit = motion-blur budget (1/120 s)
    "ae_min_exposure_us": 2500,   # shutter lower limit; below this, reduce light instead
    # Metering frames are not used for reconstruction, so they may sweep
    # beyond the motion-blur limit above
    "ae_bracket_us": [500, 4000, 31300],
    "ae_bracket_gain": 1.0,       # gain fixed while bracketing: shutter time is exact, gain carries calibration error
}


# ============================================================ menus and flow
# This section is the Pi command-line app. Recording logic lives in core/.

# config.json holds only the values tuned on this machine. Every other
# setting is the DEFAULT_CFG above and is never written to the file;
# changing those in the parameter menu lasts until the next restart.
#
#    camera_ports    which USB port is cam_A..cam_D (wiring differs per machine)
#    exposure_us     metering result (changes with the scene)
#    gain            same
#    mic_spacing_mm  distance between the two microphones (pi/audio.py default if absent)
#
# The older format (the whole DEFAULT_CFG, exposure under `capture`) is still
# read: only the four values above are taken, the rest is ignored, and the
# next save writes the new format.
CFG_FILE = "config.json"
CFG_BACKUP_DIR = "config_backups"


def load_cfg():
    cfg = copy.deepcopy(DEFAULT_CFG)
    p = HERE / CFG_FILE
    if not p.exists():
        return cfg                  # first run: saved once ensure_ports() has built the mapping
    raw = json.loads(p.read_text(encoding="utf-8"))
    cap = raw.get("capture") or {}  # older format
    for k in ("exposure_us", "gain"):
        v = raw.get(k, cap.get(k))
        if v is not None:
            cfg["capture"][k] = v
    if raw.get("camera_ports"):
        cfg["camera_ports"] = dict(raw["camera_ports"])
        # derived from the port mapping, not stored separately (two copies would diverge)
        cfg["expected_cameras"] = [cfg["camera_ports"][k]
                                   for k in sorted(cfg["camera_ports"])]
    if raw.get("mic_spacing_mm") is not None:
        cfg["mic_spacing_mm"] = raw["mic_spacing_mm"]
    return cfg


def persisted(cfg):
    """The values written to config.json."""
    out = {}
    if cfg.get("camera_ports"):
        out["camera_ports"] = dict(sorted(cfg["camera_ports"].items()))
    out["exposure_us"] = cfg["capture"]["exposure_us"]
    out["gain"] = cfg["capture"]["gain"]
    if cfg.get("mic_spacing_mm") is not None:
        out["mic_spacing_mm"] = cfg["mic_spacing_mm"]
    return out


def save_cfg(cfg):
    """Write config.json and, at the same time, a timestamped copy in config_backups/.

    Every backup is kept (each is under 1 KB), so a damaged or overwritten
    config.json can always be restored.
    """
    text = json.dumps(persisted(cfg), indent=2, ensure_ascii=False) + "\n"
    (HERE / CFG_FILE).write_text(text, encoding="utf-8")
    try:
        bdir = HERE / CFG_BACKUP_DIR
        bdir.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        b, n = bdir / f"config_{stamp}.json", 2
        while b.exists():                       # two saves within one second
            b, n = bdir / f"config_{stamp}_{n}.json", n + 1
        b.write_text(text, encoding="utf-8")
    except OSError as e:
        print(f"  ! config backup could not be written (the config itself was saved): {e}")


def open_all(cfg, cams, pub):
    """Open every camera. Returns the sources that opened successfully."""
    for c in cams:
        setup_camera(c["node"])
    srcs = []
    pub.message(f"  OPEN\n  {len(cams)} cameras...")
    for c in cams:
        s = VideoSource(c)
        try:
            s.open(cfg["capture"])
            srcs.append(s)
        except Exception as e:                        # noqa: BLE001
            pub.message(f"  FAIL\n  {c['name']}\n  {e!r}")
    return srcs


def warmup_all(srcs, cfg, pub):
    """Warm up and measure the actual byte rate (all cameras in parallel).

    Warm-up is a transient state: this function restores the previous state
    when it finishes, so no caller has to remember to do it.
    """
    m = get_menu()
    before = m.state
    m.update(phase="warmup")
    warm_s = float(cfg.get("warmup_s", 6))
    # oled=False: the state machine already has a WARMUP screen
    pub.message(f"  WARMUP {warm_s:.0f}s\n  measuring byte rate", oled=False)
    res, ths = {}, []
    for s in srcs:
        t = threading.Thread(
            target=lambda src=s: res.__setitem__(src.name, src.warmup(warm_s)))
        t.start()
        ths.append(t)
    for t in ths:
        t.join()
    m.state = before                      # restore (idle, exposure, ...)
    return res


def lock_white_balance(srcs, cfg, pub):
    """After warm-up, lock every camera's white balance to one common value.

    Intent: exposure is already locked (`AeEnable: False`) while white
    balance is automatic, and the cameras converge to different colour
    temperatures (4600 / 4000 K measured). A common value is used instead of
    locking each camera separately, because a cast shared by all cameras can
    be corrected later and per-camera casts cannot.
    AWB first converges on the real scene (hence after warm-up); the median
    of the readings is then locked.

    Measured result: locking made the cameras less consistent, so this is
    off by default.

    | between cameras, same instant | max R/G diff | max B/G diff |
    |---|---|---|
    | locked at 4000 K | 0.117 | 0.081 |
    | automatic (control 1) | 0.055 | 0.066 |
    | automatic (control 2) | 0.099 | 0.033 |

    `white_balance_temperature` is a setting, not a measurement: each camera
    approaches the same temperature, but sensor differences produce
    different colours. Automatic white balance looks at the scene and
    partly compensates for those differences. Colour cast was also found to
    be uncorrelated with reconstruction quality.

    Real consistency would need per-camera R/G and B/G normalization, which
    belongs to calibration, not to every recording. Kept for reference.
    """
    if not cfg.get("lock_white_balance", True):
        return None
    ks = [(s.name, s.wb_read()) for s in srcs]
    vals = [k for _, k in ks if k]
    if not vals:
        pub.message("  WB\n  read failed, stays auto", oled=False)
        return None
    common = int(sorted(vals)[len(vals) // 2])
    ok = [s.name for s in srcs if s.wb_lock(common)]
    pub.message("  WB " + " ".join(f"{n}={k}" for n, k in ks)
                + f"\n  locked {common}K"
                + ("" if len(ok) == len(srcs) else f" ({len(ok)}/{len(srcs)} only)"),
                oled=False)          # diagnostic detail, does not change any action in the field
    cfg["capture"]["white_balance_k"] = common
    return common


def startup(cfg, cams, pub, reuse_warm=None):
    """Startup: open cameras -> automatic exposure -> warm-up (when needed).

    Warm-up runs only at boot. Reopening the cameras after a recording or a
    calibration set, and waking from sleep, reuse the byte rate measured at
    boot (`reuse_warm`); waiting 6 s every time is not worth it, as the
    number changes little in the short term.

    Cost: the byte rate varies 3-7x with scene brightness, so after a large
    scene change the estimate (and the "seconds left") drifts. Use "warm up"
    from the menu to re-measure manually.
    """
    srcs = open_all(cfg, cams, pub)
    if not srcs:
        return [], {}, None
    ae = auto_exposure(srcs, cfg, pub) if cfg.get("auto_exposure", False) else None
    if ae:
        save_cfg(cfg)                                 # store the measured exposure/gain
        cfg["_metering"] = ae                         # also written to session.json
    else:
        c = cfg["capture"]
        for s_ in srcs:
            s_.set_exposure(c["exposure_us"], c["gain"])
    if reuse_warm:
        # Reuse the boot measurement instead of waiting 6 s. Only boot passes None.
        return srcs, reuse_warm, None
    warm = warmup_all(srcs, cfg, pub)
    lock_white_balance(srcs, cfg, pub)     # after warm-up, so AWB has converged
    prewarm_extras(cfg, pub)               # audio measurement and disk probe also happen here
    nominal = float(cfg["capture"].get("fps", 30))

    # The first warm-up is unreliable: when all cameras start together, USB
    # bandwidth is still being negotiated and UVC controls are still taking
    # effect, giving an inflated byte rate and starved fps (measured: 39.7 MB/s
    # with one camera at 11.7 fps; a second warm-up gave 35.3 MB/s with all
    # cameras normal). An abnormal result triggers one more measurement.
    if any(v["fps"] < nominal * 0.8 for v in warm.values()):
        pub.message("  LOW FPS\n  USB still negotiating\n  warming up again")
        warm = warmup_all(srcs, cfg, pub)

    tot = sum(v["bytes_per_second"] for v in warm.values())
    slow = {n: v["fps"] for n, v in warm.items() if v["fps"] < nominal * 0.8}
    # The first line of a message is the OLED title and holds only the state.
    # oled=False: the idle screen already shows the camera count and seconds left;
    # fps and byte rate are diagnostics for the terminal.
    pub.message(f"  READY\n  {len(srcs)}cam {tot/1048576:.1f}MB/s\n  "
                + " ".join(f"{n.rsplit('_', 1)[-1]}{v['fps']:.1f}"
                          for n, v in sorted(warm.items())), oled=False)
    if slow:
        # USB 2.0 isochronous bandwidth is limited. High gain amplifies noise,
        # JPEGs grow, the bandwidth runs out and some cameras cannot deliver
        # frames (measured: lens covered, 7 MB/s, all cameras at 30 fps; lights
        # on with maximum gain, 41 MB/s, one camera down to 4 fps).
        # These lines are the only diagnosis visible in the field, so they fit
        # on the OLED (about 21 characters per line).
        pub.message("  USB BANDWIDTH LOW\n  "
                    + " ".join(f"{n.rsplit('_', 1)[-1]}{f:.0f}"
                               for n, f in slow.items())
                    + f" want {nominal:.0f}\n"
                    + f"  {tot/1048576:.1f}MB/s "
                    + f"{tot/len(srcs)/nominal/1024:.0f}KB/frame\n"
                    + "  fix: light > gain > res")
    return srcs, warm, ae


# One publisher per process. The OLED is a physical device; two publishers
# would mean two threads writing to the same I2C address.
_PUB = None
_MENU = None


def get_menu():
    """The single state machine of this process.

    It is the only answer to "what is happening now"; the terminal, the OLED
    and the buttons all read it. Every place that changes what is happening
    must call `update(phase=...)`.
    """
    global _MENU
    if _MENU is None:
        _MENU = MenuMachine()
    return _MENU


def get_pub():
    """Return (creating if needed) the single StatusPublisher of this process.

    The OLED is used when available, with no flag. Failing to open it is not
    fatal: one line explains why and output falls back to the terminal.
    """
    global _PUB
    if _PUB is None:
        _PUB = StdoutPublisher()
        try:
            from pi.oled_ui import OledRenderer
            _PUB = OledRenderer(get_menu(), fallback=_PUB)
        except Exception as e:                        # noqa: BLE001
            _PUB.message(f"  NO OLED\n  stdout only: {e}")
    return _PUB


def new_session(cfg, srcs):
    return Session(cfg, list(srcs), get_pub(),
                   cfg["tmp_root"], HERE / cfg["dest_root"],
                   warmup_fn=warmup_all, write_bps=_WRITE_BPS)


def prompt(text):
    """Print the prompt before reading so it is not buffered behind the input."""
    drain_stdin()
    sys.stdout.write(text)
    sys.stdout.flush()
    try:
        return input().strip()
    except (EOFError, KeyboardInterrupt):
        return "q"


def ask(prompt_text, default=None, cast=str):
    s = prompt(f"{prompt_text}" + (f" [{default}]" if default is not None else "") + ": ")
    if not s:
        return default
    try:
        return cast(s)
    except ValueError:
        print("  Invalid value, using the default")
        return default


def report(rep):
    get_menu().update(
        phase="done" if rep.get("ok") else None,
        error=None if rep.get("ok") else rep.get("error"),
        snapshot={"files": rep.get("files"),
                  "total_mb": (rep.get("bytes") or 0) / 1048576,
                  "dest": str(rep.get("dir") or "")})
    print()
    if rep.get("ok"):
        print(f"  Done -> {rep['dir']}")
        print(f"     {rep['files']} files, {rep['bytes']/1048576:.0f} MB")
    else:
        print(f"  ERROR {rep.get('error')}")
        for f in rep.get("failed", []):
            print(f"     {f}")
    for e in rep.get("errors", []):
        print(f"  ! {e}")


# No measurement should happen after the record button is pressed. The
# cameras' warm-up already runs at boot (Session.arm skips it when `warm` is
# given); these two caches move the audio measurement and the disk write
# probe to boot as well. The only wait left after pressing is the 0.5 s start
# barrier, which must stay (see core/session.py).
_AUDIO_WARM = None          # audio byte-rate measurement
_WRITE_BPS = None           # destination write speed (bytes/s)


def prewarm_extras(cfg, pub):
    """At boot: measure audio and probe disk write speed, and cache the results.

    Only the numbers are cached; the audio device is not held. It is opened
    again when recording, so unplugging and replugging the microphone still
    works and the fallback in `add_audio()` stays effective.

    The audio byte rate is `RATE x BYTES_PER_FRAME`, independent of exposure
    and scene, so it never needs re-measuring after metering.
    """
    global _AUDIO_WARM, _WRITE_BPS
    if cfg.get("audio", True):
        try:
            from pi.audio import AudioSource, MEASURED_RATE_HZ
            a = AudioSource()
            a.open(cfg)
            _AUDIO_WARM = a.warmup(2.0)
            a.close()          # keep the numbers, release the device
            print(f"  Audio pre-check {_AUDIO_WARM['measured_bytes_per_second']/1024:.0f} KB/s"
                  f"  sample rate {MEASURED_RATE_HZ:.1f} Hz (measured at boot)")
        except Exception as e:                        # noqa: BLE001
            _AUDIO_WARM = None
            print(f"  ! Audio pre-check failed ({e}); will retry when recording")
    try:
        _WRITE_BPS = probe_write_bps(HERE / cfg["dest_root"])
        print(f"  Disk write {_WRITE_BPS/1048576:.1f} MB/s (measured at boot)")
    except Exception as e:                            # noqa: BLE001
        _WRITE_BPS = None
        print(f"  ! Disk probe failed ({e}); will retry when recording")


def add_audio(cfg, srcs, warm):
    """Add the I2S audio source to the source list. Video recording only.

    Audio is always optional. If the card is missing or cannot be opened,
    recording continues without sound and a warning is printed; a moment
    that is happening cannot be missed because of a microphone.

    Calibration does not add audio: `Session.shoot()` calls `shot()` on every
    source, and audio has no single-image concept.
    """
    if not cfg.get("audio", True):
        return list(srcs), warm
    try:
        from pi.audio import AudioSource
        a = AudioSource()
        a.open(cfg)
        # Use the boot measurement when available (saves 2 s). open() still
        # runs, so an unplugged device raises here and takes the fallback.
        w = _AUDIO_WARM if _AUDIO_WARM is not None else a.warmup(2.0)
    except Exception as e:                            # noqa: BLE001
        print(f"  ! No audio ({e}); recording video without sound")
        return list(srcs), warm
    from pi.audio import MEASURED_RATE_HZ
    print(f"  Audio hw:{a.card}  {w['measured_bytes_per_second']/1024:.0f} KB/s"
          f"  sample rate {MEASURED_RATE_HZ:.1f} Hz (measured)")
    return list(srcs) + [a], {**warm, a.name: w}


def do_record(cfg, srcs, warm, seconds=None, confirm=True, trigger=None):
    get_menu().update(phase="running")
    srcs, warm = add_audio(cfg, srcs, warm)
    ses = new_session(cfg, srcs)
    try:
        b = ses.arm("video", warm)
    except Exception as e:                            # noqa: BLE001
        print(f"  ERROR preparing: {e}")
        return False
    print()
    for line in b.lines():
        print("  " + line)
    if seconds is None and confirm:
        # This step must also be cancellable: the cameras are already open
        # and warmed up, and changing one's mind here is likely.
        raw = prompt(f"\n  Seconds to record (0 = stop manually, q = cancel) "
                     f"[{cfg['default_duration_s']}]: ")
        if is_cancel(raw):
            print("  Cancelled, nothing was recorded.")
            ses.discard()
            return True
        try:
            seconds = float(raw) if raw else cfg["default_duration_s"]
        except ValueError:
            print("  Invalid value, using the default")
            seconds = cfg["default_duration_s"]
    elif seconds is None:
        seconds = cfg["default_duration_s"]
    print("  " + b.lines(seconds if seconds > 0 else b.max_seconds)[-1])

    if trigger is not None:                           # buttons: stop with a button
        trg = trigger
        print("  Recording (press v to stop)")
    elif confirm:
        print(f"\n  ! If the rig was moved, let it settle for {cfg['settle_reminder_s']} s or more")
        print("     (without settling, 5.4 px of drift was measured, more than the subject's motion)")
        trg = EnterKeyTrigger()
        if not trg.wait_start():
            # Cancelling is reported explicitly so it cannot be mistaken for a hang.
            print("  Cancelled, nothing was recorded.")
            ses.discard()
            return True
        trg.arm_stop()
        print("  Recording (press Enter to stop)")
    else:
        trg = ImmediateTrigger()
        print("  Recording...")
    report(ses.record(seconds, trg))
    return True                                       # finalize() has closed the cameras


def term_shot_input(ses):
    """Terminal version of "shoot another set or finish". Returns "shoot" / "end".

    Separate so the button version can replace it; the capture and storage
    logic is shared, only the way of asking differs.
    """
    try:
        # `q` matches the recording path; `d` (done) is kept as well.
        k = prompt(f"  [set {ses.shots + 1}] Enter=shoot  d=done (q to cancel): ").lower()
        return "end" if k.startswith("d") or k.startswith("q") else "shoot"
    except (EOFError, KeyboardInterrupt):
        return "end"


def settle_countdown(secs, m):
    """Countdown between the button press and the shot, so the press does not shake the set.

    Calibration needs this because the cameras do not expose at exactly the
    same instant. Shooting while the rig still moves gives images of slightly
    different rig poses, which violates the rigid-rig assumption and
    corrupts the extrinsics. (Video does not need it: it is continuous and a
    shaky start can be trimmed.)

    The OLED shows no button hints during the countdown; the point is not to touch anything.
    """
    if secs <= 0:
        return
    t_end = time.monotonic() + secs
    while True:
        left = t_end - time.monotonic()
        if left <= 0:
            break
        m.update(snapshot={"wait_s": left})
        time.sleep(min(0.2, left))
    m.update(snapshot={"wait_s": None})


def do_calib(cfg, srcs, warm, shot_input=term_shot_input, delay_s=0.0):
    get_menu().update(phase="calib")
    ses = new_session(cfg, srcs)
    try:
        ses.arm("calib", warm)
    except Exception as e:                            # noqa: BLE001
        print(f"  ERROR opening: {e}")
        return
    print(f"\n  Calibration set -> {ses.dirname}")
    print("  Each Enter shoots one set (one image per camera); move the board before the next set.")
    print("  15 or more sets recommended, covering different areas and angles. Type d to finish.\n")
    while True:
        if shot_input(ses) == "end":
            break
        settle_countdown(delay_s, get_menu())
        r = ses.shoot()
        get_menu().update(snapshot={"shots": r["shot"],
                                    "last_ok": r["ok"] == r["total"]})
        print(f"    {'OK' if r['ok'] == r['total'] else '!'} set {r['shot']}: "
              f"{r['ok']}/{r['total']} cameras, {r['bytes']/1024:.0f} KB")
    if ses.shots == 0:
        # No set shot: leave no folder behind.
        print("  No set was shot, cancelled.")
        ses.discard()
        return True
    print(f"\n  {ses.shots} sets in total")
    report(ses.finalize())


# Four-tile preview: each camera contributes one quarter of its own view,
# scaled to 64x32.
#
# The quarter is chosen on purpose. The physical layout, seen from behind
# the rig (looking where the cameras look), is
#    C = top left, B = top right, D = bottom left, A = bottom right,
# and each camera shows the quarter facing the rig center: A (bottom right)
# shows its top-left quarter, D its top-right, B its bottom-left, C its
# bottom-right. The four tiles therefore look at the same central region of
# the scene from four positions, and a subject's coverage can be judged by
# the number of tiles it appears in, without judging any boundary.
#
# The opposite choice (each camera's outer quarter) would show four
# different, non-overlapping edges of the scene: fine for exposure and
# framing, but no help in judging the shared region.
PREVIEW_LAYOUT = [("cam_A", (0, 0),   (0, 0)),
                  ("cam_D", (64, 0),  (1, 0)),
                  ("cam_B", (0, 32),  (0, 1)),
                  ("cam_C", (64, 32), (1, 1))]


def preview_layout(names):
    """Select the connected cameras; positions do not move.

    Each tile position is bound to a camera name, not to an index, so a
    missing camera leaves its tile black instead of shifting the others; the
    black tile itself tells which position has no image.

    A pure function so behaviour with fewer cameras can be tested without hardware.
    """
    have = set(names)
    return [(n, o, q) for n, o, q in PREVIEW_LAYOUT if n in have]


def verify_exposure(srcs, cfg, pub, m=None, tries=2, settle=0.4):
    """After applying a metered exposure, confirm every camera actually took it.

    Brightness is compared instead of settings because:
      1. the driver's gain uses its own 0-63 scale with no known mapping to
         the `AnalogueGain` (1.0-1.5) that is sent;
      2. these UVC cameras report only `SensorTimestamp` in
         `capture_metadata()`; `ExposureTime` and `AnalogueGain` are None;
      3. brightness is what finally matters: correct settings with a wrong
         image are still wrong.

    The tolerance is wide on purpose. The cameras see slightly different
    parts of the scene, but healthy cameras measured within 3% of each other
    (75.4 / 79.6 / 80.4) while a camera that kept its old exposure read 28.3;
    a tight threshold would raise false alarms in high-contrast scenes.

    `set_controls()` is fire-and-forget, so without this check a camera that
    missed the new setting would go unnoticed.
    """
    import cv2                                        # noqa: PLC0415
    import numpy as np                                # noqa: PLC0415
    c = cfg["capture"]
    for attempt in range(tries):
        time.sleep(settle)                            # controls take a few frames to apply
        lum = {}
        for s in srcs:
            g = _preview_gray(s, cv2, np)
            if g is not None:
                lum[s.name] = float(g.mean())
        if len(lum) < 2:
            print("  ! Not enough frames captured, exposure check skipped")
            return True
        med = float(np.median(list(lum.values())))
        bad = [n for n, v in lum.items() if not (0.6 * med <= v <= 1.6 * med)]
        print("  Exposure check  " + "  ".join(f"{n.rsplit('_', 1)[-1]} {v:.0f}"
                                         for n, v in sorted(lum.items()))
              + f"  median {med:.0f}")
        if not bad:
            return True
        for n in bad:
            src = next(s for s in srcs if s.name == n)
            # read_controls() is deliberately not printed here: the driver's
            # control values can lag behind what Picamera2 sent (a camera set
            # to 1/4 exposure, visibly darker, still read the old value).
            # A wrong number would send the investigation the wrong way.
            print(f"  ! {n} brightness {lum[n]:.0f} is off the median {med:.0f}"
                  + ("  resetting once..." if attempt + 1 < tries else ""))
            if attempt + 1 < tries:
                src.set_exposure(c["exposure_us"], c["gain"])
    # Still inconsistent after a reset: this must be visible. The fault does
    # not break the program, it silently makes that camera's footage unusable.
    tag = " ".join(n.rsplit("_", 1)[-1] for n in bad)
    # Stop on the ERROR screen and wait for a key press. A plain message would
    # be overwritten by the next status message, and the return value alone
    # would not stop the flow. From the idle screen the operator can choose to
    # meter again.
    if m is not None:
        m.update(error="EXPOSURE MISMATCH " + tag)
    else:
        pub.message("  EXPOSURE MISMATCH\n  " + tag + "\n  re-meter or check cable")
    print(f"  ! {', '.join(bad)} still inconsistent after a reset; "
          "their footage differs in brightness from the others, do not use it for training.")
    return False


def host_id():
    """`hostname@IP` for the info screen.

    The IP is found by connecting a UDP socket and reading the local address
    (no packet is sent, and the several addresses of `hostname -I` need no
    parsing). Without a network only the hostname is returned.
    """
    name = socket.gethostname()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return f"{name}@{s.getsockname()[0]}"
    except OSError:
        return name


def do_info(cfg, srcs, pub, btn, m):
    """Version and status. Read-only; any button exits.

    In the field there is no SSH, and the first question when something goes
    wrong is which version is running.

    The version comes from `core/__init__.py`; it is only read here.
    """
    m.update(snapshot={
        "core_version": CORE_VERSION,
        "core_date": CORE_DATE,
        "cams": len(srcs),
        "audio_ok": audio_status(cfg),
        "free_bytes": free_bytes(HERE / cfg["dest_root"]),
        "host": host_id(),
    })
    # The OLED background thread draws INFO from m.view(); nothing needs to be pushed.
    m.update(phase="info")
    print()
    print(f"  Core v{CORE_VERSION} ({CORE_DATE})  {len(srcs)} cameras"
          f"  host {host_id()}")
    _a = audio_status(cfg)
    print(f"  Audio {'ok' if _a else 'off' if _a is None else 'NO MIC'}"
          f"  free space {free_bytes(HERE / cfg['dest_root'])/2**30:.1f} GB")
    # Any button exits (same convention as preview / mic test).
    # Without buttons, return immediately; the information is printed above.
    if btn is not None:
        while btn.get(timeout=0.1) is None:
            pass
    m.update(phase="idle")


def do_preview(srcs, pub, btn, m):
    """Four-camera preview on the OLED; any button exits.

    No camera setting is changed: the running 1080p MJPEG streams and the
    current fixed manual exposure are used, so the preview shows the exposure
    that will actually be recorded.

    The whole 128x64 area is image: no title, border, divider or exit hint.

    Measured 8.04 fps: capture and decode of four cameras 26.7 ms, compositing
    1.8 ms, I2C 96.0 ms. The key is `cv2.IMREAD_REDUCED_GRAYSCALE_8`, which
    scales by 1/8 while decoding (1920x1080 -> 240x135); a full PIL decode of
    four frames takes 104 ms. Decoding costs almost nothing extra because it
    happens inside the 33 ms wait for the next frame.
    """
    if not hasattr(pub, "take_over"):
        pub.message("  PREVIEW\n  needs an OLED")
        return
    try:
        import select
        import cv2
        import numpy as np
        from PIL import Image
    except Exception as e:                             # noqa: BLE001
        pub.message(f"  PREVIEW\n  needs cv2/PIL: {e}")
        return

    by_name = {s.name: s for s in srcs}
    layout = preview_layout(by_name)
    if not layout:
        pub.message("  PREVIEW\n  no camera")
        return

    m.update(phase="preview")
    dev = pub.take_over()
    n, t0 = 0, time.monotonic()
    try:
        while True:
            # Both the buttons and the keyboard exit; otherwise an OLED
            # without buttons (or operation over SSH) could enter but not leave.
            if btn is not None and btn.get(timeout=0) is not None:
                break
            if sys.stdin.isatty() and select.select([sys.stdin], [], [], 0)[0]:
                sys.stdin.readline()
                break
            canvas = Image.new("L", (128, 64), 0)
            for name, (ox, oy), (qx, qy) in layout:
                g = _preview_gray(by_name[name], cv2, np)
                if g is None:
                    continue
                h_, w_ = g.shape
                sub = g[qy*h_//2:(qy+1)*h_//2, qx*w_//2:(qx+1)*w_//2]
                canvas.paste(Image.fromarray(cv2.resize(sub, (64, 32))),
                             (ox, oy))
            dev.blit(dev.pack(canvas.convert("1")))    # dither once, after compositing
            n += 1
    except Exception as e:                             # noqa: BLE001
        pub.message(f"  PREVIEW\n  stopped: {e}")
    finally:
        el = max(time.monotonic() - t0, 1e-6)
        pub.release()
        m.update(phase="idle")
        print(f"  Preview {n} frames / {el:.1f} s = {n/el:.2f} fps")


def do_mic_test(pub, btn, m):
    """Microphone levels, left and right separately. Any button or key exits.

    Answers "are both microphones alive" in the field, where there is no SSH
    (a failed microphone shows up as sound on one side only).

    ```
    MIC TEST
    L ########----  -18.2 dB
    R #######-----  -20.5 dB
    any key to exit
    ```

    No gain is applied. The bar covers -80 to 0 dBFS, which stretches the
    scale rather than amplifying the signal; the numbers are true dBFS.
    The recorder always stores raw PCM (see the top of pi/audio.py).

    The sound card is exclusive: this screen is only reachable from idle and
    must release the card on exit, otherwise recording fails (`close()` in
    the `finally` block).
    """
    import select
    try:
        from pi.audio import LevelMeter, bar_ratio
    except Exception as e:                             # noqa: BLE001
        pub.message(f"  MIC TEST\n  no audio: {e}")
        return

    meter = LevelMeter()
    try:
        meter.open()
    except Exception as e:                             # noqa: BLE001
        pub.message(f"  MIC TEST\n  {e}")
        return

    m.update(phase="mic_test")
    # Take over the OLED when present (same approach as the preview); otherwise terminal only.
    dev = pub.take_over() if hasattr(pub, "take_over") else None
    n = 0
    try:
        while True:
            if btn is not None and btn.get(timeout=0) is not None:
                break
            if sys.stdin.isatty() and select.select([sys.stdin], [], [], 0)[0]:
                sys.stdin.readline()
                break
            lv = meter.read()
            if dev is not None:
                _mic_draw_oled(dev, lv, bar_ratio)
            if sys.stdin.isatty():
                # redraw three lines in place: up three lines, clear, print
                print(("\x1b[3A" if n else "") + "\x1b[J"
                      + "\n".join("  " + x for x in mic_lines(lv, bar_ratio)[1:]))
            n += 1
    except Exception as e:                             # noqa: BLE001
        pub.message(f"  MIC TEST\n  stopped: {e}")
    finally:
        meter.close()                # must release the card, or recording fails afterwards
        if dev is not None:
            pub.release()
        m.update(phase="idle")
        print(f"  Microphone test: {n} updates")


def _mic_draw_oled(dev, lv, ratio):
    """MIC TEST on the OLED. The bars are drawn rectangles, not text.

    Block characters do not exist in the OLED's bitmap font, so
    `draw.rectangle` is used; text is limited to the title, L/R and the dB values.
    """
    from PIL import Image, ImageDraw
    img = Image.new("1", (128, 64), 0)
    d = ImageDraw.Draw(img)
    d.text((3, 0), "MIC TEST", fill=1)
    for i, (name, db) in enumerate(zip(("L", "R"), lv)):
        y = 20 + i * 18
        d.text((3, y), name, fill=1)
        x0, x1 = 14, 74                       # left and right edges of the bar
        d.rectangle([x0, y + 1, x1, y + 9], outline=1, fill=0)
        w = int(round(ratio(db) * (x1 - x0 - 2)))
        if w > 0:
            d.rectangle([x0 + 1, y + 2, x0 + 1 + w, y + 8], outline=1, fill=1)
        d.text((x1 + 5, y), f"{db:5.1f}", fill=1)
    dev.blit(dev.pack(img))


def mic_lines(lv, ratio, width=12):
    """Format (left dBFS, right dBFS) as three terminal lines. Pure strings, easy to test.

    The OLED does not use this; it draws rectangles because block characters
    are missing from its bitmap font.
    """
    out = ["MIC TEST"]
    for name, db in zip(("L", "R"), lv):
        fill = int(round(ratio(db) * width))
        out.append(f"{name} {'#' * fill}{'-' * (width - fill)}  {db:6.1f} dB")
    out.append("any key to exit")
    return out


def _preview_gray(src, cv2, np):
    """Take one frame from an already running MJPEG stream and decode it to 1/8-size grayscale."""
    r = src.p.capture_request()
    try:
        buf = r.make_buffer("main").tobytes()
    finally:
        r.release()
    eoi = buf.rfind(b"\xff\xd9")
    if eoi < 0:
        return None
    return cv2.imdecode(np.frombuffer(buf[:eoi + 2], np.uint8),
                        cv2.IMREAD_REDUCED_GRAYSCALE_8)


def try_buttons(pub):
    """Use the GPIO buttons when present; otherwise continue without them.

    There is no flag: the OLED and buttons are part of the device, so there
    is no reason to declare which one is used. On a machine without them,
    this prints one line and everything else behaves the same.
    """
    try:
        from pi.buttons import Buttons
        return Buttons()
    except Exception as e:                             # noqa: BLE001
        pub.message(f"  NO BUTTONS\n  keyboard only: {e}")
        return None


def power_off(srcs, pub, m):
    """Safe shutdown. In the field there is no SSH, and cutting power can damage the storage.

    Requires root, provided by a narrowly scoped sudoers file
    (`rig-poweroff.sudoers`) that allows only `/usr/sbin/poweroff`, not
    password-less sudo in general.

    Without that file this fails, and the failure must be visible: an
    operator who believes the Pi has shut down will pull the power, which is
    exactly what this function exists to prevent.
    """
    for s in srcs:
        s.close()
    m.update(phase="done", message="powering off")
    pub.message("  POWER OFF\n  wait for the green LED\n  to stop blinking")
    time.sleep(2.0)                       # give the screen time to show the message
    if hasattr(pub, "shutdown"):
        pub.shutdown()
    r = subprocess.run(["sudo", "-n", "/usr/sbin/poweroff"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  ERROR shutdown failed (sudoers file not installed?): {r.stderr.strip()}")
        print("     See the header of rig-poweroff.sudoers for installation")
        return 1
    return 0


def quit_all(srcs, pub, m):
    for s in srcs:
        s.close()
    m.update(phase="done")
    # shutdown() is not close(): close() ends one output session and is called
    # after every recording; the hardware is shut down only when the program exits.
    if hasattr(pub, "shutdown"):
        pub.shutdown()
    return 0


def wait_ack(btn, m):
    """Stay on the done / error screen until a button is pressed.

    In the field this screen is the only way to see whether the recording
    succeeded and how many files it has; reopening the cameras immediately
    would hide it.
    """
    if btn is None:
        return
    btn.drain()
    while m.state in (m.DONE, m.ERROR):
        ev = btn.get(timeout=0.2)
        if ev:
            m.handle(ev)


def go_sleep(srcs, cfg, pub, m):
    """Automatic camera sleep: close the cameras after a long idle period (the Pi stays on).

    Running cameras keep producing heat. Outdoors, without power or cooling,
    idle time and accumulated heat limit how many sets can be shot.
    The OLED stays on (at reduced contrast) and the buttons keep working.
    """
    for s in srcs:
        s.close()
    m.update(phase="sleep")
    # oled=False: the SLEEP screen already says the same thing
    pub.message(f"  SLEEP\n  cameras off after {cfg.get('sleep_after_s', 60):.0f}s"
                f"\n  press any key", oled=False)
    return []


def wake_up(cfg, pub, m, warm):
    """Wake from sleep: reopen the cameras only, without a warm-up.

    The byte rate measured at boot is reused; use "warm up" from the menu to
    re-measure. Exposure is a fixed manual value, so no settling is needed.
    """
    return startup(cfg, detect(cfg.get("camera_ports")), pub,
                   reuse_warm=warm)


def wait_choice(btn, prompt_text="  Choice: ", timeout=None):
    """Wait for the keyboard and the buttons at the same time; return whichever comes first.

        ("key", "1")     a line was entered on the terminal
        ("btn", Event)   a physical button was pressed
        ("eof", None)    the keyboard was closed (end of a pipe)
        ("timeout", None)

    Both sources only request a state transition, in different ways: the
    keyboard jumps by number, the buttons cycle.

    Without a terminal stdin must not be selected: when started at boot
    `stdin` is not a tty, `select` would always report it readable and the
    loop would spin. Hence the `isatty()` check.
    """
    import select
    tty = sys.stdin.isatty()
    if tty:
        sys.stdout.write(prompt_text)
        sys.stdout.flush()
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            return "timeout", None
        if btn is not None:
            ev = btn.get(timeout=0.05)
            if ev:
                if tty:                      # the prompt is already printed; end the line
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                return "btn", ev
        if not tty:
            continue
        if select.select([sys.stdin], [], [], 0.05)[0]:
            line = sys.stdin.readline()
            if not line:
                return "eof", None
            return "key", line.strip().lower()


def term_meter(cfg, srcs, warm, ae, pub, m):
    """Terminal metering: measure, then ask on the keyboard whether to apply. Returns (warm, ae)."""
    m.update(phase="exposure")
    print()
    ok = True
    sug = auto_exposure(srcs, cfg, pub, apply=False)
    c2 = cfg["capture"]
    print(f"\n  Current setting {c2['exposure_us']/1000:.1f} ms / gain {c2['gain']}")
    if sug and prompt("  Apply the suggested value? (y/n): ").lower().startswith("y"):
        c2["exposure_us"], c2["gain"] = sug["exposure_us"], sug["gain"]
        save_cfg(cfg)
        for s in srcs:
            s.set_exposure(c2["exposure_us"], c2["gain"])
        ae = sug
        print("  Applied and saved to config.json")
        warm = warmup_all(srcs, cfg, pub)
        # Verify after the warm-up: warmup_all() prints a message that would
        # otherwise overwrite the warning.
        ok = verify_exposure(srcs, cfg, pub, m)
    else:
        for s in srcs:                                # restore the current setting
            s.set_exposure(c2["exposure_us"], c2["gain"])
    if ok:
        m.update(phase="idle")        # on failure, stay on ERROR until a key is pressed
    return warm, ae


def outdoor_meter(cfg, srcs, warm, pub, btn, m):
    """Button metering: measure -> show old and new values -> wait for a decision.

    Returns warm (re-measured if the exposure was applied).

    Shares `auto_exposure(apply=False)` with the terminal path; only the way
    of asking differs. Applying triggers a new warm-up because the byte rate
    follows the exposure (longer exposure -> brighter image -> larger JPEG ->
    higher byte rate -> fewer seconds available).
    """
    m.update(phase="exposure")
    ok = True
    sug = auto_exposure(srcs, cfg, pub, apply=False)
    c = cfg["capture"]
    if not sug:
        m.update(error="metering failed")
        return warm
    m.update(phase="expose_apply", snapshot={
        "exposure_us": c["exposure_us"], "gain": c["gain"],
        "sug_exposure_us": sug["exposure_us"], "sug_gain": sug["gain"],
        "loss_pct": sug.get("loss_pct")})
    act = None
    while m.state == m.EXPOSE_APPLY:
        ev = btn.get(timeout=0.2)
        if ev:
            act = m.handle(ev)
    if act == Action.APPLY_EXPOSURE:
        c["exposure_us"], c["gain"] = sug["exposure_us"], sug["gain"]
        save_cfg(cfg)
        for s in srcs:
            s.set_exposure(c["exposure_us"], c["gain"])
        warm = warmup_all(srcs, cfg, pub)     # exposure changed, so re-measure the byte rate
        ok = verify_exposure(srcs, cfg, pub, m)   # after the warm-up, as above
    else:
        for s in srcs:                        # restore the current setting
            s.set_exposure(c["exposure_us"], c["gain"])
    if ok:
        m.update(phase="idle")    # on failure, stay on ERROR until a key is pressed
    return warm


def button_shot_input(btn, m):
    """Button version for calibration: `v` shoots a set, `^` finishes."""
    while True:
        ev = btn.get(timeout=0.2)
        if ev is None:
            continue
        act = m.handle(ev)
        if act == Action.SHOOT:
            return "shoot"
        if act == Action.FINISH:
            return "end"


def idle_snapshot(cfg, srcs, warm):
    """What the idle screen shows.

    The number of seconds that can be recorded is shown while idle, instead
    of failing in the middle of a recording.

    The byte rate comes from the warm-up and the write speed is not
    re-probed (that takes seconds), so this is an estimate; `Session.arm()`
    recomputes it with measured values when recording starts.
    """
    rate = sum(v["bytes_per_second"] for v in warm.values()) or 1
    dest = HERE / cfg["dest_root"]
    b = Budget(free_bytes(cfg["tmp_root"]), free_bytes(dest), rate, rate)
    return {"cams": len(srcs), "free_bytes": free_bytes(dest),
            "max_seconds": b.max_seconds,
            "audio_ok": audio_status(cfg)}


def audio_status(cfg):
    """Whether the microphone is present: `None` = disabled, `True` = card found, `False` = not found.

    This is a detection result, not the configuration flag, so an unplugged
    microphone is reported. `find_card()` reads `/proc/asound/cards` and is cheap.
    """
    if not cfg.get("audio", True):
        return None
    try:
        from pi.audio import find_card
        return find_card() is not None
    except Exception:                                 # noqa: BLE001
        return False


# Terminal number keys mapped to menu item keys. The state machine uses the
# item keys; to change the menu order, edit MENU_ITEMS in `core/menu.py`.
TERM_KEYS = {"1": "record", "2": "calib", "3": "params",
             "4": "warmup", "5": "expose", "0": "preview"}


# ---------------------------------------------------------------- terminal layout
# This section only formats text: no input, no hardware, no state changes.
# It is separate so `tools/check_ui_golden.py` can feed it fixed data and get
# fully deterministic output; output on real hardware varies on every run
# (fps, byte rate, free space, timestamps).


def main_menu_lines(cfg, srcs, warm, ae):
    c = cfg["capture"]
    rate = sum(v["bytes_per_second"] for v in warm.values()) / 1048576
    return [
        "\n" + "=" * 56,
        "  rig recorder",
        "=" * 56,
        f"  Cameras     {len(srcs)} ready  " + "  ".join(s.name for s in srcs),
        f"  Exposure    {c['exposure_us']/1000:.1f} ms  gain {c['gain']}"
        + (f"  (auto: scene {ae['scene_stops']:.1f} stops / window "
           f"{ae['window_stops']:.1f} stops / loss {ae['loss_pct']:.2f}%)"
           if ae else "  (manual)"),
        f"  Resolution  {c['resolution'][0]}x{c['resolution'][1]}  "
        f"byte rate {rate:.1f} MB/s",
        f"  Output      {HERE / cfg['dest_root']}",
        f"  Core        v{CORE_VERSION} ({CORE_DATE})",
        "-" * 56,
        "  1) Record\n  2) Calibration set\n  3) Parameters\n"
        "  4) Warm up again\n"
        "  5) Meter exposure (when the scene or light changes; suggests, never applies by itself)\n  q) Quit",
    ]


def params_lines(cfg):
    c = cfg["capture"]
    return [
        f"\n    1) Resolution        {c['resolution'][0]}x{c['resolution'][1]}",
        f"    2) Exposure          {c['exposure_us']} us ({c['exposure_us']/1000:.1f} ms)",
        f"    3) Gain              {c['gain']}",
        f"    4) Warm-up           {cfg['warmup_s']} s",
        f"    5) Default length    {cfg['default_duration_s']} s",
        f"    7) Expected cameras  "
        f"{', '.join(cfg.get('expected_cameras') or []) or '(not set)'}",
        f"    6) Auto exposure     {'on' if cfg['auto_exposure'] else 'off'}"
        f" (shutter {cfg['ae_min_exposure_us']/1000:.1f}-"
        f"{cfg['ae_max_exposure_us']/1000:.1f} ms)",
    ]


def menu_params(cfg):
    c = cfg["capture"]
    for line in params_lines(cfg):
        print(line)
    k = prompt("  Change which item? (1-7, Enter = back): ")
    if k == "1":
        c["resolution"] = [ask("    Width", c["resolution"][0], int),
                           ask("    Height", c["resolution"][1], int)]
    elif k == "2":
        c["exposure_us"] = ask("    Exposure in us (3300 verified)", c["exposure_us"], int)
    elif k == "3":
        c["gain"] = ask("    Gain (1.484 is the maximum)", c["gain"], float)
    elif k == "4":
        cfg["warmup_s"] = ask("    Warm-up seconds (the first 2.5 s let UVC settle)", cfg["warmup_s"], float)
    elif k == "5":
        cfg["default_duration_s"] = ask("    Seconds", cfg["default_duration_s"], float)
    elif k == "6":
        cfg["auto_exposure"] = not cfg["auto_exposure"]
        if cfg["auto_exposure"]:
            cfg["ae_max_exposure_us"] = ask(
                "    Max exposure in us (longer is brighter but blurs motion more)",
                cfg["ae_max_exposure_us"], int)
            cfg["ae_min_exposure_us"] = ask(
                "    Min exposure in us (below this, reduce light instead)",
                cfg["ae_min_exposure_us"], int)
    elif k == "7":
        # Use the currently detected cameras as the new reference: typing a
        # list by hand is error-prone, and this item is used precisely when
        # the hardware has changed.
        cams2 = detect(cfg.get("camera_ports"))
        cfg["camera_ports"] = {c["port"]: c["name"] for c in cams2 if c.get("port")}
        cfg["expected_cameras"] = [c["name"] for c in cams2]
        print(f"    -> {', '.join(cfg['expected_cameras']) or '(none detected)'}")
    else:
        return
    save_cfg(cfg)
    if k in ("2", "3", "7"):
        print("  Saved to config.json")
    else:
        # Only exposure, gain and the camera mapping are saved (see load_cfg)
        print("  Effective until the next restart, then back to the default")


def cams_lines(cams):
    if not cams:
        return ["  ERROR no camera found. Check lsusb and ls /dev/video*"]
    out = [f"  {len(cams)} cameras detected:"]
    for c in cams:
        out.append(f"    {c['name']:<7} {str(c['node']):<13} {c['controller']}")
    per = {}
    for c in cams:
        per.setdefault(c["controller"], []).append(c["name"])
    for k, v in per.items():
        if len(v) > 1:
            out.append(f"    ! {k} has {len(v)} cameras ({', '.join(v)}): a shared bus can drop frames")
    return out


def show_cams(cams):
    for line in cams_lines(cams):
        print(line)


def ensure_ports(cfg, cams):
    """Create or extend the "USB port -> name" mapping. Returns True if the config was written.

    The mapping must be created while every camera is connected. Creating it
    with a camera missing would store shifted names permanently, which is
    worse than having no mapping:

        no mapping, cameras >= expected count   -> create (this is the reference)
        no mapping, cameras <  expected count   -> do not create, warn only
        mapping exists                          -> add newly seen ports only

    An older config.json may hold only `expected_cameras` (names without
    ports), from which the mapping cannot be restored; it is rebuilt this way.
    """
    have = {c["port"]: c["name"] for c in cams if c.get("port")}
    ports = dict(cfg.get("camera_ports") or {})
    if ports:
        new = {p: n for p, n in have.items() if p not in ports}
        if not new:
            return False
        ports.update(new)
        cfg["camera_ports"] = ports
        save_cfg(cfg)
        print(f"  (new ports remembered: {', '.join(f'{p}={n}' for p, n in new.items())})")
        return True

    want = len(cfg.get("expected_cameras") or [])
    if want and len(have) < want:
        print(f"  ! Only {len(have)} cameras detected (expected {want}); "
              f"the port mapping is not created.")
        print("     Creating it with a camera missing would store wrong names. Connect all cameras and run again.")
        return False
    cfg["camera_ports"] = have
    cfg["expected_cameras"] = [c["name"] for c in cams]
    save_cfg(cfg)
    print(f"  (port mapping created: "
          f"{', '.join(f'{p}={n}' for p, n in sorted(have.items()))})")
    return True


def check_expected(cfg, cams, assume_yes=False):
    """Warn when the cameras do not match the expected list. Warns only; always returns True.

    A camera can fail to enumerate on USB while the others record cleanly,
    and nothing would reveal it until the footage is processed. The warning
    is printed before recording instead.

    Recording with a missing camera still works; preprocessing and training
    accept fewer cameras. The warning only informs.

    There is no confirmation prompt: a prompt answerable only from the
    keyboard would block forever when the device is operated with its two
    buttons. A question that can stop the flow must be answerable in every
    way the device is operated.

    The comparison is against the expected list, not a hard-coded count of
    four. The list comes from the port mapping in `config.json`; without one,
    the first run's cameras become the reference.

    This is platform code, not core: detecting cameras is Pi-specific.
    """
    # Compare ports, not names: the reference should say which socket lost its camera.
    ports = cfg.get("camera_ports") or {}
    want = [ports[p] for p in sorted(ports)] or cfg.get("expected_cameras")
    got = [c["name"] for c in cams]
    if not want:
        return True                                   # the reference is created by ensure_ports
    missing = [n for n in want if n not in got]
    extra = [n for n in got if n not in want]
    if not missing and not extra:
        return True

    print()
    print(f"  ! Cameras do not match: expected {len(want)} ({', '.join(want)}), "
          f"found {len(got)}")
    if missing:
        print(f"    Missing: {', '.join(missing)}")
        print("       Usually a USB enumeration failure (cable, power, timing).")
        print("       To fix: check the connector -> replug -> reboot.")
    if extra:
        print(f"    Unexpected: {', '.join(extra)}")
    print("    Recording still works with fewer cameras; the window gets smaller and the initial point cloud sparser.")
    # No prompt: continue directly. The warning above is printed, and the idle
    # screen shows how many cameras are online.
    return True


def main():
    ap = argparse.ArgumentParser(description="rig recorder")
    ap.add_argument("--rec", type=float, metavar="SECONDS", help="record directly")
    ap.add_argument("--calib", action="store_true", help="go straight to calibration-set mode")
    ap.add_argument("--yes", "-y", action="store_true", help="start without asking")
    # Two uses: keep shooting when the microphone fails, and recording a
    # control set (with and without audio) to compare dropped frames.
    ap.add_argument("--no-audio", action="store_true",
                    help="do not record audio (video unchanged)")
    a = ap.parse_args()

    cfg = load_cfg()
    pub = get_pub()

    print("\n  Detecting cameras...")
    cams = detect(cfg.get("camera_ports"))
    show_cams(cams)
    if not cams:
        return 1
    if ensure_ports(cfg, cams):                       # create or extend the port mapping
        cams = detect(cfg.get("camera_ports"))        # rename with the new mapping
    if not check_expected(cfg, cams, a.yes):
        return 1
    print()
    # Opening the cameras, automatic exposure and warm-up happen before the
    # menu, so a mode can start recording immediately.
    if a.no_audio:
        cfg["audio"] = False               # add_audio() skips audio
    srcs, warm, ae = startup(cfg, cams, pub)
    if not srcs:
        print("  ERROR no camera could be opened")
        return 1

    if a.rec is not None:
        do_record(cfg, srcs, warm, a.rec, confirm=not a.yes)
        return 0
    if a.calib:
        do_calib(cfg, srcs, warm)
        return 0

    # The state machine is the single source of state. The terminal jumps by
    # number (`goto`), the buttons cycle and confirm (`handle`): the state is
    # shared, the events are not. Merging both into one event stream would
    # make the terminal tedious (five presses to reach the fifth item).
    m = get_menu()
    btn = try_buttons(pub)
    tty = sys.stdin.isatty()
    redraw = True
    sleep_after = float(cfg.get("sleep_after_s", 0))
    while True:
        if m.state == m.IDLE:
            m.update(phase="idle", snapshot=idle_snapshot(cfg, srcs, warm))
        if redraw and tty:
            for line in main_menu_lines(cfg, srcs, warm, ae):
                print(line)
        redraw = False
        # Only idle can lead to sleep: the cameras must stay open while
        # recording, calibrating or metering.
        # Idle time is counted from now, when the loop is back and waiting for
        # input. Each loop iteration starts right after an action has
        # finished, and actions block (info, preview, recording...), so
        # counting from the previous key press would include their duration.
        left = None
        if m.state == m.IDLE and srcs and sleep_after > 0:
            left = sleep_after
        src, val = wait_choice(btn, timeout=left)

        if src == "timeout":
            srcs = go_sleep(srcs, cfg, pub, m)
            continue
        if m.state == m.SLEEP:
            # Any key only wakes the system; see core/menu.py.
            m.handle(val if src == "btn" else Event.OK)
            srcs, warm, ae = wake_up(cfg, pub, m, warm)
            if not srcs:
                m.update(error="no camera")
            else:
                m.update(phase="idle")
            redraw = True
            continue

        if src == "eof" or (src == "key" and val in ("q", "quit", "exit")):
            return quit_all(srcs, pub, m)
        if src == "key":
            act = m.goto(TERM_KEYS.get(val, ""))
            redraw = True
        else:
            act = m.handle(val)
            # Cursor moves do not reprint the terminal menu (the OLED already
            # shows them, and reprinting on every press would flood the terminal).
            redraw = act is not None
        hands_free = src == "btn"          # this action was started with a button

        if act in (Action.START_RECORD, Action.START_CALIB):
            if btn:
                btn.drain()                # drop presses made on the previous screen
            trg = None
            if hands_free:
                from pi.buttons import ButtonTrigger
                trg = ButtonTrigger(btn, m)
            if act == Action.START_RECORD:
                do_record(cfg, srcs, warm,
                          seconds=0 if hands_free else None,
                          confirm=not hands_free, trigger=trg)
            else:
                do_calib(cfg, srcs, warm,
                         shot_input=((lambda ses: button_shot_input(btn, m))
                                     if hands_free else term_shot_input),
                         # Only the physical buttons need the settling delay;
                         # a keyboard over SSH does not touch the rig.
                         delay_s=(float(cfg.get("calib_delay_s", 3.0))
                                  if hands_free else 0.0))
            if hands_free:
                wait_ack(btn, m)           # stay on done / error until a press
            print()                                   # the cameras were closed; reopen them
            # No new warm-up: the boot byte rate is reused.
            # Use "warm up" from the menu to re-measure.
            srcs, warm, ae = startup(cfg, detect(cfg.get("camera_ports")),
                                     pub, reuse_warm=warm)
            if not srcs:
                return 1
            m.update(phase="idle")
            redraw = True
        elif act == Action.START_PREVIEW:
            do_preview(srcs, pub, btn, m)

        elif act == Action.START_MIC_TEST:
            do_mic_test(pub, btn, m)
            redraw = True
        elif act == Action.SHOW_INFO:
            do_info(cfg, srcs, pub, btn, m)
            redraw = True
        elif act == Action.OPEN_PARAMS:
            menu_params(cfg)
            m.update(phase="idle")
            redraw = True
        elif act == Action.START_WARMUP:
            print()
            warm = warmup_all(srcs, cfg, pub)
            m.update(phase="idle")
            redraw = True
        elif act == Action.START_EXPOSURE:
            if hands_free:
                warm = outdoor_meter(cfg, srcs, warm, pub, btn, m)
            else:
                warm, ae = term_meter(cfg, srcs, warm, ae, pub, m)
            redraw = True
        elif act == Action.SHUTDOWN:
            # "shutdown" in the menu powers the Pi off; it does not just exit
            # the program (systemd would restart it immediately). Use q on the
            # keyboard to exit the program.
            return power_off(srcs, pub, m)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(130)
