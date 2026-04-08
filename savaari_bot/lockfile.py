"""Single-instance lock so double-clicking the tray doesn't start two copies.

Approach: bind a TCP socket on 127.0.0.1:8765. If it's already bound, another
instance is running — open its dashboard in a browser and exit. We use the
same port the FastAPI dashboard will use, so the lock and the actual server
share fate.
"""

from __future__ import annotations

import logging
import socket
import webbrowser

log = logging.getLogger("savaari_bot.lockfile")

LOCK_HOST = "127.0.0.1"
LOCK_PORT = 8765


def acquire_or_redirect() -> socket.socket | None:
    """Return a bound socket on success, or None if another instance was found.

    The caller can close the returned socket immediately before uvicorn starts
    — there's an unavoidable race window, but for a single-user desktop app
    on a fixed port that's fine.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((LOCK_HOST, LOCK_PORT))
    except OSError:
        log.warning("port %d already in use — assuming another instance", LOCK_PORT)
        try:
            webbrowser.open(f"http://{LOCK_HOST}:{LOCK_PORT}/")
        except Exception:
            pass
        s.close()
        return None
    return s
