#!/usr/bin/env bash
# Copy the recorder to the target machine (Raspberry Pi).
#
#     bash tools/deploy.sh user@host
#
# Everything that runs on the Pi is in this repository; the script only saves
# typing. It copies the repository to ~/stg-recorder on the target.
#
# config.json and config_backups/ are never sent (excluded below): they hold
# values tuned on the target machine, and a local copy (for example one
# created while testing) would overwrite them. scp has no exclude option,
# hence the tar pipe.
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "usage: bash tools/deploy.sh user@host" >&2
  exit 2
fi
HOST="$1"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "  -> ${HOST}:~/stg-recorder"

# Stale __pycache__ can keep old modules in use; clear both sides
find "$ROOT" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
ssh "$HOST" "rm -rf ~/stg-recorder/__pycache__ ~/stg-recorder/*/__pycache__ 2>/dev/null || true; mkdir -p ~/stg-recorder"

tar -C "$ROOT" -cf - --exclude='./config.json' --exclude='./config_backups' \
    --exclude='__pycache__' --exclude='./.git' --exclude='./captures' . \
  | ssh "$HOST" "tar -xf - -C ~/stg-recorder"

echo "  Checking imports..."
ssh "$HOST" "cd ~/stg-recorder && python3 -c \"
import importlib.util
s=importlib.util.spec_from_file_location('rig','rig.py')
m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
print(f'  OK core v{m.CORE_VERSION} ({m.CORE_DATE}),', len(m.detect()), 'cameras detected')\"" \
    2>&1 | grep -v "INFO Camera"

cat <<'MSG'

  Next, on the target machine:
      cd ~/stg-recorder
      python3 rig.py                interactive menu
      python3 rig.py --rec 30 --yes record 30 seconds
      python3 rig.py --calib        calibration-set mode

  If the boot service is installed, restart it to load the new code:
      sudo systemctl restart rig

  Output goes to ~/stg-recorder/captures/.
MSG
