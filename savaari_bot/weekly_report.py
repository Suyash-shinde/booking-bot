"""Weekly report builder.

Reads route stats from analytics + city/car-type names from db caches and
produces:

  - a plain-text report (for the dashboard preview)
  - the same report as Telegram-flavoured HTML (for actually sending)

The report has three sections:

  1. **Headline numbers** for the period: total broadcasts seen, taken vs
     cancelled, total alerts the bot fired, total Confirms the user tapped.
  2. **Top 5 contested routes** (highest avg responders, n>=3).
  3. **Top 5 quiet routes** (lowest avg responders among routes with n>=3
     so the user can target them).
"""

from __future__ import annotations

import html
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import db
from .analytics import RouteStat, query_route_stats


# ---------- helpers ----------

def _route_label(stat: RouteStat, cities: dict[str, str], car_types: dict[str, str]) -> str:
    src = cities.get(stat.source_city, stat.source_city or "?")
    dst = cities.get(stat.dest_city, stat.dest_city or "?")
    car = car_types.get(stat.car_type_id, stat.car_type_id or "any")
    return f"{src} → {dst} ({car})"


def _car_types_lookup(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        r["car_type_id"]: r["car_name"]
        for r in conn.execute("SELECT car_type_id, car_name FROM car_types").fetchall()
    }


def _headline_counts(conn: sqlite3.Connection, days: int) -> dict[str, int]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    total = conn.execute(
        "SELECT COUNT(*) FROM broadcasts WHERE first_seen_at >= ?", (cutoff,)
    ).fetchone()[0]
    vanished = conn.execute(
        "SELECT COUNT(*) FROM broadcasts WHERE first_seen_at >= ? AND vanished_at IS NOT NULL",
        (cutoff,),
    ).fetchone()[0]
    # Same heuristic as analytics: a vanished broadcast is "taken" iff it
    # ever recorded a non-zero responded_vendors_count.
    taken = conn.execute(
        """
        SELECT COUNT(*) FROM broadcasts b
         WHERE b.first_seen_at >= ?
           AND b.vanished_at IS NOT NULL
           AND EXISTS (
                 SELECT 1 FROM broadcast_history h
                  WHERE h.broadcast_id = b.broadcast_id
                    AND h.responded_vendors_count > 0
               )
        """,
        (cutoff,),
    ).fetchone()[0]
    alerts = conn.execute(
        "SELECT COUNT(*) FROM alerts WHERE sent_at >= ?", (cutoff,)
    ).fetchone()[0]
    confirms = conn.execute(
        "SELECT COUNT(*) FROM accept_log WHERE attempted_at >= ? AND result_ok = 1",
        (cutoff,),
    ).fetchone()[0]
    return {
        "total": int(total),
        "vanished": int(vanished),
        "taken": int(taken),
        "cancelled": int(vanished - taken),
        "alerts_fired": int(alerts),
        "confirms": int(confirms),
    }


# ---------- the report ----------

@dataclass
class WeeklyReport:
    days: int
    headline: dict[str, int]
    contested: list[RouteStat]
    quiet: list[RouteStat]
    cities: dict[str, str]
    car_types: dict[str, str]

    def to_text(self) -> str:
        lines: list[str] = []
        h = self.headline
        lines.append(f"Savaari Bot — last {self.days} days")
        lines.append("")
        lines.append("Headline:")
        lines.append(
            f"  • {h['total']} broadcasts seen "
            f"({h['taken']} taken, {h['cancelled']} cancelled)"
        )
        lines.append(
            f"  • {h['alerts_fired']} alerts fired, {h['confirms']} confirms tapped"
        )
        if h["alerts_fired"]:
            rate = int(round(h["confirms"] / h["alerts_fired"] * 100))
            lines.append(f"    → action rate {rate}%")

        if self.contested:
            lines.append("")
            lines.append("Most contested routes (skip these unless you can move first):")
            for s in self.contested:
                lines.append(
                    f"  🔥 {_route_label(s, self.cities, self.car_types)} — "
                    f"avg {s.avg_responders:.1f} responders, "
                    f"take {int(s.take_rate * 100)}%, n={s.samples}"
                )

        if self.quiet:
            lines.append("")
            lines.append("Quiet routes (target these for higher win rate):")
            for s in self.quiet:
                lines.append(
                    f"  🟢 {_route_label(s, self.cities, self.car_types)} — "
                    f"avg {s.avg_responders:.1f} responders, "
                    f"take {int(s.take_rate * 100)}%, n={s.samples}"
                )

        if not (self.contested or self.quiet):
            lines.append("")
            lines.append("(not enough route history yet — keep the bot running for a few days)")

        return "\n".join(lines)

    def to_html(self) -> str:
        h = self.headline
        out: list[str] = []
        out.append(f"<b>Savaari Bot — last {self.days} days</b>")
        out.append("")
        out.append(
            f"📊 {h['total']} broadcasts · {h['taken']} taken · {h['cancelled']} cancelled"
        )
        out.append(f"🔔 {h['alerts_fired']} alerts · ✅ {h['confirms']} confirms")
        if h["alerts_fired"]:
            rate = int(round(h["confirms"] / h["alerts_fired"] * 100))
            out.append(f"<i>action rate {rate}%</i>")

        if self.contested:
            out.append("")
            out.append("<b>🔥 Most contested</b>")
            for s in self.contested:
                out.append(
                    "• " + html.escape(_route_label(s, self.cities, self.car_types))
                    + f" — avg {s.avg_responders:.1f}, take {int(s.take_rate * 100)}%, n={s.samples}"
                )

        if self.quiet:
            out.append("")
            out.append("<b>🟢 Quietest</b>")
            for s in self.quiet:
                out.append(
                    "• " + html.escape(_route_label(s, self.cities, self.car_types))
                    + f" — avg {s.avg_responders:.1f}, take {int(s.take_rate * 100)}%, n={s.samples}"
                )

        if not (self.contested or self.quiet):
            out.append("")
            out.append("<i>not enough route history yet — keep the bot running</i>")

        return "\n".join(out)


def build_report(conn: sqlite3.Connection, *, days: int = 7, top_n: int = 5) -> WeeklyReport:
    headline = _headline_counts(conn, days)
    rows = query_route_stats(conn, days=days, min_samples=3)
    cities = db.cities_lookup(conn)
    car_types = _car_types_lookup(conn)

    contested = sorted(rows, key=lambda r: -r.avg_responders)[:top_n]
    contested = [r for r in contested if r.avg_responders > 0]

    # Quiet routes: lowest avg responders. Tie-break by sample size desc so
    # we don't suggest a route with one stale sample.
    quiet_pool = sorted(rows, key=lambda r: (r.avg_responders, -r.samples))
    quiet = quiet_pool[:top_n]

    return WeeklyReport(
        days=days,
        headline=headline,
        contested=contested,
        quiet=quiet,
        cities=cities,
        car_types=car_types,
    )
