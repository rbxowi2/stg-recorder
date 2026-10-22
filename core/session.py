"""Recording state machine and data format. Platform-independent.

    IDLE -> ARMED (warm-up, byte rate, budget) -> RECORDING / SHOOTING -> FLUSHING

Output is written to tmpfs first and moved to the destination afterwards; the
source copy is deleted only after every file has been verified.

All sources must timestamp with the same clock (CLOCK_MONOTONIC). For audio,
ALSA must use SND_PCM_TSTAMP_TYPE_MONOTONIC. This cannot be fixed afterwards:
the offset between different clocks drifts, so recordings taken with mixed
clocks cannot be re-aligned later.
"""
from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path

from . import CORE_VERSION
from .budget import LOW_WATER_MB, Budget, free_bytes, probe_write_bps

CLOCK = "CLOCK_MONOTONIC"
SCHEMA = 1


def move_session(src: Path, dst: Path, on_progress=None):
    """Copy a session to its destination; delete the source only after verification.

    Until the copy is confirmed, the tmpfs copy is the only copy of the data.
    """
    files = sorted(f for f in src.rglob("*") if f.is_file())
    total = sum(f.stat().st_size for f in files)
    avail = free_bytes(dst.parent if dst.parent.exists() else dst)
    if avail < total * 1.02:
        return {"ok": False, "bytes": total,
                "error": f"Not enough space at destination: need {total/1048576:.0f} MB, "
                         f"have {avail/1048576:.0f} MB (source kept)"}
    dst.mkdir(parents=True, exist_ok=True)
    done, bad = 0, []
    for f in files:
        out = dst / f.relative_to(src)
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(f, out)
            if out.stat().st_size != f.stat().st_size:
                bad.append(str(f.relative_to(src)))
        except OSError as e:
            bad.append(f"{f.relative_to(src)}: {e}")
        done += f.stat().st_size
        if on_progress:
            on_progress(done, total)
    if bad:
        return {"ok": False, "error": "Verification failed (source kept)",
                "failed": bad[:10], "bytes": total}
    shutil.rmtree(src, ignore_errors=True)
    return {"ok": True, "bytes": total, "files": len(files), "dest": str(dst)}


RIG_HINT_NAME = "PUT_RIG_FOLDER_HERE.txt"
RIG_HINT_TEXT = """\
Put the rig folder here, next to {rec}.
(rig is the folder calibrate.py creates inside the calibration set.)
Once it is here, this folder is the shooting root:
select it in the studio app for preprocessing and training.

Shooting root layout:

  {root}/             <- shooting root (this folder)
  |-- PUT_RIG_FOLDER_HERE.txt  <- this file; keep or delete
  |-- {rec}/     <- recording from the Pi (do not modify)
  |   |-- cam_A/ cam_B/ cam_C/ cam_D/
  |   |-- audio/
  |   `-- session.json
  |-- rig/                     <- * YOU ADD THIS: copied from the calibration set
  |-- stgN/                    <- created by preprocessing (dataset)
  `-- <name>_out/              <- created by training (model)

Note: rig goes in THIS folder, not inside {rec}.
"""


# ============================================================ session state machine
# IDLE -> ARMED (warm-up, byte rate, budget) -> RECORDING / SHOOTING
#      -> FLUSHING (close files, write session.json, move) -> IDLE

class Session:
    def __init__(self, cfg, sources, pub, tmp_root, dest_root, warmup_fn=None,
                 write_bps=None):
        self.cfg, self.sources, self.pub = cfg, sources, pub
        self.warmup_fn = warmup_fn
        # The destination write speed is a hardware property and does not
        # change between recordings, so the platform layer may measure it once
        # at boot and pass it in. None means measure it here.
        # Only the speed may be cached; free space must be re-read every time.
        self.write_bps = write_bps
        self.tmp_root, self.dest_root = Path(tmp_root), Path(dest_root)
        self.state, self.mode = "IDLE", None
        self.dirname = self.tmp_dir = self.budget = None
        self.warm, self.errors = {}, []
        self.t_start, self.shots = None, 0

    def arm(self, mode, warm=None):
        """Prepare a session.

        If `warm` is None the cameras are opened and warmed up here. Normally
        the startup sequence has already done that and passes the result in,
        so choosing a mode does not cost another warm-up.
        """
        self.mode, self.state = mode, "ARMED"
        self.dirname = time.strftime(
            ("rec_" if mode == "video" else "calib_") + "%Y%m%d_%H%M%S")
        self.tmp_dir = self.tmp_root / self.dirname

        if warm is None:
            # How to warm up is a platform concern (threads, what to measure).
            # The core only requires {name: {bytes_per_second, fps}}.
            if self.warmup_fn is None:
                raise RuntimeError("No warm-up data and no warmup_fn given")
            warm = self.warmup_fn(self.sources, self.cfg, self.pub)
        self.warm = warm
        self.sources = [s for s in self.sources if s.p is not None]
        if not self.sources:
            raise RuntimeError("No camera could be opened")

        self.budget = Budget(free_bytes(self.tmp_root), free_bytes(self.dest_root),
                             sum(v["bytes_per_second"] for v in warm.values()),
                             self.write_bps if self.write_bps is not None
                             else probe_write_bps(self.dest_root))
        return self.budget

    def record(self, limit_s, trigger):
        cap = self.budget.max_seconds
        if limit_s <= 0 or limit_s > cap:
            if limit_s > cap:
                self.pub.message(f"LIMIT\nasked {limit_s:.0f}s > cap\n"
                                 f"using {cap:.0f}s ({self.budget.limited_by})")
            limit_s = cap

        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        for s in self.sources:
            s.begin(self.tmp_dir)
        self.state = "RECORDING"
        barrier, stop = threading.Barrier(len(self.sources)), threading.Event()
        # This 0.5 s serves two purposes, and removing it would silently break both:
        #   1. all cameras start at the same instant (after the barrier every
        #      thread waits for the same t_start)
        #   2. settling time after pressing the physical button (the rig
        #      shakes briefly when the button is pressed)
        self.t_start = time.monotonic() + 0.5
        deadline = self.t_start + limit_s

        def _run(src):
            try:
                barrier.wait(timeout=30)
                while time.monotonic() < self.t_start:
                    time.sleep(0.001)
                while time.monotonic() < deadline and not stop.is_set():
                    src.step()
            except Exception as e:                    # noqa: BLE001
                src.error = repr(e)
                self.errors.append(f"{src.name}: {e!r}")

        ths = [threading.Thread(target=_run, args=(s,)) for s in self.sources]
        for t in ths:
            t.start()
        try:
            while any(t.is_alive() for t in ths):
                time.sleep(0.5)
                if trigger.should_stop():
                    stop.set()
                    self.pub.message("STOPPING\nstop requested")
                if free_bytes(self.tmp_root) < LOW_WATER_MB * 1048576:
                    stop.set()
                    self.errors.append("tmpfs nearly full, stopped early")
                    self.pub.message("LOW SPACE\ntmpfs full, closing cleanly")
                self._publish(limit_s)
        except KeyboardInterrupt:
            stop.set()
            self.pub.message("INTERRUPTED\nclosing cleanly")
        for t in ths:
            t.join()
        self.pub.close()
        for s in self.sources:
            s.end()
        return self.finalize()

    def shoot(self):
        """Calibration set: shoot one set (one image per camera). Can be called repeatedly."""
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.state = "SHOOTING"
        self.shots += 1
        n, barrier, out = self.shots, threading.Barrier(len(self.sources)), {}

        def _one(src):
            try:
                barrier.wait(timeout=10)
                out[src.name] = src.shot(self.tmp_dir / src.name / f"shot_{n:04d}.jpg")
            except Exception as e:                    # noqa: BLE001
                out[src.name] = 0
                self.errors.append(f"{src.name} shot{n}: {e!r}")

        ths = [threading.Thread(target=_one, args=(s,)) for s in self.sources]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        return {"shot": n, "ok": sum(1 for v in out.values() if v > 0),
                "total": len(self.sources), "bytes": sum(out.values())}

    def discard(self):
        """Cancel: close the cameras and remove temporary data. No output folder is created.

        `finalize()` would always create a folder and write session.json, so
        cancelling through it would leave an empty folder behind.
        Closing the cameras is still required; otherwise they cannot be
        reopened next time.
        """
        self.state = "FLUSHING"
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        self.pub.close()
        for s in self.sources:
            s.close()
        self.state = "IDLE"
        return {"ok": True, "discarded": True, "dir": None,
                "files": 0, "bytes": 0, "errors": self.errors}

    def shoot_root(self):
        """The shooting root folder a recording is placed in.

        Recording: `<dest_root>/<time>/rec_<time>/`. The extra outer folder is
        the shooting root expected by preprocessing; after copying a
        recording to the PC, the calibration's `rig/` folder is placed next to
        `rec_<time>/` and no further folder needs to be created.
        Calibration set: `<dest_root>/calib_<time>/`, which calibrate.py reads directly.
        """
        if self.mode == "video":
            return self.dest_root / self.dirname[len("rec_"):]
        return self.dest_root

    def finalize(self):
        self.state = "FLUSHING"
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self._manifest()
        root = self.shoot_root()
        dest = root / self.dirname
        total = sum(f.stat().st_size for f in self.tmp_dir.rglob("*") if f.is_file())
        eta = total / max(self.budget.write_bps, 1) if self.budget else 0
        # Folder name only: a full path does not fit on one OLED line. The
        # terminal prints the full path once the move succeeds.
        self.pub.message(f"MOVE\n{total/1048576:.0f}MB, eta {eta:.0f}s"
                         f"\n-> {dest.name}")
        rep = move_session(self.tmp_dir, dest, lambda d, t: self.pub.publish(
            {"state": "FLUSHING", "free_mb": free_bytes(self.dest_root) / 1048576,
             "sources": {"move": {"fps": 100.0 * d / max(t, 1)}}}))
        if rep.get("ok") and root != self.dest_root:
            # A hint for people, not data: preprocessing ignores this file.
            try:
                (root / RIG_HINT_NAME).write_text(
                    RIG_HINT_TEXT.format(root=root.name, rec=self.dirname),
                    encoding="utf-8")
            except OSError:
                pass                        # failing to write the hint does not affect the recording
        self.pub.close()
        for s in self.sources:
            s.close()
        self.state = "IDLE"
        rep["dir"] = str(dest)
        rep["root"] = str(root)
        rep["errors"] = self.errors
        return rep

    def _stream_entry(self, s):
        """How one source is described in `session.json`.

        Branches on `kind`; not every source is a camera (audio sources have
        no `cam` attribute).
        """
        d = {"name": s.name, "kind": s.kind, "path": s.name,
             "stats": s.stats(), "warmup": self.warm.get(s.name)}
        if s.kind == "audio":
            d["container"] = "wav"
            d["index"] = f"{s.name}/index.json"       # a single object, not per-frame
        else:
            d["container"] = ("mjpg" if self.mode == "video"
                              else "jpg-sequence")
            d["index"] = (f"{s.name}/index.jsonl" if self.mode == "video"
                          else None)
            d["camera"] = {k: s.cam[k]
                           for k in ("id", "num", "node", "controller")}
        return d

    def _audio_entry(self):
        """Top-level `audio` summary. None when there is no audio source.

        Full details are in `audio/index.json`; this summary lets consumers
        decide whether audio exists, how long it is, and whether samples were
        dropped, without opening a second file.
        """
        for s in self.sources:
            if s.kind == "audio":
                st = s.stats()
                return {"path": s.name, "index": f"{s.name}/index.json",
                        "seconds": st.get("seconds"),
                        "frames": st.get("frames"),
                        "xruns": st.get("xruns")}
        return None

    def _manifest(self):
        """Write session.json, the single source of truth for consumers.

        Consumers must read it to decide which streams exist instead of
        scanning for `cam_*` folders, which would miss the audio stream.
        """
        doc = {
            "schema": SCHEMA, "core_version": CORE_VERSION, "mode": self.mode,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "clock": CLOCK,                           # audio must use the same clock
            "capture": self.cfg["capture"], "warmup_s": self.cfg.get("warmup_s"),
            "streams": [self._stream_entry(s) for s in self.sources],
            "metering": self.cfg.get("_metering"),    # scene properties measured at metering
            "audio": self._audio_entry(),
            "shots": self.shots if self.mode == "calib" else None,
            "budget": ({"rate_bps": self.budget.rate_bps,
                        "tmpfs_free": self.budget.tmp_free,
                        "dest_free": self.budget.dest_free,
                        "max_seconds": self.budget.max_seconds,
                        "limited_by": self.budget.limited_by}
                       if self.budget else None),
            "errors": self.errors,
        }
        (self.tmp_dir / "session.json").write_text(
            json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")

    def _publish(self, limit_s):
        el = max(time.monotonic() - self.t_start, 0) if self.t_start else 0
        free = free_bytes(self.tmp_root)
        self.pub.publish({
            "state": self.state, "elapsed": el, "limit": limit_s,
            "sources": {s.name: s.stats() for s in self.sources},
            "free_mb": free / 1048576,
            "remain": free * 0.9 / max(self.budget.rate_bps if self.budget else 1, 1)})
