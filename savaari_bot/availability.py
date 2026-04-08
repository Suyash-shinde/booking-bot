"""Driver/car availability gate.

Per-broadcast call to `FETCH_DRIVERS_WITH_CARS_LIST_NPS` with TTL caching
and per-booking locking so that two concurrent ticks don't double-fetch the
same booking.

Cache key is the booking_id alone — Savaari's response depends on the
booking's car_type_id and pickup time but those are immutable per booking,
so booking_id is the right granularity.

Cache TTL is configurable (default 60 s) — short enough that drivers
finishing their current trip become eligible quickly, long enough that we
don't hammer the endpoint when many broadcasts arrive in the same tick.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from .savaari import SavaariClient

log = logging.getLogger("savaari_bot.availability")


@dataclass
class Eligibility:
    eligible_count: int
    fetched_at: float
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and self.eligible_count > 0

    @property
    def known(self) -> bool:
        """True if we have a real answer (success or zero); False if there
        was a transport error and we shouldn't trust the count."""
        return not self.error


class AvailabilityCache:
    def __init__(self, client: SavaariClient, ttl_s: float = 60.0):
        self.client = client
        self.ttl_s = ttl_s
        self._cache: dict[str, Eligibility] = {}
        # Per-booking lock so concurrent callers de-duplicate.
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def _lock_for(self, booking_id: str) -> asyncio.Lock:
        async with self._guard:
            lock = self._locks.get(booking_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[booking_id] = lock
            return lock

    def _fresh(self, e: Eligibility) -> bool:
        return (time.monotonic() - e.fetched_at) <= self.ttl_s

    async def get(
        self,
        *,
        booking_id: str,
        user_id: str,
        admin_id: str,
        usertype: str = "Vendor",
    ) -> Eligibility:
        booking_id = str(booking_id)
        cached = self._cache.get(booking_id)
        if cached and self._fresh(cached):
            return cached

        lock = await self._lock_for(booking_id)
        async with lock:
            # Re-check in case another waiter just populated it.
            cached = self._cache.get(booking_id)
            if cached and self._fresh(cached):
                return cached
            try:
                data = await self.client.fetch_drivers_with_cars(
                    booking_id=booking_id,
                    user_id=user_id,
                    admin_id=admin_id,
                    usertype=usertype,
                )
                rs = data.get("resultset") or {}
                cars = rs.get("carRecordList") or []
                e = Eligibility(eligible_count=len(cars), fetched_at=time.monotonic())
            except Exception as exc:
                log.warning("availability fetch for %s failed: %s", booking_id, exc)
                e = Eligibility(
                    eligible_count=0,
                    fetched_at=time.monotonic(),
                    error=f"{type(exc).__name__}: {exc}",
                )
            self._cache[booking_id] = e
            return e

    def invalidate(self, booking_id: str | None = None) -> None:
        if booking_id is None:
            self._cache.clear()
        else:
            self._cache.pop(str(booking_id), None)
