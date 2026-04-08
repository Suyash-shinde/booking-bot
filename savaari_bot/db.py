"""SQLite storage for broadcasts, history and config.

The schema is intentionally narrow at Phase 0. Every poll writes:

  * One row in `broadcasts` per broadcast_id (insert-or-update).
  * One row in `broadcast_history` per broadcast per poll (the time series).

Later phases (3+) will add `fleet_cars`, `accepted_bookings`, etc. Migrations
are tracked via PRAGMA user_version so we can grow without stomping data.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 7


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # autocommit
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current >= SCHEMA_VERSION:
        return

    if current < 1:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS broadcasts (
                broadcast_id   TEXT PRIMARY KEY,
                booking_id     TEXT NOT NULL,
                first_seen_at  TEXT NOT NULL,
                last_seen_at   TEXT NOT NULL,
                vanished_at    TEXT,
                source_city    TEXT,
                dest_city      TEXT,
                car_type_id    TEXT,
                car_type       TEXT,
                trip_type_name TEXT,
                start_date     TEXT,
                start_time     TEXT,
                pick_loc       TEXT,
                drop_loc       TEXT,
                itinerary      TEXT,
                first_fare     INTEGER,
                last_fare      INTEGER,
                max_fare       INTEGER,
                taken_by_us    INTEGER NOT NULL DEFAULT 0,
                raw_json       TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_broadcasts_booking
                ON broadcasts(booking_id);
            CREATE INDEX IF NOT EXISTS idx_broadcasts_route
                ON broadcasts(source_city, dest_city);
            CREATE INDEX IF NOT EXISTS idx_broadcasts_open
                ON broadcasts(vanished_at);

            CREATE TABLE IF NOT EXISTS broadcast_history (
                broadcast_id           TEXT NOT NULL,
                observed_at            TEXT NOT NULL,
                fare                   INTEGER,
                vendor_cost            INTEGER,
                has_responded          TEXT,
                responded_vendors_count INTEGER,
                PRIMARY KEY (broadcast_id, observed_at)
            );

            CREATE INDEX IF NOT EXISTS idx_history_broadcast
                ON broadcast_history(broadcast_id);
            """
        )

    if current < 2:
        conn.executescript(
            """
            -- One row per Telegram alert we've sent. message_id lets us
            -- edit-in-place when the user taps a button or the price bumps.
            CREATE TABLE IF NOT EXISTS alerts (
                broadcast_id TEXT PRIMARY KEY,
                booking_id   TEXT NOT NULL,
                chat_id      TEXT NOT NULL,
                message_id   INTEGER NOT NULL,
                sent_at      TEXT NOT NULL,
                last_fare    INTEGER,
                status       TEXT NOT NULL DEFAULT 'pending',
                status_at    TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status);

            -- Audit trail: every postInterest call ever made by the bot.
            -- Phase 1 only writes here from the Telegram tap path; later
            -- phases (auto-confirm) will reuse the same table with a
            -- different `source`.
            CREATE TABLE IF NOT EXISTS accept_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                broadcast_id TEXT NOT NULL,
                booking_id   TEXT NOT NULL,
                attempted_at TEXT NOT NULL,
                result_ok    INTEGER NOT NULL,
                result_text  TEXT,
                source       TEXT NOT NULL,
                dry_run      INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_accept_attempted
                ON accept_log(attempted_at);
            """
        )

    if current < 3:
        conn.executescript(
            """
            -- Cache of car_type_id -> car_name from getNewBusiness payloads.
            -- Used by the Settings panel to render the per-car-type rate
            -- table without making the user remember car_type_ids.
            CREATE TABLE IF NOT EXISTS car_types (
                car_type_id TEXT PRIMARY KEY,
                car_name    TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );
            """
        )

    if current < 4:
        conn.executescript(
            """
            -- Vendor's actual fleet. Lat/lng are kept alongside a free-text
            -- location label so the user can refresh both via the dashboard.
            -- car_type_id may be NULL — in that case the car is considered
            -- eligible for any car type.
            CREATE TABLE IF NOT EXISTS fleet_cars (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                label           TEXT NOT NULL,
                car_type_id     TEXT,
                location_text   TEXT,
                location_lat    REAL,
                location_lng    REAL,
                busy_until_ts   TEXT,
                notes           TEXT,
                updated_at      TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_fleet_busy ON fleet_cars(busy_until_ts);

            -- Geocode cache: query string -> Nominatim result. Persisted so
            -- the bot doesn't re-hit Nominatim across restarts and stays
            -- friendly to their 1 req/sec policy.
            CREATE TABLE IF NOT EXISTS geocode_cache (
                query        TEXT PRIMARY KEY,
                lat          REAL,
                lng          REAL,
                display_name TEXT,
                fetched_at   TEXT NOT NULL
            );

            -- Routing cache: (from, to) rounded to 4 decimals (~10m) ->
            -- driving distance + duration from OSRM.
            CREATE TABLE IF NOT EXISTS route_cache (
                from_lat   REAL NOT NULL,
                from_lng   REAL NOT NULL,
                to_lat     REAL NOT NULL,
                to_lng     REAL NOT NULL,
                distance_m INTEGER,
                duration_s INTEGER,
                fetched_at TEXT NOT NULL,
                PRIMARY KEY (from_lat, from_lng, to_lat, to_lng)
            );
            """
        )

    if current < 5:
        conn.executescript(
            """
            -- City id -> name. Populated from getNewBusiness payloads
            -- (resultset.source_cities and resultset.dest_cities). Used by
            -- the analytics dashboard so route summaries show "Mumbai → Pune"
            -- instead of "114 → 1993".
            CREATE TABLE IF NOT EXISTS cities (
                city_id    TEXT PRIMARY KEY,
                city_name  TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )

    if current < 6:
        # Add picked_car_id and predicted_end_ts to alerts so the auto
        # position update on confirm knows which car to move and when it
        # frees up. ALTER TABLE ADD COLUMN is the simplest forward-only
        # migration that doesn't touch existing rows.
        conn.executescript(
            """
            ALTER TABLE alerts ADD COLUMN picked_car_id   INTEGER;
            ALTER TABLE alerts ADD COLUMN predicted_end_ts TEXT;
            ALTER TABLE alerts ADD COLUMN drop_loc_text   TEXT;
            """
        )

    if current < 7:
        # Sync the user's actual Savaari fleet into fleet_cars. Each row
        # gets a savaari_car_id linking it back to the upstream record so
        # we can re-sync without duplicating. Existing manually-added
        # rows have NULL savaari_car_id and are left untouched.
        #
        # savaari_drivers is a separate cache; we don't pair drivers to
        # cars yet (the FETCH_ALL_* endpoints don't expose the link).
        conn.executescript(
            """
            ALTER TABLE fleet_cars ADD COLUMN savaari_car_id TEXT;
            CREATE INDEX IF NOT EXISTS idx_fleet_savaari_id
                ON fleet_cars(savaari_car_id);

            CREATE TABLE IF NOT EXISTS savaari_drivers (
                savaari_driver_id TEXT PRIMARY KEY,
                driver_name       TEXT NOT NULL,
                driver_number     TEXT,
                dl_number         TEXT,
                dl_validity       TEXT,
                active            INTEGER NOT NULL DEFAULT 1,
                nps               TEXT,
                updated_at        TEXT NOT NULL
            );
            """
        )

    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def open_db(path: Path) -> sqlite3.Connection:
    conn = _connect(path)
    _migrate(conn)
    return conn


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _to_int(val: Any) -> int | None:
    if val is None:
        return None
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return None


def upsert_broadcast(conn: sqlite3.Connection, b: dict[str, Any], now: str) -> bool:
    """Insert a broadcast if new, or update last_seen_at otherwise.

    Returns True if this is the first time we've seen this broadcast_id.
    """
    bid = str(b.get("broadcast_id") or "")
    if not bid:
        return False

    fare = _to_int(b.get("total_amt") or b.get("gross_amount"))
    row = conn.execute(
        "SELECT broadcast_id, first_fare, max_fare FROM broadcasts WHERE broadcast_id = ?",
        (bid,),
    ).fetchone()

    if row is None:
        conn.execute(
            """
            INSERT INTO broadcasts (
                broadcast_id, booking_id, first_seen_at, last_seen_at,
                source_city, dest_city, car_type_id, car_type, trip_type_name,
                start_date, start_time, pick_loc, drop_loc, itinerary,
                first_fare, last_fare, max_fare, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                bid,
                str(b.get("booking_id") or ""),
                now,
                now,
                str(b.get("source_city") or ""),
                str(b.get("dest_city") or ""),
                str(b.get("car_type_id") or ""),
                str(b.get("car_type") or ""),
                str(b.get("trip_type_name") or ""),
                str(b.get("start_date") or ""),
                str(b.get("start_time") or ""),
                str(b.get("pick_loc") or ""),
                str(b.get("drop_loc") or ""),
                str(b.get("itinerary") or ""),
                fare,
                fare,
                fare,
                json.dumps(b, separators=(",", ":")),
            ),
        )
        return True

    new_max = max(row["max_fare"] or 0, fare or 0) or row["max_fare"]
    conn.execute(
        """
        UPDATE broadcasts
           SET last_seen_at = ?,
               last_fare    = ?,
               max_fare     = ?,
               raw_json     = ?
         WHERE broadcast_id = ?
        """,
        (now, fare, new_max, json.dumps(b, separators=(",", ":")), bid),
    )
    return False


def insert_history(conn: sqlite3.Connection, b: dict[str, Any], now: str) -> None:
    bid = str(b.get("broadcast_id") or "")
    if not bid:
        return
    responded = b.get("responded_vendor_list")
    responded_count = len(responded) if isinstance(responded, list) else 0
    conn.execute(
        """
        INSERT OR REPLACE INTO broadcast_history (
            broadcast_id, observed_at, fare, vendor_cost,
            has_responded, responded_vendors_count
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            bid,
            now,
            _to_int(b.get("total_amt") or b.get("gross_amount")),
            _to_int(b.get("vendor_cost")),
            str(b.get("has_responded") or ""),
            responded_count,
        ),
    )


def mark_vanished(
    conn: sqlite3.Connection, seen_ids: Iterable[str], now: str
) -> int:
    """Stamp `vanished_at` on any open broadcast not in the latest poll.

    Returns the number of rows updated. A vanished broadcast was either taken
    by some vendor or auto-cancelled — Phase 6 will tell them apart by
    comparing against `auto_cancel_at`.
    """
    seen = set(seen_ids)
    open_rows = conn.execute(
        "SELECT broadcast_id FROM broadcasts WHERE vanished_at IS NULL"
    ).fetchall()
    to_close = [r["broadcast_id"] for r in open_rows if r["broadcast_id"] not in seen]
    if not to_close:
        return 0
    placeholders = ",".join("?" * len(to_close))
    conn.execute(
        f"UPDATE broadcasts SET vanished_at = ? WHERE broadcast_id IN ({placeholders})",
        (now, *to_close),
    )
    return len(to_close)


def insert_alert(
    conn: sqlite3.Connection,
    *,
    broadcast_id: str,
    booking_id: str,
    chat_id: str,
    message_id: int,
    fare: int | None,
    now: str,
    picked_car_id: int | None = None,
    predicted_end_ts: str | None = None,
    drop_loc_text: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO alerts
            (broadcast_id, booking_id, chat_id, message_id, sent_at, last_fare,
             status, status_at, picked_car_id, predicted_end_ts, drop_loc_text)
        VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
        """,
        (
            broadcast_id, booking_id, chat_id, message_id, now, fare, now,
            picked_car_id, predicted_end_ts, drop_loc_text,
        ),
    )


def get_alert(conn: sqlite3.Connection, broadcast_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM alerts WHERE broadcast_id = ?", (broadcast_id,)
    ).fetchone()


def claim_alert_pending(conn: sqlite3.Connection, broadcast_id: str, now: str) -> bool:
    """Atomically transition pending -> confirming. Returns True iff we won
    the race; on False, another tap (or another device) already grabbed it.
    """
    cur = conn.execute(
        "UPDATE alerts SET status='confirming', status_at=? WHERE broadcast_id=? AND status='pending'",
        (now, broadcast_id),
    )
    return cur.rowcount == 1


def set_alert_status(
    conn: sqlite3.Connection, broadcast_id: str, status: str, now: str
) -> None:
    conn.execute(
        "UPDATE alerts SET status=?, status_at=? WHERE broadcast_id=?",
        (status, now, broadcast_id),
    )


def update_alert_fare(
    conn: sqlite3.Connection, broadcast_id: str, fare: int | None
) -> None:
    conn.execute(
        "UPDATE alerts SET last_fare=? WHERE broadcast_id=?",
        (fare, broadcast_id),
    )


def insert_accept_log(
    conn: sqlite3.Connection,
    *,
    broadcast_id: str,
    booking_id: str,
    now: str,
    result_ok: bool,
    result_text: str,
    source: str,
    dry_run: bool,
) -> None:
    conn.execute(
        """
        INSERT INTO accept_log
            (broadcast_id, booking_id, attempted_at, result_ok, result_text, source, dry_run)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            broadcast_id,
            booking_id,
            now,
            1 if result_ok else 0,
            result_text,
            source,
            1 if dry_run else 0,
        ),
    )


def upsert_car_types(
    conn: sqlite3.Connection, items: Iterable[dict[str, Any]], now: str
) -> int:
    """Cache car_type_id -> car_name mappings from a getNewBusiness payload's
    `resultset.car_types` list. Returns the count written."""
    written = 0
    for item in items or []:
        cid = str(item.get("car_type_id") or "").strip()
        cname = str(item.get("car_name") or "").strip()
        if not cid or not cname:
            continue
        conn.execute(
            """
            INSERT INTO car_types (car_type_id, car_name, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(car_type_id) DO UPDATE
                SET car_name=excluded.car_name, updated_at=excluded.updated_at
            """,
            (cid, cname, now),
        )
        written += 1
    return written


def list_car_types(conn: sqlite3.Connection) -> list[dict[str, str]]:
    return [
        {"car_type_id": r["car_type_id"], "car_name": r["car_name"]}
        for r in conn.execute(
            "SELECT car_type_id, car_name FROM car_types ORDER BY car_name"
        ).fetchall()
    ]


def upsert_cities(
    conn: sqlite3.Connection, items: Iterable[dict[str, Any]], now: str
) -> int:
    """Cache city_id -> city_name mappings from the source_cities /
    dest_cities arrays in a getNewBusiness payload. Returns count written."""
    written = 0
    for item in items or []:
        cid = str(item.get("city_id") or "").strip()
        cname = str(item.get("city_name") or "").strip()
        if not cid or not cname:
            continue
        conn.execute(
            """
            INSERT INTO cities (city_id, city_name, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(city_id) DO UPDATE
                SET city_name=excluded.city_name, updated_at=excluded.updated_at
            """,
            (cid, cname, now),
        )
        written += 1
    return written


def cities_lookup(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        r["city_id"]: r["city_name"]
        for r in conn.execute("SELECT city_id, city_name FROM cities").fetchall()
    }


def upsert_savaari_drivers(
    conn: sqlite3.Connection, items: Iterable[dict[str, Any]], now: str
) -> int:
    """Cache driver records pulled from FETCH_ALL_DRIVERS."""
    written = 0
    for d in items or []:
        did = str(d.get("id") or "").strip()
        name = str(d.get("driver_name") or "").strip()
        if not did or not name:
            continue
        conn.execute(
            """
            INSERT INTO savaari_drivers
                (savaari_driver_id, driver_name, driver_number, dl_number,
                 dl_validity, active, nps, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(savaari_driver_id) DO UPDATE SET
                driver_name=excluded.driver_name,
                driver_number=excluded.driver_number,
                dl_number=excluded.dl_number,
                dl_validity=excluded.dl_validity,
                active=excluded.active,
                nps=excluded.nps,
                updated_at=excluded.updated_at
            """,
            (
                did,
                name,
                str(d.get("driver_number") or ""),
                str(d.get("DL_number") or d.get("dl_number") or ""),
                str(d.get("Dl_validity") or d.get("dl_validity") or ""),
                int(d.get("active") or 0),
                str(d.get("nps") or ""),
                now,
            ),
        )
        written += 1
    return written


def list_savaari_drivers(
    conn: sqlite3.Connection, *, only_active: bool = True
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM savaari_drivers"
    if only_active:
        sql += " WHERE active = 1"
    sql += " ORDER BY driver_name COLLATE NOCASE"
    return [dict(r) for r in conn.execute(sql).fetchall()]


def counts_today(conn: sqlite3.Connection) -> dict[str, int]:
    """Cheap aggregates for the dashboard. UTC date boundary."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    alerts = conn.execute(
        "SELECT COUNT(*) FROM alerts WHERE substr(sent_at,1,10)=?", (today,)
    ).fetchone()[0]
    confirms = conn.execute(
        "SELECT COUNT(*) FROM accept_log WHERE result_ok=1 AND substr(attempted_at,1,10)=?",
        (today,),
    ).fetchone()[0]
    return {"alerts_today": alerts, "confirms_today": confirms}


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Wrap a batch write in BEGIN/COMMIT for speed (poller writes ~250 rows)."""
    conn.execute("BEGIN")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
