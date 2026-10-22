"""Status publisher interface and the terminal implementation.

`StdoutPublisher` only uses standard output and is platform-independent.
Implementations that need hardware (the OLED) live in the platform layer (`pi/`).
"""
from __future__ import annotations

import sys


def fmt_source(v: dict) -> str:
    """How one source appears on the status line.

    Not every source has a frame rate. An audio source reports `fps` as 0.0,
    and printing `audio: 0.0` would look like a failure, so audio shows the
    number of seconds recorded instead.

    The decision is based on the presence of the `seconds` key, not on the
    source name; the core does not assume the audio source is called `audio`.
    """
    if v.get("seconds") is not None:
        x = v.get("xruns") or 0
        return f"{v['seconds']:5.1f}s" + (f" !{x}" if x else "")
    return f"{v.get('fps', 0):4.1f}"


class StatusPublisher:
    def publish(self, snap: dict) -> None: ...

    def message(self, text: str, oled: bool = True) -> None:
        """A one-off message.

        Use `oled=False` for messages about something that already has a
        status screen. Otherwise the same event would be shown twice, once by
        the state machine and once here, in two different formats.

            status screen   "what is happening now"   the state machine is the only source
            message()       "something happened"      log-like, for the terminal

        Rule: if the event has a corresponding state, pass `oled=False`.
        """

    def close(self) -> None: ...


class StdoutPublisher(StatusPublisher):
    """Single-line overwrite on the terminal. More robust over SSH than curses."""

    def __init__(self):
        self._dirty = False

    def publish(self, s: dict) -> None:
        cams = "  ".join(f"{n}:{fmt_source(v)}"
                         for n, v in sorted(s.get("sources", {}).items()))
        line = (f"\r[{s.get('state')}] {s.get('elapsed', 0):6.1f}s"
                + (f"/{s['limit']:.0f}s" if s.get("limit") else "")
                + f"  {cams}  free {s.get('free_mb', 0):6.0f}MB"
                + (f"  left {s['remain']:5.0f}s" if s.get("remain") else "")
                + "   ")
        sys.stdout.write(line[:150])
        sys.stdout.flush()
        self._dirty = True

    def message(self, text: str, oled: bool = True) -> None:
        # The terminal ignores `oled`; that flag only controls the OLED.
        if self._dirty:
            sys.stdout.write("\n")
            self._dirty = False
        print(text)

    def close(self) -> None:
        if self._dirty:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._dirty = False
