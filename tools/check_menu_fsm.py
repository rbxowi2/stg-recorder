#!/usr/bin/env python3
"""check_menu_fsm.py - transition tests for the menu state machine.

    python tools/check_menu_fsm.py

`core/menu.py` drives the terminal, the OLED and the two buttons. A mistake
there raises no exception; it shows up as "a press does nothing" or "the wrong
thing happens", which would otherwise only be found standing in front of the
rig. The state machine is pure (no hardware, no files), so the whole flow can
be tested on any computer.

This checks transitions, not appearance; layout is covered by
`check_ui_golden.py`, and the feel of the buttons only on the device.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.menu import MenuMachine as M, Event as E, Action as A, items  # noqa: E402

FAIL = []


def chk(cond, msg):
    if not cond:
        FAIL.append(msg)
        print(f"     ! {msg}")


def main():
    # Menu contents. Metering and the microphone test are on the OLED because
    # both are needed in the field, where there is no SSH.
    oled_keys = ["preview", "record", "calib", "mic", "info", "expose", "shutdown"]
    chk([i.key for i in items(on_oled=True)] == oled_keys,
        f"OLED menu should be {oled_keys}")
    chk(len(items()) == 9, "terminal menu should have 9 items")

    # idle -> menu -> cycle -> confirm -> run
    m = M()
    m.handle(E.NEXT)
    chk(m.state == m.MENU, "any key in idle should open the menu")
    m.handle(E.NEXT)
    chk(m.cursor == 1, "^ in the menu should move the cursor down")
    for _ in range(len(oled_keys) - 1):
        m.handle(E.NEXT)
    chk(m.cursor == 0, "the cursor should wrap to 0")
    # cursor 0 is preview (no confirmation); record is needed to test confirmation
    m.handle(E.NEXT)
    chk(m.cursor == 1, "cursor 1 should be record")
    m.handle(E.OK)
    chk(m.state == m.CONFIRM, "v in the menu should open the confirmation")
    m.handle(E.NEXT)
    chk(m.state == m.MENU, "^ in the confirmation should cancel back to the menu")
    m.handle(E.OK)
    chk(m.handle(E.OK) == A.START_RECORD and m.state == m.RUNNING,
        "v in the confirmation should start recording")

    # one press stops the recording
    chk(m.handle(E.OK) == A.FINISH, "v while recording should return FINISH directly")

    m2 = M()
    m2.update(phase="running")
    chk(m2.handle(E.NEXT) is None and m2.state == m2.RUNNING,
        "^ while recording should do nothing")

    # calibration
    m3 = M()
    m3.update(phase="calib")
    chk(m3.handle(E.OK) == A.SHOOT, "v in calibration should shoot a set")
    chk(m3.handle(E.NEXT) == A.FINISH, "^ in calibration should finish")

    # busy states ignore input
    for ph in ("warmup", "flushing", "params", "exposure"):
        mm = M()
        mm.update(phase=ph)
        chk(mm.handle(E.OK) is None and mm.state == ph,
            f"{ph} should ignore input while busy")

    # done / error
    mm = M()
    mm.update(phase="done")
    mm.handle(E.NEXT)
    chk(mm.state == mm.IDLE, "any key after done should return to idle")
    mm = M()
    mm.update(error="cam_C lost")
    chk(mm.state == mm.ERROR and mm.message == "cam_C lost",
        "an error should enter ERROR with its message")
    mm.handle(E.OK)
    chk(mm.state == mm.IDLE and mm.message == "",
        "returning to idle after an error should clear the message")

    # terminal jumps
    m4 = M()
    chk(m4.goto("expose") == A.START_EXPOSURE and m4.state == m4.EXPOSURE,
        "terminal goto should run directly, without the confirmation screen")
    chk(M().goto("params") == A.OPEN_PARAMS, "goto params should return its Action")
    chk(M().goto("nope") is None, "goto of an unknown item should return None")

    # view returns data only
    m5 = M({"elapsed_s": 12.4, "limit_s": 60, "remain_s": 106})
    m5.update(phase="running")
    v = m5.view()
    chk(v["state"] == "running" and v["data"]["elapsed_s"] == 12.4,
        "view should return the state and raw values")
    chk(all(not isinstance(x, str) for x in v["data"].values() if x is not None),
        "view must not format anything (that is the renderer's job)")
    m6 = M()
    m6.handle(E.NEXT)
    chk(m6.view(on_oled=True)["items"] ==
        ["preview", "record", "calib set", "mic test", "info",
         "meter exp", "shutdown"],
        "view(on_oled=True) should return those seven items")
    chk(len(m6.view()["items"]) == 9, "view() for the terminal should have 9 items")

    # info is read-only: any button exits, with no side effect
    for ev in (E.NEXT, E.OK):
        mi = M()
        chk(mi.goto("info") == A.SHOW_INFO and mi.state == mi.INFO,
            "goto info should enter info")
        chk(mi.handle(ev) is None and mi.state == mi.IDLE,
            f"{ev} in info should return to idle without an action")
    mi = M()
    mi.goto("info")
    mi.update(snapshot={"core_version": "1.7.0", "core_date": "2026-08-28"})
    d = mi.view(on_oled=True)["data"]
    chk(d.get("core_version") == "1.7.0" and "free_bytes" in d,
        "the info view should include the version and free space")

    # preview: any button exits
    for ev in (E.NEXT, E.OK):
        m9 = M()
        chk(m9.goto("preview") == A.START_PREVIEW and m9.state == m9.PREVIEW,
            "goto preview should enter the preview")
        chk(m9.handle(ev) is None and m9.state == m9.IDLE,
            f"{ev} in the preview should exit to idle")

    # sleep: any key only wakes
    for ev in (E.NEXT, E.OK):
        m8 = M()
        m8.update(phase="sleep")
        chk(m8.handle(ev) == A.WAKE, f"{ev} while asleep should return WAKE")
        chk(m8.state == m8.SLEEP,
            "the state changes only after the cameras are open (app layer), not here")

    # metering: ask, never apply automatically
    m7 = M()
    chk(m7.goto("expose") == A.START_EXPOSURE, "goto expose should start metering")
    m7.update(phase="exposure")
    chk(m7.handle(E.OK) is None and m7.state == m7.EXPOSURE,
        "metering should ignore input while running")
    m7.update(phase="expose_apply")
    chk(m7.handle(E.NEXT) is None and m7.state == m7.IDLE,
        "^ on the metering result should not apply and return to idle")
    m7.update(phase="expose_apply")
    chk(m7.handle(E.OK) == A.APPLY_EXPOSURE and m7.state == m7.IDLE,
        "v on the metering result should apply and return to idle")

    if FAIL:
        print(f"\n  x {len(FAIL)} menu state machine checks failed.")
        return 1
    print("  OK all menu state machine transitions are correct.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
