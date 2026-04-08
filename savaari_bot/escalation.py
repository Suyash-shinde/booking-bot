"""Escalation curve modelling.

For each (source, dest, car_type) bucket, compute how typical broadcasts
escalate in price before they're taken or cancelled, using the
broadcast_history time series we already collect.

We deliberately keep the bucket coarse (no time-of-day or day-of-week
splits yet) so the per-bucket sample sizes are large enough to be useful
in the first weeks of running. The Phase 5 SQL is the foundation; this
module adds the per-broadcast trajectory analysis on top.

Outputs per bucket:

  samples            number of finished broadcasts in the window
  median_steps       median number of distinct fare values seen per broadcast
  p10_final          10th percentile of the final observed fare
  p50_final          median final fare
  p90_final          90th percentile final fare
  take_rate          fraction of finished broadcasts that were "taken"
                     (anyone responded before vanishing)

Per-alert advice:

  - "wait" if the current fare is well below the bucket's p50 final fare
    AND the take rate is high (the booking is likely to escalate AND get
    taken at a higher price). The user can opt into actually suppressing
    these via cfg.suppress_below_p50.
  - "grab" if the current fare is already at or above p50.
  - "no data" if samples < min_samples.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

log = logging.getLogger("savaari_bot.escalation")


# ---------- helpers ----------

def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Linear-interpolated percentile. Empty list returns 0."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * pct
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return float(sorted_vals[f])
    return float(sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f))


def _cutoff_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=int(days))).isoformat(timespec="seconds")


# ---------- dataclasses ----------

@dataclass
class EscalationStat:
    source_city: str
    dest_city: str
    car_type_id: str
    samples: int
    median_steps: float
    p10_final: int
    p50_final: int
    p90_final: int
    take_rate: float

    def key(self) -> tuple[str, str, str]:
        return (self.source_city, self.dest_city, self.car_type_id)


@dataclass
class EscalationHint:
    """Per-alert hint shown in the Telegram message."""
    samples: int
    p50_final: int
    p90_final: int
    median_steps: float
    take_rate: float
    current_fare: int
    advice: str  # "wait" | "grab" | "neutral" | "unknown"
    emoji: str

    def short(self) -> str:
        if self.samples == 0:
            return "📈 <i>no history</i>"
        # Terse one-liner. Verbose stats are on the dashboard's
        # Escalation table for when the user wants the full picture.
        if self.advice == "wait":
            # WAIT depends on the take rate, so it's the one piece of
            # context worth keeping inline.
            return (
                f"{self.emoji} <b>WAIT</b> · target ₹{self.p50_final:,}"
                f" (n={self.samples}, take {int(self.take_rate*100)}%)"
            )
        verb = {"grab": "GRAB", "neutral": "OK"}.get(self.advice, "—")
        return f"{self.emoji} <b>{verb}</b> · p50 ₹{self.p50_final:,} (n={self.samples})"


# ---------- query ----------

def query_escalation_stats(
    conn: sqlite3.Connection,
    *,
    days: int = 14,
    min_samples: int = 1,
) -> list[EscalationStat]:
    """Walk every (source, dest, car_type) bucket and compute escalation
    stats from broadcast_history time series.

    SQL fetches all rows in one shot then groups in Python — easier to
    read and not noticeably slower than a complex SQL CTE for our scale
    (typically <50k history rows in a 14-day window).
    """
    cutoff = _cutoff_iso(days)
    sql = """
    SELECT b.source_city, b.dest_city, b.car_type_id,
           b.broadcast_id, b.vanished_at,
           h.fare, h.responded_vendors_count
      FROM broadcasts b
      JOIN broadcast_history h ON h.broadcast_id = b.broadcast_id
     WHERE b.first_seen_at >= ?
     ORDER BY b.broadcast_id, h.observed_at
    """
    rows = conn.execute(sql, (cutoff,)).fetchall()

    # Group rows into per-broadcast trajectories.
    by_bid: dict[str, dict[str, Any]] = {}
    for r in rows:
        bid = r["broadcast_id"]
        d = by_bid.setdefault(
            bid,
            {
                "src": str(r["source_city"] or ""),
                "dst": str(r["dest_city"] or ""),
                "ctype": str(r["car_type_id"] or ""),
                "vanished": r["vanished_at"],
                "fares": [],
                "max_resp": 0,
            },
        )
        if r["fare"] is not None:
            d["fares"].append(int(r["fare"]))
        if r["responded_vendors_count"] is not None:
            d["max_resp"] = max(d["max_resp"], int(r["responded_vendors_count"]))

    # Bucket trajectories by route key.
    buckets: dict[tuple[str, str, str], dict[str, list]] = {}
    for d in by_bid.values():
        if d["vanished"] is None:
            continue  # only finished broadcasts
        if not d["fares"]:
            continue
        key = (d["src"], d["dst"], d["ctype"])
        bk = buckets.setdefault(key, {"finals": [], "steps": [], "taken": []})
        bk["finals"].append(d["fares"][-1])
        # "steps" = distinct fare values observed (1 means no escalation).
        bk["steps"].append(len(set(d["fares"])))
        bk["taken"].append(1 if d["max_resp"] > 0 else 0)

    out: list[EscalationStat] = []
    for key, bk in buckets.items():
        n = len(bk["finals"])
        if n < min_samples:
            continue
        finals_sorted = sorted(bk["finals"])
        steps_sorted = sorted(bk["steps"])
        out.append(
            EscalationStat(
                source_city=key[0],
                dest_city=key[1],
                car_type_id=key[2],
                samples=n,
                median_steps=_percentile(steps_sorted, 0.5),
                p10_final=int(round(_percentile(finals_sorted, 0.10))),
                p50_final=int(round(_percentile(finals_sorted, 0.50))),
                p90_final=int(round(_percentile(finals_sorted, 0.90))),
                take_rate=sum(bk["taken"]) / n,
            )
        )
    out.sort(key=lambda s: -s.samples)
    return out


# ---------- per-alert advice ----------

def hint_for(stat: Optional[EscalationStat], current_fare: Optional[int]) -> EscalationHint:
    if stat is None or stat.samples == 0 or current_fare is None:
        return EscalationHint(
            samples=0, p50_final=0, p90_final=0, median_steps=0.0,
            take_rate=0.0, current_fare=current_fare or 0,
            advice="unknown", emoji="📈",
        )
    # The "wait" zone: current is meaningfully below p50 AND take rate is
    # decent. The thresholds are intentionally conservative (10% below,
    # 50% take rate) so we don't tell the user to skip a real opportunity.
    if (
        current_fare < int(stat.p50_final * 0.90)
        and stat.take_rate >= 0.5
        and stat.samples >= 3
    ):
        return EscalationHint(
            samples=stat.samples, p50_final=stat.p50_final, p90_final=stat.p90_final,
            median_steps=stat.median_steps, take_rate=stat.take_rate,
            current_fare=current_fare, advice="wait", emoji="⏳",
        )
    if current_fare >= stat.p50_final:
        return EscalationHint(
            samples=stat.samples, p50_final=stat.p50_final, p90_final=stat.p90_final,
            median_steps=stat.median_steps, take_rate=stat.take_rate,
            current_fare=current_fare, advice="grab", emoji="🎯",
        )
    return EscalationHint(
        samples=stat.samples, p50_final=stat.p50_final, p90_final=stat.p90_final,
        median_steps=stat.median_steps, take_rate=stat.take_rate,
        current_fare=current_fare, advice="neutral", emoji="📈",
    )


# ---------- TTL cache ----------

@dataclass
class _CacheEntry:
    fetched_at: float
    by_key: dict[tuple[str, str, str], EscalationStat] = field(default_factory=dict)
    all_rows: list[EscalationStat] = field(default_factory=list)


class EscalationCache:
    def __init__(self, conn: sqlite3.Connection, *, ttl_s: float = 300.0, days: int = 14):
        self.conn = conn
        self.ttl_s = ttl_s
        self.days = days
        self._entry: Optional[_CacheEntry] = None

    def _refresh(self) -> _CacheEntry:
        rows = query_escalation_stats(self.conn, days=self.days, min_samples=1)
        e = _CacheEntry(
            fetched_at=time.monotonic(),
            by_key={r.key(): r for r in rows},
            all_rows=rows,
        )
        self._entry = e
        log.info("escalation: refreshed (%d buckets, %d days)", len(rows), self.days)
        return e

    def get_all(self, force: bool = False) -> list[EscalationStat]:
        e = self._entry
        if force or e is None or (time.monotonic() - e.fetched_at) > self.ttl_s:
            e = self._refresh()
        return e.all_rows

    def get_by_route(self, src: str, dst: str, ctype: str) -> Optional[EscalationStat]:
        self.get_all()
        if self._entry is None:
            return None
        return self._entry.by_key.get((str(src), str(dst), str(ctype)))

    def invalidate(self) -> None:
        self._entry = None
