"""Historical analytics on the broadcast/history tables.

Two responsibilities:

  1. Compute per-route + per-car-type aggregates so the dashboard can show
     "which corridors are crowded vs quiet" and so the notifier can tag each
     incoming alert with a competition score.

  2. Cache those aggregates with a TTL — they're moderately expensive
     queries and only need to be a few minutes fresh to be useful.

Schema we're working from (already populated by the poller):

    broadcasts(broadcast_id, source_city, dest_city, car_type_id,
               first_seen_at, vanished_at, first_fare, last_fare, max_fare)
    broadcast_history(broadcast_id, observed_at, fare, responded_vendors_count)

Take vs cancel heuristic: a broadcast that vanished is "taken" if at least
one history row recorded a non-zero `responded_vendors_count` while it was
open; otherwise it was likely auto-cancelled. This avoids fighting with
timezone-mismatched `auto_cancel_at` strings.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("savaari_bot.analytics")


# ---------- dataclasses ----------

@dataclass
class RouteStat:
    source_city: str
    dest_city: str
    car_type_id: str
    samples: int
    avg_responders: float
    max_responders: int
    take_rate: float
    avg_first_fare: int
    avg_max_fare: int
    avg_escalation: int       # max_fare - first_fare averaged

    def key(self) -> tuple[str, str, str]:
        return (self.source_city, self.dest_city, self.car_type_id)


@dataclass
class CompetitionTag:
    """Lightweight tag attached to an alert."""
    samples: int
    avg_responders: float
    take_rate: float
    label: str  # "quiet" / "moderate" / "hot" / "unknown"
    emoji: str  # "🟢" / "🟡" / "🔥" / "⚪"

    def short(self) -> str:
        if self.samples == 0:
            return f"{self.emoji} <i>no history yet</i>"
        return (
            f"{self.emoji} {self.label} route "
            f"(avg {self.avg_responders:.1f} responders, "
            f"take {int(self.take_rate * 100)}%, n={self.samples})"
        )


# ---------- queries ----------

# Window in days defaulting to 14 — enough samples per route once the bot
# has been running for ~2 weeks, but recent enough that the picture isn't
# polluted by stale weeks.
def query_route_stats(
    conn: sqlite3.Connection,
    *,
    days: int = 14,
    min_samples: int = 1,
) -> list[RouteStat]:
    sql = """
    WITH last_history AS (
        SELECT broadcast_id,
               MAX(responded_vendors_count) AS max_resp
          FROM broadcast_history
         GROUP BY broadcast_id
    ),
    outcomes AS (
        SELECT b.source_city, b.dest_city, b.car_type_id,
               b.first_fare, b.max_fare,
               b.vanished_at,
               COALESCE(lh.max_resp, 0) AS max_resp,
               CASE
                 WHEN b.vanished_at IS NULL                       THEN 'open'
                 WHEN COALESCE(lh.max_resp, 0) > 0                THEN 'taken'
                 ELSE 'cancelled'
               END AS outcome
          FROM broadcasts b
          LEFT JOIN last_history lh ON b.broadcast_id = lh.broadcast_id
         WHERE b.first_seen_at >= datetime('now', ? )
    )
    SELECT source_city, dest_city, car_type_id,
           COUNT(*)                          AS samples,
           AVG(max_resp)                     AS avg_responders,
           MAX(max_resp)                     AS max_responders,
           AVG(first_fare)                   AS avg_first_fare,
           AVG(max_fare)                     AS avg_max_fare,
           AVG(COALESCE(max_fare,0) - COALESCE(first_fare,0)) AS avg_escalation,
           SUM(CASE WHEN outcome='taken' THEN 1 ELSE 0 END) * 1.0
             / NULLIF(SUM(CASE WHEN outcome IN ('taken','cancelled') THEN 1 ELSE 0 END), 0)
             AS take_rate
      FROM outcomes
     GROUP BY source_city, dest_city, car_type_id
    HAVING samples >= ?
     ORDER BY samples DESC, avg_responders DESC
    """
    rows = conn.execute(sql, (f"-{int(days)} days", int(min_samples))).fetchall()
    out: list[RouteStat] = []
    for r in rows:
        out.append(
            RouteStat(
                source_city=str(r["source_city"] or ""),
                dest_city=str(r["dest_city"] or ""),
                car_type_id=str(r["car_type_id"] or ""),
                samples=int(r["samples"] or 0),
                avg_responders=float(r["avg_responders"] or 0.0),
                max_responders=int(r["max_responders"] or 0),
                take_rate=float(r["take_rate"] or 0.0),
                avg_first_fare=int(r["avg_first_fare"] or 0),
                avg_max_fare=int(r["avg_max_fare"] or 0),
                avg_escalation=int(r["avg_escalation"] or 0),
            )
        )
    return out


# ---------- competition labelling ----------

def _label_for(avg_responders: float) -> tuple[str, str]:
    """Map average responders to a label + emoji.

    These thresholds are deliberately gentle since most Savaari routes show
    very low responder counts (most broadcasts vanish before any vendor
    responds publicly). The user can re-tune later by editing this function
    if their region's distribution differs.
    """
    if avg_responders >= 3.0:
        return "hot", "🔥"
    if avg_responders >= 1.0:
        return "moderate", "🟡"
    return "quiet", "🟢"


def tag_for(stat: Optional[RouteStat]) -> CompetitionTag:
    if stat is None or stat.samples == 0:
        return CompetitionTag(0, 0.0, 0.0, "unknown", "⚪")
    label, emoji = _label_for(stat.avg_responders)
    return CompetitionTag(
        samples=stat.samples,
        avg_responders=stat.avg_responders,
        take_rate=stat.take_rate,
        label=label,
        emoji=emoji,
    )


# ---------- TTL cache ----------

@dataclass
class _CacheEntry:
    fetched_at: float
    by_key: dict[tuple[str, str, str], RouteStat] = field(default_factory=dict)
    all_rows: list[RouteStat] = field(default_factory=list)


class AnalyticsCache:
    """Refreshes route stats at most once every `ttl_s` seconds. Reads are
    plain dict lookups by (source_city, dest_city, car_type_id)."""

    def __init__(self, conn: sqlite3.Connection, *, ttl_s: float = 300.0, days: int = 14):
        self.conn = conn
        self.ttl_s = ttl_s
        self.days = days
        self._entry: Optional[_CacheEntry] = None

    def _refresh(self) -> _CacheEntry:
        rows = query_route_stats(self.conn, days=self.days, min_samples=1)
        entry = _CacheEntry(
            fetched_at=time.monotonic(),
            by_key={r.key(): r for r in rows},
            all_rows=rows,
        )
        self._entry = entry
        log.info("analytics: refreshed (%d routes, %d days)", len(rows), self.days)
        return entry

    def get_all(self, force: bool = False) -> list[RouteStat]:
        e = self._entry
        if force or e is None or (time.monotonic() - e.fetched_at) > self.ttl_s:
            e = self._refresh()
        return e.all_rows

    def get_by_route(
        self, source_city: str, dest_city: str, car_type_id: str
    ) -> Optional[RouteStat]:
        # Touch get_all() to ensure we're populated and within TTL.
        self.get_all()
        if self._entry is None:
            return None
        return self._entry.by_key.get((str(source_city), str(dest_city), str(car_type_id)))

    def invalidate(self) -> None:
        self._entry = None
