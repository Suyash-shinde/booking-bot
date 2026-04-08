"""Geocoding (Nominatim) and routing (OSRM) clients with DB-backed caches.

Both endpoints used here are free and operated by community projects:

  - Nominatim: ToS requires a real User-Agent that identifies your app and a
    contact, max 1 req/sec, results must be cached. We do all of those.
  - OSRM router.project-osrm.org: a public demo intended for "small,
    occasional usage". For heavier traffic, point `osrm_base` at a
    self-hosted instance.

The cache lives in the same SQLite file the rest of the bot uses. Cache
keys: geocode by raw query string; routes by (lat,lng,lat,lng) rounded to
4 decimals (~10 m).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

log = logging.getLogger("savaari_bot.geo")


# A coordinate rounded to 4 decimals (~11 m at the equator). Tight enough
# to keep distinct addresses distinct, loose enough that two geocodes of
# the same building hit the same cache row.
def _round4(x: float) -> float:
    return round(float(x), 4)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- Nominatim ----------

@dataclass
class GeocodeResult:
    lat: float
    lng: float
    display_name: str

    def short(self) -> str:
        return self.display_name.split(",", 1)[0] if self.display_name else f"{self.lat:.4f},{self.lng:.4f}"


class Geocoder:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        base_url: str,
        user_agent: str,
        timeout_s: float = 15.0,
        min_interval_s: float = 1.05,
    ):
        self.conn = conn
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self.timeout_s = timeout_s
        self.min_interval_s = min_interval_s
        self._last_call = 0.0
        self._lock = asyncio.Lock()

    def cached(self, query: str) -> Optional[GeocodeResult]:
        row = self.conn.execute(
            "SELECT lat, lng, display_name FROM geocode_cache WHERE query=?",
            (query,),
        ).fetchone()
        if row is None or row["lat"] is None:
            return None
        return GeocodeResult(
            lat=float(row["lat"]),
            lng=float(row["lng"]),
            display_name=row["display_name"] or "",
        )

    async def geocode(self, query: str) -> Optional[GeocodeResult]:
        if not query or not query.strip():
            return None
        query = query.strip()
        cached = self.cached(query)
        if cached is not None:
            return cached

        # Serialize calls and respect Nominatim's 1 req/sec limit.
        async with self._lock:
            since = time.monotonic() - self._last_call
            if since < self.min_interval_s:
                await asyncio.sleep(self.min_interval_s - since)
            try:
                async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                    resp = await client.get(
                        f"{self.base_url}/search",
                        params={"q": query, "format": "json", "limit": 1},
                        headers={"User-Agent": self.user_agent, "Accept": "application/json"},
                    )
                    resp.raise_for_status()
                    items = resp.json() or []
            except Exception as e:
                log.warning("nominatim failed for %r: %s", query[:60], e)
                return None
            finally:
                self._last_call = time.monotonic()

        if not items:
            # Cache the miss too so we don't keep retrying every poll.
            self.conn.execute(
                """
                INSERT OR REPLACE INTO geocode_cache
                    (query, lat, lng, display_name, fetched_at)
                VALUES (?, NULL, NULL, '', ?)
                """,
                (query, _now()),
            )
            return None

        first = items[0]
        try:
            lat = float(first["lat"])
            lng = float(first["lon"])
        except (KeyError, ValueError, TypeError):
            log.warning("nominatim returned malformed item for %r", query[:60])
            return None
        display = str(first.get("display_name") or "")

        self.conn.execute(
            """
            INSERT OR REPLACE INTO geocode_cache
                (query, lat, lng, display_name, fetched_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (query, lat, lng, display, _now()),
        )
        return GeocodeResult(lat=lat, lng=lng, display_name=display)


# ---------- OSRM ----------

@dataclass
class Route:
    distance_m: int
    duration_s: int

    @property
    def distance_km(self) -> float:
        return self.distance_m / 1000.0


class Router:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        base_url: str,
        timeout_s: float = 15.0,
    ):
        self.conn = conn
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def cached(
        self, from_lat: float, from_lng: float, to_lat: float, to_lng: float
    ) -> Optional[Route]:
        row = self.conn.execute(
            """
            SELECT distance_m, duration_s FROM route_cache
             WHERE from_lat=? AND from_lng=? AND to_lat=? AND to_lng=?
            """,
            (_round4(from_lat), _round4(from_lng), _round4(to_lat), _round4(to_lng)),
        ).fetchone()
        if row is None or row["distance_m"] is None:
            return None
        return Route(distance_m=int(row["distance_m"]), duration_s=int(row["duration_s"] or 0))

    async def route(
        self, from_lat: float, from_lng: float, to_lat: float, to_lng: float
    ) -> Optional[Route]:
        cached = self.cached(from_lat, from_lng, to_lat, to_lng)
        if cached is not None:
            return cached

        url = (
            f"{self.base_url}/route/v1/driving/"
            f"{from_lng},{from_lat};{to_lng},{to_lat}"
        )
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                resp = await client.get(
                    url, params={"overview": "false", "alternatives": "false"}
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            log.warning("osrm route failed: %s", e)
            return None

        if data.get("code") != "Ok" or not data.get("routes"):
            log.warning("osrm route returned %s", data.get("code"))
            return None
        r0 = data["routes"][0]
        try:
            distance_m = int(round(float(r0["distance"])))
            duration_s = int(round(float(r0["duration"])))
        except (KeyError, ValueError, TypeError):
            return None

        self.conn.execute(
            """
            INSERT OR REPLACE INTO route_cache
                (from_lat, from_lng, to_lat, to_lng, distance_m, duration_s, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _round4(from_lat), _round4(from_lng),
                _round4(to_lat), _round4(to_lng),
                distance_m, duration_s, _now(),
            ),
        )
        return Route(distance_m=distance_m, duration_s=duration_s)
