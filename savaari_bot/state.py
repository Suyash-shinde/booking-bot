"""Shared mutable state passed between the poller, the web UI and the tray.

Phase 0.5 keeps it deliberately tiny: a thin object that holds the live
Config, the latest poll snapshot and a few flags. Cross-thread access is
guarded by a single lock; the dashboard and tray icon read freely, the
poller writes after every tick.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from . import config


@dataclass
class PollSnapshot:
    at: str = ""
    total_broadcasts: int = 0
    new_count: int = 0
    price_up_count: int = 0
    vanished_count: int = 0
    last_error: str = ""


@dataclass
class AppState:
    cfg: config.Config = field(default_factory=config.Config)
    last_poll: PollSnapshot = field(default_factory=PollSnapshot)
    last_ok_at: Optional[str] = None
    last_error_at: Optional[str] = None
    last_error_msg: str = ""
    paused: bool = False
    auth_failed: bool = False
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    # Set to True from the web UI / tray when the user wants the process to
    # exit cleanly. Orchestrator polls this.
    shutdown_requested: bool = False
    # Set to True after a successful config write so the orchestrator knows it
    # can (re)start the poller with fresh credentials.
    config_dirty: bool = False

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def update_poll(self, **kwargs: Any) -> None:
        with self._lock:
            for k, v in kwargs.items():
                setattr(self.last_poll, k, v)
            self.last_ok_at = self.last_poll.at
            self.auth_failed = False
            # A successful poll wipes any stale error banner. Without this
            # a one-time network blip stays on the dashboard forever.
            self.last_error_msg = ""
            self.last_error_at = None

    def record_error(self, msg: str, *, auth: bool = False) -> None:
        with self._lock:
            self.last_error_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.last_error_msg = msg
            self.last_poll.last_error = msg
            if auth:
                self.auth_failed = True

    def request_shutdown(self) -> None:
        with self._lock:
            self.shutdown_requested = True

    def mark_config_dirty(self) -> None:
        with self._lock:
            self.config_dirty = True

    def consume_config_dirty(self) -> bool:
        with self._lock:
            d = self.config_dirty
            self.config_dirty = False
            return d

    def snapshot(self, today_counts: dict[str, int] | None = None) -> dict[str, Any]:
        """Read-only dict for the dashboard."""
        with self._lock:
            return {
                "started_at": self.started_at,
                "paused": self.paused,
                "auth_failed": self.auth_failed,
                "last_ok_at": self.last_ok_at,
                "last_error_at": self.last_error_at,
                "last_error_msg": self.last_error_msg,
                "last_poll": {
                    "at": self.last_poll.at,
                    "total_broadcasts": self.last_poll.total_broadcasts,
                    "new_count": self.last_poll.new_count,
                    "price_up_count": self.last_poll.price_up_count,
                    "vanished_count": self.last_poll.vanished_count,
                },
                "today": today_counts or {"alerts_today": 0, "confirms_today": 0},
                "config": {
                    "vendor_token_set": bool(self.cfg.vendor_token),
                    "telegram_set": bool(self.cfg.telegram_bot_token and self.cfg.telegram_chat_id),
                    "poll_interval_s": self.cfg.poll_interval_s,
                    "fare_floor": self.cfg.fare_floor,
                    "dry_run_accept": self.cfg.dry_run_accept,
                },
            }
