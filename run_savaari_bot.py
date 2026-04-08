"""Top-level entry point for the PyInstaller build.

PyInstaller treats whatever file you pass it as a *script*, not as a member
of a package, so the relative imports inside `savaari_bot/main.py`
(`from . import app`) explode at runtime with:

    ImportError: attempted relative import with no known parent package

This shim sits at the project root, imports the real package by absolute
name, and calls into it. The result behaves identically to
`python -m savaari_bot.main` from a dev install.
"""

from savaari_bot.app import run


if __name__ == "__main__":
    raise SystemExit(run())
