"""Tests for Phase 4: fleet, geocoding, routing, deadhead-aware alerts.

All tests use fakes for Nominatim/OSRM/Telegram so they run offline.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from typing import Any

from savaari_bot import config, db, fleet
from savaari_bot.geo import GeocodeResult, Geocoder, Route, Router
from savaari_bot.notifier import TelegramNotifier
from savaari_bot.profit import apply_deadhead, estimate
from savaari_bot.savaari import SavaariClient
from savaari_bot.state import AppState
from savaari_bot.telegram import TelegramBot


# ---------- fakes ----------

class FakeGeocoder(Geocoder):
    """Bypass network entirely; return programmable answers per query."""

    def __init__(self, conn, answers: dict[str, GeocodeResult | None] | None = None):
        super().__init__(conn, base_url="x", user_agent="x", min_interval_s=0)
        self.answers = answers or {}
        self.calls: list[str] = []

    async def geocode(self, query: str):
        self.calls.append(query)
        if query in self.answers:
            ans = self.answers[query]
            if ans is not None:
                # Persist into the cache so the next call hits it.
                self.conn.execute(
                    "INSERT OR REPLACE INTO geocode_cache (query, lat, lng, display_name, fetched_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (query, ans.lat, ans.lng, ans.display_name, "2026-01-01"),
                )
            return ans
        return None


class FakeRouter(Router):
    def __init__(self, conn, factor_km: float = 1.0):
        """Returns a deterministic distance proportional to haversine
        between the two points (factor_km * haversine_km)."""
        super().__init__(conn, base_url="x")
        self.factor_km = factor_km
        self.calls: list[tuple[float, float, float, float]] = []

    async def route(self, from_lat, from_lng, to_lat, to_lng):
        self.calls.append((from_lat, from_lng, to_lat, to_lng))
        from savaari_bot.fleet import _haversine_km
        d_km = _haversine_km(from_lat, from_lng, to_lat, to_lng) * self.factor_km
        return Route(distance_m=int(d_km * 1000), duration_s=int(d_km * 60))


class FakeBot(TelegramBot):
    def __init__(self):
        super().__init__(token="x", chat_id="111")
        self.sent = []

    async def send_message(self, text, *, buttons=None, chat_id=None):
        self.sent.append({"text": text, "buttons": buttons})
        return {"message_id": len(self.sent), "chat": {"id": 111}}

    async def edit_message_text(self, *a, **k): pass
    async def answer_callback_query(self, *a, **k): pass


def fresh():
    cfg = config.Config(
        telegram_chat_id="111",
        enable_deadhead=True,
        fuel_rate_default=8.0,
        driver_pct_default=25.0,
    )
    state = AppState(cfg=cfg)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db._migrate(conn)
    return state, conn


# Real-world ish coordinates we can reason about.
PUNE = GeocodeResult(lat=18.5204, lng=73.8567, display_name="Pune")
MUMBAI = GeocodeResult(lat=19.0760, lng=72.8777, display_name="Mumbai")
KOLHAPUR = GeocodeResult(lat=16.7050, lng=74.2433, display_name="Kolhapur")


# ---------- profit.apply_deadhead ----------

def test_apply_deadhead_subtracts_only_fuel():
    """Driver pay is a percentage of the booking value, NOT of km driven —
    so an extra empty drive only adds fuel cost, never driver cost."""
    cfg = config.Config(fuel_rate_default=8.0, driver_pct_default=25.0)
    b = {"vendor_cost": "5000", "package_kms": "200"}
    p = estimate(b, cfg)
    base_net = p.net
    p2 = apply_deadhead(p, cfg, 50.0)
    # 50 km * ₹8/km = 400; driver_cost stays the same.
    assert p2.deadhead_km == 50.0
    assert p2.deadhead_cost == 400, p2
    assert p2.driver_cost == p.driver_cost, "driver pay must not change on deadhead"
    assert p2.net == base_net - 400, (base_net, p2.net)
    s = p2.short()
    assert "deadhead ₹400/50km" in s, s
    print("ok  apply_deadhead fuel-only")


def test_apply_deadhead_uses_per_car_fuel_override():
    cfg = config.Config(
        fuel_rate_default=8.0,
        driver_pct_default=25.0,
        fuel_rate_per_car_type={"7": 11.0},
        driver_pct_per_car_type={"7": 30.0},
    )
    b = {"car_type_id": "7", "vendor_cost": "5000", "package_kms": "200"}
    p = estimate(b, cfg)
    p2 = apply_deadhead(p, cfg, 30.0, car_id="7")
    # 30 km * ₹11/km = 330. Driver pay still 30% of 5000 = 1500, untouched.
    assert p2.deadhead_cost == 330, p2
    assert p2.driver_cost == 1500
    print("ok  apply_deadhead per-car")


# ---------- fleet CRUD ----------

def test_fleet_crud_round_trip():
    _, conn = fresh()
    cid = fleet.upsert_car(
        conn, label="KA-01", car_type_id="3",
        location_text="Pune", location_lat=18.52, location_lng=73.85,
    )
    cars = fleet.list_cars(conn)
    assert len(cars) == 1 and cars[0].id == cid
    fleet.upsert_car(
        conn, id=cid, label="KA-01-X", car_type_id="3",
        location_text="Pune Airport", location_lat=18.58, location_lng=73.92,
    )
    c = fleet.get_car(conn, cid)
    assert c.label == "KA-01-X" and c.location_text == "Pune Airport"
    assert fleet.delete_car(conn, cid) is True
    assert fleet.list_cars(conn) == []
    print("ok  fleet crud")


# ---------- best_car_for ----------

async def test_best_car_picks_closest_routed():
    _, conn = fresh()
    fleet.upsert_car(conn, label="A-far",  car_type_id="3",
                     location_text="Kolhapur", location_lat=KOLHAPUR.lat, location_lng=KOLHAPUR.lng)
    fleet.upsert_car(conn, label="B-near", car_type_id="3",
                     location_text="Mumbai",   location_lat=MUMBAI.lat,   location_lng=MUMBAI.lng)
    fleet.upsert_car(conn, label="C-wrong-type", car_type_id="7",
                     location_text="Mumbai",   location_lat=MUMBAI.lat,   location_lng=MUMBAI.lng)

    geo = FakeGeocoder(conn, {"Pune Airport": PUNE})
    router = FakeRouter(conn)

    pick = await fleet.best_car_for(
        conn,
        booking={"car_type_id": "3", "pick_loc": "Pune Airport"},
        geocoder=geo,
        router=router,
    )
    assert pick is not None
    assert pick.car.label == "B-near", pick.car.label
    assert pick.distance_km is not None
    assert pick.estimated is False
    print("ok  best car closest")


async def test_best_car_falls_back_to_any_when_no_type_match():
    _, conn = fresh()
    fleet.upsert_car(conn, label="WagonR", car_type_id="3",
                     location_text="Pune", location_lat=PUNE.lat, location_lng=PUNE.lng)
    geo = FakeGeocoder(conn, {"Mumbai Airport": MUMBAI})
    router = FakeRouter(conn)
    pick = await fleet.best_car_for(
        conn,
        booking={"car_type_id": "47", "pick_loc": "Mumbai Airport"},  # no type-3 expected
        geocoder=geo,
        router=router,
    )
    assert pick is not None
    assert pick.car.label == "WagonR"
    print("ok  best car type fallback")


async def test_best_car_returns_none_when_fleet_empty():
    _, conn = fresh()
    pick = await fleet.best_car_for(
        conn, booking={"car_type_id": "3", "pick_loc": "Pune"},
        geocoder=FakeGeocoder(conn), router=FakeRouter(conn),
    )
    assert pick is None
    print("ok  best car empty fleet")


async def test_best_car_geocodes_location_text_on_demand():
    _, conn = fresh()
    fleet.upsert_car(conn, label="X", car_type_id="3", location_text="Mumbai")
    geo = FakeGeocoder(conn, {"Mumbai": MUMBAI, "Pune": PUNE})
    router = FakeRouter(conn)
    pick = await fleet.best_car_for(
        conn, booking={"car_type_id": "3", "pick_loc": "Pune"},
        geocoder=geo, router=router,
    )
    assert pick.car.label == "X"
    # The car should have been updated with coords.
    c = fleet.get_car(conn, pick.car.id)
    assert c.location_lat is not None and c.location_lng is not None
    print("ok  best car geocode on demand")


async def test_best_car_busy_until_filters():
    _, conn = fresh()
    fleet.upsert_car(conn, label="busy", car_type_id="3",
                     location_text="Pune", location_lat=PUNE.lat, location_lng=PUNE.lng,
                     busy_until_ts="2099-01-01T00:00:00+00:00")
    fleet.upsert_car(conn, label="free", car_type_id="3",
                     location_text="Mumbai", location_lat=MUMBAI.lat, location_lng=MUMBAI.lng)
    geo = FakeGeocoder(conn, {"Pune Airport": PUNE})
    router = FakeRouter(conn)
    pick = await fleet.best_car_for(
        conn,
        booking={"car_type_id": "3", "pick_loc": "Pune Airport",
                 "pickup_time": "2026-04-08T07:00:00+00:00"},
        geocoder=geo, router=router,
    )
    # 'busy' is in Pune (closest!) but locked out — should pick 'free' from Mumbai.
    assert pick.car.label == "free"
    print("ok  best car busy filter")


# ---------- end-to-end through notifier ----------

async def test_notifier_includes_deadhead_in_alert():
    state, conn = fresh()
    fleet.upsert_car(conn, label="KA-1", car_type_id="3",
                     location_text="Mumbai", location_lat=MUMBAI.lat, location_lng=MUMBAI.lng)
    geo = FakeGeocoder(conn, {"Pune Airport": PUNE})
    router = FakeRouter(conn)
    sav = SavaariClient(vendor_token="x")
    n = TelegramNotifier(state, conn, FakeBot(), sav, geocoder=geo, router=router)
    b = {
        "broadcast_id": "1",
        "booking_id": "1",
        "car_type_id": "3",
        "vendor_cost": "5000",
        "package_kms": "200",
        "pick_loc": "Pune Airport",
        "exclusions": "Toll",
    }
    await n.alert_new(b)
    assert len(n.bot.sent) == 1
    text = n.bot.sent[0]["text"]
    assert "Best car: <b>KA-1</b>" in text, text
    assert "deadhead" in text, text
    assert "Net ≈" in text
    print("ok  notifier deadhead")


async def test_deadhead_can_push_below_floor():
    state, conn = fresh()
    state.cfg.fare_floor = 1500
    state.cfg.fare_floor_basis = "net"
    fleet.upsert_car(conn, label="KA-1", car_type_id="3",
                     location_text="Kolhapur", location_lat=KOLHAPUR.lat, location_lng=KOLHAPUR.lng)
    geo = FakeGeocoder(conn, {"Pune Airport": PUNE})
    # Force a *huge* deadhead so the alert goes below the floor.
    router = FakeRouter(conn, factor_km=10.0)
    sav = SavaariClient(vendor_token="x")
    n = TelegramNotifier(state, conn, FakeBot(), sav, geocoder=geo, router=router)
    b = {
        "broadcast_id": "1",
        "booking_id": "1",
        "car_type_id": "3",
        "vendor_cost": "5000",
        "package_kms": "200",
        "pick_loc": "Pune Airport",
        "exclusions": "Toll",
    }
    await n.alert_new(b)
    # Without deadhead, base net = 5000 - 200*8 - 25%*5000 = 2150 > 1500 → would alert.
    # Kolhapur→Pune ~233 km haversine * 10x routing factor → ~18,640 fuel cost.
    # Net plummets well below the floor, alert is suppressed.
    assert n.bot.sent == [], n.bot.sent
    print("ok  deadhead pushes below floor")


async def test_disabled_deadhead_skips_geocoder():
    state, conn = fresh()
    state.cfg.enable_deadhead = False
    fleet.upsert_car(conn, label="KA-1", car_type_id="3", location_text="Mumbai",
                     location_lat=MUMBAI.lat, location_lng=MUMBAI.lng)
    geo = FakeGeocoder(conn, {"Pune Airport": PUNE})
    router = FakeRouter(conn)
    sav = SavaariClient(vendor_token="x")
    n = TelegramNotifier(state, conn, FakeBot(), sav, geocoder=geo, router=router)
    b = {
        "broadcast_id": "1",
        "booking_id": "1",
        "car_type_id": "3",
        "vendor_cost": "5000",
        "package_kms": "200",
        "pick_loc": "Pune Airport",
        "exclusions": "Toll",
    }
    await n.alert_new(b)
    assert len(n.bot.sent) == 1
    text = n.bot.sent[0]["text"]
    assert "deadhead" not in text and "Best car" not in text
    assert geo.calls == [], geo.calls
    print("ok  disabled skip")


async def main():
    test_apply_deadhead_subtracts_only_fuel()
    test_apply_deadhead_uses_per_car_fuel_override()
    test_fleet_crud_round_trip()
    await test_best_car_picks_closest_routed()
    await test_best_car_falls_back_to_any_when_no_type_match()
    await test_best_car_returns_none_when_fleet_empty()
    await test_best_car_geocodes_location_text_on_demand()
    await test_best_car_busy_until_filters()
    await test_notifier_includes_deadhead_in_alert()
    await test_deadhead_can_push_below_floor()
    await test_disabled_deadhead_skips_geocoder()
    print()
    print("ALL OK")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
