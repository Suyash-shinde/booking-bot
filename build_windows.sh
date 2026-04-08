#!/usr/bin/env bash
# Build SavaariBot.exe from Linux using Docker + Wine + Windows Python.
#
# Usage:
#   ./build_windows.sh           # production build (no console window)
#   ./build_windows.sh debug     # debug build with a visible console
#                                # so you can see Python tracebacks
#
# Output: dist/SavaariBot.exe         (production)
#         dist/SavaariBot-debug.exe   (debug)
#
# Why Docker: PyInstaller doesn't cross-compile. The image bundles a real
# Windows Python interpreter under Wine and runs PyInstaller exactly as it
# would on a Windows machine, so the .exe is a real PE32+ binary that runs
# on Windows 10/11 with no extra runtime needed.
#
# The image is `tobix/pywine:3.12` (~1 GB pull on first run, then cached).
set -euo pipefail

cd "$(dirname "$0")"

MODE="${1:-prod}"
case "$MODE" in
    prod)
        SPEC="SavaariBot.spec"
        OUT="dist/SavaariBot.exe"
        ;;
    debug)
        SPEC="SavaariBot-debug.spec"
        OUT="dist/SavaariBot-debug.exe"
        ;;
    *)
        echo "Usage: $0 [prod|debug]" >&2
        exit 1
        ;;
esac

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

# The image's Wine prefix already has Python 3.12 installed. We do
# everything inside the container as root: cleanup AND build. Doing the
# cleanup outside (as the host user) breaks because the previous build
# left root-owned files in build/ and dist/.
#
# At the end we also chown the outputs back to the calling user (best
# effort) so you can delete or move them without sudo.
HOST_UID="$(id -u)"
HOST_GID="$(id -g)"

$DOCKER run --rm \
    --user 0:0 \
    -e SPEC="$SPEC" \
    -e HOST_UID="$HOST_UID" \
    -e HOST_GID="$HOST_GID" \
    -v "$PROJECT_DIR:/src" \
    -w /src \
    "$IMAGE" \
    sh -c '
        set -e
        echo "--- cleaning previous build artifacts ---"
        rm -rf build/ dist/ __pycache__/ savaari_bot/__pycache__/
        echo
        echo "--- python version inside wine ---"
        wine python --version
        echo
        echo "--- installing build deps ---"
        wine python -m pip install --upgrade pip
        wine python -m pip install -r requirements.txt pyinstaller
        echo
        echo "--- running pyinstaller on $SPEC ---"
        wine python -m PyInstaller "$SPEC" --noconfirm --clean
        echo
        echo "--- handing build artifacts back to host user $HOST_UID:$HOST_GID ---"
        chown -R "$HOST_UID:$HOST_GID" build/ dist/ savaari_bot/__pycache__/ 2>/dev/null || true
    '

echo
if [ -f "$OUT" ]; then
    SIZE=$(du -h "$OUT" | cut -f1)
    echo "==> SUCCESS: $OUT  ($SIZE)"
    file "$OUT"
else
    echo "==> FAILED: $OUT was not produced"
    exit 1
fi
