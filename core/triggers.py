"""Trigger interface and hardware-independent implementations.

Triggers that need hardware (GPIO buttons) live in the platform layer (`pi/`).
"""
from __future__ import annotations

import sys
import threading
import time


def drain_stdin():
    """Discard pending terminal input.

    Opening the cameras and warming up takes 6-10 s. Keys pressed during that
    time stay in the terminal buffer and would otherwise be consumed by the
    next `input()`, which looks like a selection that did nothing.
    """
    try:
        import termios
        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except Exception:                                 # not POSIX or not a TTY
        pass


# Words that cancel. Defined once here; the duration prompt, EnterKeyTrigger
# and the calibration loop all use it.
CANCEL_KEYS = ("q", "quit", "n", "cancel")


def is_cancel(s) -> bool:
    return str(s).strip().lower() in CANCEL_KEYS


class Trigger:
    def wait_start(self) -> bool: ...
    def should_stop(self) -> bool: ...


class ImmediateTrigger(Trigger):
    def __init__(self):
        self._stop = threading.Event()

    def wait_start(self):
        return True

    def should_stop(self):
        return self._stop.is_set()


class EnterKeyTrigger(Trigger):
    """Press Enter to start, press Enter again to stop.

    stdin is polled with `select` instead of being read by a background
    thread. A thread blocked in `input()` would outlive the recording and
    swallow the first key pressed back in the menu.

    `wait_start()` returning False means the user chose not to start (Ctrl-C,
    EOF, or a cancel word); the caller is responsible for closing the cameras
    and returning to the menu.
    """

    def __init__(self, prompt="  Press Enter to start (q to cancel)..."):
        self.prompt = prompt
        self._armed = False

    def wait_start(self):
        try:
            drain_stdin()
            s = input(self.prompt)
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        return not is_cancel(s)

    def arm_stop(self):
        drain_stdin()                                 # drop keys pressed before the start
        self._armed = True

    def should_stop(self):
        if not self._armed:
            return False
        try:
            import select
            if select.select([sys.stdin], [], [], 0)[0]:
                sys.stdin.readline()
                self._armed = False
                return True
        except Exception:                             # not POSIX or not a TTY
            pass
        return False
