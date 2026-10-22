"""Pi peripherals: GPIO trigger.

The core only knows the `core.triggers.Trigger` interface, so how the hardware
is wired here does not affect the state machine.

The OLED is not here: the driver is in `oled.py` and the screens in `oled_ui.py`.
"""
from __future__ import annotations

from core.triggers import Trigger


class GpioTrigger(Trigger):
    """Placeholder: connect button edge events to wait_start / should_stop.

    Two buttons, short press only (`^` on GPIO 5 at the top, `v` on GPIO 6
    below), with debouncing.
    """

    def __init__(self, *a, **k):
        raise NotImplementedError("GPIO trigger not implemented; the interface is ready")
