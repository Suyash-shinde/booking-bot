"""Tests for Phase 4.5: auto-position-update on confirm + Telegram commands."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from typing import Any

from savaari_bot import config, db, fleet
from savaari_bot.geo import GeocodeResult, Geocoder, Route, Router
from savaari_bot.notifier import (
    TelegramNotifier,
    _predict_trip_end,
)
from savaari_bot.savaari import SavaariClient
from savaari_bot.state import AppState
from savaari_bot.telegram import CallbackQuery, IncomingMessage, TelegramBot


# ---------- fakes ----------

class FakeBot(TelegramBot):
    def __init__(self):
        super().__init__(token="x", chat_id="111")
        self.sent: list[dict[str, Any]] = []
        self.edited: list[dict[str, Any]] = []

    async def send_message(self, text, *, buttons=None, chat_id=None):
        self.sent.append({"text": text, "buttons": buttons})
        return {"message_id": len(self.sent), "chat": {"id": 111}}

    async def edit_message_text(self, chat_id, message_id, text, *, buttons=None):
        self.edited.append({"text": text})

    async def answer_callback_query(self, *a, **k):
        pass


class FakeGeocoder(Geocoder):
    def __init__(self, conn, answers=None):
        super().__init__(conn, base_url="x", user_agent="x", min_interval_s=0)
        self.answers = answers or {}

    async def geocode(self, query: str):
        return self.answers.get(query)


class FakeRouter(Router):
    def __init__(self, conn):
        super().__init__(conn, base_url="x")
    async def route(self, *a, **k):
        return Route(distance_m=10000, duration_s=600)


class FakeSavaari(SavaariClient):
    def __init__(self):
        super().__init__(vendor_token="x")
        self.calls = []
    async def post_interest(self, broadcast_id, booking_id, packed_bookings=""):
        self.calls.append((broadcast_id, booking_id))
        return {"status": True, "message": "fake-accepted"}


def fresh():
    cfg = config.Config(
        telegram_chat_id="111",
        enable_deadhead=True,
        annotate_competition=False,
        dry_run_accept=False,  # we want auto-relocate to fire
    )
    state = AppState(cfg=cfg)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db._migrate(conn)
    return state, conn


PUNE = GeocodeResult(lat=18.5204, lng=73.8567, display_name="Pune")
MUMBAI = GeocodeResult(lat=19.0760, lng=72.8777, display_name="Mumbai")
GOA = GeocodeResult(lat=15.2993, lng=74.1240, display_name="Goa")


# ---------- _predict_trip_end ----------

def test_predict_trip_end_basic():
    end = _predict_trip_end({
        "pickup_time": "2026-04-08 06:30:00",
        "num_days": "1",
        "package_kms": "300",
    })
    # 1 day = 24h, 300/50=6h → max(24,6)=24h
    assert end is not None and end.startswith("2026-04-09T06:30:00"), end
    print("ok  predict trip end basic")


def test_predict_trip_end_multi_day():
    end = _predict_trip_end({
        "pickup_time": "2026-04-08 07:00:00",
        "num_days": "3",
        "package_kms": "1000",
    })
    assert end.startswith("2026-04-11T07:00:00"), end  # +72h
    print("ok  predict trip end multi day")


def test_predict_trip_end_unparseable_returns_none():
    assert _predict_trip_end({"pickup_time": ""}) is None
    assert _predict_trip_end({}) is None
    print("ok  predict trip end none")


# ---------- alert stores picked_car_id + trip end ----------

async def test_alert_stores_picked_car_and_trip_end():
    state, conn = fresh()
    fleet.upsert_car(
        conn, label="KA-1", car_type_id="3",
        location_text="Mumbai", location_lat=MUMBAI.lat, location_lng=MUMBAI.lng,
    )
    geo = FakeGeocoder(conn, {"Pune Airport": PUNE})
    n = TelegramNotifier(
        state, conn, FakeBot(), FakeSavaari(),
        geocoder=geo, router=FakeRouter(conn),
    )
    b = {
        "broadcast_id": "10",
        "booking_id": "100",
        "car_type_id": "3",
        "vendor_cost": "5000",
        "package_kms": "200",
        "pick_loc": "Pune Airport",
        "drop_loc": "Goa",
        "pickup_time": "2026-04-08 09:00:00",
        "num_days": "1",
        "exclusions": "Toll",
    }
    await n.alert_new(b)
    row = db.get_alert(conn, "10")
    assert row is not None
    assert row["picked_car_id"] == 1, row["picked_car_id"]
    assert row["drop_loc_text"] == "Goa"
    assert row["predicted_end_ts"].startswith("2026-04-09T09:00:00"), row["predicted_end_ts"]
    print("ok  alert stores picked car + end ts")


# ---------- auto-relocate on confirm ----------

async def test_confirm_relocates_picked_car():
    state, conn = fresh()
    fleet.upsert_car(
        conn, label="KA-1", car_type_id="3",
        location_text="Mumbai", location_lat=MUMBAI.lat, location_lng=MUMBAI.lng,
    )
    geo = FakeGeocoder(conn, {"Pune Airport": PUNE, "Goa": GOA})
    n = TelegramNotifier(
        state, conn, FakeBot(), FakeSavaari(),
        geocoder=geo, router=FakeRouter(conn),
    )
    b = {
        "broadcast_id": "10",
        "booking_id": "100",
        "car_type_id": "3",
        "vendor_cost": "5000",
        "package_kms": "200",
        "pick_loc": "Pune Airport",
        "drop_loc": "Goa",
        "pickup_time": "2026-04-08 09:00:00",
        "num_days": "1",
        "exclusions": "Toll",
    }
    await n.alert_new(b)
    cbq = CallbackQuery(id="cb", from_user_id=1, message_id=1, chat_id=111, data="c:10")
    await n.handle_callback(cbq)

    # Car 1 should now be at Goa coords and busy until ~next day.
    car = fleet.get_car(conn, 1)
    assert abs(car.location_lat - GOA.lat) < 1e-6, car.location_lat
    assert abs(car.location_lng - GOA.lng) < 1e-6
    assert car.location_text == "Goa"
    assert car.busy_until_ts.startswith("2026-04-09T09:00:00")
    print("ok  confirm relocates picked car")


async def test_confirm_with_no_picked_car_is_safe():
    state, conn = fresh()
    # No fleet at all → car_pick is None → picked_car_id stays NULL.
    geo = FakeGeocoder(conn, {})
    n = TelegramNotifier(
        state, conn, FakeBot(), FakeSavaari(),
        geocoder=geo, router=FakeRouter(conn),
    )
    b = {
        "broadcast_id": "10",
        "booking_id": "100",
        "car_type_id": "3",
        "vendor_cost": "5000",
        "package_kms": "200",
        "pick_loc": "Pune Airport",
        "drop_loc": "Goa",
        "pickup_time": "2026-04-08 09:00:00",
        "num_days": "1",
        "exclusions": "Toll",
    }
    await n.alert_new(b)
    cbq = CallbackQuery(id="cb", from_user_id=1, message_id=1, chat_id=111, data="c:10")
    # Should not crash even though there's no picked car.
    await n.handle_callback(cbq)
    print("ok  confirm with no picked car is safe")


# ---------- Telegram commands ----------

async def test_help_command():
    state, conn = fresh()
    n = TelegramNotifier(state, conn, FakeBot(), FakeSavaari())
    await n.handle_message(IncomingMessage(message_id=1, chat_id=111, from_user_id=1, text="/help"))
    assert any("Commands" in s["text"] for s in n.bot.sent)
    print("ok  /help")


async def test_unknown_command():
    state, conn = fresh()
    n = TelegramNotifier(state, conn, FakeBot(), FakeSavaari())
    await n.handle_message(IncomingMessage(1, 111, 1, "/foo"))
    assert any("Unknown command" in s["text"] for s in n.bot.sent)
    print("ok  unknown command")


async def test_unauthorized_chat_ignored():
    state, conn = fresh()
    n = TelegramNotifier(state, conn, FakeBot(), FakeSavaari())
    await n.handle_message(IncomingMessage(1, 999, 1, "/help"))
    assert n.bot.sent == []
    print("ok  unauthorized chat")


async def test_pause_resume_commands():
    state, conn = fresh()
    n = TelegramNotifier(state, conn, FakeBot(), FakeSavaari())
    await n.handle_message(IncomingMessage(1, 111, 1, "/pause"))
    assert state.paused is True
    await n.handle_message(IncomingMessage(1, 111, 1, "/resume"))
    assert state.paused is False
    print("ok  /pause /resume")


async def test_cars_empty_and_populated():
    state, conn = fresh()
    n = TelegramNotifier(state, conn, FakeBot(), FakeSavaari())
    await n.handle_message(IncomingMessage(1, 111, 1, "/cars"))
    assert any("No cars registered" in s["text"] for s in n.bot.sent)

    fleet.upsert_car(
        conn, label="KA-01-MX-9999", car_type_id="3",
        location_text="Mumbai", location_lat=MUMBAI.lat, location_lng=MUMBAI.lng,
    )
    n.bot.sent.clear()
    await n.handle_message(IncomingMessage(1, 111, 1, "/cars"))
    text = n.bot.sent[0]["text"]
    assert "KA-01-MX-9999" in text and "Mumbai" in text
    print("ok  /cars")


async def test_where_command_relocates_and_geocodes():
    state, conn = fresh()
    fleet.upsert_car(
        conn, label="KA-01-MX-9999", car_type_id="3",
        location_text="Mumbai", location_lat=MUMBAI.lat, location_lng=MUMBAI.lng,
        busy_until_ts="2099-01-01T00:00:00",
    )
    geo = FakeGeocoder(conn, {"Pune Airport": PUNE})
    n = TelegramNotifier(state, conn, FakeBot(), FakeSavaari(), geocoder=geo)
    await n.handle_message(IncomingMessage(1, 111, 1, "/where KA-01 Pune Airport"))
    car = fleet.get_car(conn, 1)
    assert car.location_text == "Pune Airport"
    assert abs(car.location_lat - PUNE.lat) < 1e-6
    # /where should also free the car.
    assert car.busy_until_ts is None, car.busy_until_ts
    assert any("moved to" in s["text"] for s in n.bot.sent)
    print("ok  /where")


async def test_where_command_unknown_car():
    state, conn = fresh()
    n = TelegramNotifier(state, conn, FakeBot(), FakeSavaari())
    await n.handle_message(IncomingMessage(1, 111, 1, "/where ABC Pune"))
    assert any("No car matching" in s["text"] for s in n.bot.sent)
    print("ok  /where missing car")


async def test_where_command_usage_when_short():
    state, conn = fresh()
    n = TelegramNotifier(state, conn, FakeBot(), FakeSavaari())
    await n.handle_message(IncomingMessage(1, 111, 1, "/where"))
    assert any("Usage" in s["text"] for s in n.bot.sent)
    print("ok  /where usage")


async def test_free_command_clears_busy():
    state, conn = fresh()
    fleet.upsert_car(
        conn, label="KA-1", car_type_id="3",
        location_text="Mumbai", location_lat=MUMBAI.lat, location_lng=MUMBAI.lng,
        busy_until_ts="2099-01-01T00:00:00",
    )
    n = TelegramNotifier(state, conn, FakeBot(), FakeSavaari())
    await n.handle_message(IncomingMessage(1, 111, 1, "/free KA-1"))
    car = fleet.get_car(conn, 1)
    assert car.busy_until_ts is None
    print("ok  /free")


async def test_status_command():
    state, conn = fresh()
    n = TelegramNotifier(state, conn, FakeBot(), FakeSavaari())
    await n.handle_message(IncomingMessage(1, 111, 1, "/status"))
    assert any("Savaari Bot" in s["text"] for s in n.bot.sent)
    print("ok  /status")


# ---------- main ----------

async def main():
    test_predict_trip_end_basic()
    test_predict_trip_end_multi_day()
    test_predict_trip_end_unparseable_returns_none()
    await test_alert_stores_picked_car_and_trip_end()
    await test_confirm_relocates_picked_car()
    await test_confirm_with_no_picked_car_is_safe()
    await test_help_command()
    await test_unknown_command()
    await test_unauthorized_chat_ignored()
    await test_pause_resume_commands()
    await test_cars_empty_and_populated()
    await test_where_command_relocates_and_geocodes()
    await test_where_command_unknown_car()
    await test_where_command_usage_when_short()
    await test_free_command_clears_busy()
    await test_status_command()
    print()
    print("ALL OK")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
