"""Two GPIO buttons -> `core.menu.Event`.

    from pi.buttons import Buttons
    b = Buttons()
    ev = b.get(timeout=0.2)        # Event.NEXT / Event.OK / None

## Wiring

```
^  upper button   GPIO 5   physical pin 29  -+
   GND                     physical pin 30  -+ three adjacent pins
v  lower button   GPIO 6   physical pin 31  -+
```

GPIO 5 and 6 are chosen deliberately: GPIO 0-8 are pulled up by default at
boot, so each button connects between its GPIO and GND with no external
resistor and does not trigger during boot.

No conflict with I2S (GPIO 18-21) or the OLED (GPIO 2-3).

## Short press only

There is no long press. Destructive actions use a confirmation screen
instead, because a confirmation screen is visible and a long press is not.

    ^ upper    move / cancel / back
    v lower    confirm / select / shoot one set / stop

## Debouncing

Mechanical contacts bounce and produce several edges per press.
`gpiozero`'s `bounce_time` ignores changes for N seconds after an edge.
N = 20 ms is a starting point (typical bounce is 1-5 ms, with a 4x margin).
Acceptance test: 20 presses must produce exactly 20 events; more means
bounce gets through, fewer means the lockout is too long.
"""
from __future__ import annotations

import queue

from core.menu import Action, Event
from core.triggers import Trigger

PIN_UP = 5           # ^ upper button
PIN_DOWN = 6         # v lower button
DEBOUNCE_MS = 20


class Buttons:
    def __init__(self, up=PIN_UP, down=PIN_DOWN, debounce_ms=DEBOUNCE_MS):
        from gpiozero import Button        # the Pi 5 uses the lgpio backend, not RPi.GPIO
        self._q = queue.Queue()
        # pull_up=True: high when released, low when pressed (default pull-up on GPIO 0-8)
        self._up = Button(up, pull_up=True, bounce_time=debounce_ms / 1000)
        self._down = Button(down, pull_up=True, bounce_time=debounce_ms / 1000)
        self._up.when_pressed = lambda: self._q.put(Event.NEXT)
        self._down.when_pressed = lambda: self._q.put(Event.OK)

    def get(self, timeout=0.2):
        """Wait for one event. Returns None on timeout so the caller can do other work."""
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self):
        """Discard queued events.

        Used right after a state change: presses made on the previous screen
        must not be applied to the new one.
        """
        n = 0
        while not self._q.empty():
            self._q.get_nowait()
            n += 1
        return n

    def close(self):
        self._up.close()
        self._down.close()


class ButtonTrigger(Trigger):
    """Stop a recording with the buttons.

    Each event is passed to the menu state machine; the recording stops when
    the state machine returns `Action.FINISH` (one press of `v` while recording).

    `should_stop()` is called by the recording loop every 0.5 s and must not block.
    """

    def __init__(self, buttons, menu):
        self.buttons, self.menu = buttons, menu

    def wait_start(self):
        return True                       # already confirmed in the menu

    def should_stop(self):
        ev = self.buttons.get(timeout=0)  # non-blocking
        return self.menu.handle(ev) == Action.FINISH if ev else False
