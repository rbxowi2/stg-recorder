#!/usr/bin/env bash
# Install the recorder on a Raspberry Pi 5. Run on the Pi, from ~/stg-recorder:
#
#     bash setup.sh
#
# Every step checks first and skips work that is already done, so the script
# can be run again safely. Steps that need root ask for the sudo password.
#
#   1. apt packages
#   2. /boot/firmware/config.txt (I2C for the OLED, I2S for the microphones)
#   3. boot service (systemd + tmux)
#   4. password-less poweroff for the menu's shutdown entry (scoped sudoers)
#   5. import check
#
# A reboot is required after step 2 changes config.txt.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="$HOME/stg-recorder"
CONFIG_TXT=/boot/firmware/config.txt
NEED_REBOOT=0

pass() { echo "  PASS  $*"; }
info() { echo "  ....  $*"; }
fail() { echo "  FAIL  $*" >&2; exit 1; }

if [ "$HERE" != "$TARGET" ]; then
  fail "the repository must be at $TARGET (it is at $HERE); the boot service expects that path"
fi

# ------------------------------------------------------------------ 1. packages
# apt, not pip: picamera2 is tied to the system libcamera, and a pip build can
# install fine yet fail to open the cameras.
PKGS="python3-picamera2 python3-pil python3-numpy python3-opencv v4l-utils \
python3-gpiozero python3-lgpio python3-smbus2 i2c-tools alsa-utils tmux"
MISSING=""
for p in $PKGS; do
  dpkg -s "$p" >/dev/null 2>&1 || MISSING="$MISSING $p"
done
if [ -n "$MISSING" ]; then
  info "installing:$MISSING"
  sudo apt-get update
  # shellcheck disable=SC2086
  sudo apt-get install -y $MISSING
fi
pass "apt packages"

# ------------------------------------------------------------------ 2. config.txt
add_line() {
  if ! grep -qxF "$1" "$CONFIG_TXT"; then
    info "adding '$1' to $CONFIG_TXT"
    echo "$1" | sudo tee -a "$CONFIG_TXT" >/dev/null
    NEED_REBOOT=1
  fi
}
[ -f "$CONFIG_TXT" ] || fail "$CONFIG_TXT not found (not a Raspberry Pi OS install?)"
add_line "dtparam=i2c_arm=on"
add_line "dtparam=i2s=on"
add_line "dtoverlay=googlevoicehat-soundcard"
pass "config.txt (I2C, I2S)"

# ------------------------------------------------------------------ 3. boot service
SERVICE=/etc/systemd/system/rig.service
TMP="$(mktemp)"
sed -e "s|__USER__|$USER|g" -e "s|__HOME__|$HOME|g" "$HERE/rig.service" > "$TMP"
if ! sudo cmp -s "$TMP" "$SERVICE" 2>/dev/null; then
  info "installing $SERVICE"
  sudo cp "$TMP" "$SERVICE"
  sudo systemctl daemon-reload
  sudo systemctl enable rig
fi
rm -f "$TMP"
pass "boot service (start now with: sudo systemctl start rig)"

# ------------------------------------------------------------------ 4. poweroff permission
SUDOERS=/etc/sudoers.d/rig-poweroff
TMP="$(mktemp)"
sed -e "s|__USER__|$USER|g" "$HERE/rig-poweroff.sudoers" > "$TMP"
if ! sudo cmp -s "$TMP" "$SUDOERS" 2>/dev/null; then
  # Validate before installing: a broken sudoers file disables sudo entirely.
  sudo visudo -cf "$TMP" >/dev/null || fail "sudoers file failed validation; nothing installed"
  sudo install -m 440 -o root -g root "$TMP" "$SUDOERS"
  info "installed $SUDOERS"
fi
rm -f "$TMP"
pass "poweroff permission"

# ------------------------------------------------------------------ 5. check
cd "$HERE"
python3 -c "
import importlib.util
s=importlib.util.spec_from_file_location('rig','rig.py')
m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
print(f'  PASS  core v{m.CORE_VERSION} ({m.CORE_DATE}), {len(m.detect())} cameras detected')
" 2>&1 | grep -v "INFO Camera"

echo
if [ "$NEED_REBOOT" = 1 ]; then
  echo "  config.txt changed: reboot now (sudo reboot). The recorder starts on boot."
else
  echo "  Done. Start the recorder with: sudo systemctl start rig"
  echo "  Attach to it with: tmux attach -t rig"
fi
