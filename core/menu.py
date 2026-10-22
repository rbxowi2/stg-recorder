"""Menu state machine for button operation. Platform-independent.

    idle -> menu -> confirm -> run -> move to disk -> done -> idle

This module does two things: it turns events into state transitions, and it
reports the current state together with its data. It does not touch hardware,
record, move files, or lay out text.

    input       two kinds of events (see Event), produced by the platform's Trigger
    output      view(): state + data; each renderer decides how to format it
    side effect returns an Action for the app layer to execute; results are
                fed back with update()

## Why view() does not return a formatted screen

The terminal and the OLED have different needs:

    terminal   keyboard, scrolling, history; used for parameters and debugging
    OLED       two buttons, five lines; only "what is happening" and "what can I press"

Returning pre-formatted lines would force both renderers to look the same.
The state machine therefore exposes state and data only.

## Why there are only two events

The hardware has two buttons stacked vertically (`^` on GPIO 5 at the top,
`v` on GPIO 6 below), short press only:

    ^ upper button    move, cancel, back
    v lower button    confirm, select, shoot one set, stop

Destructive actions are protected by a confirmation screen rather than a long
press: a confirmation screen is visible, a long press gives no feedback.

## Terminal and buttons do not share events

The terminal jumps directly to an item by number; the buttons cycle and
confirm. Both are only different ways of requesting a transition; the state
itself is shared.

    buttons    handle(Event.NEXT / Event.OK)
    terminal   goto(key)
"""
from __future__ import annotations


class Event:
    """Button events. Short press only; see the module docstring."""

    NEXT = "next"        # ^ upper button (GPIO 5): move / cancel / back
    OK = "ok"            # v lower button (GPIO 6): confirm / select / shoot / stop


class Action:
    """Work the state machine asks the app layer to do. Report back with update()."""

    START_RECORD = "start_record"
    START_CALIB = "start_calib"
    START_PREVIEW = "start_preview"      # four-camera preview (exposure and framing)
    START_MIC_TEST = "start_mic_test"    # microphone levels, left and right separately
    SHOW_INFO = "show_info"              # version and current status
    START_WARMUP = "start_warmup"        # re-run warm-up (terminal only)
    START_EXPOSURE = "start_exposure"    # measure a suggested exposure
    OPEN_PARAMS = "open_params"          # parameter settings (terminal only)
    APPLY_EXPOSURE = "apply_exposure"    # apply the exposure just measured
    WAKE = "wake"                        # wake from sleep (reopen the cameras)
    SHOOT = "shoot"                      # shoot one set in calibration mode
    FINISH = "finish"                    # finish the current task
    SHUTDOWN = "shutdown"


class Item:
    """One menu item.

    `on_oled` decides whether the item appears on the OLED. Tasks that are not
    done in the field stay off it: parameter editing needs input and
    comparison, which two buttons and five lines cannot provide.
    """

    def __init__(self, key, label, action, on_oled, confirm=True):
        self.key = key
        self.label = label
        self.action = action
        self.on_oled = on_oled
        self.confirm = confirm       # show a confirmation screen first


MENU_ITEMS = [
    # Preview comes first: it is what you do before shooting, and it changes
    # nothing, so a mistaken press costs nothing.
    Item("preview",  "preview",    Action.START_PREVIEW,  True,  confirm=False),
    Item("record",   "record",     Action.START_RECORD,   True),
    Item("calib",    "calib set",  Action.START_CALIB,    True),
    # Exposure metering and the microphone test are on the OLED because both
    # are needed in the field, where there is no SSH. Metering runs with one
    # press and needs no input. The microphone test catches a dead microphone
    # (symptom: sound on one side only).
    Item("mic",      "mic test",   Action.START_MIC_TEST, True,  confirm=False),
    # Info is read-only; it answers "which version is running" in the field.
    Item("info",     "info",       Action.SHOW_INFO,      True,  confirm=False),
    Item("expose",   "meter exp",  Action.START_EXPOSURE, True,  confirm=False),
    Item("params",   "params",     Action.OPEN_PARAMS,    False, confirm=False),
    Item("warmup",   "warm up",    Action.START_WARMUP,   False, confirm=False),
    Item("shutdown", "shutdown",   Action.SHUTDOWN,       True),
]


def items(on_oled=False):
    """Items to show. `on_oled=True` returns only the ones usable in the field."""
    return [i for i in MENU_ITEMS if i.on_oled] if on_oled else list(MENU_ITEMS)


def find(key):
    for i in MENU_ITEMS:
        if i.key == key:
            return i
    return None


class MenuMachine:
    """Menu state machine.

    Usage (app layer):

        m = MenuMachine()
        m.update(snapshot)              # feed new status
        act = m.handle(Event.NEXT)      # a button press, may return an Action
        act = m.goto("record")          # terminal jump, may return an Action
        v = m.view()                    # for renderers: state + data

    `snapshot` is the Session status dictionary; missing fields are tolerated.
    """

    IDLE = "idle"
    SLEEP = "sleep"                      # cameras off to save power, OLED still on
    MENU = "menu"
    CONFIRM = "confirm"
    WARMUP = "warmup"
    RUNNING = "running"
    PREVIEW = "preview"                  # four-camera preview, full screen
    MIC_TEST = "mic_test"                # microphone levels
    INFO = "info"                        # core version and status, any key exits
    CALIB = "calib"
    PARAMS = "params"
    EXPOSURE = "exposure"
    EXPOSE_APPLY = "expose_apply"        # metering done, ask whether to apply
    FLUSHING = "flushing"
    DONE = "done"
    ERROR = "error"

    def __init__(self, snapshot=None):
        self.state = self.IDLE
        self.cursor = 0                  # only used by the buttons (on the OLED list)
        self.snap = dict(snapshot or {})
        self.message = ""

    # ---------------------------------------------------------- input from the app
    def update(self, snapshot=None, message=None, error=None, phase=None):
        """The app layer reports the current situation.

        `phase` sets the state directly (see the table below); everything
        else only updates the displayed data.
        """
        if snapshot:
            self.snap.update(snapshot)
        if message is not None:
            self.message = message
        if error:
            self.state, self.message = self.ERROR, str(error)
            return
        table = {"warmup": self.WARMUP, "running": self.RUNNING,
                 "calib": self.CALIB, "params": self.PARAMS,
                 "exposure": self.EXPOSURE, "flushing": self.FLUSHING,
                 "expose_apply": self.EXPOSE_APPLY, "sleep": self.SLEEP,
                 "preview": self.PREVIEW, "mic_test": self.MIC_TEST,
                 "info": self.INFO,
                 "done": self.DONE, "idle": self.IDLE}
        if phase in table:
            self.state = table[phase]

    # ---------------------------------------------------------- events
    def handle(self, event):
        """Handle one button event. Returns an Action or None."""
        st = self.state

        if st == self.SLEEP:
            # While asleep, any key only wakes the system and does not open the
            # menu: reopening the cameras takes several seconds and the screen
            # must show what it is waiting for.
            return Action.WAKE

        if st == self.IDLE:
            self.state, self.cursor = self.MENU, 0
            return None                          # both buttons open the menu

        if st == self.MENU:
            vis = items(on_oled=True)
            if event == Event.NEXT:
                self.cursor = (self.cursor + 1) % len(vis)
                return None
            it = vis[self.cursor]
            if it.confirm:
                self.state = self.CONFIRM
                return None
            return self._run(it)

        if st == self.CONFIRM:
            if event == Event.NEXT:              # ^ = cancel
                self.state = self.MENU
                return None
            return self._run(items(on_oled=True)[self.cursor])

        if st == self.RUNNING:
            # One press stops the recording; there is no confirmation step.
            if event == Event.OK:
                return Action.FINISH             # the app reports the next state (moving)
            return None                          # ^ has no effect while recording

        if st == self.CALIB:
            if event == Event.OK:
                return Action.SHOOT
            return Action.FINISH                 # ^ = end calibration

        if st == self.EXPOSE_APPLY:
            self.state = self.IDLE
            # Never applied automatically: a new exposure changes every later
            # shot, so it must be an explicit choice.
            return None if event == Event.NEXT else Action.APPLY_EXPOSURE

        if st in (self.PREVIEW, self.MIC_TEST, self.INFO):
            # Any button exits. These screens have no room to explain which
            # button does what.
            self.state = self.IDLE
            return None

        if st in (self.WARMUP, self.FLUSHING, self.PARAMS, self.EXPOSURE):
            return None                          # busy, input ignored

        if st in (self.DONE, self.ERROR):
            self.state = self.IDLE               # any key returns to idle
            self.message = ""
        return None

    def goto(self, key):
        """Terminal: jump directly to an item, skipping the cycling.

        No confirmation screen here: the terminal asks with `prompt()` itself.
        The confirmation screen exists for the two-button OLED.
        """
        it = find(key)
        return self._run(it) if it else None

    def _run(self, it):
        self.state = {Action.START_RECORD: self.RUNNING,
                      Action.START_CALIB: self.CALIB,
                      Action.START_PREVIEW: self.PREVIEW,
                      Action.START_MIC_TEST: self.MIC_TEST,
                      Action.SHOW_INFO: self.INFO,
                      Action.START_WARMUP: self.WARMUP,
                      Action.START_EXPOSURE: self.EXPOSURE,
                      Action.OPEN_PARAMS: self.PARAMS,
                      }.get(it.action, self.state)
        return it.action

    # ---------------------------------------------------------- for renderers
    def view(self, on_oled=False):
        """Current state and its data. No formatting.

        `data` holds raw values (`None` means not known yet); whether a value
        is shown as `2.6G` or `2670 MB` is up to the renderer.
        """
        s = self.snap
        v = {"state": self.state, "message": self.message, "data": {}}

        if self.state in (self.MENU, self.CONFIRM):
            vis = items(on_oled=on_oled)
            v["items"] = [i.label for i in vis]
            v["keys"] = [i.key for i in vis]
            v["cursor"] = self.cursor if on_oled else None
            if self.state == self.CONFIRM:
                v["data"]["max_seconds"] = s.get("max_seconds")

        pick = {
            self.IDLE: ("cams", "free_bytes", "max_seconds", "audio_ok"),
            self.SLEEP: ("cams", "free_bytes", "asleep_s"),
            self.WARMUP: ("warmup_s", "ready"),
            self.RUNNING: ("elapsed_s", "limit_s", "free_bytes", "remain_s",
                           "fps", "drops", "audio_s", "xruns"),
            # `wait_s` = countdown between the press and the actual shot
            # (lets the rig settle; see do_calib in rig.py)
            self.CALIB: ("shots", "last_ok", "wait_s"),
            self.EXPOSE_APPLY: ("exposure_us", "gain",
                                "sug_exposure_us", "sug_gain", "loss_pct"),
            self.FLUSHING: ("move_pct", "moved_mb", "total_mb", "free_bytes"),
            self.DONE: ("files", "total_mb", "dest"),
            # The version comes from core/__init__.py; the app layer feeds it in.
            self.INFO: ("core_version", "core_date", "cams", "audio_ok",
                        "free_bytes", "host"),
        }.get(self.state, ())
        for k in pick:
            v["data"][k] = s.get(k)
        return v
