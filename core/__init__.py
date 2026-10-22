"""Recording core: the platform-independent part of the recorder.

    session       recording state machine and the session.json format
    budget        storage guard (how many seconds fit)
    exposure      exposure algorithm (pure functions)
    triggers      Trigger interface and hardware-independent implementations
    publishers    StatusPublisher interface and StdoutPublisher
    menu          menu state machine for button operation (event -> state -> view())

Design rules:

- `menu.view()` does not lay out text. It returns the state and raw values;
  each renderer (terminal, OLED) formats them for its own medium.
- The core does not assume every source is a camera. `session._stream_entry()`
  branches on `kind` (camera or audio).
- Every display string is ASCII. The built-in bitmap font of the 128x64 OLED
  only renders latin-1, and `pub.message()` goes through the same path.
- Nothing platform-specific is allowed here (picamera2, v4l2-ctl, /sys/,
  RPi.GPIO). `tools/check_core_purity.py` enforces this.

The `Source` contract (camera / audio) is documented in `pi/camera.py`.
Implementations: `VideoSource` in `pi/camera.py`, `AudioSource` in `pi/audio.py`.
The Raspberry Pi 5 implementation lives in `pi/`; porting to other hardware
means rewriting that layer only, the data format stays the same.
"""
# Single source of the core version. Bump it whenever an interface or the
# data format changes.
CORE_VERSION = "1.9.2"
CORE_DATE = "2026-10-22"
