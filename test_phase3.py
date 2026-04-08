"""Tests for the driver/car availability gate (Phase 3)."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import time
from typing import Any

from savaari_bot import config, db
from savaari_bot.availability import AvailabilityCache, Eligibility
from savaari_bot.notifier import TelegramNotifier
from savaari_bot.savaari import SavaariClient
from savaari_bot.state import AppState
from savaari_bot.telegram import TelegramBot


# ---------- fakes ----------

class FakeBot(TelegramBot):
    def __init__(self):
        super().__init__(token="x", chat_id="111")
        self.sent: list[dict[str, Any]] = []

    async def send_message(self, text, *, buttons=None, chat_id=None):
        self.sent.append({"text": text, "buttons": buttons})
        return {"message_id": len(self.sent), "chat": {"id": 111}}

    async def edit_message_text(self, *a, **k):
        pass

    async def answer_callback_query(self, *a, **k):
        pass


class FakeSavaari(SavaariClient):
    """Programmable fake. Each call increments self.calls and returns the
    next value from self.queue (or raises if queue empty)."""

    def __init__(self, queue: list[Any]):
        super().__init__(vendor_token="x")
        self.queue = list(queue)
        self.calls: list[dict[str, Any]] = []

    async def fetch_drivers_with_cars(self, *, booking_id, user_id, admin_id, usertype="Vendor"):
        self.calls.append({"booking_id": booking_id, "user_id": user_id})
        if not self.queue:
            raise RuntimeError("queue empty")
        v = self.queue.pop(0)
        if isinstance(v, Exception):
            raise v
        return {"resultset": {"carRecordList": v}}


def fresh_state_and_db(**cfg_overrides):
    base = dict(
        telegram_chat_id="111",
        vendor_user_id="70032",
        annotate_eligibility=True,
        require_eligible_car=False,
        eligibility_cache_ttl_s=60.0,
    )
    base.update(cfg_overrides)
    cfg = config.Config(**base)
    state = AppState(cfg=cfg)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db._migrate(conn)
    return state, conn


SAMPLE = {
    "broadcast_id": "15123456",
    "booking_id": "10567890",
    "vendor_cost": "5000",
    "package_kms": "200",
    "exclusions": "Toll",
}


# ---------- cache behaviour ----------

async def test_cache_serves_repeats_without_call():
    sav = FakeSavaari([[{"car_number": "KA01"}]])
    cache = AvailabilityCache(sav, ttl_s=60.0)
    e1 = await cache.get(booking_id="b1", user_id="u", admin_id="u")
    e2 = await cache.get(booking_id="b1", user_id="u", admin_id="u")
    assert e1.eligible_count == 1
    assert e2.eligible_count == 1
    assert len(sav.calls) == 1, f"expected 1 fetch, got {len(sav.calls)}"
    print("ok  cache repeats")


async def test_cache_concurrent_dedup():
    sav = FakeSavaari([[{"car_number": "KA01"}]])
    cache = AvailabilityCache(sav, ttl_s=60.0)
    results = await asyncio.gather(
        *[cache.get(booking_id="b1", user_id="u", admin_id="u") for _ in range(8)]
    )
    assert all(r.eligible_count == 1 for r in results)
    assert len(sav.calls) == 1, f"expected 1 fetch under concurrency, got {len(sav.calls)}"
    print("ok  cache concurrent dedup")


async def test_cache_ttl_expiry():
    sav = FakeSavaari([[{"car_number": "KA01"}], [{"car_number": "KA02"}]])
    cache = AvailabilityCache(sav, ttl_s=0.05)
    e1 = await cache.get(booking_id="b1", user_id="u", admin_id="u")
    await asyncio.sleep(0.08)
    e2 = await cache.get(booking_id="b1", user_id="u", admin_id="u")
    assert len(sav.calls) == 2
    print("ok  cache ttl")


async def test_cache_error_recorded():
    sav = FakeSavaari([RuntimeError("boom")])
    cache = AvailabilityCache(sav, ttl_s=60.0)
    e = await cache.get(booking_id="b1", user_id="u", admin_id="u")
    assert e.error.startswith("RuntimeError")
    assert e.known is False
    print("ok  cache error recorded")


# ---------- notifier integration ----------

async def test_annotate_only_passes_zero_through():
    state, conn = fresh_state_and_db(annotate_eligibility=True, require_eligible_car=False)
    sav = FakeSavaari([[]])  # zero eligible
    cache = AvailabilityCache(sav, ttl_s=60.0)
    n = TelegramNotifier(state, conn, FakeBot(), sav, availability=cache)
    await n.alert_new(SAMPLE)
    # Annotate-only should never suppress; the rendered message no longer
    # carries the eligibility text but the alert still goes out.
    assert len(n.bot.sent) == 1, "annotate-only should never suppress"
    print("ok  annotate-only zero passes")


async def test_gate_suppresses_zero():
    state, conn = fresh_state_and_db(require_eligible_car=True)
    sav = FakeSavaari([[]])
    cache = AvailabilityCache(sav, ttl_s=60.0)
    n = TelegramNotifier(state, conn, FakeBot(), sav, availability=cache)
    await n.alert_new(SAMPLE)
    assert n.bot.sent == [], "gate should have suppressed"
    # Alert row should NOT have been created.
    assert db.get_alert(conn, "15123456") is None
    print("ok  gate suppress zero")


async def test_gate_passes_nonzero_and_annotates():
    state, conn = fresh_state_and_db(require_eligible_car=True)
    sav = FakeSavaari([[{"car_number": "KA01"}, {"car_number": "KA02"}]])
    cache = AvailabilityCache(sav, ttl_s=60.0)
    n = TelegramNotifier(state, conn, FakeBot(), sav, availability=cache)
    await n.alert_new(SAMPLE)
    # The gate let it through; rendered text is plain (no eligible-cars
    # annotation any more).
    assert len(n.bot.sent) == 1
    print("ok  gate pass nonzero")


async def test_gate_fails_open_on_error():
    state, conn = fresh_state_and_db(require_eligible_car=True)
    sav = FakeSavaari([RuntimeError("network down")])
    cache = AvailabilityCache(sav, ttl_s=60.0)
    n = TelegramNotifier(state, conn, FakeBot(), sav, availability=cache)
    await n.alert_new(SAMPLE)
    # The gate must NOT silently drop the alert when the API failed.
    assert len(n.bot.sent) == 1, "gate should fail-open on transport errors"
    text = n.bot.sent[0]["text"]
    assert "Eligible cars" not in text and "No eligible" not in text
    print("ok  gate fails open on error")


async def test_gate_skipped_when_no_user_id():
    state, conn = fresh_state_and_db(vendor_user_id="", require_eligible_car=True)
    sav = FakeSavaari([])  # nothing should be called
    cache = AvailabilityCache(sav, ttl_s=60.0)
    n = TelegramNotifier(state, conn, FakeBot(), sav, availability=cache)
    await n.alert_new(SAMPLE)
    assert len(n.bot.sent) == 1
    assert sav.calls == [], "should not call API without user_id"
    print("ok  gate skipped without user_id")


async def test_no_calls_when_both_flags_off():
    state, conn = fresh_state_and_db(annotate_eligibility=False, require_eligible_car=False)
    sav = FakeSavaari([])
    cache = AvailabilityCache(sav, ttl_s=60.0)
    n = TelegramNotifier(state, conn, FakeBot(), sav, availability=cache)
    await n.alert_new(SAMPLE)
    assert sav.calls == []
    print("ok  no calls when both flags off")


async def main():
    await test_cache_serves_repeats_without_call()
    await test_cache_concurrent_dedup()
    await test_cache_ttl_expiry()
    await test_cache_error_recorded()
    await test_annotate_only_passes_zero_through()
    await test_gate_suppresses_zero()
    await test_gate_passes_nonzero_and_annotates()
    await test_gate_fails_open_on_error()
    await test_gate_skipped_when_no_user_id()
    await test_no_calls_when_both_flags_off()
    print()
    print("ALL OK")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
