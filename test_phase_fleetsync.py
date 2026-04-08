"""Tests for the Savaari fleet sync (FETCH_ALL_CARS / FETCH_ALL_DRIVERS)."""

from __future__ import annotations

import sqlite3
import sys

from savaari_bot import db, fleet


def fresh_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db._migrate(conn)
    return conn


# Sample shaped to match what the real Savaari endpoint returns.
SAMPLE_CARS = [
    {
        "id": "236136",
        "car_brand": "Innova Crysta",
        "car_number": "MH14MH7436",
        "active": "1",
        "car_type_id": "47",
        "car_make": "2025",
        "ownership": "Outsourced",
    },
    {
        "id": "179436",
        "car_brand": "Ertiga",
        "car_number": "MH14JL2471",
        "active": "1",
        "car_type_id": "7",
    },
    {
        "id": "999999",
        "car_brand": "Old Wagon R",
        "car_number": "DE-AC-TI-VA",
        "active": "0",   # should be skipped
        "car_type_id": "3",
    },
    {
        # Missing id — should be skipped.
        "car_brand": "Ghost",
        "car_number": "GH0ST",
        "active": "1",
    },
]

SAMPLE_DRIVERS = [
    {
        "id": "101764",
        "driver_name": "Suresh Jadhav",
        "driver_number": "8446231965",
        "DL_number": "MH14XYZ123",
        "active": "1",
        "nps": "85",
    },
    {
        "id": "101765",
        "driver_name": "Ramesh Patil",
        "driver_number": "9876543210",
        "active": "1",
    },
]


# ---------- sync_cars_from_savaari ----------

def test_sync_inserts_new_cars():
    conn = fresh_db()
    stats = fleet.sync_cars_from_savaari(conn, SAMPLE_CARS)
    assert stats["inserted"] == 2, stats
    assert stats["updated"] == 0
    assert stats["skipped_inactive"] == 1
    assert stats["skipped_no_id"] == 1
    cars = fleet.list_cars(conn)
    labels = sorted(c.label for c in cars)
    assert labels == ["Ertiga (MH14JL2471)", "Innova Crysta (MH14MH7436)"], labels
    # car_type_id should be carried through.
    by_label = {c.label: c for c in cars}
    assert by_label["Innova Crysta (MH14MH7436)"].car_type_id == "47"
    assert by_label["Ertiga (MH14JL2471)"].car_type_id == "7"
    # Both should have a savaari_car_id link.
    assert by_label["Innova Crysta (MH14MH7436)"].savaari_car_id == "236136"
    print("ok  sync inserts new")


def test_sync_updates_existing_preserves_location():
    conn = fresh_db()
    fleet.sync_cars_from_savaari(conn, SAMPLE_CARS)
    # Pretend the user added a location to one of the synced cars.
    car = fleet.get_car_by_savaari_id(conn, "236136")
    fleet.upsert_car(
        conn, id=car.id, label=car.label, car_type_id=car.car_type_id,
        location_text="Pune Airport", location_lat=18.58, location_lng=73.92,
        busy_until_ts="2099-01-01T00:00:00", notes=car.notes,
        savaari_car_id="236136",
    )
    # Now Savaari renames the car (e.g. registration changed).
    new_payload = [{
        "id": "236136",
        "car_brand": "Innova Crysta",
        "car_number": "MH14NEW9999",
        "active": "1",
        "car_type_id": "47",
    }]
    stats = fleet.sync_cars_from_savaari(conn, new_payload)
    assert stats["updated"] == 1 and stats["inserted"] == 0
    car2 = fleet.get_car_by_savaari_id(conn, "236136")
    # Label should reflect the new number.
    assert car2.label == "Innova Crysta (MH14NEW9999)"
    # But location + busy_until were preserved.
    assert car2.location_text == "Pune Airport"
    assert abs(car2.location_lat - 18.58) < 1e-6
    assert car2.busy_until_ts == "2099-01-01T00:00:00"
    print("ok  sync update preserves location")


def test_sync_does_not_touch_manually_added_cars():
    conn = fresh_db()
    # User manually adds a car (no savaari_car_id).
    fleet.upsert_car(
        conn, label="My Personal Car", car_type_id="3",
        location_text="Mumbai", location_lat=19.0, location_lng=72.8,
    )
    # Sync the savaari payload.
    fleet.sync_cars_from_savaari(conn, SAMPLE_CARS)
    cars = fleet.list_cars(conn)
    # Should have manual + 2 synced = 3 total.
    assert len(cars) == 3
    by_savaari_id = {c.savaari_car_id: c for c in cars}
    assert None in by_savaari_id  # the manual one
    manual = by_savaari_id[None]
    assert manual.label == "My Personal Car"
    assert manual.location_text == "Mumbai"
    print("ok  sync leaves manual cars alone")


def test_sync_idempotent_on_repeated_calls():
    conn = fresh_db()
    fleet.sync_cars_from_savaari(conn, SAMPLE_CARS)
    fleet.sync_cars_from_savaari(conn, SAMPLE_CARS)
    fleet.sync_cars_from_savaari(conn, SAMPLE_CARS)
    cars = fleet.list_cars(conn)
    assert len(cars) == 2
    print("ok  sync idempotent")


def test_sync_handles_brand_only_or_number_only_labels():
    conn = fresh_db()
    fleet.sync_cars_from_savaari(conn, [
        {"id": "1", "car_brand": "Wagon R", "car_number": "", "active": "1"},
        {"id": "2", "car_brand": "", "car_number": "MH99XX1234", "active": "1"},
        {"id": "3", "car_brand": "", "car_number": "", "active": "1"},
    ])
    cars = fleet.list_cars(conn)
    labels = sorted(c.label for c in cars)
    assert labels == ["MH99XX1234", "Savaari car #3", "Wagon R #1"], labels
    print("ok  sync label fallback")


# ---------- driver upsert ----------

def test_upsert_drivers_basic():
    conn = fresh_db()
    n = db.upsert_savaari_drivers(conn, SAMPLE_DRIVERS, db._utcnow())
    assert n == 2
    drivers = db.list_savaari_drivers(conn)
    assert len(drivers) == 2
    by_name = {d["driver_name"]: d for d in drivers}
    assert by_name["Suresh Jadhav"]["driver_number"] == "8446231965"
    assert by_name["Suresh Jadhav"]["dl_number"] == "MH14XYZ123"
    print("ok  upsert drivers")


def test_upsert_drivers_idempotent_and_filters_inactive():
    conn = fresh_db()
    db.upsert_savaari_drivers(conn, SAMPLE_DRIVERS + [{
        "id": "999",
        "driver_name": "Inactive Guy",
        "active": "0",
    }], db._utcnow())
    db.upsert_savaari_drivers(conn, SAMPLE_DRIVERS, db._utcnow())  # second call
    active_only = db.list_savaari_drivers(conn, only_active=True)
    all_drivers = db.list_savaari_drivers(conn, only_active=False)
    assert len(active_only) == 2
    assert len(all_drivers) == 3
    print("ok  upsert drivers idempotent + active filter")


def main():
    test_sync_inserts_new_cars()
    test_sync_updates_existing_preserves_location()
    test_sync_does_not_touch_manually_added_cars()
    test_sync_idempotent_on_repeated_calls()
    test_sync_handles_brand_only_or_number_only_labels()
    test_upsert_drivers_basic()
    test_upsert_drivers_idempotent_and_filters_inactive()
    print()
    print("ALL OK")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
