# Savaari Bot

A local Python service that turns the Savaari Vendor dashboard into a smart,
phone-driven booking assistant. It polls the vendor API, sends rich Telegram
alerts you can act on with one tap, computes per-booking profit (including
deadhead distance), filters by your real fleet capacity, and learns from
historical data which routes are worth chasing and when to grab vs. wait.

It runs as a single tray-icon Windows app with a built-in local web
dashboard. No terminal, no public URL, no third-party server.

---

# Part 1 — Features

## Core: notifications

- **Polls Savaari Vendor every N seconds** (default 10s) using the same
  `getNewBusiness` API the official dashboard uses. Token is grabbed once
  from `sessionStorage.vendorToken` and never expires in normal use.
- **Telegram alerts for every new broadcast**, with all the trip details:
  fare, vendor cost, route, car type, pickup time and address, drop
  address, auto-cancel time, and whether other vendors have already
  responded.
- **Price-bump alerts.** When a booking gets re-broadcast at a higher
  price, the existing message is *edited in place* instead of spamming a
  new one — you see `📈 Price up 3,985 → 4,365` on the same chat row.
- **Restart-safe deduplication.** The bot remembers every alert it has
  sent (broadcast_id keyed) so you never get the same booking twice, even
  across crashes or restarts.

## One-tap accept

- **Inline Telegram buttons.** Each alert ships with `[ ✅ Confirm ₹3,985 ]`
  and `[ ⏭ Skip ]`. Tapping Confirm fires the same `postInterest` call the
  dashboard makes.
- **Atomic race protection.** Two simultaneous taps (e.g. from two phones)
  resolve to exactly one accept via SQL `UPDATE ... WHERE status='pending'`.
- **Dry-run safety belt.** Default ON. The Confirm flow runs end-to-end
  but never actually calls Savaari, so you can rehearse the whole loop
  without taking real bookings. Every action is logged with the dry-run
  flag for an audit trail.
- **Live message edits.** After Confirm, the message updates in real time:
  `⏳ Confirming booking #X…` → `✅ Booking #X` (or `❌ Already taken`).
- **Chat-id authorisation.** Only the configured chat can drive the bot.
  Callbacks from anywhere else are rejected.

## Profit estimator

- **One-line profit math** in every alert:
  `Net ≈ ₹2,140 (fuel ₹1,184 / driver ₹650 / toll ₹0 / deadhead ₹443/39km · 296km)`
- **Three-coefficient model** per car type: fuel ₹/km, driver ₹/km, toll
  ₹/km. Tunable globally with per-car-type overrides.
- **Toll-aware**: if Savaari's `exclusions` field says "Toll", the
  customer pays directly and the bot leaves it out of the calculation.
- **Multi-day handling**: estimates km via `max(min_km_per_day × num_days,
  package_kms)`.
- **Negative-profit warning**: net below zero gets a `⚠️` prefix so you
  spot money-losers at a glance.
- **Net or gross fare floor.** Configure a minimum profit (or minimum
  gross fare) and bookings below it never reach your phone.

## Eligibility gate

- **"Can I actually serve this?" check** before alerting. Calls Savaari's
  `FETCH_DRIVERS_WITH_CARS_LIST_NPS` (the same one the dashboard uses for
  the assign-driver dropdown) and counts how many of your (driver, car)
  combinations are eligible for that specific booking.
- **Annotate-only mode** (default): every alert shows `🚗 Eligible cars: 2`
  or `🚗 No eligible cars right now` — useful even before you trust the
  gate to suppress.
- **Hard-suppression mode** (opt-in): alerts with zero eligible cars are
  silently dropped.
- **Fail-open on errors.** A 503 from Savaari never causes silent alert
  loss — the eligibility line just gets omitted.
- **Per-booking 60s cache** with concurrent-call dedup so a tick with 30
  new broadcasts only fires 30 distinct API calls, not 30×.
- **Auto-bootstrap** of the vendor's user_id from `vendordetails`. The
  user never needs to know it exists.

## Deadhead-aware best-car selection

- **Manual fleet management.** Add each of your cars in the dashboard
  with a label, car type, and free-text location. The location is
  geocoded once via Nominatim and the lat/lng is stored for routing.
- **Best-car picker.** For each booking, the bot:
  1. Filters out cars that are still busy past the pickup time
  2. Filters by car type (with fallback to "any" if no exact match)
  3. Routes from each candidate to the geocoded pickup via OSRM
  4. Picks the lowest-distance candidate
  5. Falls back to haversine if OSRM fails, or "first candidate, distance
     unknown" if geocoding fails
- **Deadhead cost subtraction.** The picked car's deadhead distance is
  multiplied by fuel + driver rates and removed from the net profit.
  Bookings whose gross fare looked fine but actually lose money once
  the deadhead is included get visibly flagged.
- **Best-car-shown in alert**: `🛣 Best car: KA-01-MX-9999 — deadhead 39 km`
- **Auto-position-update on Confirm.** When you tap Confirm, the picked
  car's location automatically becomes the booking's drop address, and
  it's marked busy until the predicted trip end. The next deadhead
  calculation knows the car is now somewhere else.
- **`/where` Telegram command** to manually relocate a car from your
  phone, plus `/free` to clear a busy lock if a trip ended early.
- **Geocode + route caches** in SQLite — Nominatim and OSRM are called
  at most once per unique address/route across the lifetime of the bot.

## Competition density tracker

- **Per-route historical aggregates.** For every (source, dest, car_type)
  bucket, the bot computes from its own broadcast history: sample count,
  average responding-vendor count, max responders, take rate, average
  first fare, average maximum fare, average escalation.
- **One-line tag in every alert**: `🟢 quiet route`, `🟡 moderate route`,
  `🔥 hot route`, with the actual numbers — `(avg 1.4 responders, take 65%, n=12)`.
- **Unknown-route fallback**: `⚪ no history yet` until the bucket fills.
- **Dashboard table** sorted by sample size showing every route the bot
  has seen in the configurable window (default 14 days).
- **Weekly digest report**, manually triggered or sent on demand to
  Telegram, with: headline counters (broadcasts seen / taken / cancelled
  / alerts fired / confirms tapped), top 5 most contested routes, top 5
  quietest routes. Each line includes the city + car names resolved from
  cached IDs (so you read "Mumbai → Pune" instead of "114 → 1993").

## Escalation curves (grab vs. wait)

- **Per-route price trajectory model.** Walks every finalized broadcast's
  history, computes the median number of distinct fare values seen,
  P10/P50/P90 of the final observed fare, and the take rate for that
  bucket.
- **In-alert hint** based on the bucket model and the current fare:
  - `⏳ WAIT` — current fare is more than 10% below the bucket's P50,
    take rate ≥ 50%, and ≥ 3 prior samples. Conservative on purpose so
    the bot doesn't tell you to skip a real opportunity.
  - `🎯 GRAB` — current fare is at or above the bucket's P50.
  - `📈 OK` — current fare is in the in-between zone.
  - `📈 no escalation history yet` — bucket has no samples.
- **Optional wait-suppression** (off by default): alerts where the model
  says WAIT are silently dropped, so you only get pinged when the price
  has actually escalated to the buy zone.
- **Dashboard table** showing every bucket's median steps, P10/P50/P90
  fares, and take rate, all in one place.

## Telegram commands (drive the bot from your phone)

- `/help` — list all commands
- `/cars` — list your fleet with current location, coords, and busy status
- `/where <label substring> <new location>` — relocate a car (geocoded)
- `/free <label>` — clear a busy lock
- `/pause` / `/resume` — stop and start polling
- `/status` — quick health line: last poll time, broadcast counts, paused/auth state
- All commands are gated on chat-id; messages from any other chat are silently ignored.

## Local web dashboard

A built-in FastAPI dashboard at `http://127.0.0.1:8765/` with cards for:

- **Status** — running/paused/auth-failed pill, started_at, last poll
  time, today's alert and confirm counts, dry-run mode indicator, error
  banner if anything's wrong.
- **First-run wizard** — paste vendorToken, optional Telegram bot token
  + chat id, poll interval, fare floor (gross or net basis), dry-run
  toggle. Runs automatically the first time you launch.
- **Profit settings** — fuel/driver/toll defaults plus a per-car-type
  override table populated automatically from the latest poll.
- **Eligibility gate** — bootstrapped vendor_user_id, annotate/suppress
  toggles, cache TTL, **Test gate** button that calls the live
  endpoint against the most recent open broadcast.
- **Fleet & deadhead** — toggle deadhead, set Nominatim User-Agent,
  add/delete cars (geocodes inline with clear pass/fail messages).
- **Analytics** — window-days input, headline counters, full route
  table sorted by samples. Plus an **Escalation curves** subsection
  with toggles and per-bucket P10/P50/P90 + median step count + take
  rate. **Preview report** + **Send to Telegram now** buttons.
- **Test alert** button to verify the Telegram pipeline end-to-end with
  a synthetic message.
- **Pause / Resume / Quit** buttons.

## Packaging & lifecycle

- **Tray-icon Windows app** via pystray. Right-click → Open Dashboard /
  Pause / Quit. No terminal window thanks to `pyinstaller --noconsole`.
- **Single binary** via PyInstaller (`SavaariBot.spec`). Build on a
  Windows machine or via the GitHub Actions workflow in
  `build_windows.md`. Linux dev still works (headless fallback when
  no `DISPLAY`).
- **Single-instance lock** via TCP port-bind on 8765. Double-clicking
  the icon when one's already running just opens its dashboard.
- **All state in `%APPDATA%\SavaariBot\`** (or `~/.local/share/savaari_bot`
  on Linux): SQLite DB, rotating log, TOML config. The .exe itself is
  portable.
- **Schema migrations** are forward-only via `PRAGMA user_version`. The
  DB is at v6.
- **Crash recovery**: every loop wraps its tick in try/except + backoff.
  A network blip never crashes the worker.
- **Auth-failure backoff**: a rejected vendorToken triggers a 60s wait
  instead of a tight retry loop, and the dashboard shows a clear "token
  rejected" pill.

---

# Part 2 — How it works

## Big-picture architecture

```
                  +----------------------------------+
                  |          SavaariBot.exe          |
                  |       (single Python proc)       |
                  +-----------------+----------------+
                                    |
        +---------------------------+----------------------------+
        |                           |                            |
   main thread                 worker thread                tray menu
   (pystray)                   (asyncio loop)              callbacks
        |                           |
        v                           v
   tray.run()         +---------------------------+
                      |        Worker._amain      |
                      |  ┌─────────────────────┐  |
                      |  │ uvicorn FastAPI :8765│ |
                      |  ├─────────────────────┤  |
                      |  │  Poller (every 10s) │  |
                      |  ├─────────────────────┤  |
                      |  │  Telegram long-poll │  |
                      |  ├─────────────────────┤  |
                      |  │  watcher (config)   │  |
                      |  └─────────────────────┘  |
                      +-------------|-------------+
                                    |
                              SQLite (single conn)
                              %APPDATA%\SavaariBot\savaari.sqlite3
```

A single OS process. The main thread owns the tray icon (because pystray
on Windows insists on the main thread). A worker thread owns its own
asyncio event loop, which in turn runs four cooperating tasks:

1. The **uvicorn server** for the dashboard.
2. The **Poller** that hits Savaari every N seconds.
3. The **Telegram bot** (long-poll `getUpdates`).
4. A **watcher** that wakes up every 0.5 s to check whether the user
   saved new config from the dashboard, and a shutdown flag.

All four tasks talk to a shared `AppState` object, guarded by a single
threading lock, so the tray and the dashboard can read/write the same
state safely.

## Module map

| Module | Lines | Purpose |
|---|---:|---|
| `app.py` | ~280 | Worker orchestrator. Owns the loop, the DB conn, and (re)starts of poller/telegram on config change. |
| `main.py` | 12 | Entry point. Calls `app.run()`. |
| `tray.py` | ~120 | pystray icon + menu + headless fallback. |
| `web.py` | ~750 | FastAPI app. Inline HTML dashboard + JSON routes for every settings card. |
| `state.py` | ~110 | `AppState` + `PollSnapshot`. Lock-guarded shared state. |
| `lockfile.py` | ~40 | TCP single-instance lock. |
| `config.py` | ~180 | TOML config loader/saver with nested `[profit]` table. |
| `db.py` | ~360 | SQLite schema + migrations + per-table CRUD helpers. |
| `savaari.py` | ~120 | Async httpx client for `getNewBusiness`, `vendordetails`, `fetch_drivers_with_cars`, `postInterest`. |
| `telegram.py` | ~200 | Async raw-httpx Bot API client. `sendMessage`, `editMessageText`, `answerCallbackQuery`, `getUpdates` long-poll. |
| `poller.py` | ~170 | The 10s loop. Diffs new broadcasts, fires events, dispatches notifier calls *after* DB commit. |
| `notifier.py` | ~520 | The brain. Format alerts, gate by eligibility, compute profit + deadhead + competition + escalation, dispatch Confirm/Skip + slash commands, auto-relocate on accept. |
| `availability.py` | ~110 | TTL+lock cache for the eligibility gate. |
| `geo.py` | ~230 | Nominatim + OSRM clients with SQLite-backed caches. |
| `fleet.py` | ~220 | Fleet CRUD + best-car-for-booking selection. |
| `profit.py` | ~120 | Profit estimator + `apply_deadhead`. |
| `analytics.py` | ~210 | Per-route stat queries + `tag_for` (competition labels) + TTL cache. |
| `weekly_report.py` | ~190 | Headline counters + contested/quiet ranking + text/HTML rendering. |
| `escalation.py` | ~250 | Per-route price-trajectory aggregates + WAIT/GRAB hint + TTL cache. |

## Database schema (v6)

```
broadcasts(
  broadcast_id PK, booking_id, first_seen_at, last_seen_at, vanished_at,
  source_city, dest_city, car_type_id, car_type, trip_type_name,
  start_date, start_time, pick_loc, drop_loc, itinerary,
  first_fare, last_fare, max_fare, taken_by_us, raw_json
)
broadcast_history(
  broadcast_id, observed_at, fare, vendor_cost,
  has_responded, responded_vendors_count,
  PRIMARY KEY (broadcast_id, observed_at)
)
alerts(
  broadcast_id PK, booking_id, chat_id, message_id, sent_at,
  last_fare, status, status_at,
  picked_car_id, predicted_end_ts, drop_loc_text     -- v6
)
accept_log(
  id, broadcast_id, booking_id, attempted_at,
  result_ok, result_text, source, dry_run
)
car_types(car_type_id PK, car_name, updated_at)
cities(city_id PK, city_name, updated_at)
fleet_cars(
  id PK, label, car_type_id, location_text, location_lat, location_lng,
  busy_until_ts, notes, updated_at
)
geocode_cache(query PK, lat, lng, display_name, fetched_at)
route_cache(from_lat, from_lng, to_lat, to_lng PK, distance_m, duration_s, fetched_at)
```

Two tables drive everything else: **broadcasts** holds one row per
broadcast id with summary stats, and **broadcast_history** holds one row
per (broadcast_id, observation timestamp) for the time series. Every
analytics feature reduces to a SQL query over these two.

## Polling loop

`Poller.run()` is a `while not stop` loop that calls `_tick` every
`poll_interval_s` seconds. Each tick:

1. **Fetch** `getNewBusiness` via `SavaariClient`.
2. **Snapshot** the current `last_fare` for every still-open broadcast
   (used for price-bump detection later).
3. **Open a SQLite transaction** so the entire batch of upserts commits
   atomically — important because the next poll mustn't start until
   we've stamped `vanished_at` on the things that disappeared.
4. For each broadcast in the payload:
   - `upsert_broadcast` — insert if new, update `last_seen_at` and
     fare-tracking columns otherwise. Returns `is_new`.
   - `insert_history` — write one history row capturing the current
     fare and `responded_vendors_count`.
   - If `is_new`, queue an `alert_new` notifier call. If not new, compare
     fare against the snapshot and queue an `alert_price_up` if it went up.
5. **Mark vanished** any open broadcast that wasn't in this payload.
6. **Cache `car_types` and cities** from the same payload (free metadata
   that comes with every poll).
7. **Commit** the transaction.
8. **Now dispatch** the queued notifier calls — *after* commit so the
   SQLite write lock is never held across an outbound HTTP call.

If anything raises, the loop logs the exception, records it in
`AppState.last_error`, backs off (15 s for transport, 60 s for auth
failures), and continues. The poller never crashes.

## Notifier path: from broadcast to phone

When the poller queues `notifier.alert_new(b)`, this happens (in order):

1. **Profit estimate** via `profit.estimate(b, cfg)` — gross fare, fuel,
   driver, toll, net.
2. **Best-car selection** via `fleet.best_car_for(b, geocoder, router)`
   if `enable_deadhead` is on. Geocodes the pickup, routes from each
   eligible candidate, returns the closest. This may also lazily geocode
   the car's `location_text` if no coords are stored yet.
3. **Apply deadhead** to the profit estimate if the picker returned a
   distance.
4. **Net fare-floor check** against the (possibly deadhead-adjusted)
   profit. If below floor, return without alerting.
5. **Escalation hint** lookup. If `suppress_below_p50` is on and the
   model says WAIT, return without alerting.
6. **Eligibility gate** call. If `require_eligible_car` is on and the
   API returned a clean zero, return.
7. **Dedup**: take the `_inflight` lock, check if there's already an
   `alerts` row for this broadcast_id. If yes, return (restart-safe).
8. **Competition tag** lookup from analytics cache.
9. **Render** the HTML alert text via `_format_alert(...)` with all the
   tags assembled.
10. **Send** to Telegram via `bot.send_message`. Capture the returned
    `message_id`.
11. **Persist** an `alerts` row including the picked car id, predicted
    trip end, and drop location text — needed later for auto-relocate.

The whole thing is async; multiple new broadcasts in one tick fan out
through `await self.notifier.alert_new(b)` calls in series so the
in-process `_inflight` lock and the eligibility cache see consistent
state.

## Confirm path: from tap to relocation

When the user taps `[ ✅ Confirm ]` in Telegram:

1. Telegram delivers a `callback_query` to `getUpdates` on the next
   long-poll cycle.
2. `TelegramBot._parse_cbq` packages it into a `CallbackQuery` dataclass.
3. `notifier.handle_callback` is called and dispatches by data prefix:
   `c:<broadcast_id>` → `_on_confirm`.
4. **Authorise**: chat_id must match `cfg.telegram_chat_id`.
5. **Fetch** the alert row from SQLite to recover booking_id, chat_id,
   message_id, picked_car_id, predicted_end_ts, drop_loc_text.
6. **Atomic claim**: `UPDATE alerts SET status='confirming' WHERE
   broadcast_id=? AND status='pending'`. If `rowcount == 0`, another tap
   already won; reply "already handled" and stop.
7. **Acknowledge** the callback so the spinner stops on the user's phone.
8. **Edit message** to `⏳ Confirming booking #X…` for instant feedback.
9. **Call `postInterest`** unless `dry_run_accept` is on. The dry-run
   path logs the call but doesn't actually hit Savaari — the user's
   experience is otherwise identical.
10. **Insert into `accept_log`** with result_ok, result_text, source,
    and dry_run flag for the audit trail.
11. **Update alert status** to `confirmed` or `failed`.
12. **Edit message** again to the final state: `✅ Booking #X` or
    `❌ Already taken / window closed`.
13. **Auto-relocate the picked car** (Phase 4.5):
    - If `picked_car_id` is set, fetch the car from `fleet_cars`.
    - Geocode the booking's drop location via Nominatim.
    - Update the car's location and stamp `busy_until_ts =
      predicted_end_ts`.
    - The next deadhead calculation now knows the car is in Goa, busy
      until tomorrow morning.

The whole sequence is wrapped in try/except. If anything fails after the
status claim, the alert is marked `failed` and a follow-up message
explains why.

## Skip path

`[ ⏭ Skip ]` tap → `notifier._on_skip` → SQL `UPDATE alerts SET
status='skipped'` → edit message to `⏭ Skipped`. No API call to Savaari.
The skip is remembered, so a re-broadcast of the same broadcast_id won't
re-alert (a re-broadcast at a higher price *will* — that's a price-up
alert, handled separately).

## Slash-command path

`getUpdates` also returns plain `message` objects when the user types
something. `TelegramBot._parse_msg` packages them into `IncomingMessage`,
`notifier.handle_message` dispatches by command name, and each handler
either reads from SQLite (`/cars`, `/status`), writes to SQLite (`/where`,
`/free`), or twiddles `AppState.paused` (`/pause`, `/resume`). Every
command goes through the same chat-id authorisation as callbacks.

## Geocoding & routing

Two thin httpx clients with SQLite-backed caches:

- **Geocoder** (Nominatim). Single global asyncio lock so concurrent
  callers serialise. Tracks `_last_call` and sleeps to enforce
  ≥1 sec between calls (Nominatim's stated limit). Sets a real
  User-Agent (configurable in the Settings panel; the default is
  rejected by Nominatim's WAF if it contains "test"). Caches both hits
  and misses so no query is ever retried.
- **Router** (OSRM). Cache key is `(from_lat, from_lng, to_lat, to_lng)`
  rounded to 4 decimals (~10 m), so two pickups at the same building
  reuse the route. Returns `Route(distance_m, duration_s)`.

Both fail-soft: a None return propagates through `fleet.best_car_for`
and the user sees `🛣 Best car: ... (distance unknown)` instead of an
exception. Across the lifetime of the bot, each unique address is
geocoded once and each unique (car, pickup) pair is routed once.

## Best-car selection

`fleet.best_car_for(booking, geocoder, router)`:

1. List all cars; abort if empty.
2. Filter out cars whose `busy_until_ts` is past the booking's pickup time.
3. Filter to cars matching the booking's `car_type_id`. If none match,
   fall back to the unfiltered free list (some cars may have NULL
   `car_type_id`, meaning "any type").
4. Geocode the pickup address.
5. For each candidate, ensure it has lat/lng (lazy-geocode on demand
   from `location_text` and persist back).
6. Compute haversine distance to the pickup as a coarse tier-1
   fallback.
7. Try OSRM route from each candidate. The candidate with the smallest
   routed distance wins.
8. If routing failed for all, return the candidate with the smallest
   haversine.
9. If geocoding the pickup failed entirely, return the first candidate
   with `distance_km=None` so the alert can still mention which car
   would go (just without deadhead math).

## Profit math

`profit.estimate(b, cfg)`:

```
estimated_km   = max(min_km_per_day * num_days, package_kms)
fuel_rate      = cfg.fuel_rate_per_car_type.get(car_id, cfg.fuel_rate_default)
driver_rate    = cfg.driver_rate_per_car_type.get(car_id, cfg.driver_rate_default)
fuel_cost      = round(estimated_km * fuel_rate)
driver_cost    = round(estimated_km * driver_rate)
toll_cost      = 0 if "toll" in exclusions.lower() else round(estimated_km * cfg.toll_per_km)
earned         = vendor_cost + (night_charge if nightcharge_status else 0)
net            = earned - fuel_cost - driver_cost - toll_cost
```

`profit.apply_deadhead(p, cfg, deadhead_km, car_id)` returns a copy
of `p` with `deadhead_cost = round(deadhead_km * (fuel_rate +
driver_rate))` subtracted from `net` and the deadhead fields populated.
Toll is not charged on deadhead — vendors typically don't pay tolls
heading back empty.

## Analytics queries

`analytics.query_route_stats(conn, days, min_samples)` is a single SQL
that:

1. Computes per-broadcast `max_resp` from `broadcast_history`.
2. Joins to `broadcasts` to get the `(source_city, dest_city, car_type_id)`
   key and the `vanished_at` flag.
3. Classifies each broadcast as `taken` (max_resp > 0), `cancelled`
   (vanished, max_resp == 0), or `open`.
4. Groups by route key, returns sample count, avg responders, take rate,
   avg first/max fare, avg escalation.

`analytics.tag_for(stat)` maps avg responders to `🟢 quiet` (<1.0),
`🟡 moderate` (1.0–3.0), or `🔥 hot` (≥3.0). Thresholds are gentle
because most Savaari routes show very low responder counts.

`AnalyticsCache` wraps it with a TTL (default 5 min) so the dashboard
and the per-alert tag lookup share one query.

## Escalation curves

`escalation.query_escalation_stats(conn, days, min_samples)`:

1. Pulls every history row inside the window joined to its broadcast.
2. Groups in Python into per-broadcast trajectories: list of fares
   observed plus `max_resp`.
3. Skips broadcasts where `vanished_at IS NULL` (still open).
4. Buckets by `(source, dest, car_type)`.
5. Per bucket, computes:
   - `samples` = number of finished trajectories
   - `median_steps` = median count of distinct fare values per trajectory
   - `p10/p50/p90_final` = linear-interpolated percentiles of the final
     observed fare
   - `take_rate` = fraction of trajectories where any responder appeared

`escalation.hint_for(stat, current_fare)` then maps the bucket + the
booking's current fare to one of:

- `wait` — current fare < p50 × 0.90 AND take_rate ≥ 0.5 AND samples ≥ 3
- `grab` — current fare ≥ p50
- `neutral` — in between
- `unknown` — no stat or zero samples

The wait threshold is intentionally conservative (10% below p50 + ≥50%
take rate + ≥3 samples) so the bot doesn't tell you to skip a real
opportunity. The optional `suppress_below_p50` flag actually drops the
alert; default mode just annotates.

## Weekly report

`weekly_report.build_report(conn, days, top_n)`:

1. Compute headline counters from `broadcasts`, `alerts`, `accept_log`
   inside the window.
2. Pull route stats from `analytics.query_route_stats` with `min_samples=3`.
3. Sort routes by `-avg_responders` for the contested list, by
   `(avg_responders, -samples)` for the quiet list.
4. Resolve `source_city` / `dest_city` IDs to names via `cities` table.
5. Resolve `car_type_id` to names via `car_types` table.
6. Render via `to_text()` (dashboard preview) or `to_html()` (Telegram).

The result has three sections: headline numbers, top contested routes
(skip these unless you're fast), top quiet routes (target these). Empty
sections are hidden so a fresh DB doesn't show fake "0 responders"
contested rows.

## Config & state

`Config` is a single dataclass holding everything: tokens, intervals,
fare floor, profit coefficients, fleet+geo settings, analytics window,
escalation settings. Persisted to `config.toml` in the data dir using
hand-rolled TOML serialization (no external dependency). Nested
`[profit]` and `[profit.fuel_rate_per_car_type]` tables for the
per-car-type override dicts.

Loading is `config.load()` → constructs default `Config()` → applies
TOML overrides → applies env-var overrides (`SAVAARI_VENDOR_TOKEN`,
`SAVAARI_POLL_INTERVAL`, etc.) for development convenience. Type
coercion based on the default field type so an int field stays an int.

`AppState` is the runtime state — current `Config`, last poll
snapshot, paused/auth-failed flags, shutdown signal, "config dirty"
trigger. Lock-guarded so the watcher coroutine, the FastAPI handlers,
and the tray menu can read/write safely.

When the user saves config from the dashboard:
1. The route handler updates `state.cfg` in place.
2. Calls `config.save(cfg)` to persist.
3. Calls `state.mark_config_dirty()`.
4. The watcher coroutine sees the dirty flag on its next 0.5 s tick and
   calls `_build_geo()`, `_bootstrap_user_id_if_needed()`,
   `_restart_telegram_if_possible()`, `_restart_poller_if_possible()`
   so the new settings take effect immediately without a process
   restart.

## Lifecycle of a single launch

1. `python -m savaari_bot.main` (or `SavaariBot.exe`) → `app.run()`.
2. Load config from `%APPDATA%\SavaariBot\config.toml`. Set up logging.
3. Acquire the TCP lock on `127.0.0.1:8765`. If another instance has it,
   open the existing dashboard in the browser and exit.
4. Build the `AppState`. Spawn the worker thread. The thread starts its
   own asyncio loop and calls `Worker._amain`.
5. `_amain` opens the SQLite DB (with WAL mode for concurrent reads),
   migrates schema, builds the geocoder/router, builds the analytics +
   escalation caches.
6. If `vendor_token` is set but `vendor_user_id` is empty, call
   `vendordetails` once to bootstrap it. Persist back to TOML.
7. If Telegram creds are set, start the Telegram task and the Notifier.
   Otherwise leave them idle.
8. If `vendor_token` is set, start the Poller task. Otherwise leave it
   idle (the dashboard will say "first-run setup").
9. Start the watcher task.
10. Start the FastAPI/uvicorn task. (This is the one we await.)
11. Back in the main thread, if `vendor_token` is empty, open the
    dashboard URL in the default browser so the user sees the wizard.
12. Run the tray icon (or headless wait if no DISPLAY). Block here.
13. When the user clicks Quit (or `/api/quit`), tray sets
    `shutdown_requested`. The watcher sees it and tells uvicorn to
    `should_exit`. The server task ends, the poller and Telegram tasks
    are stopped, the DB is closed, the worker thread exits. The main
    thread joins the worker for up to 10 s and returns 0.

That's the whole loop: one process, one DB connection, one shared state
object, four cooperating coroutines. Every feature in Part 1 hangs off
this scaffolding.
