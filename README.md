# savaari_bot — Phase 0

Local Python service that polls the Savaari Vendor `getNewBusiness` API and
stores every broadcast in SQLite. No notifications, no Telegram, no UI yet —
that's Phase 1+. The point of Phase 0 is to get a clean, durable history
flowing so every later feature can hang off real data.

## Install

```bash
cd savaari_bot
python -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Configure

The bot reads `config.toml` from its per-user data directory:

* Linux/macOS: `~/.local/share/savaari_bot/config.toml`
* Windows: `%APPDATA%\SavaariBot\config.toml`

On first run with no token set, the bot creates a stub config and exits.
Open the file and paste your `vendorToken`:

```toml
vendor_token = "RTYzQytUWFQ0Z3J0aXZpdXl..."
poll_interval_s = 10.0
telegram_bot_token = ""
telegram_chat_id = ""
fare_floor = 0
paused = false
```

To grab the token: log into <https://vendor.savaari.com>, open DevTools
console, and run `sessionStorage.vendorToken`. Copy the string between the
quotes. The token is long-lived (verified to work without `PHPSESSID`), so
this is a paste-once setup.

You can also set `SAVAARI_VENDOR_TOKEN=...` in the environment to override.

## Run

```bash
python -m savaari_bot.main
```

You should see lines like:

```
2026-04-08 12:00:00 INFO  savaari_bot.poller: tick: 238 broadcasts (238 new, 0 price-up, 0 vanished)
2026-04-08 12:00:10 INFO  savaari_bot.poller: tick: 240 broadcasts (3 new, 1 price-up, 1 vanished)
```

The first tick treats every broadcast as new (seeding the DB). Subsequent
ticks only emit `NEW` and `BUMP` events for actual changes.

Stop with Ctrl-C.

## Where data lives

* `savaari.sqlite3` — broadcasts + per-poll history
* `savaari.log` — rotating log (2 MB × 3)
* `config.toml` — config

All under the data dir above. Back up the .sqlite3 file occasionally; the
history is what later phases (escalation modelling, positioning advice) are
built on.

## Schema

```
broadcasts(
  broadcast_id PK, booking_id, first_seen_at, last_seen_at, vanished_at,
  source_city, dest_city, car_type_id, car_type, trip_type_name,
  start_date, start_time, pick_loc, drop_loc, itinerary,
  first_fare, last_fare, max_fare, taken_by_us, raw_json
)

broadcast_history(
  broadcast_id, observed_at, fare, vendor_cost, has_responded,
  responded_vendors_count
)
```

`vanished_at` is stamped by the poller when a broadcast disappears from a
later poll — that's the signal it was either taken by some vendor or
auto-cancelled. Phase 6 will tell those apart by comparing against
`auto_cancel_at`.
