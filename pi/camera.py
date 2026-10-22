"""Cameras: detection, UVC settings, video source. Specific to Picamera2 and v4l2-ctl.

Camera names are bound to physical USB ports (see `detect()`), and the
calibration is bound to those names. If names and calibration disagree, the
intrinsics of one camera are applied to another camera's images: the
reconstruction is wrong and no error is reported. Moving a camera to a
different port therefore requires a new calibration set.
The number of cameras is not fixed; every detected camera is used.

Source contract (video and audio): open / warmup / begin / step / end / close / stats
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import time
from pathlib import Path

try:
    from picamera2 import Picamera2
except ImportError:
    Picamera2 = None


NAMES = ["cam_A", "cam_B", "cam_C", "cam_D", "cam_E", "cam_F", "cam_G", "cam_H"]


def _node_table():
    t = {}
    for n in glob.glob("/sys/class/video4linux/video*"):
        try:
            if open(os.path.join(n, "index")).read().strip() != "0":
                continue                       # index 1 is the metadata node
            real = os.path.realpath(os.path.join(n, "device"))
        except OSError:
            continue
        mu = re.search(r"/[0-9a-f]*?([0-9a-f]{6})\.usb/", real)
        mp = re.search(r"/\d+-([\d.]+):(\d+\.\d+)$", real)
        mc = re.search(r"/(xhci-hcd\.\d+)/", real)
        if mu and mp:
            t[(mu.group(1), mp.group(1), mp.group(2))] = (
                "/dev/" + os.path.basename(n), mc.group(1) if mc else "?")
    return t


def port_of(cam_id):
    """Extract the physical USB port from a libcamera Id, e.g. `200000-1`.

    In `usb@200000-1:1.0-32e6:9221`:
        `200000`  USB controller
        `-1`      physical port on that controller

    Together they are unique and do not change when the same socket is
    re-plugged; this is the camera's identity. Using a different socket
    changes it, which also requires a new calibration.
    """
    m = re.search(r"usb@([0-9a-f]+)-([\d.]+):", cam_id or "")
    return f"{m.group(1).zfill(6)}-{m.group(2)}" if m else None


def detect(ports=None):
    """Enumerate cameras. `ports` maps USB port -> name (from `config.json`).

    Names are bound to USB ports, not to the sorted position. With
    position-based names, a missing camera shifts the names of the ones
    after it:

        all four present             300000-1 missing
        200000-1  cam_A              200000-1  cam_A
        200000-2  cam_B              200000-2  cam_B
        300000-1  cam_C              (missing)
        300000-2  cam_D              300000-2  cam_C   <- was cam_D

    Because the calibration stores name -> pose, preprocessing would then
    apply cam_C's extrinsics to cam_D's images, silently.

    When `ports` is None or empty the names are assigned in sorted order; that
    is the first-run path that creates the mapping (see `rig.ensure_ports()`).
    """
    if Picamera2 is None:
        return []
    try:
        infos = Picamera2.global_camera_info()
    except Exception as e:                                  # noqa: BLE001
        print(f"  Camera enumeration failed: {e!r}")
        return []
    tab = _node_table()
    ports = dict(ports or {})
    used = set(ports.values())
    out = []
    for i, info in enumerate(sorted(infos, key=lambda x: x["Id"])):
        m = re.search(r"usb@([0-9a-f]+)-([\d.]+):(\d+\.\d+)-", info["Id"])
        node, ctrl = (tab.get((m.group(1).zfill(6), m.group(2), m.group(3)),
                              (None, "?")) if m else (None, "?"))
        port = port_of(info["Id"])
        name = ports.get(port)
        if name is None:
            # unknown port (or no mapping yet): next unused name
            name = next((n for n in NAMES if n not in used), f"cam_{i}")
            used.add(name)
        out.append({"name": name, "num": info["Num"], "id": info["Id"],
                    "port": port, "node": node, "controller": ctrl})
    return out


def setup_camera(node):
    """UVC-level settings.

    `exposure_dynamic_framerate=0` stops the camera from lowering the frame
    rate to lengthen exposure; otherwise a darker scene drops it to 15 fps
    and doubles the synchronization error.
    Focus must stay fixed: changing focus changes the intrinsics and
    invalidates the calibration.
    """
    if not node:
        return
    for names, val in (
            (["auto_exposure", "exposure_auto"], 3),
            (["exposure_dynamic_framerate", "exposure_auto_priority"], 0),
            (["white_balance_automatic", "white_balance_temperature_auto"], 1),
            (["focus_automatic_continuous", "focus_auto"], 0)):
        for nm in names:
            if subprocess.run(["v4l2-ctl", "-d", node, f"--set-ctrl={nm}={val}"],
                              capture_output=True).returncode == 0:
                break


class VideoSource:
    kind = "video"

    def __init__(self, cam):
        self.cam, self.name = cam, cam["name"]
        self.p = self.stream = self.index = None
        self.n = self.bad = self.off = self._gaps = 0
        self._t0 = self._last_ts = self._period = None
        self.error = None

    def open(self, cfg):
        if Picamera2 is None:
            raise RuntimeError("picamera2 is not available (not running on a Pi?)")
        self.p = Picamera2(camera_num=self.cam["num"])
        self.p.configure(self.p.create_video_configuration(
            main={"size": tuple(cfg["resolution"]), "format": "MJPEG"},
            buffer_count=int(cfg.get("buffer_count", 6))))
        self.p.start()
        self.p.set_controls({"AeEnable": False,
                             "ExposureTime": int(cfg["exposure_us"]),
                             "AnalogueGain": float(cfg["gain"])})

    def warmup(self, seconds):
        """Discard frames while the sensor settles and measure the real byte rate.

        The length must be measured after truncating at the JPEG end marker:
        `make_buffer()` returns the allocated buffer (fixed maximum size),
        which overestimates about 5x. JPEG size also varies 3-7x between
        scenes (43-483 KB per frame measured), so an estimate cannot be used.
        """
        time.sleep(min(seconds, 2.5))
        n = total = 0
        t0 = time.monotonic()
        while time.monotonic() - t0 < max(seconds - 2.5, 1.0):
            r = self.p.capture_request()
            b = r.make_buffer("main").tobytes()
            r.release()
            eoi = b.rfind(b"\xff\xd9")
            total += (eoi + 2) if eoi >= 0 else 0
            n += 1
        dt = max(time.monotonic() - t0, 1e-6)
        return {"frames": n, "bytes": total, "bytes_per_frame": total / max(n, 1),
                "bytes_per_second": total / dt, "fps": n / dt}

    def begin(self, session_dir):
        d = session_dir / self.name
        d.mkdir(parents=True, exist_ok=True)
        self.stream = open(d / "stream.mjpg", "wb", buffering=1 << 20)
        self.index = open(d / "index.jsonl", "w", encoding="utf-8")
        # _t0 is not set here: the start barrier comes after opening the files,
        # and counting that wait would underestimate fps. It starts at the
        # first capture instead.

    def step(self):
        if self._t0 is None:
            self._t0 = time.monotonic()
        r = self.p.capture_request()
        buf = r.make_buffer("main").tobytes()
        ts = r.get_metadata().get("SensorTimestamp")
        r.release()

        eoi = buf.rfind(b"\xff\xd9")
        if eoi < 0:                                   # no end marker = corrupt frame
            self.bad += 1
            return 0
        jpg = buf[:eoi + 2]
        self.stream.write(jpg)
        self.index.write(json.dumps(
            {"i": self.n, "ts_ns": ts, "off": self.off, "len": len(jpg)}) + "\n")

        if ts and self._last_ts:                      # dropped-frame detection
            d = ts - self._last_ts
            if self._period is None:
                self._period = d
            elif d > self._period * 1.5:
                self._gaps += 1
            else:
                self._period = 0.9 * self._period + 0.1 * d
        self._last_ts = ts
        self.off += len(jpg)
        self.n += 1
        return len(jpg)

    def shot(self, path):
        """Calibration set: capture one image and write it to disk."""
        r = self.p.capture_request()
        buf = r.make_buffer("main").tobytes()
        r.release()
        eoi = buf.rfind(b"\xff\xd9")
        if eoi < 0:
            self.bad += 1
            return 0
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(buf[:eoi + 2])
        self.n += 1
        return eoi + 2

    def end(self):
        for f in (self.stream, self.index):
            if f:
                f.flush()
                f.close()
        self.stream = self.index = None

    def close(self):
        try:
            if self.p:
                self.p.close()
        except Exception:                             # noqa: BLE001
            pass
        self.p = None

    def stats(self):
        el = (time.monotonic() - self._t0) if self._t0 else 0.0
        return {"frames": self.n, "bad": self.bad, "bytes": self.off,
                "drops": self._gaps, "fps": self.n / el if el > 0.5 else 0.0,
                "kb": self.off / max(self.n, 1) / 1024, "error": self.error}


    # ------------------------------------------------------- exposure
    def set_exposure(self, exposure_us, gain):
        """Send exposure settings. Fire-and-forget.

        `set_controls()` queues the controls and reports nothing, so a camera
        that did not take the setting gives no sign of it. Verification is
        done by comparing image brightness (see `rig.verify_exposure()`).
        """
        self.p.set_controls({"AeEnable": False,
                             "ExposureTime": int(exposure_us),
                             "AnalogueGain": float(gain)})

    def read_controls(self):
        """Read back the exposure and gain the driver reports. None if unavailable.

        Picamera2's `capture_metadata()` cannot be used: these UVC cameras
        only report `SensorTimestamp`; `ExposureTime` and `AnalogueGain` are
        None. v4l2-ctl is the only way, as in `wb_read()`.

        Units differ from `set_exposure()`: `exposure_time_absolute` is in
        100 us steps (6509 us -> 65), and `gain` is the driver's own 0-63
        scale with no known mapping to `AnalogueGain` (1.0-1.5). Gain can
        only be compared between cameras, not against the requested value.
        The value can also be stale, so it is for debugging only.
        """
        if not self.cam.get("node"):
            return None
        r = subprocess.run(["v4l2-ctl", "-d", self.cam["node"],
                            "--get-ctrl=exposure_time_absolute,gain"],
                           capture_output=True, text=True)
        out = {}
        for line in r.stdout.splitlines():
            k, _, v = line.partition(":")
            try:
                out[k.strip()] = int(v.strip().split()[0])
            except (ValueError, IndexError):
                pass
        if "exposure_time_absolute" not in out:
            return None
        return {"exposure_us": out["exposure_time_absolute"] * 100,
                "gain_raw": out.get("gain")}

    # ------------------------------------------------------- white balance
    def wb_read(self):
        """Read the colour temperature (K) that AWB has converged to. None if unavailable.

        While AWB is on, this control is marked inactive, but the value read
        back reflects the automatic result (four cameras were measured at
        4600 / 4000 K, the inconsistency a lock would remove).
        """
        if not self.cam.get("node"):
            return None
        r = subprocess.run(["v4l2-ctl", "-d", self.cam["node"],
                            "--get-ctrl=white_balance_temperature"],
                           capture_output=True, text=True)
        try:
            return int(r.stdout.strip().split(":")[1])
        except Exception:                             # noqa: BLE001
            return None

    def wb_lock(self, kelvin):
        """Turn AWB off and lock the colour temperature. Returns whether it succeeded."""
        if not self.cam.get("node"):
            return False
        for nm, val in (("white_balance_automatic", 0),
                        ("white_balance_temperature", int(kelvin))):
            if subprocess.run(["v4l2-ctl", "-d", self.cam["node"],
                               f"--set-ctrl={nm}={val}"],
                              capture_output=True).returncode != 0:
                return False
        return True

    def gain_range(self):
        """Ask the camera for its gain range; it differs between models."""
        try:
            lo, hi, _ = self.p.camera_controls["AnalogueGain"]
            return float(lo), float(hi)
        except Exception:                             # noqa: BLE001
            return 1.0, 1.484                         # measured for these UVC cameras

    def gray(self, skip=6):
        """Discard `skip` frames so new controls apply, then decode one full-size grayscale frame.

        Called during setup only; recording never decodes (MJPEG is passed through).

        No downscaling: downscaling averages areas, so small over-exposed
        regions get diluted (0.441% over-exposed at full resolution measured
        0.000% on a 1/8 thumbnail).
        """
        import numpy as np
        for _ in range(skip):
            self.p.capture_request().release()
        r = self.p.capture_request()
        buf = r.make_buffer("main").tobytes()
        r.release()
        eoi = buf.rfind(b"\xff\xd9")
        if eoi < 0:
            return None
        try:
            import io as _io
            from PIL import Image
            im = Image.open(_io.BytesIO(buf[:eoi + 2])).convert("L")
            return np.asarray(im, dtype=np.uint8)
        except Exception:                             # noqa: BLE001
            return None

    def brightness(self, skip=6):
        """Discard `skip` frames, then decode one frame and measure its brightness.

        Returns (mean brightness, over-exposed %, under-exposed %), on 0-255.
        Called during setup only; recording never decodes.
        """
        for _ in range(skip):
            self.p.capture_request().release()
        r = self.p.capture_request()
        buf = r.make_buffer("main").tobytes()
        r.release()
        eoi = buf.rfind(b"\xff\xd9")
        if eoi < 0:
            return None
        try:
            import io
            from PIL import Image
            im = Image.open(io.BytesIO(buf[:eoi + 2]))
            im.draft("L", (im.width // 8, im.height // 8))   # downscale while decoding, much faster
            px = im.convert("L").tobytes()
        except Exception:                             # noqa: BLE001
            return None
        n = len(px)
        return (sum(px) / n,
                100.0 * sum(1 for v in px if v >= 250) / n,
                100.0 * sum(1 for v in px if v <= 8) / n)
