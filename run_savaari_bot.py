"""Top-level entry point for the PyInstaller build.

PyInstaller treats whatever file you pass it as a *script*, not as a member
of a package, so the relative imports inside `savaari_bot/main.py`
(`from . import app`) explode at runtime with:

    ImportError: attempted relative import with no known parent package

This shim sits at the project root, imports the real package by absolute
name, and calls into it. The result behaves identically to
`python -m savaari_bot.main` from a dev install.

Windows --noconsole gotcha: when PyInstaller builds with console=False, the
resulting binary is launched via pythonw, which sets sys.stdout and
sys.stderr to None. Anything that tries to write to them — uvicorn, our
StreamHandler, even a stray print() — crashes the process *before* the
FastAPI server can bind, which is why the dashboard then shows
"connection refused". The fix is to swap None for an open file handle
at the very top of the program, before any other module imports.
"""

import os
import sys

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

# Now it's safe to import the rest of the app.
from savaari_bot.app import run  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(run())
