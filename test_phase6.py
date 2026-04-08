"""Tests for Phase 6: escalation curves + WAIT/GRAB hints in alerts."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

from savaari_bot import config, db
from savaari_bot.escalation import (
    EscalationCache,
    EscalationStat,
    _percentile,
    hint_for,
    query_escalation_stats,
)
from savaari_bot.notifier import TelegramNotifier
from savaari_bot.savaari import SavaariClient
from savaari_bot.state import AppState
from savaari_bot.telegram import TelegramBot


def fresh_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db._migrate(conn)
    return conn


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def seed_trajectory(
    conn,
    *,
    bid: str,
    src: str,
    dst: str,
    car_type_id: str,
    fare_steps: list[int],
    first_seen: datetime,
    vanished_after_minutes: int = 30,
    responses_at_end: int = 0,
):
    """Insert a broadcast + history rows representing a fare trajectory."""
    vanished = first_seen + timedelta(minutes=vanished_after_minutes)
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
            bid, bid, _iso(first_seen), _iso(vanished), _iso(vanished),
            src, dst, car_type_id,
            fare_steps[0], fare_steps[-1], max(fare_steps),
        ),
    )
    for i, fare in enumerate(fare_steps):
        ts = first_seen + timedelta(minutes=i)
        responses = responses_at_end if i == len(fare_steps) - 1 else 0
        conn.execute(
            """
            INSERT INTO broadcast_history
                (broadcast_id, observed_at, fare, vendor_cost, has_responded, responded_vendors_count)
            VALUES (?,?,?,?,?,?)
            """,
            (bid, _iso(ts), fare, None, "YES" if responses > 0 else "NO", responses),
        )


# ---------- _percentile ----------

def test_percentile_basic():
    assert _percentile([], 0.5) == 0.0
    assert _percentile([10], 0.5) == 10.0
    assert _percentile([1, 2, 3, 4, 5], 0.0) == 1.0
    assert _percentile([1, 2, 3, 4, 5], 1.0) == 5.0
    assert _percentile([1, 2, 3, 4, 5], 0.5) == 3.0
    # Even-length: linear interp.
    assert abs(_percentile([1, 2, 3, 4], 0.5) - 2.5) < 1e-9
    print("ok  percentile")


# ---------- query_escalation_stats ----------

def test_escalation_stats_simple_route():
    conn = fresh_db()
    now = datetime.now(timezone.utc) - timedelta(hours=1)
    # Three broadcasts, all on the same route, all escalating two steps.
    seed_trajectory(conn, bid="b1", src="114", dst="1993", car_type_id="3",
                    fare_steps=[3000, 3200, 3400], first_seen=now, responses_at_end=2)
    seed_trajectory(conn, bid="b2", src="114", dst="1993", car_type_id="3",
                    fare_steps=[3000, 3200, 3400], first_seen=now, responses_at_end=1)
    seed_trajectory(conn, bid="b3", src="114", dst="1993", car_type_id="3",
                    fare_steps=[3000, 3200, 3400], first_seen=now, responses_at_end=0)
    rows = query_escalation_stats(conn, days=14)
    assert len(rows) == 1
    s = rows[0]
    assert s.samples == 3
    assert s.median_steps == 3.0   # 3 distinct fare values
    assert s.p50_final == 3400
    assert abs(s.take_rate - (2/3)) < 1e-9
    print("ok  escalation stats simple")


def test_escalation_stats_skips_open_broadcasts():
    conn = fresh_db()
    now = datetime.now(timezone.utc) - timedelta(hours=1)
    # One vanished, one still open.
    seed_trajectory(conn, bid="done", src="114", dst="1993", car_type_id="3",
                    fare_steps=[3000, 3200], first_seen=now, responses_at_end=1)
    # Open broadcast (no vanished_at).
    conn.execute(
        """
        INSERT INTO broadcasts
            (broadcast_id, booking_id, first_seen_at, last_seen_at, vanished_at,
             source_city, dest_city, car_type_id, car_type, trip_type_name,
             start_date, start_time, pick_loc, drop_loc, itinerary,
             first_fare, last_fare, max_fare, raw_json)
        VALUES ('open','open',?,?,NULL,'114','1993','3','','','','','','','',3000,3000,3000,'')
        """,
        (_iso(now), _iso(now)),
    )
    conn.execute(
        """
        INSERT INTO broadcast_history (broadcast_id, observed_at, fare, vendor_cost, has_responded, responded_vendors_count)
        VALUES ('open',?,3000,NULL,'NO',0)
        """,
        (_iso(now),),
    )
    rows = query_escalation_stats(conn, days=14)
    assert len(rows) == 1
    assert rows[0].samples == 1
    print("ok  escalation skips open broadcasts")


def test_escalation_stats_multiple_routes_sorted():
    conn = fresh_db()
    now = datetime.now(timezone.utc) - timedelta(hours=1)
    for i in range(5):
        seed_trajectory(conn, bid=f"a{i}", src="114", dst="1993", car_type_id="3",
                        fare_steps=[3000, 3300], first_seen=now, responses_at_end=1)
    for i in range(2):
        seed_trajectory(conn, bid=f"b{i}", src="114", dst="560", car_type_id="7",
                        fare_steps=[8000], first_seen=now, responses_at_end=0)
    rows = query_escalation_stats(conn, days=14)
    # Sorted by samples desc.
    assert rows[0].samples == 5
    assert rows[1].samples == 2
    print("ok  escalation multiple sorted")


# ---------- hint_for ----------

def test_hint_unknown_when_no_history():
    h = hint_for(None, 1000)
    assert h.advice == "unknown" and h.samples == 0
    assert "no history" in h.short()
    print("ok  hint unknown")


def test_hint_wait_when_below_p50_high_take():
    s = EscalationStat("a","b","3", samples=10, median_steps=2,
                       p10_final=2800, p50_final=4000, p90_final=4400, take_rate=0.8)
    h = hint_for(s, 3000)  # 3000 < 4000*0.9 = 3600
    assert h.advice == "wait", h.advice
    assert "WAIT" in h.short()
    print("ok  hint wait")


def test_hint_grab_when_at_or_above_p50():
    s = EscalationStat("a","b","3", samples=10, median_steps=2,
                       p10_final=2800, p50_final=4000, p90_final=4400, take_rate=0.8)
    h = hint_for(s, 4000)
    assert h.advice == "grab"
    assert "GRAB" in h.short()
    h2 = hint_for(s, 4200)
    assert h2.advice == "grab"
    print("ok  hint grab")


def test_hint_neutral_in_between():
    s = EscalationStat("a","b","3", samples=10, median_steps=2,
                       p10_final=2800, p50_final=4000, p90_final=4400, take_rate=0.8)
    h = hint_for(s, 3800)  # > 3600 (90% of p50), < 4000
    assert h.advice == "neutral"
    print("ok  hint neutral")


def test_hint_does_not_recommend_wait_with_low_take_rate():
    # Low take rate means waiting probably means missing the booking.
    s = EscalationStat("a","b","3", samples=10, median_steps=2,
                       p10_final=2800, p50_final=4000, p90_final=4400, take_rate=0.2)
    h = hint_for(s, 3000)
    assert h.advice != "wait"
    print("ok  hint refuses wait when low take rate")


def test_hint_requires_min_samples_for_wait():
    s = EscalationStat("a","b","3", samples=2, median_steps=2,
                       p10_final=2800, p50_final=4000, p90_final=4400, take_rate=0.8)
    h = hint_for(s, 3000)
    assert h.advice != "wait"
    print("ok  hint requires min samples for wait")


# ---------- EscalationCache ----------

def test_escalation_cache_ttl_and_invalidate():
    conn = fresh_db()
    now = datetime.now(timezone.utc) - timedelta(hours=1)
    seed_trajectory(conn, bid="b1", src="114", dst="1993", car_type_id="3",
                    fare_steps=[3000, 3200], first_seen=now, responses_at_end=1)
    cache = EscalationCache(conn, ttl_s=60.0, days=14)
    rows1 = cache.get_all()
    assert len(rows1) == 1
    seed_trajectory(conn, bid="b2", src="114", dst="1993", car_type_id="3",
                    fare_steps=[3000, 3200], first_seen=now, responses_at_end=1)
    rows2 = cache.get_all()
    assert rows2[0].samples == 1, "should still be cached"
    cache.invalidate()
    rows3 = cache.get_all()
    assert rows3[0].samples == 2
    print("ok  escalation cache")


# ---------- notifier integration ----------

class FakeBot(TelegramBot):
    def __init__(self):
        super().__init__(token="x", chat_id="111")
        self.sent = []
    async def send_message(self, text, *, buttons=None, chat_id=None):
        self.sent.append({"text": text, "buttons": buttons})
        return {"message_id": len(self.sent), "chat": {"id": 111}}
    async def edit_message_text(self, *a, **k): pass
    async def answer_callback_query(self, *a, **k): pass


async def test_alert_includes_grab_hint():
    conn = fresh_db()
    now = datetime.now(timezone.utc) - timedelta(hours=1)
    for i in range(5):
        seed_trajectory(conn, bid=f"prior{i}", src="114", dst="1993", car_type_id="3",
                        fare_steps=[3000, 3300, 3500], first_seen=now, responses_at_end=2)
    cfg = config.Config(
        telegram_chat_id="111",
        annotate_escalation=True, annotate_competition=False,
    )
    state = AppState(cfg=cfg)
    cache = EscalationCache(conn)
    n = TelegramNotifier(
        state, conn, FakeBot(), SavaariClient(vendor_token="x"),
        escalation=cache,
    )
    b = {
        "broadcast_id": "new1", "booking_id": "new1",
        "source_city": "114", "dest_city": "1993", "car_type_id": "3",
        "vendor_cost": "3500", "total_amt": "3700",
        "package_kms": "150", "exclusions": "Toll",
    }
    await n.alert_new(b)
    text = n.bot.sent[0]["text"]
    # Final fare in seeded data is 3500; total_amt above is 3700 > p50.
    assert "GRAB" in text or "OK" in text, text
    print("ok  alert grab hint")


async def test_alert_suppressed_when_wait_and_flag_on():
    conn = fresh_db()
    now = datetime.now(timezone.utc) - timedelta(hours=1)
    # P50 final = 4000, all takes
    for i in range(5):
        seed_trajectory(conn, bid=f"prior{i}", src="114", dst="1993", car_type_id="3",
                        fare_steps=[3000, 3500, 4000], first_seen=now, responses_at_end=3)
    cfg = config.Config(
        telegram_chat_id="111",
        annotate_escalation=True, suppress_below_p50=True, annotate_competition=False,
    )
    state = AppState(cfg=cfg)
    cache = EscalationCache(conn)
    n = TelegramNotifier(
        state, conn, FakeBot(), SavaariClient(vendor_token="x"),
        escalation=cache,
    )
    b = {
        "broadcast_id": "new", "booking_id": "new",
        "source_city": "114", "dest_city": "1993", "car_type_id": "3",
        "vendor_cost": "2500", "total_amt": "3000",  # well below p50 of 4000
        "package_kms": "150", "exclusions": "Toll",
    }
    await n.alert_new(b)
    assert n.bot.sent == [], "wait-mode should have suppressed"
    print("ok  alert suppressed when wait")


async def test_alert_not_suppressed_with_flag_off():
    conn = fresh_db()
    now = datetime.now(timezone.utc) - timedelta(hours=1)
    for i in range(5):
        seed_trajectory(conn, bid=f"prior{i}", src="114", dst="1993", car_type_id="3",
                        fare_steps=[3000, 3500, 4000], first_seen=now, responses_at_end=3)
    cfg = config.Config(
        telegram_chat_id="111",
        annotate_escalation=True, suppress_below_p50=False, annotate_competition=False,
    )
    state = AppState(cfg=cfg)
    cache = EscalationCache(conn)
    n = TelegramNotifier(
        state, conn, FakeBot(), SavaariClient(vendor_token="x"),
        escalation=cache,
    )
    b = {
        "broadcast_id": "new", "booking_id": "new",
        "source_city": "114", "dest_city": "1993", "car_type_id": "3",
        "vendor_cost": "2500", "total_amt": "3000",
        "package_kms": "150", "exclusions": "Toll",
    }
    await n.alert_new(b)
    assert len(n.bot.sent) == 1
    text = n.bot.sent[0]["text"]
    assert "WAIT" in text, text  # still annotated, just not suppressed
    print("ok  alert not suppressed when flag off")


async def test_unknown_route_renders_no_history_yet():
    conn = fresh_db()
    cfg = config.Config(
        telegram_chat_id="111",
        annotate_escalation=True, annotate_competition=False,
    )
    state = AppState(cfg=cfg)
    cache = EscalationCache(conn)
    n = TelegramNotifier(
        state, conn, FakeBot(), SavaariClient(vendor_token="x"),
        escalation=cache,
    )
    b = {
        "broadcast_id": "x", "booking_id": "x",
        "source_city": "999", "dest_city": "888", "car_type_id": "3",
        "vendor_cost": "3000", "package_kms": "100", "exclusions": "Toll",
    }
    await n.alert_new(b)
    text = n.bot.sent[0]["text"]
    assert "no history" in text
    print("ok  unknown route hint")


async def test_disabled_skips_lookup():
    conn = fresh_db()
    cfg = config.Config(
        telegram_chat_id="111",
        annotate_escalation=False, annotate_competition=False,
    )
    state = AppState(cfg=cfg)
    cache = EscalationCache(conn)
    n = TelegramNotifier(
        state, conn, FakeBot(), SavaariClient(vendor_token="x"),
        escalation=cache,
    )
    b = {
        "broadcast_id": "x", "booking_id": "x",
        "source_city": "999", "dest_city": "888", "car_type_id": "3",
        "vendor_cost": "3000", "package_kms": "100", "exclusions": "Toll",
    }
    await n.alert_new(b)
    text = n.bot.sent[0]["text"]
    # When the flag is off, none of the escalation hint variants should appear.
    assert "WAIT" not in text and "GRAB" not in text and "no history" not in text
    print("ok  disabled skips")


# ---------- main ----------

async def main():
    test_percentile_basic()
    test_escalation_stats_simple_route()
    test_escalation_stats_skips_open_broadcasts()
    test_escalation_stats_multiple_routes_sorted()
    test_hint_unknown_when_no_history()
    test_hint_wait_when_below_p50_high_take()
    test_hint_grab_when_at_or_above_p50()
    test_hint_neutral_in_between()
    test_hint_does_not_recommend_wait_with_low_take_rate()
    test_hint_requires_min_samples_for_wait()
    test_escalation_cache_ttl_and_invalidate()
    await test_alert_includes_grab_hint()
    await test_alert_suppressed_when_wait_and_flag_on()
    await test_alert_not_suppressed_with_flag_off()
    await test_unknown_route_renders_no_history_yet()
    await test_disabled_skips_lookup()
    print()
    print("ALL OK")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
