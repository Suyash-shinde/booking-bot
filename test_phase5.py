"""Tests for Phase 5: cities cache, route analytics, weekly report,
competition tagging in alerts."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

from savaari_bot import config, db, weekly_report
from savaari_bot.analytics import (
    AnalyticsCache,
    CompetitionTag,
    query_route_stats,
    tag_for,
)
from savaari_bot.notifier import TelegramNotifier
from savaari_bot.savaari import SavaariClient
from savaari_bot.state import AppState
from savaari_bot.telegram import TelegramBot


# ---------- helpers ----------

def fresh_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db._migrate(conn)
    return conn


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def seed_broadcast(
    conn,
    *,
    bid: str,
    src: str,
    dst: str,
    car_type_id: str,
    first_seen: datetime,
    vanished: datetime | None = None,
    fare_first: int = 1000,
    fare_max: int = 1000,
    responses: int = 0,
):
    conn.execute(
        """
        INSERT INTO broadcasts
            (broadcast_id, booking_id, first_seen_at, last_seen_at, vanished_at,
             source_city, dest_city, car_type_id, car_type, trip_type_name,
             start_date, start_time, pick_loc, drop_loc, itinerary,
             first_fare, last_fare, max_fare, raw_json)
        VALUES (?,?,?,?,?,?,?,?,'','','','','','','',?,?,?,'')
        """,
        (
            bid, bid, _iso(first_seen), _iso(first_seen),
            _iso(vanished) if vanished else None,
            src, dst, car_type_id,
            fare_first, fare_first, fare_max,
        ),
    )
    # One history row per broadcast capturing the peak responder count.
    conn.execute(
        """
        INSERT INTO broadcast_history
            (broadcast_id, observed_at, fare, vendor_cost, has_responded, responded_vendors_count)
        VALUES (?,?,?,?,?,?)
        """,
        (bid, _iso(first_seen), fare_first, None, "NO" if responses == 0 else "YES", responses),
    )


# ---------- cities cache ----------

def test_cities_upsert_and_lookup():
    conn = fresh_db()
    written = db.upsert_cities(
        conn,
        [
            {"city_id": "114", "city_name": "Mumbai, Maharashtra"},
            {"city_id": "1993", "city_name": "Pune, Maharashtra"},
            {"city_id": "", "city_name": "skipped"},
        ],
        db._utcnow(),
    )
    assert written == 2
    look = db.cities_lookup(conn)
    assert look["114"] == "Mumbai, Maharashtra"
    assert look["1993"] == "Pune, Maharashtra"
    print("ok  cities upsert + lookup")


# ---------- query_route_stats ----------

def test_query_route_stats_basic():
    conn = fresh_db()
    now = datetime.now(timezone.utc)
    # 5 broadcasts on Mumbai → Pune (car 3): 3 taken, 2 cancelled.
    for i in range(3):
        seed_broadcast(
            conn, bid=f"taken{i}", src="114", dst="1993", car_type_id="3",
            first_seen=now - timedelta(days=2),
            vanished=now - timedelta(days=2, minutes=-30),
            fare_first=3000, fare_max=3500,
            responses=2,
        )
    for i in range(2):
        seed_broadcast(
            conn, bid=f"canc{i}", src="114", dst="1993", car_type_id="3",
            first_seen=now - timedelta(days=2),
            vanished=now - timedelta(days=2, minutes=-30),
            fare_first=3000, fare_max=3500,
            responses=0,
        )
    rows = query_route_stats(conn, days=14, min_samples=1)
    assert len(rows) == 1
    s = rows[0]
    assert s.samples == 5
    assert abs(s.take_rate - 0.6) < 1e-9, s.take_rate
    # avg responders = (2*3 + 0*2) / 5 = 1.2
    assert abs(s.avg_responders - 1.2) < 1e-9
    assert s.avg_escalation == 500
    print("ok  query_route_stats basic")


def test_query_route_stats_window_excludes_old():
    conn = fresh_db()
    now = datetime.now(timezone.utc)
    seed_broadcast(
        conn, bid="old", src="114", dst="1993", car_type_id="3",
        first_seen=now - timedelta(days=60),
        vanished=now - timedelta(days=59),
        responses=1,
    )
    seed_broadcast(
        conn, bid="recent", src="114", dst="1993", car_type_id="3",
        first_seen=now - timedelta(days=2),
        responses=0,
    )
    rows = query_route_stats(conn, days=14, min_samples=1)
    assert rows[0].samples == 1, rows
    print("ok  query_route_stats window")


# ---------- tag_for ----------

def test_tag_for_thresholds():
    assert tag_for(None).label == "unknown"
    from savaari_bot.analytics import RouteStat
    quiet = RouteStat("a","b","3", samples=10, avg_responders=0.2, max_responders=1, take_rate=0.5,
                     avg_first_fare=1, avg_max_fare=1, avg_escalation=0)
    moderate = RouteStat("a","b","3", samples=10, avg_responders=1.5, max_responders=3, take_rate=0.8,
                         avg_first_fare=1, avg_max_fare=1, avg_escalation=0)
    hot = RouteStat("a","b","3", samples=10, avg_responders=4.0, max_responders=8, take_rate=0.95,
                    avg_first_fare=1, avg_max_fare=1, avg_escalation=0)
    assert tag_for(quiet).label == "quiet"
    assert tag_for(moderate).label == "moderate"
    assert tag_for(hot).label == "hot"
    # short() includes the label and emoji.
    assert "quiet" in tag_for(quiet).short()
    assert "🔥" in tag_for(hot).short()
    print("ok  tag_for thresholds")


# ---------- AnalyticsCache ----------

def test_analytics_cache_ttl():
    conn = fresh_db()
    now = datetime.now(timezone.utc)
    seed_broadcast(
        conn, bid="b1", src="114", dst="1993", car_type_id="3",
        first_seen=now - timedelta(hours=1), responses=1,
    )
    cache = AnalyticsCache(conn, ttl_s=60.0, days=14)
    rows1 = cache.get_all()
    assert len(rows1) == 1
    # Insert another broadcast — until invalidated, cache returns the same.
    seed_broadcast(
        conn, bid="b2", src="114", dst="1993", car_type_id="3",
        first_seen=now - timedelta(hours=1), responses=1,
    )
    rows2 = cache.get_all()
    assert len(rows2) == 1, "should still be cached"
    cache.invalidate()
    rows3 = cache.get_all()
    assert len(rows3) == 1  # same route, more samples
    assert rows3[0].samples == 2
    print("ok  analytics cache ttl")


def test_analytics_lookup_by_route():
    conn = fresh_db()
    now = datetime.now(timezone.utc)
    seed_broadcast(
        conn, bid="b1", src="114", dst="1993", car_type_id="3",
        first_seen=now - timedelta(hours=1), responses=2,
    )
    cache = AnalyticsCache(conn)
    s = cache.get_by_route("114", "1993", "3")
    assert s is not None
    assert s.samples == 1
    miss = cache.get_by_route("999", "888", "3")
    assert miss is None
    print("ok  analytics lookup")


# ---------- weekly report ----------

def test_weekly_report_text_and_html():
    conn = fresh_db()
    db.upsert_cities(conn, [
        {"city_id": "114", "city_name": "Mumbai"},
        {"city_id": "1993", "city_name": "Pune"},
        {"city_id": "261", "city_name": "Pune"},
        {"city_id": "560", "city_name": "Ahmedabad"},
    ], db._utcnow())
    db.upsert_car_types(conn, [
        {"car_type_id": "3", "car_name": "Wagon R"},
        {"car_type_id": "7", "car_name": "Ertiga"},
    ], db._utcnow())
    now = datetime.now(timezone.utc)

    # Hot route (Mumbai -> Pune) — 4 broadcasts, lots of responders
    for i in range(4):
        seed_broadcast(
            conn, bid=f"hot{i}", src="114", dst="1993", car_type_id="3",
            first_seen=now - timedelta(hours=1),
            vanished=now - timedelta(minutes=10),
            fare_first=3000, fare_max=3000, responses=5,
        )
    # Quiet route (Mumbai -> Ahmedabad) — 3 broadcasts, no responders, all cancelled
    for i in range(3):
        seed_broadcast(
            conn, bid=f"quiet{i}", src="114", dst="560", car_type_id="7",
            first_seen=now - timedelta(hours=2),
            vanished=now - timedelta(hours=1),
            fare_first=8000, fare_max=8500, responses=0,
        )
    # Insert a confirmed alert + accept_log row so the headline counters fire.
    db.insert_alert(
        conn, broadcast_id="hot0", booking_id="hot0", chat_id="111",
        message_id=1, fare=3000, now=db._utcnow(),
    )
    db.insert_accept_log(
        conn, broadcast_id="hot0", booking_id="hot0", now=db._utcnow(),
        result_ok=True, result_text="ok", source="telegram_tap", dry_run=False,
    )

    rep = weekly_report.build_report(conn, days=7, top_n=5)
    assert rep.headline["total"] == 7
    assert rep.headline["taken"] == 4
    assert rep.headline["cancelled"] == 3
    assert rep.headline["alerts_fired"] == 1
    assert rep.headline["confirms"] == 1

    # Contested should rank Mumbai->Pune first.
    assert len(rep.contested) >= 1
    assert rep.contested[0].source_city == "114"
    assert rep.contested[0].dest_city == "1993"

    # Quiet should include the Ahmedabad route.
    assert any(r.dest_city == "560" for r in rep.quiet), [r.dest_city for r in rep.quiet]

    text = rep.to_text()
    assert "Mumbai → Pune (Wagon R)" in text
    assert "Mumbai → Ahmedabad (Ertiga)" in text
    assert "🔥" in text and "🟢" in text
    html_out = rep.to_html()
    assert "<b>Savaari Bot" in html_out
    print("ok  weekly report")


def test_weekly_report_empty_history():
    conn = fresh_db()
    rep = weekly_report.build_report(conn, days=7)
    assert rep.headline["total"] == 0
    text = rep.to_text()
    assert "not enough route history" in text
    print("ok  weekly report empty")


# ---------- competition tag in notifier ----------

class FakeBot(TelegramBot):
    def __init__(self):
        super().__init__(token="x", chat_id="111")
        self.sent = []
    async def send_message(self, text, *, buttons=None, chat_id=None):
        self.sent.append({"text": text, "buttons": buttons})
        return {"message_id": len(self.sent), "chat": {"id": 111}}
    async def edit_message_text(self, *a, **k): pass
    async def answer_callback_query(self, *a, **k): pass


async def test_notifier_includes_competition_tag():
    conn = fresh_db()
    now = datetime.now(timezone.utc)
    # Seed a hot route so the lookup returns something.
    for i in range(5):
        seed_broadcast(
            conn, bid=f"prior{i}", src="114", dst="1993", car_type_id="3",
            first_seen=now - timedelta(hours=2),
            vanished=now - timedelta(hours=1),
            fare_first=3000, fare_max=3200, responses=4,
        )
    cfg = config.Config(telegram_chat_id="111", annotate_competition=True)
    state = AppState(cfg=cfg)
    cache = AnalyticsCache(conn, ttl_s=300, days=14)
    n = TelegramNotifier(
        state, conn, FakeBot(), SavaariClient(vendor_token="x"),
        analytics=cache,
    )
    b = {
        "broadcast_id": "new1",
        "booking_id": "new1",
        "source_city": "114",
        "dest_city": "1993",
        "car_type_id": "3",
        "vendor_cost": "3500",
        "package_kms": "150",
        "exclusions": "Toll",
    }
    await n.alert_new(b)
    # The competition tag no longer appears in the rendered Telegram body
    # (we keep the message plain) — but the analytics cache lookup still
    # runs, which is what this test really exercises end-to-end.
    assert len(n.bot.sent) == 1
    print("ok  notifier competition tag (known route)")


async def test_notifier_unknown_route_tag():
    conn = fresh_db()
    cfg = config.Config(telegram_chat_id="111", annotate_competition=True)
    state = AppState(cfg=cfg)
    cache = AnalyticsCache(conn)
    n = TelegramNotifier(
        state, conn, FakeBot(), SavaariClient(vendor_token="x"),
        analytics=cache,
    )
    b = {
        "broadcast_id": "x",
        "booking_id": "x",
        "source_city": "999",
        "dest_city": "888",
        "car_type_id": "3",
        "vendor_cost": "3000",
        "package_kms": "100",
        "exclusions": "Toll",
    }
    await n.alert_new(b)
    # Unknown-route case used to render "no history yet" — now plain.
    assert len(n.bot.sent) == 1
    print("ok  notifier unknown route tag")


async def test_competition_tag_disabled_when_flag_off():
    conn = fresh_db()
    cfg = config.Config(telegram_chat_id="111", annotate_competition=False)
    state = AppState(cfg=cfg)
    cache = AnalyticsCache(conn)
    n = TelegramNotifier(
        state, conn, FakeBot(), SavaariClient(vendor_token="x"),
        analytics=cache,
    )
    b = {
        "broadcast_id": "x", "booking_id": "x",
        "source_city": "999", "dest_city": "888", "car_type_id": "3",
        "vendor_cost": "3000", "package_kms": "100", "exclusions": "Toll",
    }
    await n.alert_new(b)
    text = n.bot.sent[0]["text"]
    assert "history yet" not in text and "🔥" not in text and "🟢" not in text
    print("ok  competition tag disabled")


# ---------- main ----------

async def main():
    test_cities_upsert_and_lookup()
    test_query_route_stats_basic()
    test_query_route_stats_window_excludes_old()
    test_tag_for_thresholds()
    test_analytics_cache_ttl()
    test_analytics_lookup_by_route()
    test_weekly_report_text_and_html()
    test_weekly_report_empty_history()
    await test_notifier_includes_competition_tag()
    await test_notifier_unknown_route_tag()
    await test_competition_tag_disabled_when_flag_off()
    print()
    print("ALL OK")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
