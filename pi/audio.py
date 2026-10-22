"""I2S stereo audio source (Raspberry Pi 5 implementation).

Two ICS-43434 microphones form a stereo pair (the SEL pin selects left or
right) on I2S0.

## Why an arecord subprocess instead of reading ALSA from Python

Measured on the target hardware:

  true sample rate 47997.732 Hz (47.3 ppm slower than the nominal 48000)
  first and second half of a 5-minute run differ by 0.8 ppm -> the rate is constant
  accumulated error over 10 s is 0.47 ms, 24x smaller than the 11.10 ms
  spread already present between the cameras

"Start time + rate" is therefore enough to rebuild the timeline; per-block
timestamps are not needed. arecord is a separate process, unaffected by the
Python GIL, and needs no extra package.

## The one real risk: xruns

After a dropped block, "start time + rate" is offset from that point on, and
arecord only reports it on stderr. stderr is therefore captured to a file and
the xruns are recorded in index.json.
"""
from __future__ import annotations

import json
import re
import signal
import subprocess
import time
from pathlib import Path

# Nominal rate requested from ALSA. The ICS-43434 supports 23.2-51.6 kHz only,
# so neither 44.1 nor 96 kHz works; at 48 kHz SCK = 64 x 48000 = 3.072 MHz.
RATE = 48000
CHANNELS = 2
FORMAT = "S32_LE"
BYTES_PER_FRAME = 8                      # 2 ch x 32 bit

# Measured true sample rate. Re-measure after changing the board or the OS.
# It is written to index.json so consumers know the rate at recording time.
MEASURED_RATE_HZ = 47997.732
MEASURED_RATE_SOURCE = "five-minute linear fit (-47.3 ppm)"

# Physical distance between the two microphones (mm).
# The player uses it to place the two virtual speakers (world-locked stereo).
# It is a property of this rig; override it with `mic_spacing_mm` in
# config.json instead of changing the code.
# A spacing wider than the camera span puts the virtual speakers outside the
# window frame, which is physically correct: sound comes from a wider area
# than the window shows.
MIC_SPACING_MM = 200.0

# Card name hints. The googlevoicehat overlay provides a generic I2S stereo capture device.
CARD_HINTS = ("sndrpigooglevoi", "googlevoicehat", "i2s")

_XRUN = re.compile(r"overrun|xrun|underrun", re.I)

# Lower end of the MIC TEST level bar (dBFS). See `bar_ratio()`.
DB_FLOOR = -80.0

# The recorder applies no level analysis and no gain: it records raw data.
# Gain is a presentation concern and belongs to the player. This also avoids
# scanning the audio file at the end of a recording, while hundreds of
# megabytes of video are being moved.
def find_card():
    """Find the I2S card in /proc/asound/cards and return its number, or None.

    The card number is not hard-coded: it depends on what was enumerated at
    boot (HDMI devices usually take 0 and 1).
    """
    try:
        txt = Path("/proc/asound/cards").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in txt.splitlines():
        m = re.match(r"\s*(\d+)\s+\[(\S+)", line)
        if m and any(h in m.group(2).lower() for h in CARD_HINTS):
            return int(m.group(1))
    return None


def _cmd(card, seconds=None):
    c = ["arecord", "-D", f"hw:{card}", "-f", FORMAT,
         "-c", str(CHANNELS), "-r", str(RATE), "-t", "wav"]
    if seconds:
        c += ["-d", str(int(seconds))]
    return c


def dbfs(rms):
    """Linear RMS (0-1) to dBFS. Zero or negative returns the floor, never -inf."""
    import math
    return DB_FLOOR if rms <= 0 else max(DB_FLOOR, 20.0 * math.log10(rms))


def bar_ratio(db):
    """dBFS to a bar length ratio (0-1).

    The range is -80 to 0 dBFS. I2S MEMS microphones reserve full scale for
    120+ dB SPL, and normal speech is around -60 dBFS; with a -60 to 0 range
    the bar would sit at the bottom while someone talks.

    This is not gain: samples are untouched and the displayed number is still
    the true dBFS; only the bar scale is stretched.
    """
    return min(1.0, max(0.0, (db - DB_FLOOR) / (0.0 - DB_FLOOR)))


class LevelMeter:
    """Live I2S levels, left and right separately. Used by the MIC TEST screen.

        m = LevelMeter()
        m.open()                 # raises with a readable message on failure
        while ...:
            lv = m.read()        # (left dBFS, right dBFS); last value if no new data
        m.close()

    Completely separate from recording: it only reads and computes RMS, writes
    no files and applies no gain. The sound card is exclusive, so this cannot
    run while recording.

    It still uses `arecord` with the same card lookup and parameters
    (`find_card()`, S32_LE / 2 ch / 48 kHz) so that "works in the test but not
    when recording" is as unlikely as possible; the only difference is raw
    output to stdout instead of a WAV file.
    """

    def __init__(self, block_ms=100):
        self.block = max(1, int(RATE * block_ms / 1000)) * BYTES_PER_FRAME
        self.proc = None
        self.card = None
        self.last = (DB_FLOOR, DB_FLOOR)

    def open(self):
        self.card = find_card()
        if self.card is None:
            raise RuntimeError("I2S sound card not found")
        self.proc = subprocess.Popen(
            ["arecord", "-D", f"hw:{self.card}", "-f", FORMAT,
             "-c", str(CHANNELS), "-r", str(RATE), "-t", "raw"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def read(self):
        """Return `(left dBFS, right dBFS)`.

        If a full block is not available, the previous value is returned: a
        frozen bar for one tick is better than a flash to -80, which would be
        read as a dead microphone.
        """
        if self.proc is None or self.proc.stdout is None:
            return self.last
        raw = self.proc.stdout.read(self.block)
        if not raw or len(raw) < BYTES_PER_FRAME:
            return self.last
        self.last = rms_pair(raw)
        return self.last

    def close(self):
        """Must be called: the sound card is exclusive and recording needs it."""
        if self.proc is None:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=2)
        except Exception:                                    # noqa: BLE001
            try:
                self.proc.kill()
            except Exception:                                # noqa: BLE001
                pass
        finally:
            self.proc = None


def rms_pair(raw):
    """A block of S32_LE stereo raw data -> `(left dBFS, right dBFS)`.

    The ICS-43434 has 24 valid bits with the low 8 bits zero, so samples are
    divided by 2^31 (the same scaling the player uses when reading the WAV).
    """
    import numpy as np
    n = (len(raw) // BYTES_PER_FRAME) * BYTES_PER_FRAME
    if n == 0:                    # not even one frame: return the floor directly
        return (DB_FLOOR, DB_FLOOR)   # instead of averaging an empty array (numpy warns)
    a = np.frombuffer(raw[:n], "<i4").astype("float64") / 2147483648.0
    a = a.reshape(-1, CHANNELS)
    return tuple(dbfs(float(np.sqrt((a[:, c] ** 2).mean())))
                 for c in range(CHANNELS))


class AudioSource:
    """Same contract as `VideoSource` (see core/session.py).

    Three differences: `kind` is "audio", there is no `.cam`, and there is no
    `shot()` (calibration takes single images with no continuous timeline, so
    calibration mode never adds this source).
    """

    kind = "audio"
    name = "audio"

    def __init__(self):
        self.p = None                    # not None = opened (Session filters on this)
        self.card = None
        self.proc = None
        self.dir = self.wav = self.errlog = None
        self.start_ts_ns = None
        self.bytes_at_end = 0
        self.xruns = []
        self.mic_spacing_mm = MIC_SPACING_MM
        self.error = None
        self._errfh = None

    # ------------------------------------------------------------ open
    def open(self, cfg):
        self.mic_spacing_mm = float(cfg.get("mic_spacing_mm", MIC_SPACING_MM))
        self.card = find_card()
        if self.card is None:
            raise RuntimeError("I2S sound card not found. "
                               "config.txt needs dtparam=i2s=on and "
                               "dtoverlay=googlevoicehat-soundcard, followed by a reboot")
        # Short probe: confirm the card opens and accepts the format.
        r = subprocess.run(_cmd(self.card, 1) + ["/dev/null"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"hw:{self.card} cannot be opened: {r.stderr.strip()[:200]}")
        self.p = self.card                # mark as opened

    def warmup(self, seconds):
        """Measure the actual byte rate.

        The audio byte rate is fixed (sample rate x 8), unlike MJPEG which
        varies 3-7x with the scene. It is still measured once to confirm the
        device actually delivers data.
        """
        t = max(min(seconds, 2.0), 0.5)
        t0 = time.monotonic()
        r = subprocess.run(_cmd(self.card) + ["-d", str(int(max(t, 1))),
                                              "/dev/stdout"],
                           capture_output=True)
        dt = max(time.monotonic() - t0, 1e-6)
        got = max(len(r.stdout) - 44, 0)              # minus the WAV header
        return {"frames": got // BYTES_PER_FRAME, "bytes": got,
                "bytes_per_second": RATE * BYTES_PER_FRAME,
                "measured_bytes_per_second": got / dt,
                "fps": 0.0}                            # fps does not apply to audio

    # ------------------------------------------------------------ recording
    def begin(self, session_dir):
        """Prepare paths only; recording starts at the first step().

        Same pattern as `_t0` in VideoSource: `Session.record()` calls
        `begin()` on every source, then aligns them with a barrier and waits
        for `t_start`. Starting here would put audio half a second ahead.
        """
        self.dir = Path(session_dir) / "audio"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.wav = self.dir / "audio.wav"
        self.errlog = self.dir / "arecord.log"

    def step(self):
        """The first call starts arecord; later calls only check that it is alive.

        The sleep is required: `Session.record()` calls `src.step()` in a
        tight loop, and returning immediately would saturate a CPU core that
        the cameras need.
        """
        if self.proc is None:
            self._errfh = open(self.errlog, "w", encoding="utf-8")
            self.proc = subprocess.Popen(
                _cmd(self.card) + [str(self.wav)],
                stdout=subprocess.DEVNULL, stderr=self._errfh)
            # This is when the start command was sent, not when the first
            # sample was taken; ALSA buffering adds about one period. That
            # systematic offset is corrected by the measured `offset_ms`.
            self.start_ts_ns = time.monotonic_ns()
            return 0
        if self.proc.poll() is not None:
            raise RuntimeError(f"arecord exited early (returncode {self.proc.returncode})")
        time.sleep(0.2)
        return 0

    def end(self):
        """Send SIGINT so arecord finalizes the WAV header, then write index.json."""
        if self.proc is not None:
            try:
                self.proc.send_signal(signal.SIGINT)
                self.proc.wait(timeout=10)
            except Exception:                          # noqa: BLE001
                self.proc.kill()
                self.proc.wait(timeout=5)
            self.proc = None
        if self._errfh:
            self._errfh.close()
            self._errfh = None
        self._collect()
        self._write_index()

    def close(self):
        if self.proc is not None:
            self.proc.kill()
            self.proc = None

    # ------------------------------------------------------------ finishing
    def _collect(self):
        self.bytes_at_end = self.wav.stat().st_size if self.wav.exists() else 0
        if self.errlog and self.errlog.exists():
            txt = self.errlog.read_text(encoding="utf-8", errors="replace")
            self.xruns = [l.strip() for l in txt.splitlines() if _XRUN.search(l)]

    @property
    def frames(self):
        return max(self.bytes_at_end - 44, 0) // BYTES_PER_FRAME

    def _write_index(self):
        """Write `audio/index.json`.

        `sample_rate` is the nominal rate requested from ALSA;
        `sample_rate_measured_hz` is the measured true rate, which consumers
        should use to rebuild the timeline.
        """
        doc = {
            "schema": "audio/1",
            "start_ts_ns": self.start_ts_ns,
            "sample_rate": RATE,
            "sample_rate_measured_hz": MEASURED_RATE_HZ,
            "sample_rate_source": MEASURED_RATE_SOURCE,
            "channels": CHANNELS,
            "format": FORMAT,
            "valid_bits": 24,
            "frames": self.frames,
            "device": f"ICS-43434 x2 (I2S0, SEL=GND/3V3), hw:{self.card}",
            "mic_spacing_mm": self.mic_spacing_mm,
            "offset_ms": None,            # filled in after a clap measurement
            "xruns": self.xruns,
        }
        (self.dir / "index.json").write_text(
            json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
        return doc

    # ------------------------------------------------------------ status
    def stats(self):
        n = (self.wav.stat().st_size if (self.wav and self.wav.exists())
             else 0)
        frames = max(n - 44, 0) // BYTES_PER_FRAME
        return {"frames": frames,
                "seconds": round(frames / MEASURED_RATE_HZ, 2),
                "bytes": n,
                "xruns": len(self.xruns),
                "fps": 0.0}
