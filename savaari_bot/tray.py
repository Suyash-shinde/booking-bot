"""System-tray icon for the bot.

The tray runs in the main thread (pystray.Icon.run blocks). The poller +
FastAPI run in a background thread, communicating via AppState. We import
pystray lazily so the rest of the app still imports cleanly on a headless
Linux box where pystray's xlib backend isn't available.
"""

from __future__ import annotations

import logging
import threading
import webbrowser
from typing import Callable

from .state import AppState

log = logging.getLogger("savaari_bot.tray")

DASHBOARD_URL = "http://127.0.0.1:8765/"


def _make_icon_image():
    """Generate a tiny 64x64 icon at runtime so we don't ship a binary asset."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (64, 64), (42, 109, 244))  # savaari-blue
    draw = ImageDraw.Draw(img)
    # White "S" in the middle, font-free so it works without bundled fonts.
    draw.rectangle((14, 12, 50, 22), fill="white")
    draw.rectangle((14, 28, 50, 38), fill="white")
    draw.rectangle((14, 44, 50, 54), fill="white")
    return img


class TrayApp:
    def __init__(self, state: AppState, on_quit: Callable[[], None]):
        self.state = state
        self.on_quit = on_quit
        self._icon = None

    def _menu(self):
        import pystray

        return pystray.Menu(
            pystray.MenuItem("Open dashboard", lambda: webbrowser.open(DASHBOARD_URL), default=True),
            pystray.MenuItem(
                lambda item: "Resume" if self.state.paused else "Pause",
                self._toggle_pause,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        )

    def _toggle_pause(self, _icon=None, _item=None):
        self.state.paused = not self.state.paused
        log.info("tray: paused=%s", self.state.paused)
        if self._icon:
            self._icon.update_menu()

    def _quit(self, _icon=None, _item=None):
        log.info("tray: quit requested")
        self.state.request_shutdown()
        if self._icon:
            self._icon.stop()
        self.on_quit()

    def run(self) -> None:
        # Detect headless Linux up front: no DISPLAY means no X11 to talk to.
        # On Windows/macOS pystray brings its own backend that doesn't need
        # this var, so the check is gated behind sys.platform.
        import os
        import sys

        if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
            log.info("no DISPLAY — running headless")
            self._run_headless()
            return

        try:
            import pystray
        except Exception as e:
            log.warning("pystray not available (%s) — running headless", e)
            self._run_headless()
            return

        try:
            self._icon = pystray.Icon(
                "savaari_bot",
                icon=_make_icon_image(),
                title="Savaari Bot",
                menu=self._menu(),
            )
        except Exception as e:
            log.warning("could not create tray icon (%s) — running headless", e)
            self._run_headless()
            return

        log.info("tray icon running; right-click for menu")
        try:
            self._icon.run()
        except Exception as e:
            log.warning("tray icon crashed (%s) — falling back to headless", e)
            self._run_headless()

    def _run_headless(self) -> None:
        """Block until shutdown_requested is set; used when no GUI is available."""
        log.info("dashboard available at %s", DASHBOARD_URL)
        ev = threading.Event()
        # Poll the shared flag once a second.
        while not self.state.shutdown_requested:
            if ev.wait(1.0):
                break
        self.on_quit()
