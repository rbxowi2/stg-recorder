"""Storage guard: how long a recording can run before space runs out.

Recording is two-stage: frames are written to RAM (tmpfs) first and moved to
the destination disk afterwards. Either side can run short. If tmpfs fills,
the recording stops early; if the destination is too small, a finished
recording cannot be moved. Both limits are computed before starting and the
tighter one is used.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

SAFETY = 0.90          # use at most 90% of the free space
LOW_WATER_MB = 200     # stop recording when tmpfs drops below this


def free_bytes(path) -> int:
    p = Path(path)
    while not p.exists():
        p = p.parent
    return shutil.disk_usage(p).free


def probe_write_bps(dest: Path, mb: int = 24) -> float:
    """Measure the write speed of the destination.

    Measured rather than assumed because storage devices vary widely
    (an SD card writes roughly 23 MB/s).
    """
    dest.mkdir(parents=True, exist_ok=True)
    probe, blob = dest / ".write_probe.tmp", b"\0" * (1 << 20)
    try:
        t0 = time.monotonic()
        with open(probe, "wb") as f:
            for _ in range(mb):
                f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        return mb * 1048576 / max(time.monotonic() - t0, 1e-6)
    except OSError:
        return 20 * 1048576
    finally:
        probe.unlink(missing_ok=True)


class Budget:
    def __init__(self, tmp_free, dest_free, rate_bps, write_bps):
        self.tmp_free, self.dest_free = tmp_free, dest_free
        self.rate_bps, self.write_bps = rate_bps, write_bps

    @property
    def max_tmp(self):
        return self.tmp_free * SAFETY / max(self.rate_bps, 1)

    @property
    def max_dest(self):
        return self.dest_free * SAFETY / max(self.rate_bps, 1)

    @property
    def max_seconds(self):
        return min(self.max_tmp, self.max_dest)

    @property
    def limited_by(self):
        return "tmpfs" if self.max_tmp < self.max_dest else "destination"

    def transfer_s(self, rec_s):
        return rec_s * self.rate_bps / max(self.write_bps, 1)

    def lines(self, want=None):
        L = [f"Byte rate (measured)  {self.rate_bps/1048576:8.1f} MB/s",
             f"tmpfs free            {self.tmp_free/1048576:8.0f} MB"
             f"  -> max {self.max_tmp:6.0f} s",
             f"Destination free      {self.dest_free/1048576:8.0f} MB"
             f"  -> max {self.max_dest:6.0f} s",
             f"Max recording         {self.max_seconds:8.0f} s"
             f" (limited by {self.limited_by})"]
        if want:
            L.append(f"{want:.0f} s -> about {want*self.rate_bps/1048576:.0f} MB, "
                     f"move takes about {self.transfer_s(want):.0f} s")
        return L
