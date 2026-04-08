"""Tests for the profit estimator + net-vs-gross fare floor."""

from __future__ import annotations

import asyncio
import sqlite3
import sys

from savaari_bot import config, db
from savaari_bot.notifier import TelegramNotifier
from savaari_bot.profit import estimate
from savaari_bot.savaari import SavaariClient
from savaari_bot.state import AppState
from savaari_bot.telegram import TelegramBot


# ---------- profit.estimate() ----------

def test_profit_basic():
    cfg = config.Config(
        fuel_rate_default=8.0,
        driver_pct_default=25.0,
    )
    b = {
        "car_type_id": "3",
        "vendor_cost": "3187",
        "package_kms": "296",
        "min_km_per_day": "250",
        "num_days": "1",
        "nightcharge_status": "0",
        "night_charge": "308",
    }
    p = estimate(b, cfg)
    assert p.estimated_km == 296, p
    assert p.fuel_cost == 296 * 8, p
    # 25% of vendor_cost (night charge ignored, status=0)
    assert p.driver_cost == round(3187 * 0.25), p
    assert p.driver_pct == 25.0, p
    assert p.earned == 3187, p
    assert p.net == 3187 - p.fuel_cost - p.driver_cost, p
    print("ok  profit basic")


def test_profit_per_car_override():
    cfg = config.Config(
        fuel_rate_default=8.0,
        driver_pct_default=25.0,
        fuel_rate_per_car_type={"7": 11.0},
        driver_pct_per_car_type={"7": 30.0},
    )
    b = {
        "car_type_id": "7",
        "vendor_cost": "5000",
        "package_kms": "200",
    }
    p = estimate(b, cfg)
    assert p.fuel_cost == 200 * 11, p
    # 30% of 5000 = 1500
    assert p.driver_cost == 1500, p
    assert p.driver_pct == 30.0
    print("ok  profit override")


def test_profit_multi_day():
    cfg = config.Config(fuel_rate_default=8.0, driver_pct_default=25.0)
    b = {
        "vendor_cost": "10000",
        "min_km_per_day": "250",
        "num_days": "3",
        "package_kms": "0",  # not set on multi-day in some payloads
    }
    p = estimate(b, cfg)
    assert p.estimated_km == 750, p
    assert p.driver_cost == 2500
    print("ok  profit multi day")


def test_profit_night_charge_added_to_driver_base():
    cfg = config.Config(fuel_rate_default=0, driver_pct_default=20.0)
    b = {
        "vendor_cost": "1000",
        "night_charge": "500",
        "nightcharge_status": "1",
    }
    p = estimate(b, cfg)
    # earned = 1500, driver = 20% of 1500 = 300
    assert p.earned == 1500, p
    assert p.driver_cost == 300, p
    print("ok  profit night charge added")


def test_profit_short_format():
    cfg = config.Config(fuel_rate_default=8.0, driver_pct_default=25.0)
    b = {"vendor_cost": "5000", "package_kms": "200"}
    p = estimate(b, cfg)
    s = p.short()
    assert "Net ≈ ₹" in s
    assert "200km" in s
    assert "fuel" in s and "driver" in s
    assert "25%" in s, s   # short() now shows the percentage
    print("ok  profit short()")


# ---------- net fare floor ----------

class FakeBot(TelegramBot):
    def __init__(self):
        super().__init__(token="x", chat_id="111")
        self.sent = []

    async def send_message(self, text, *, buttons=None, chat_id=None):
        self.sent.append({"text": text, "buttons": buttons})
        return {"message_id": len(self.sent), "chat": {"id": 111}}

    async def edit_message_text(self, *a, **k):
        pass

    async def answer_callback_query(self, *a, **k):
        pass


async def test_net_floor_blocks_low_net():
    cfg = config.Config(
        telegram_chat_id="111",
        fare_floor=2000,
        fare_floor_basis="net",
        fuel_rate_default=10.0,
        driver_pct_default=30.0,
    )
    state = AppState(cfg=cfg)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db._migrate(conn)
    n = TelegramNotifier(state, conn, FakeBot(), SavaariClient(vendor_token="x"))
    b = {
        "broadcast_id": "1",
        "booking_id": "1",
        # net = 3000 - 200*10 - 30%*3000 = 3000 - 2000 - 900 = 100 < 2000
        "vendor_cost": "3000",
        "package_kms": "200",
    }
    await n.alert_new(b)
    assert n.bot.sent == [], "low-net booking should be filtered"
    print("ok  net floor blocks low net")


async def test_net_floor_allows_high_net():
    cfg = config.Config(
        telegram_chat_id="111",
        fare_floor=2000,
        fare_floor_basis="net",
        fuel_rate_default=5.0,
        driver_pct_default=20.0,
    )
    state = AppState(cfg=cfg)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db._migrate(conn)
    n = TelegramNotifier(state, conn, FakeBot(), SavaariClient(vendor_token="x"))
    b = {
        "broadcast_id": "1",
        "booking_id": "1",
        # net = 5000 - 200*5 - 20%*5000 = 5000 - 1000 - 1000 = 3000 > 2000
        "vendor_cost": "5000",
        "package_kms": "200",
    }
    await n.alert_new(b)
    assert len(n.bot.sent) == 1
    text = n.bot.sent[0]["text"]
    assert "Net ≈" in text
    print("ok  net floor allows high net")


async def test_gross_floor_still_works():
    cfg = config.Config(
        telegram_chat_id="111",
        fare_floor=4000,
        fare_floor_basis="gross",
    )
    state = AppState(cfg=cfg)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db._migrate(conn)
    n = TelegramNotifier(state, conn, FakeBot(), SavaariClient(vendor_token="x"))
    b = {
        "broadcast_id": "1",
        "booking_id": "1",
        "vendor_cost": "10000",  # would pass net floor easily
        "total_amt": "3500",      # but gross < 4000
        "package_kms": "100",
    }
    await n.alert_new(b)
    assert n.bot.sent == [], "gross floor should reject"
    print("ok  gross floor still works")


# ---------- car_types cache ----------

def test_car_types_upsert_and_list():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db._migrate(conn)
    written = db.upsert_car_types(
        conn,
        [
            {"car_type_id": "3", "car_name": "Wagon R or Equivalent"},
            {"car_type_id": "7", "car_name": "Ertiga or Equivalent"},
            {"car_type_id": "", "car_name": "ignored"},
        ],
        db._utcnow(),
    )
    assert written == 2
    items = db.list_car_types(conn)
    assert {i["car_type_id"] for i in items} == {"3", "7"}
    # Re-upsert with a different name to verify update.
    db.upsert_car_types(conn, [{"car_type_id": "3", "car_name": "Wagon R updated"}], db._utcnow())
    items = db.list_car_types(conn)
    name = next(i["car_name"] for i in items if i["car_type_id"] == "3")
    assert name == "Wagon R updated"
    print("ok  car_types upsert + list")


# ---------- config save/load round-trip ----------

def test_config_round_trip(tmp_dir):
    """Round-trip a Config through save() and tomllib.load(), bypassing the
    real data dir by monkey-patching `data_dir` for the duration of the test.
    """
    import tomllib
    orig_data_dir = config.data_dir
    config.data_dir = lambda: tmp_dir  # type: ignore[assignment]
    try:
        cfg = config.Config(
            vendor_token="abc",
            fuel_rate_default=9.5,
            driver_pct_default=27.5,
            fuel_rate_per_car_type={"3": 7.0, "7": 11.0},
            driver_pct_per_car_type={"3": 22.0, "7": 30.0},
            fare_floor=1500,
            fare_floor_basis="net",
        )
        config.save(cfg)
        target = tmp_dir / "config.toml"
        text = target.read_text()
        assert '[profit]' in text and '[profit.fuel_rate_per_car_type]' in text, text
        assert '[profit.driver_pct_per_car_type]' in text, text
        assert 'toll_per_km' not in text, "toll_per_km should be removed entirely"

        with target.open("rb") as f:
            raw = tomllib.load(f)
        cfg2 = config.Config()
        config._apply_dict(cfg2, raw)
        assert cfg2.vendor_token == "abc"
        assert cfg2.fuel_rate_default == 9.5
        assert cfg2.driver_pct_default == 27.5
        assert cfg2.fuel_rate_per_car_type == {"3": 7.0, "7": 11.0}
        assert cfg2.driver_pct_per_car_type == {"3": 22.0, "7": 30.0}
        assert cfg2.fare_floor_basis == "net"
        assert cfg2.fare_floor == 1500
        print("ok  config round trip")
    finally:
        config.data_dir = orig_data_dir  # type: ignore[assignment]


async def main():
    test_profit_basic()
    test_profit_per_car_override()
    test_profit_multi_day()
    test_profit_night_charge_added_to_driver_base()
    test_profit_short_format()
    await test_net_floor_blocks_low_net()
    await test_net_floor_allows_high_net()
    await test_gross_floor_still_works()
    test_car_types_upsert_and_list()
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        test_config_round_trip(Path(td))
    print()
    print("ALL OK")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
