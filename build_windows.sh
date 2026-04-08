#!/usr/bin/env bash
# Build SavaariBot.exe from Linux using Docker + Wine + Windows Python.
#
# Output: dist/SavaariBot.exe (single-file, --noconsole)
#
# Why Docker: PyInstaller doesn't cross-compile. The image bundles a real
# Windows Python interpreter under Wine and runs PyInstaller exactly as it
# would on a Windows machine, so the .exe is a real PE32+ binary that runs
# on Windows 10/11 with no extra runtime needed.
#
# The image is `tobix/pywine:3.12` (~1 GB pull on first run, then cached).
set -euo pipefail

cd "$(dirname "$0")"

IMAGE="tobix/pywine:3.12"
PROJECT_DIR="$(pwd)"

# Detect whether the current user can talk to the docker socket. If not,
# fall back to sudo (which will prompt for a password once). The cleaner
# permanent fix is `sudo usermod -aG docker $USER && newgrp docker`.
if docker info >/dev/null 2>&1; then
    DOCKER="docker"
else
    echo "==> docker socket not accessible — using sudo"
    echo "    (one-time fix: sudo usermod -aG docker \$USER && relog)"
    DOCKER="sudo docker"
fi

echo "==> Pulling $IMAGE (cached after first run)"
$DOCKER pull "$IMAGE"

echo "==> Cleaning previous build artifacts"
rm -rf build/ dist/ __pycache__/ savaari_bot/__pycache__/

# The image's Wine prefix already has Python 3.12 installed.
# We pip-install our deps + pyinstaller into that Python, then run
# PyInstaller against our spec.
#
# `--user 0:0` keeps file ownership sane on the bind mount (Wine doesn't
# care about uid/gid; the host then owns the resulting dist/).
$DOCKER run --rm \
    --user 0:0 \
    -v "$PROJECT_DIR:/src" \
    -w /src \
    "$IMAGE" \
    sh -c '
        set -e
        echo "--- python version inside wine ---"
        wine python --version
        echo
        echo "--- installing build deps ---"
        wine python -m pip install --upgrade pip
        wine python -m pip install -r requirements.txt pyinstaller
        echo
        echo "--- running pyinstaller ---"
        wine python -m PyInstaller SavaariBot.spec --noconfirm --clean
    '

echo
if [ -f dist/SavaariBot.exe ]; then
    SIZE=$(du -h dist/SavaariBot.exe | cut -f1)
    echo "==> SUCCESS: dist/SavaariBot.exe  ($SIZE)"
    file dist/SavaariBot.exe
else
    echo "==> FAILED: dist/SavaariBot.exe was not produced"
    exit 1
fi
