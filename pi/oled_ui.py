"""Draw the state machine's view() on the 128x64 OLED.

    from pi.oled_ui import OledRenderer
    oled = OledRenderer(get_menu(), fallback=StdoutPublisher())

The hardware driver is in `oled.py`; this module only decides what to draw.

## It reads the state machine instead of waiting to be pushed

A background thread asks the state machine for the current state every
250 ms. When the state changes the screen follows, without anyone having to
remember to push an update. Adding a feature only requires adding a state to
the state machine; the OLED picks it up automatically.

## Why a background thread

A full update takes about 97 ms (see `oled.py`), while one video frame is
33 ms. Drawing synchronously would cost three frames. The GIL is not an
issue: I2C writes are system calls and release it.

## Line breaking is measured in pixels

PIL's built-in `load_default()` font is proportional ('M' is 9 px, '1' is
5 px), so counting characters would cut text off. The usable width is 123 px
(inside the border, text starts at x=3); one line is 11 px, five lines
including the title.
"""
from __future__ import annotations

import threading
import time

from core.publishers import StatusPublisher

W, H = 128, 64
MAXW = 123                 # usable width (px)
ROWS = 4                   # lines available below the title
PERIOD = 0.25              # refresh period (97 ms per frame, ~3x margin)
MSG_HOLD = 2.5             # seconds a message stays before the status screen returns


def _ascii(s):
    """The built-in bitmap font covers latin-1 only; anything else becomes '?'.

    Showing '?' is better than losing the whole screen to an exception: the
    OLED is the only feedback in the field.
    """
    # Keep `\t`: the title uses it to separate left and right-aligned parts (see _render).
    return "".join(c if 32 <= ord(c) < 127 or c == "\t" else "?"
                   for c in str(s))


def _mb(b):
    """Bytes -> human-readable string. Formatting happens here, not in the state machine."""
    if b is None:
        return "--"
    g = b / 1e9
    return f"{g:.1f}G" if g >= 1 else f"{b/1e6:.0f}M"


def _s(v):
    return "--" if v is None else f"{v:.0f}s"


class OledRenderer(StatusPublisher):
    """State machine -> 128x64 screen.

    Also implements `StatusPublisher`, because `Session` pushes snapshots while
    recording; some numbers (seconds recorded, time left) are only known
    there. Pushed values are stored in the state machine, and the screen
    still reads only from the state machine.
    """

    def __init__(self, menu, addr=0x3C, bus_no=1, fallback=None):
        from PIL import Image, ImageDraw, ImageFont
        from pi.oled import Ssd1306
        self._Image, self._Draw = Image, ImageDraw
        self._font = ImageFont.load_default()
        self._dev = Ssd1306(addr, bus_no)
        self._menu = menu
        self._fb = fallback
        self._msg = None                     # (lines, expiry time)
        self._last = None
        self._dim = False
        self._paused = False
        self.errors = 0
        self._warned = False
        self._stop = threading.Event()
        self._th = threading.Thread(target=self._loop, name="oled", daemon=True)
        self._th.start()

    # ------------------------------------------------------------ interface
    def publish(self, snap: dict) -> None:
        """Status pushed by Session. Stored in the state machine, not drawn directly."""
        srcs = (snap.get("sources") or {}).values()
        fps = [v.get("fps") for v in srcs if v.get("seconds") is None]
        aud = [v.get("seconds") for v in srcs if v.get("seconds") is not None]
        self._menu.update(snapshot={
            "elapsed_s": snap.get("elapsed"),
            "limit_s": snap.get("limit"),
            "remain_s": snap.get("remain"),
            "free_bytes": (snap.get("free_mb") or 0) * 1048576,
            "fps": min(fps) if fps else None,
            "audio_s": aud[0] if aud else None,
            "move_pct": snap.get("move_pct"),
        })
        if self._fb:
            self._fb.publish(snap)

    def message(self, text: str, oled: bool = True) -> None:
        """A message shown for `MSG_HOLD` seconds, then the status screen returns.

        The first line is the title, the rest is the body. The title is never
        wrapped (it is clipped if too long); only the body is wrapped.
        """
        # `oled=False` means the event already has a status screen; do not
        # draw a second, differently formatted copy.
        if oled:
            # Split into lines before filtering characters: `_ascii()` would
            # turn `\n` into '?' and the whole text would end up in the title.
            parts = [_ascii(x) for x in str(text).strip().split("\n")]
            lines = [parts[0].strip()] + self._wrap("\n".join(parts[1:]))
            self._msg = ([x for x in lines if x], time.monotonic() + MSG_HOLD)
        if self._fb:
            self._fb.message(text, oled)

    def close(self) -> None:
        """Does not close the hardware. `Session` calls this after every recording."""
        if self._fb:
            self._fb.close()

    def take_over(self):
        """Hand the screen to the caller for direct drawing; returns the device.

        Used by the full-screen preview. The background thread pauses,
        otherwise it would redraw the status screen every 250 ms, and the
        preview needs 8 fps.
        """
        self._paused = True
        time.sleep(PERIOD)                  # let the background thread finish its current frame
        return self._dev

    def release(self):
        """Give the screen back to the background thread."""
        self._last = None                   # force a redraw
        self._paused = False

    def shutdown(self) -> None:
        """Stop the thread and turn the screen off. Called once when the program exits."""
        self._stop.set()
        self._th.join(timeout=2.0)
        try:
            self._dev.close()
        except Exception:
            pass

    # ------------------------------------------------------------ layout
    def _w(self, s):
        return self._font.getlength(s)

    def _wrap(self, text):
        out = []
        for para in str(text).split("\n"):
            words = para.strip().split()
            if not words:
                continue
            cur = words[0]
            for w in words[1:]:
                if self._w(f"{cur} {w}") <= MAXW:
                    cur = f"{cur} {w}"
                else:
                    out.append(cur)
                    cur = w
            out.append(cur)
        return out or [""]

    def _clip(self, s):
        """Clip text that cannot be wrapped.

        Paths are clipped at the front (the end is the distinctive part),
        everything else at the back.
        """
        if self._w(s) <= MAXW:
            return s
        if "/" in s:
            while s and self._w("~" + s) > MAXW:
                s = s[1:]
            return "~" + s
        while s and self._w(s) > MAXW:
            s = s[:-1]
        return s

    def lines(self, v):
        """view() -> lines to draw. This is the OLED layout only.

        The title holds only the state name; numbers and explanations go in
        the body, so the state and the measurements are never mixed.

        Rule for what goes on the OLED: would this information change the
        operator's next action in the field? Per-camera fps is therefore not
        shown while recording; the deciding value is how long recording can
        continue.
        """
        st, d = v["state"], v["data"]
        if st == "sleep":
            return ["SLEEP", "cameras off",
                    f"free {_mb(d.get('free_bytes'))}", "press any key"]
        if st == "menu":
            # No hint line here: the meaning of ^ and v is learned after one
            # press, and the items matter more.
            #
            # Scrolling window: with more items than ROWS, the window slides so
            # the cursor is always visible. Scrolling belongs to the renderer;
            # the state machine does not know the screen size.
            items = v.get("items", [])
            cur = v.get("cursor") or 0
            lo = 0
            if len(items) > ROWS:
                lo = min(max(0, cur - ROWS + 1), len(items) - ROWS)
            return ["MENU"] + [("> " if lo + i == cur else "  ") + t
                               for i, t in enumerate(items[lo:lo + ROWS])]
        if st == "idle":
            # audio_ok: None = disabled in settings (not shown), True = found,
            # False = expected but missing (microphone unplugged), which must be shown
            aud = {True: "  aud ok", False: "  NO MIC"}.get(d.get("audio_ok"), "")
            return ["READY",
                    f"{d.get('cams') or '?'}cam" + aud,
                    f"free {_mb(d.get('free_bytes'))}",
                    f"rec up to {_s(d.get('max_seconds'))}",
                    "^ = menu"]
        if st == "confirm":
            it = v.get("items", [""])[v.get("cursor") or 0]
            rows = [f"start {it}?"]
            if d.get("max_seconds"):
                rows.append(f"max {_s(d['max_seconds'])}")
            return ["CONFIRM"] + rows + ["v = yes    ^ = no"]
        if st == "warmup":
            return ["WARMUP", f"{_s(d.get('warmup_s'))}, measuring rate",
                    d.get("ready") or "starting cameras"]
        if st == "running":
            return ["REC",
                    f"{(d.get('elapsed_s') or 0):.1f}s"
                    + (f" of {d['limit_s']:.0f}s" if d.get("limit_s") else ""),
                    f"free {_mb(d.get('free_bytes'))}",
                    f"left {_s(d.get('remain_s'))}",
                    "v = stop"]
        if st == "calib":
            if d.get("wait_s"):
                # No button hints during the countdown: the point is not to touch anything
                return ["CALIB", "hold still",
                        f"shooting in {d['wait_s']:.0f}s",
                        "do not touch the rig"]
            rows = [f"shots {d.get('shots') or 0}"]
            if d.get("last_ok") is not None:
                rows.append("last " + ("OK" if d["last_ok"] else "FAIL"))
            return ["CALIB"] + rows + ["v shot    ^ end"]
        if st == "info":
            # The version is the first body line: the first question when
            # something goes wrong in the field is which version is running.
            aud = {True: "aud ok", False: "NO MIC"}.get(d.get("audio_ok"), "no aud")
            return ["INFO",
                    # The title already says INFO; one line holds about 21 characters.
                    f"v{d.get('core_version') or '?'}  {d.get('core_date') or ''}",
                    f"{d.get('cams') or '?'}cam  {aud}",
                    f"free {_mb(d.get('free_bytes'))}",
                    f"{d.get('host') or ''}"]
        if st == "exposure":
            return ["METERING", "bracket shots...", "hold the scene still"]
        if st == "expose_apply":
            def _e(us, g):
                return "--" if us is None else f"{us/1000:.1f}ms  g{g:.2f}"
            return ["EXPOSURE",
                    "now " + _e(d.get("exposure_us"), d.get("gain") or 1),
                    "new " + _e(d.get("sug_exposure_us"), d.get("sug_gain") or 1),
                    "v = apply  ^ = keep"]
        if st == "flushing":
            return ["MOVE", f"{(d.get('move_pct') or 0):.0f}%",
                    f"free {_mb(d.get('free_bytes'))}", "do not power off"]
        if st == "done":
            return ["DONE",
                    f"{d.get('files') or 0} files "
                    f"{(d.get('total_mb') or 0):.0f}MB",
                    "any key = back"]
        if st == "error":
            return ["ERROR"] + self._wrap(v.get("message") or "unknown")[:2]                 + ["any key = back"]
        # params and others are terminal-only, but the screen still states what
        # is happening so it does not look frozen.
        return [st.upper(), "on the terminal", "not on this screen"]

    def _render(self, lines):
        lines = [_ascii(x) for x in lines][:1 + ROWS]
        img = self._Image.new("1", (W, H), 0)
        d = self._Draw.Draw(img)
        d.rectangle([0, 0, W - 1, H - 1], outline=1)
        # The title may be split by `\t` into a left part and a right-aligned
        # part, so a title and a key hint do not run together.
        head = lines[0].split("\t")
        d.text((3, 2), self._clip(head[0]), font=self._font, fill=1)
        if len(head) > 1:
            d.text((W - 4 - self._w(head[1]), 2), head[1],
                   font=self._font, fill=1)
        d.line([1, 14, W - 2, 14], fill=1)
        y = 17
        for ln in lines[1:]:
            d.text((3, y), self._clip(ln), font=self._font, fill=1)
            y += 11
        return img

    def _loop(self):
        while not self._stop.is_set():
            t0 = time.monotonic()
            if self._paused:                # the preview draws directly
                self._stop.wait(PERIOD)
                continue
            try:
                if self._msg and time.monotonic() < self._msg[1]:
                    lines = self._msg[0]
                else:
                    self._msg = None
                    lines = self.lines(self._menu.view(on_oled=True))
                # Dim while asleep (OLED power is almost entirely lit pixels).
                # The command is sent only when the state changes.
                dim = self._menu.state == "sleep"
                if dim != self._dim:
                    self._dev.contrast(0x10 if dim else 0xCF)
                    self._dim = dim
                buf = self._dev.pack(self._render(lines))
                if buf != self._last:          # skip the 95 ms I2C transfer when unchanged
                    self._dev.blit(buf)
                    self._last = buf
            except Exception as e:             # noqa: BLE001
                self.errors += 1
                self._last = None              # redraw after reconnecting
                if not self._warned:
                    self._warned = True
                    if self._fb:
                        self._fb.message(f"  OLED failed (recording is fine): {e}")
            self._stop.wait(max(0.0, PERIOD - (time.monotonic() - t0)))
