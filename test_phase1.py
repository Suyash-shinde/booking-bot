"""Smoke tests for Phase 1 paths that need a fake Telegram + a fake Savaari.

Run: .venv/bin/python test_phase1.py
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from typing import Any

from savaari_bot import config, db
from savaari_bot.notifier import TelegramNotifier
from savaari_bot.savaari import SavaariClient
from savaari_bot.state import AppState
from savaari_bot.telegram import CallbackQuery, TelegramBot


# ---------- fakes ----------

class FakeBot(TelegramBot):
    def __init__(self):
        super().__init__(token="x", chat_id="111")
        self.sent: list[dict[str, Any]] = []
        self.edited: list[dict[str, Any]] = []
        self.answered: list[tuple[str, str]] = []
        self._next_id = 1000

    async def send_message(self, text, *, buttons=None, chat_id=None):
        self._next_id += 1
        self.sent.append({"text": text, "buttons": buttons, "message_id": self._next_id})
        return {"message_id": self._next_id, "chat": {"id": int(self.chat_id)}}

    async def edit_message_text(self, chat_id, message_id, text, *, buttons=None):
        self.edited.append({"chat_id": chat_id, "message_id": message_id, "text": text})

    async def answer_callback_query(self, callback_id, text=""):
        self.answered.append((callback_id, text))


class FakeSavaari(SavaariClient):
    def __init__(self):
        super().__init__(vendor_token="x")
        self.calls: list[tuple[str, str]] = []

    async def post_interest(self, broadcast_id, booking_id, packed_bookings=""):
        self.calls.append((broadcast_id, booking_id))
        return {"status": True, "message": "fake-accepted"}


# ---------- fixture ----------

def fresh_state_and_db():
    cfg = config.Config(telegram_chat_id="111", dry_run_accept=True)
    state = AppState(cfg=cfg)
    # In-memory SQLite for the test.
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db._migrate(conn)
    return state, conn


SAMPLE = {
    "broadcast_id": "15123456",
    "booking_id": "10567890",
    "car_type": "Wagon R or Equivalent",
    "trip_type_name": "Outstation",
    "itinerary": "Mumbai &rarr; Pune",
    "total_amt": "3985",
    "vendor_cost": "3187",
    "start_date": "08 Apr 2026",
    "start_time": "06:30",
    "pick_loc": "Khetwadi, Mumbai",
    "drop_loc": "",
    "auto_cancel_at": "2026-04-08 04:30:00",
    "has_responded": "NO",
}


# ---------- tests ----------

async def test_alert_new_sends_message():
    state, conn = fresh_state_and_db()
    bot, sav = FakeBot(), FakeSavaari()
    n = TelegramNotifier(state, conn, bot, sav)

    await n.alert_new(SAMPLE)
    assert len(bot.sent) == 1, "expected one sendMessage"
    msg = bot.sent[0]
    # Plain notifier-only mode: vendor_cost shown as the fare line, no
    # inline keyboard at all.
    assert "Mumbai" in msg["text"] and "₹3,187" in msg["text"], msg["text"]
    assert msg["buttons"] is None, "notifier-only mode should send no buttons"
    row = db.get_alert(conn, "15123456")
    assert row and row["status"] == "pending"
    print("ok  alert_new")


async def test_alert_new_dedup():
    state, conn = fresh_state_and_db()
    bot, sav = FakeBot(), FakeSavaari()
    n = TelegramNotifier(state, conn, bot, sav)
    await n.alert_new(SAMPLE)
    await n.alert_new(SAMPLE)  # second call should be a no-op
    assert len(bot.sent) == 1, f"dedup failed: {len(bot.sent)} sends"
    print("ok  alert_new dedup")


async def test_fare_floor_filters():
    state, conn = fresh_state_and_db()
    state.cfg.fare_floor = 10000
    bot, sav = FakeBot(), FakeSavaari()
    n = TelegramNotifier(state, conn, bot, sav)
    await n.alert_new(SAMPLE)  # fare 3985 < 10000
    assert bot.sent == [], "fare floor did not filter"
    print("ok  fare_floor")


async def test_confirm_dry_run():
    state, conn = fresh_state_and_db()
    bot, sav = FakeBot(), FakeSavaari()
    n = TelegramNotifier(state, conn, bot, sav)
    await n.alert_new(SAMPLE)

    cbq = CallbackQuery(id="cb1", from_user_id=1, message_id=1001, chat_id=111, data="c:15123456")
    await n.handle_callback(cbq)

    assert sav.calls == [], "dry-run still called postInterest!"
    row = db.get_alert(conn, "15123456")
    assert row["status"] == "confirmed", row["status"]
    log_row = conn.execute("SELECT * FROM accept_log").fetchone()
    assert log_row["dry_run"] == 1
    assert log_row["result_ok"] == 1
    assert any("DRY-RUN" in e["text"] for e in bot.edited), bot.edited
    print("ok  confirm dry-run")


async def test_confirm_live():
    state, conn = fresh_state_and_db()
    state.cfg.dry_run_accept = False
    bot, sav = FakeBot(), FakeSavaari()
    n = TelegramNotifier(state, conn, bot, sav)
    await n.alert_new(SAMPLE)

    cbq = CallbackQuery(id="cb1", from_user_id=1, message_id=1001, chat_id=111, data="c:15123456")
    await n.handle_callback(cbq)

    assert sav.calls == [("15123456", "10567890")], sav.calls
    row = db.get_alert(conn, "15123456")
    assert row["status"] == "confirmed"
    log_row = conn.execute("SELECT * FROM accept_log").fetchone()
    assert log_row["dry_run"] == 0
    print("ok  confirm live")


async def test_double_tap_atomic():
    state, conn = fresh_state_and_db()
    state.cfg.dry_run_accept = False
    bot, sav = FakeBot(), FakeSavaari()
    n = TelegramNotifier(state, conn, bot, sav)
    await n.alert_new(SAMPLE)

    cbq1 = CallbackQuery(id="cb1", from_user_id=1, message_id=1001, chat_id=111, data="c:15123456")
    cbq2 = CallbackQuery(id="cb2", from_user_id=1, message_id=1001, chat_id=111, data="c:15123456")
    await asyncio.gather(n.handle_callback(cbq1), n.handle_callback(cbq2))

    assert len(sav.calls) == 1, f"postInterest called {len(sav.calls)} times!"
    print("ok  double-tap atomic")


async def test_skip():
    state, conn = fresh_state_and_db()
    bot, sav = FakeBot(), FakeSavaari()
    n = TelegramNotifier(state, conn, bot, sav)
    await n.alert_new(SAMPLE)
    cbq = CallbackQuery(id="cb1", from_user_id=1, message_id=1001, chat_id=111, data="s:15123456")
    await n.handle_callback(cbq)
    row = db.get_alert(conn, "15123456")
    assert row["status"] == "skipped"
    assert sav.calls == []
    print("ok  skip")


async def test_unknown_chat_rejected():
    state, conn = fresh_state_and_db()
    bot, sav = FakeBot(), FakeSavaari()
    n = TelegramNotifier(state, conn, bot, sav)
    await n.alert_new(SAMPLE)
    cbq = CallbackQuery(id="cb1", from_user_id=1, message_id=1001, chat_id=999, data="c:15123456")
    await n.handle_callback(cbq)
    assert sav.calls == []
    assert any(t == "unauthorized" for _, t in bot.answered)
    print("ok  unknown chat rejected")


async def test_price_up_sends_new_message():
    """Notifier-only mode: a price bump produces a SECOND send_message
    (matching the original Chrome extension behaviour) instead of editing
    the original message in place. The user wants to be loudly notified."""
    state, conn = fresh_state_and_db()
    bot, sav = FakeBot(), FakeSavaari()
    n = TelegramNotifier(state, conn, bot, sav)
    await n.alert_new(SAMPLE)
    bumped = dict(SAMPLE)
    bumped["total_amt"] = "4365"
    await n.alert_price_up(bumped, 3985, 4365)
    # Two sends total: original alert + price-up alert.
    assert len(bot.sent) == 2, f"expected 2 sends, got {len(bot.sent)}"
    assert "Price Increased" in bot.sent[1]["text"], bot.sent[1]["text"]
    assert bot.sent[1]["buttons"] is None
    row = db.get_alert(conn, "15123456")
    assert row["last_fare"] == 4365
    print("ok  price_up")


async def main():
    await test_alert_new_sends_message()
    await test_alert_new_dedup()
    await test_fare_floor_filters()
    await test_confirm_dry_run()
    await test_confirm_live()
    await test_double_tap_atomic()
    await test_skip()
    await test_unknown_chat_rejected()
    await test_price_up_sends_new_message()
    print()
    print("ALL OK")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
