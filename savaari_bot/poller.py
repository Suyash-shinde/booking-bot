"""Polling loop: fetch getNewBusiness, write diffs to SQLite, emit events.

Phase 0 just logs events to stdout/log file. Phase 1 will replace the
`emit_*` hooks with Telegram message senders.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable

from typing import Protocol

from . import db
from .savaari import SavaariAuthError, SavaariClient
from .state import AppState

log = logging.getLogger("savaari_bot.poller")


class Notifier(Protocol):
    async def alert_new(self, b: dict[str, Any]) -> None: ...
    async def alert_price_up(self, b: dict[str, Any], old: int, new: int) -> None: ...


@dataclass
class PollerEvents:
    """Hooks the rest of the app can subscribe to. Phase 0 leaves them as
    no-ops; Phase 1 wires Telegram messages into them."""

    on_new_broadcast: Callable[[dict[str, Any]], None] = lambda b: None
    on_price_up: Callable[[dict[str, Any], int, int], None] = lambda b, old, new: None
    on_auth_failure: Callable[[Exception], None] = lambda e: None


class Poller:
    def __init__(
        self,
        client: SavaariClient,
        conn,
        events: PollerEvents,
        interval_s: float,
        state: AppState | None = None,
        notifier: Notifier | None = None,
    ):
        self.client = client
        self.conn = conn
        self.events = events
        self.interval_s = interval_s
        self.state = state
        self.notifier = notifier
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        log.info("poller starting (interval=%.1fs)", self.interval_s)
        while not self._stop.is_set():
            if self.state and self.state.paused:
                await self._sleep(self.interval_s)
                continue
            try:
                await self._tick()
            except SavaariAuthError as e:
                log.error("auth failure: %s", e)
                if self.state:
                    self.state.record_error(str(e), auth=True)
                self.events.on_auth_failure(e)
                # Back off harder on auth failures so we don't spin.
                await self._sleep(max(self.interval_s * 6, 60.0))
                continue
            except Exception as e:
                log.exception("poll tick crashed; backing off")
                if self.state:
                    self.state.record_error(f"{type(e).__name__}: {e}")
                await self._sleep(self.interval_s * 3)
                continue
            await self._sleep(self.interval_s)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _tick(self) -> None:
        payload = await self.client.get_new_business()
        rs = payload.get("resultset") or {}
        broadcasts = SavaariClient.broadcasts(payload)
        car_types = rs.get("car_types") or []
        # source_cities and dest_cities each have city_id/city_name objects.
        # Merge them so we don't miss any (some cities only appear as
        # destinations and vice-versa).
        cities_payload = list(rs.get("source_cities") or []) + list(rs.get("dest_cities") or [])
        now = db._utcnow()

        new_count = 0
        price_up_count = 0
        seen_ids: list[str] = []
        # Collected during the DB transaction, dispatched to the notifier
        # *after* commit so we never hold the SQLite lock across an
        # outbound HTTP call.
        new_to_notify: list[dict[str, Any]] = []
        price_up_to_notify: list[tuple[dict[str, Any], int, int]] = []

        with db.transaction(self.conn):
            # Snapshot current fares before upserting so we can detect bumps.
            existing = {
                r["broadcast_id"]: r["last_fare"]
                for r in self.conn.execute(
                    "SELECT broadcast_id, last_fare FROM broadcasts WHERE vanished_at IS NULL"
                ).fetchall()
            }

            for b in broadcasts:
                bid = str(b.get("broadcast_id") or "")
                if not bid:
                    continue
                seen_ids.append(bid)

                is_new = db.upsert_broadcast(self.conn, b, now)
                db.insert_history(self.conn, b, now)

                if is_new:
                    new_count += 1
                    self.events.on_new_broadcast(b)
                    new_to_notify.append(b)
                else:
                    old_fare = existing.get(bid)
                    new_fare = db._to_int(b.get("total_amt") or b.get("gross_amount"))
                    if old_fare is not None and new_fare is not None and new_fare > old_fare:
                        price_up_count += 1
                        self.events.on_price_up(b, old_fare, new_fare)
                        price_up_to_notify.append((b, old_fare, new_fare))

            vanished = db.mark_vanished(self.conn, seen_ids, now)
            db.upsert_car_types(self.conn, car_types, now)
            db.upsert_cities(self.conn, cities_payload, now)

        log.info(
            "tick: %d broadcasts (%d new, %d price-up, %d vanished)",
            len(broadcasts),
            new_count,
            price_up_count,
            vanished,
        )
        if self.state:
            self.state.update_poll(
                at=now,
                total_broadcasts=len(broadcasts),
                new_count=new_count,
                price_up_count=price_up_count,
                vanished_count=vanished,
                last_error="",
            )

        if self.notifier:
            for b in new_to_notify:
                try:
                    await self.notifier.alert_new(b)
                except Exception:
                    log.exception("notifier.alert_new crashed")
            for b, old, new in price_up_to_notify:
                try:
                    await self.notifier.alert_price_up(b, old, new)
                except Exception:
                    log.exception("notifier.alert_price_up crashed")
