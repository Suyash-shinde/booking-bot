"""Fleet car management + best-car selection.

A "car" here is the user's own (driver, vehicle) combination — the smallest
thing that can be dispatched to a Savaari booking. The user enters them
manually in the dashboard. Each row holds the current location (text +
optional lat/lng) and an optional `busy_until_ts` so the picker knows when
each car frees up.

Best-car logic:

  1. Drop cars that are still busy past the booking's pickup time.
  2. Filter to cars whose `car_type_id` matches the booking's `car_type_id`.
     If none match, fall back to all available cars (the user might keep
     car_type_id NULL).
  3. For each candidate, route from car position -> pickup. The candidate
     with the smallest distance wins. If routing fails for everyone, return
     the candidate with the smallest haversine distance as a coarse
     fallback.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from .geo import Geocoder, Router

log = logging.getLogger("savaari_bot.fleet")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- dataclasses ----------

@dataclass
class FleetCar:
    id: int
    label: str
    car_type_id: Optional[str]
    location_text: str
    location_lat: Optional[float]
    location_lng: Optional[float]
    busy_until_ts: Optional[str]
    notes: str
    updated_at: str
    savaari_car_id: Optional[str] = None  # NULL = manually added in dashboard

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class CarPick:
    car: FleetCar
    distance_km: Optional[float]
    estimated: bool  # True if distance came from haversine fallback


# ---------- CRUD ----------

def _row_to_car(r: sqlite3.Row) -> FleetCar:
    keys = r.keys() if hasattr(r, "keys") else []
    return FleetCar(
        id=int(r["id"]),
        label=r["label"],
        car_type_id=r["car_type_id"],
        location_text=r["location_text"] or "",
        location_lat=(float(r["location_lat"]) if r["location_lat"] is not None else None),
        location_lng=(float(r["location_lng"]) if r["location_lng"] is not None else None),
        busy_until_ts=r["busy_until_ts"],
        notes=r["notes"] or "",
        updated_at=r["updated_at"],
        savaari_car_id=(r["savaari_car_id"] if "savaari_car_id" in keys else None),
    )


def list_cars(conn: sqlite3.Connection) -> list[FleetCar]:
    rows = conn.execute(
        "SELECT * FROM fleet_cars ORDER BY label COLLATE NOCASE"
    ).fetchall()
    return [_row_to_car(r) for r in rows]


def get_car(conn: sqlite3.Connection, car_id: int) -> Optional[FleetCar]:
    r = conn.execute("SELECT * FROM fleet_cars WHERE id=?", (car_id,)).fetchone()
    return _row_to_car(r) if r else None


def upsert_car(
    conn: sqlite3.Connection,
    *,
    id: Optional[int] = None,
    label: str,
    car_type_id: Optional[str] = None,
    location_text: str = "",
    location_lat: Optional[float] = None,
    location_lng: Optional[float] = None,
    busy_until_ts: Optional[str] = None,
    notes: str = "",
    savaari_car_id: Optional[str] = None,
) -> int:
    now = _now()
    if id is None:
        cur = conn.execute(
            """
            INSERT INTO fleet_cars
                (label, car_type_id, location_text, location_lat, location_lng,
                 busy_until_ts, notes, updated_at, savaari_car_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (label, car_type_id, location_text, location_lat, location_lng,
             busy_until_ts, notes, now, savaari_car_id),
        )
        return int(cur.lastrowid)
    conn.execute(
        """
        UPDATE fleet_cars
           SET label=?, car_type_id=?, location_text=?, location_lat=?, location_lng=?,
               busy_until_ts=?, notes=?, updated_at=?, savaari_car_id=COALESCE(?, savaari_car_id)
         WHERE id=?
        """,
        (label, car_type_id, location_text, location_lat, location_lng,
         busy_until_ts, notes, now, savaari_car_id, id),
    )
    return int(id)


def get_car_by_savaari_id(conn: sqlite3.Connection, savaari_id: str) -> Optional[FleetCar]:
    r = conn.execute(
        "SELECT * FROM fleet_cars WHERE savaari_car_id = ?", (str(savaari_id),)
    ).fetchone()
    return _row_to_car(r) if r else None


def sync_cars_from_savaari(
    conn: sqlite3.Connection, items: list[dict[str, Any]]
) -> dict[str, int]:
    """Upsert cars pulled from FETCH_ALL_CARS into fleet_cars.

    Behaviour:
      - Match existing rows by `savaari_car_id`. If a row exists, update
        the label and car_type_id but PRESERVE the user's location +
        busy_until_ts (those are the only things they own locally).
      - For brand-new Savaari cars, insert with empty location — the
        user fills it in later.
      - Skip rows where `active != "1"` (deactivated cars in Savaari).
      - Manually-added rows (savaari_car_id IS NULL) are NEVER touched.
      - This function NEVER deletes rows. If a car was removed from
        Savaari, the local row stays and a future cleanup pass can
        flag it.

    Returns a small stats dict for the dashboard.
    """
    inserted = 0
    updated = 0
    skipped_inactive = 0
    skipped_no_id = 0
    for c in items or []:
        sid = str(c.get("id") or "").strip()
        if not sid:
            skipped_no_id += 1
            continue
        if str(c.get("active") or "").strip() not in ("1", "true", "True"):
            skipped_inactive += 1
            continue

        brand = str(c.get("car_brand") or "").strip()
        number = str(c.get("car_number") or "").strip()
        car_type_id = str(c.get("car_type_id") or "").strip() or None
        # Build a human-friendly label from what's available.
        if brand and number:
            label = f"{brand} ({number})"
        elif number:
            label = number
        elif brand:
            label = f"{brand} #{sid}"
        else:
            label = f"Savaari car #{sid}"

        existing = get_car_by_savaari_id(conn, sid)
        if existing is None:
            upsert_car(
                conn,
                label=label,
                car_type_id=car_type_id,
                location_text="",  # user fills this in
                savaari_car_id=sid,
                notes=f"synced from savaari id={sid}",
            )
            inserted += 1
        else:
            # Update the things that come from Savaari, leave location
            # and busy_until alone.
            upsert_car(
                conn,
                id=existing.id,
                label=label,
                car_type_id=car_type_id,
                location_text=existing.location_text,
                location_lat=existing.location_lat,
                location_lng=existing.location_lng,
                busy_until_ts=existing.busy_until_ts,
                notes=existing.notes,
                savaari_car_id=sid,
            )
            updated += 1
    return {
        "inserted": inserted,
        "updated": updated,
        "skipped_inactive": skipped_inactive,
        "skipped_no_id": skipped_no_id,
    }


def delete_car(conn: sqlite3.Connection, car_id: int) -> bool:
    cur = conn.execute("DELETE FROM fleet_cars WHERE id=?", (car_id,))
    return cur.rowcount > 0


# ---------- best-car selection ----------

def _is_free(car: FleetCar, pickup_iso: Optional[str]) -> bool:
    if not car.busy_until_ts:
        return True
    if not pickup_iso:
        # Without a pickup timestamp we err on the side of "still busy"
        # for any car whose `busy_until_ts` is in the future.
        return car.busy_until_ts <= _now()
    return car.busy_until_ts <= pickup_iso


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _filter_candidates(cars: list[FleetCar], car_type_id: str, pickup_iso: Optional[str]) -> list[FleetCar]:
    free = [c for c in cars if _is_free(c, pickup_iso)]
    if not free:
        return []
    if not car_type_id:
        return free
    typed = [c for c in free if (c.car_type_id or "") == car_type_id]
    return typed if typed else free


async def best_car_for(
    conn: sqlite3.Connection,
    *,
    booking: dict[str, Any],
    geocoder: Geocoder,
    router: Router,
) -> Optional[CarPick]:
    """Pick the closest free car of the right type for `booking`.

    Returns None if there are no candidates at all. If we can't geocode
    pickup or route to anyone, returns the candidate with the smallest
    haversine fallback distance (or any candidate if no car has coords).
    """
    cars = list_cars(conn)
    if not cars:
        return None
    car_type_id = str(booking.get("car_type_id") or "")
    pickup_iso = booking.get("pickup_time") or booking.get("auto_cancel_at") or None
    candidates = _filter_candidates(cars, car_type_id, pickup_iso)
    if not candidates:
        return None

    # Geocode pickup once.
    pick_text = (booking.get("pick_loc") or "").strip()
    pick_geo = await geocoder.geocode(pick_text) if pick_text else None
    if pick_geo is None:
        # No pickup coords -> can't compute deadhead. Return the first
        # candidate so the alert at least mentions which car would go.
        return CarPick(car=candidates[0], distance_km=None, estimated=False)

    # Ensure each candidate has lat/lng (geocode their location_text on
    # demand and persist back into fleet_cars).
    for c in candidates:
        if c.location_lat is not None and c.location_lng is not None:
            continue
        if not c.location_text:
            continue
        cgeo = await geocoder.geocode(c.location_text)
        if cgeo is None:
            continue
        c.location_lat, c.location_lng = cgeo.lat, cgeo.lng
        upsert_car(
            conn,
            id=c.id,
            label=c.label,
            car_type_id=c.car_type_id,
            location_text=c.location_text,
            location_lat=cgeo.lat,
            location_lng=cgeo.lng,
            busy_until_ts=c.busy_until_ts,
            notes=c.notes,
        )

    # Route from each candidate that has coords. Track the best by routed
    # distance, then by haversine fallback if routing failed everywhere.
    best_routed: Optional[CarPick] = None
    best_haver: Optional[CarPick] = None

    for c in candidates:
        if c.location_lat is None or c.location_lng is None:
            continue
        haver = _haversine_km(c.location_lat, c.location_lng, pick_geo.lat, pick_geo.lng)
        cand_haver = CarPick(car=c, distance_km=haver, estimated=True)
        if best_haver is None or haver < (best_haver.distance_km or 1e9):
            best_haver = cand_haver

        route = await router.route(c.location_lat, c.location_lng, pick_geo.lat, pick_geo.lng)
        if route is None:
            continue
        cand_routed = CarPick(car=c, distance_km=route.distance_km, estimated=False)
        if best_routed is None or route.distance_km < (best_routed.distance_km or 1e9):
            best_routed = cand_routed

    if best_routed is not None:
        return best_routed
    if best_haver is not None:
        return best_haver
    # No coords on any candidate.
    return CarPick(car=candidates[0], distance_km=None, estimated=False)
