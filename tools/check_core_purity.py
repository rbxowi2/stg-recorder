#!/usr/bin/env python3
"""Check the recording core: (1) no platform-specific code, (2) no uncommitted changes.

    python tools/check_core_purity.py

(1) For the core to be platform-independent, nothing Pi- or Linux-specific
    may appear in `core/`. Reviewing this by eye misses things, so it is checked.

(2) The core and the platform layer live in the same folder (so the whole
    folder can be copied), which makes it easy to touch the core while
    editing the platform layer. The working tree is compared with the last
    git commit. Changing the core is fine, but it is a version event: bump
    `CORE_VERSION` and commit with a message that says why. The risk is a
    change nobody noticed.

Exit code 0 = clean and no uncommitted core changes.
"""


import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "core"

# Any of these in code (not comments) is a leak.
BANNED = [
    (r"\bpicamera2\b", "Picamera2 is the Pi-specific camera library"),
    (r"\bv4l2-ctl\b", "v4l2-ctl is a Linux-specific command"),
    (r"/sys/class", "/sys is a Linux-specific file system"),
    (r"\bRPi\.GPIO\b|\bgpiozero\b|\blgpio\b", "GPIO libraries are Pi-specific"),
    (r"\btermios\b", "termios is POSIX-specific"),
    (r"/dev/video", "/dev/video is a Linux-specific device node"),
    (r"\balsaaudio\b|\bsounddevice\b", "audio back ends belong to the platform layer"),
]

# Intentional exceptions, with the reason
ALLOW = {
    ("triggers.py", "termios"):
        "drain_stdin wraps it in try/except and silently skips on non-POSIX systems",
}


def strip_comments_and_strings(src: str) -> str:
    """Remove comments and strings, keeping only executable code."""
    src = re.sub(r'"""[\s\S]*?"""', '""', src)
    src = re.sub(r"'''[\s\S]*?'''", "''", src)
    src = re.sub(r"#.*", "", src)
    src = re.sub(r'"[^"\n]*"', '""', src)
    src = re.sub(r"'[^'\n]*'", "''", src)
    return src


def check_drift() -> int:
    """Compare `core/` with the last git commit. Returns the number of changed files."""
    import subprocess
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain", "--", "core"],
            cwd=ROOT, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            print("  ! Not a git repository; drift cannot be checked")
            return 0
    except Exception as e:                                   # noqa: BLE001
        print(f"  ! git is not available ({e}); drift cannot be checked")
        return 0

    lines = [x for x in r.stdout.splitlines() if x.strip()]
    if not lines:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              cwd=ROOT, capture_output=True, text=True).stdout.strip()
        print(f"  OK identical to the last commit ({head}); no uncommitted core changes.")
        return 0

    print("  ! Uncommitted changes in the core:")
    mark = {"M": "modified", "A": "added", "D": "deleted", "R": "renamed", "?": "untracked"}
    for x in lines:
        st, path = x[:2].strip(), x[3:]
        print(f"     {mark.get(st[:1], st):9s}  {path}")
    print()
    print("     Intentional: bump CORE_VERSION in core/__init__.py and commit with the reason.")
    print("     Accidental: inspect with `git diff -- core` and restore with `git checkout`.")
    return len(lines)


def main() -> int:
    if not CORE.is_dir():
        print(f"  x {CORE} not found")
        return 2
    bad = []
    for f in sorted(CORE.rglob("*.py")):
        code = strip_comments_and_strings(f.read_text(encoding="utf-8"))
        for pat, why in BANNED:
            for m in re.finditer(pat, code):
                key = (f.name, m.group(0))
                if key in ALLOW:
                    continue
                line = code[:m.start()].count("\n") + 1
                bad.append((f.relative_to(ROOT), line, m.group(0), why))

    for f, line, hit, why in bad:
        print(f"  ! {f}:{line}  contains {hit!r} - {why}")
    if bad:
        print(f"\n  x The core is contaminated ({len(bad)} places). "
              f"That code belongs in the platform layer (pi/).")
        return 1

    n = len(list(CORE.rglob("*.py")))
    print(f"  OK core is clean ({n} files). Porting only requires rewriting the platform layer.")
    for (fn, hit), why in ALLOW.items():
        print(f"     exception: {hit} in {fn} - {why}")
    return 1 if check_drift() else 0


if __name__ == "__main__":
    sys.exit(main())
