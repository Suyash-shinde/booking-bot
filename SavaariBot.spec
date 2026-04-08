# PyInstaller spec for Savaari Bot.
#
# Build options (in order of preference):
#
#   1. From Linux via Docker (cleanest, no Windows machine needed):
#         ./build_windows.sh
#      This runs PyInstaller inside a Wine + Windows-Python image and
#      drops dist/SavaariBot.exe.
#
#   2. From a real Windows box:
#         pip install -r requirements.txt pyinstaller
#         pyinstaller SavaariBot.spec --noconfirm
#
#   3. From a GitHub Actions Windows runner — see build_windows.md.
#
# Why a .spec file instead of CLI flags: PyInstaller has a habit of missing
# hidden imports for FastAPI/uvicorn/pystray/our own submodules pulled in
# dynamically. Listing them here once gives reproducible builds.

# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_submodules

hiddenimports = []
# Web stack — uvicorn loads its loops/protocols/lifespan classes by string
# name at runtime, so PyInstaller's static analysis misses them.
hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("uvicorn.lifespan")
hiddenimports += collect_submodules("uvicorn.protocols")
hiddenimports += collect_submodules("uvicorn.loops")
hiddenimports += collect_submodules("fastapi")
hiddenimports += collect_submodules("starlette")
hiddenimports += collect_submodules("anyio")
hiddenimports += collect_submodules("anyio._backends")
hiddenimports += collect_submodules("h11")
hiddenimports += collect_submodules("httpcore")
hiddenimports += collect_submodules("httpx")
# Our own package — make sure every submodule lands in the bundle even if
# it isn't directly imported by main.py at static-analysis time.
hiddenimports += collect_submodules("savaari_bot")
hiddenimports += [
    "pystray._win32",  # Windows tray backend
    "PIL.Image",
    "PIL.ImageDraw",
    "email.parser",     # uvicorn dependency some hooks miss
]

a = Analysis(
    # NOTE: don't point this at savaari_bot/main.py — PyInstaller would
    # treat it as a loose script, not a package member, and the relative
    # imports inside the package would crash with
    # "attempted relative import with no known parent package".
    ["run_savaari_bot.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "test", "unittest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="SavaariBot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                # UPX is unreliable cross-built; bigger but works
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,            # no terminal window on Windows
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon="savaari.ico",     # drop a real .ico in the project root and uncomment
)
