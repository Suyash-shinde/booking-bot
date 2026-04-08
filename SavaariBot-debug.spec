# Debug build of Savaari Bot.
#
# Identical to SavaariBot.spec EXCEPT:
#   - console=True   so any Python traceback is visible in a black cmd window
#   - name=SavaariBot-debug
#
# Use this when the production build silently dies on launch — you'll see
# the actual error in the console instead of staring at a vanished tray icon.
#
# Build:  ./build_windows.sh debug
# Run:    double-click dist\SavaariBot-debug.exe   (a console window opens)

# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_submodules

hiddenimports = []
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
hiddenimports += collect_submodules("savaari_bot")
hiddenimports += [
    "pystray._win32",
    "PIL.Image",
    "PIL.ImageDraw",
    "email.parser",
]

a = Analysis(
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
    name="SavaariBot-debug",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,             # <-- the only real difference
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
