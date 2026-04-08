"""Entry point: launches the full app (poller + dashboard + tray icon).

Run with:  python -m savaari_bot.main
"""

from __future__ import annotations

from . import app


def main() -> None:
    raise SystemExit(app.run())


if __name__ == "__main__":
    main()
