# stg-recorder

Synchronized four-camera recorder for a Raspberry Pi 5. It records the input
for a 4D Gaussian Splatting pipeline: four 1080p MJPEG streams with
per-frame timestamps on one clock, optional stereo I2S audio, and calibration
image sets. It is operated with two buttons and a small OLED in the field, or
over SSH with a keyboard.

## Hardware

**[HARDWARE.pdf](HARDWARE.pdf)** contains the parts list (camera and
microphone specifications), the 40-pin header wiring, the boot configuration
and the rig layout with measured camera spacing. In short:

| Part | Used |
|---|---|
| Computer | Raspberry Pi 5, 8 GB, NVMe SSD |
| Cameras | 4x UVC USB 2.0, 2 MP (1920x1080), MJPEG 30 fps, two per USB controller |
| Microphones | 2x ICS-43434 I2S MEMS (stereo) |
| Display / input | SSD1306 128x64 I2C OLED, two push buttons on GPIO 5 and 6 |
| Layout | 2x2 camera grid, 153.0 mm horizontal and 93.5 mm vertical between lens centers |

`tools/make_hardware_pdf.py` regenerates the PDF (needs `reportlab`).

## Repository layout

```
core/         platform-independent recording core (state machine, data format,
              storage budget, exposure algorithm, menu state machine)
pi/           Raspberry Pi 5 implementation (cameras, I2S audio, OLED, buttons)
rig.py        entry point: startup, menus, recording and calibration flows
oecf.json     measured response curve of the cameras (used by metering)
calib/        tools to re-measure oecf.json for a different camera model
rig.service   systemd unit (boot start inside tmux)
rig-poweroff.sudoers   lets the menu power the Pi off safely
setup.sh      installer, run on the Pi
tools/        deploy script, checks, hardware PDF generator
```

Porting to other hardware means rewriting `pi/` (and re-measuring
`oecf.json`); `core/` and the data format stay the same.

## Install

Raspberry Pi OS 64-bit (Debian 13 based) with Python 3.13 was used.

1. Copy the repository to the Pi as `~/stg-recorder`, either with
   `git clone` on the Pi or from another machine:

   ```bash
   bash tools/deploy.sh user@host
   ```

2. On the Pi:

   ```bash
   cd ~/stg-recorder
   bash setup.sh
   ```

   It installs the apt packages, adds the I2C and I2S lines to
   `/boot/firmware/config.txt`, installs the boot service and the scoped
   poweroff permission, and checks that the cameras are detected. Each step
   prints PASS and is skipped when already done. Reboot if it says
   `config.txt changed`.

3. Connect all four cameras before the first start. On the first start the
   recorder binds each camera name to its USB port and saves the mapping in
   `config.json`; with a camera missing it refuses to create the mapping.

**Verify:** the OLED shows `READY` and `4cam`; the `info` screen shows the
core version and `hostname@IP`.

`setup.sh` has been written from the steps used on the development Pi but
has not been run on a freshly installed system. If an apt package name
differs on your release, install the equivalent and run the script again.

## Use

The recorder starts at boot inside tmux. Attach over SSH with
`tmux attach -t rig` (detach with Ctrl-B then D). Stop the service before
running `rig.py` by hand: the cameras can only be opened by one process.

| Command | Does |
|---|---|
| `python3 rig.py` | interactive menu |
| `python3 rig.py --rec 30 --yes` | record 30 seconds |
| `python3 rig.py --calib` | calibration-set mode (Enter shoots a set, `d` finishes) |
| `python3 rig.py --no-audio` | record without audio |

Buttons: `^` (upper) moves / cancels, `v` (lower) selects / confirms /
shoots / stops. The OLED menu has preview, record, calib set, mic test,
info, meter exp and shutdown. `shutdown` powers the Pi off; never cut the
power while the green LED blinks.

Before recording in a new scene or light, use **meter exp** and apply the
suggestion. The recorder then checks that all four cameras actually took the
new exposure (by comparing image brightness) and stops on an error screen if
one did not.

**The rig must stay in a fixed position and must not move during a
recording.**

## Shooting a calibration set

What the calibration images must be, and a recommended way to shoot them
(static scene, no board), is described in
[stg-studio/DATA_FORMAT.md, section 2](https://github.com/rbxowi2/stg-studio/blob/main/DATA_FORMAT.md#2-calibration-set).
This section only covers operating the recorder.

1. Start calibration mode: OLED menu **calib set**, or `python3 rig.py --calib`
   over SSH.
2. For each set, hold the rig still. With the buttons, `v` starts a 3 s
   countdown before the shot so the press does not shake the rig, and `^`
   finishes. Over SSH, Enter shoots and `d` finishes.
3. Check that every set reports all four cameras (OLED: last set OK).
4. Copy the `calib_*` folder from `captures/` to the PC and run the
   **Calibrate** tab of stg-studio.

Calibration sets and recordings come from the same cameras in the same
mode, stored as the cameras deliver them, so they share the same source as
required. Keep the focus rings untouched between calibration and recording.

## Output

Recordings and calibration sets go to `~/stg-recorder/captures/`.
Frames are written to RAM (tmpfs) first and moved to disk after recording,
because worst-case storage latency, not average bandwidth, decides whether
frames are dropped. The maximum recording length is therefore bounded by the
tmpfs size (about 103 s with 4 GB) and is shown on the idle screen.

```
captures/
  20260923_102443/                  shooting root
    PUT_RIG_FOLDER_HERE.txt         reminder: copy the calibration's rig/ here
    rec_20260923_102443/
      cam_A/ ... cam_D/             stream.mjpg + index.jsonl (offset, length, timestamp per frame)
      audio/                        audio.wav + index.json (start time, measured sample rate, xruns)
      session.json                  the manifest: streams, settings, budget, errors
  calib_20260923_151621/
    cam_A/ ... cam_D/               shot_0001.jpg, shot_0002.jpg, ...
    session.json
```

`session.json` is the only source of truth for consumers: read it to find the
streams instead of scanning for folders. All timestamps use
`CLOCK_MONOTONIC`, so video and audio can be aligned exactly.

The fields that stg-studio actually reads, and how to prepare the same data
with other cameras, are specified in
[stg-studio/DATA_FORMAT.md](https://github.com/rbxowi2/stg-studio/blob/main/DATA_FORMAT.md).

## config.json

Holds only the values tuned on this machine; everything else is
`DEFAULT_CFG` in `rig.py`.

```json
{
  "camera_ports": {"200000-1": "cam_A", "200000-2": "cam_B",
                   "300000-1": "cam_C", "300000-2": "cam_D"},
  "exposure_us": 3300,
  "gain": 1.484,
  "mic_spacing_mm": 195
}
```

Every write also stores a timestamped copy in `config_backups/`. Neither is
sent by `tools/deploy.sh`.

## Checks

```bash
python tools/check_menu_fsm.py      # menu state machine transitions (no hardware)
python tools/check_ui_golden.py     # terminal layout against tools/golden_ui.txt
python tools/check_core_purity.py   # core/ has no platform-specific code
```

## New camera model

`oecf.json` describes this camera model's response. For a different model,
measure it again in front of a static scene with no moving light sources
(TV, monitors):

```bash
python3 calib/sweep_shutter.py      # on the Pi
python3 calib/sweep_gain.py         # on the Pi
python calib/fit_oecf.py sweep.json gsweep.json -o oecf.json
```

Without a valid `oecf.json`, metering stops with an explicit error instead of
using a wrong curve.

## Third-party software

Runs on Raspberry Pi OS and uses picamera2 / libcamera, Pillow, NumPy,
OpenCV, gpiozero, smbus2, tmux and ALSA `arecord`, installed from the
distribution under their own licenses. No third-party source code is
included in this repository.
