"""Orchestrator: starts FastAPI, the poller and the tray icon as one app.

Threading layout:

  main thread        -> tray icon (pystray.Icon.run blocks until quit)
  worker thread      -> asyncio loop running:
                          - uvicorn.Server.serve()    (FastAPI dashboard)
                          - Poller.run()              (background task)
                          - config-watcher tick       (restarts poller on save)

We avoid putting asyncio in the main thread because pystray on Windows
expects to own the main thread. The worker thread owns its own loop and the
tray pokes shared state via the AppState lock.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys

import uvicorn

from . import config, db, lockfile, weekly_report
from .analytics import AnalyticsCache
from .availability import AvailabilityCache
from .escalation import EscalationCache
from .geo import Geocoder, Router
from .notifier import TelegramNotifier
from .poller import Poller, PollerEvents
from .savaari import SavaariAuthError, SavaariClient
from .state import AppState
from .telegram import TelegramBot
from .tray import TrayApp, DASHBOARD_URL
from .web import make_app

log = logging.getLogger("savaari_bot.app")


def setup_logging(log_path: Path) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        return
    # The file handler is the only one we truly need at runtime; if it
    # fails for any reason we still want the rest of startup to keep
    # running rather than crashing the whole process.
    try:
        file_h = RotatingFileHandler(
            log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        file_h.setFormatter(fmt)
        root.addHandler(file_h)
    except Exception:
        pass
    # The stream handler is best-effort: when PyInstaller builds with
    # --noconsole on Windows, sys.stdout is None unless something else
    # patched it. Skip the handler entirely in that case so logging
    # never tries to write to None and crashes startup.
    if sys.stdout is not None:
        try:
            stream = logging.StreamHandler(sys.stdout)
            stream.setFormatter(fmt)
            root.addHandler(stream)
        except Exception:
            pass
    # uvicorn pumps too many access logs at INFO; tame them.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def make_events(state: AppState) -> PollerEvents:
    log_e = logging.getLogger("savaari_bot.events")

    def on_new(b):
        log_e.info(
            "NEW  %s  %s  %s -> %s  fare=%s",
            b.get("broadcast_id"),
            b.get("car_type"),
            (b.get("itinerary1") or "?").strip(),
            (b.get("itinerary2") or "?").strip(),
            b.get("total_amt"),
        )

    def on_price_up(b, old, new):
        log_e.info("BUMP %s  %s -> %s  (%s)", b.get("broadcast_id"), old, new, b.get("itinerary"))

    def on_auth(e):
        log_e.error("token rejected — open the dashboard and paste a fresh vendorToken")

    return PollerEvents(on_new_broadcast=on_new, on_price_up=on_price_up, on_auth_failure=on_auth)


class Worker:
    """Owns the asyncio loop, the FastAPI server and the (re)startable poller."""

    def __init__(self, state: AppState):
        self.state = state
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self._server: uvicorn.Server | None = None
        self._poller_task: asyncio.Task | None = None
        self._poller: Poller | None = None
        self._db_conn = None
        self._tg_bot: TelegramBot | None = None
        self._tg_task: asyncio.Task | None = None
        self._notifier: TelegramNotifier | None = None
        self._client: SavaariClient | None = None
        self._availability: AvailabilityCache | None = None
        self._geocoder: Geocoder | None = None
        self._router: Router | None = None
        self._analytics: AnalyticsCache | None = None
        self._escalation: EscalationCache | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name="savaari-worker", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._amain())
        finally:
            self.loop.close()

    async def _amain(self) -> None:
        # Open the DB once and reuse it.
        self._db_conn = db.open_db(self.state.cfg.db_path)

        # FastAPI/uvicorn server task.
        api = make_app(self.state)
        cfg = uvicorn.Config(
            api,
            host=lockfile.LOCK_HOST,
            port=lockfile.LOCK_PORT,
            log_level="warning",
            lifespan="off",
            # On Windows --noconsole builds, uvicorn's default logging
            # config tries to attach a StreamHandler to sys.stdout — which
            # is None — and crashes the server before it can bind. Pass
            # log_config=None to suppress uvicorn's own setup; our
            # setup_logging() above already routes everything to the
            # rotating file logger in %APPDATA%\SavaariBot\.
            log_config=None,
            access_log=False,
        )
        self._server = uvicorn.Server(cfg)
        server_task = asyncio.create_task(self._server.serve(), name="uvicorn")

        # Make the worker accessible to web routes before we start anything;
        # routes like /api/test-alert need it.
        self.state.worker = self  # type: ignore[attr-defined]
        # Geocoder + router live for the lifetime of the worker and don't
        # depend on Telegram or even the vendor token — they only need a
        # DB connection. Building them here makes /api/fleet (which
        # geocodes new cars on demand) work before Telegram is configured.
        self._build_geo()
        # Analytics cache hangs off the same DB connection.
        self._analytics = AnalyticsCache(
            self._db_conn,
            ttl_s=self.state.cfg.analytics_refresh_s,
            days=self.state.cfg.analytics_window_days,
        )
        # Escalation cache shares the same TTL/window as analytics —
        # they're computed from the same broadcast_history rows.
        self._escalation = EscalationCache(
            self._db_conn,
            ttl_s=self.state.cfg.analytics_refresh_s,
            days=self.state.cfg.analytics_window_days,
        )
        # If a vendor token is set but we don't yet know the vendor's
        # user_id, fetch it once. Persisted to config so future runs skip.
        await self._bootstrap_user_id_if_needed()
        # Start telegram + poller (only if creds are present).
        await self._restart_telegram_if_possible()
        await self._restart_poller_if_possible()

        # Watch for config-dirty + shutdown signals.
        watcher_task = asyncio.create_task(self._watch(), name="watcher")

        await server_task  # exits when self._server.should_exit = True
        watcher_task.cancel()
        if self._poller_task:
            self._poller.stop()
            try:
                await self._poller_task
            except Exception:
                pass
        if self._tg_task and self._tg_bot:
            self._tg_bot.stop()
            try:
                await self._tg_task
            except Exception:
                pass
        if self._db_conn:
            self._db_conn.close()

    def _build_geo(self) -> None:
        cfg = self.state.cfg
        self._geocoder = Geocoder(
            self._db_conn,
            base_url=cfg.nominatim_base,
            user_agent=cfg.nominatim_user_agent,
        )
        self._router = Router(self._db_conn, base_url=cfg.osrm_base)

    async def _watch(self) -> None:
        """Trigger restarts on config save and stop the server on shutdown."""
        while True:
            await asyncio.sleep(0.5)
            if self.state.consume_config_dirty():
                log.info("config changed — rebuilding geo + restarting telegram + poller")
                # Geo settings (UA / base URLs) may have changed; rebuild
                # before anything else so the next API call uses fresh values.
                self._build_geo()
                # Analytics + escalation window/refresh may have changed.
                if self._analytics is not None:
                    self._analytics.ttl_s = self.state.cfg.analytics_refresh_s
                    self._analytics.days = self.state.cfg.analytics_window_days
                    self._analytics.invalidate()
                if self._escalation is not None:
                    self._escalation.ttl_s = self.state.cfg.analytics_refresh_s
                    self._escalation.days = self.state.cfg.analytics_window_days
                    self._escalation.invalidate()
                await self._bootstrap_user_id_if_needed()
                await self._restart_telegram_if_possible()
                await self._restart_poller_if_possible()
            if self.state.shutdown_requested:
                log.info("shutdown signal received — stopping server")
                if self._server:
                    self._server.should_exit = True
                return

    async def _bootstrap_user_id_if_needed(self) -> None:
        cfg = self.state.cfg
        if not cfg.vendor_token or cfg.vendor_user_id:
            return
        log.info("looking up vendor user_id from vendordetails")
        client = SavaariClient(
            vendor_token=cfg.vendor_token,
            base_url=cfg.api_base,
            timeout_s=cfg.request_timeout_s,
            user_agent=cfg.user_agent,
        )
        try:
            data = await client.vendor_details()
        except SavaariAuthError as e:
            log.error("vendordetails auth failure: %s", e)
            return
        except Exception:
            log.exception("vendordetails crashed")
            return
        rs = data.get("resultset") or {}
        uid = str(rs.get("id") or "").strip()
        if not uid:
            log.warning("vendordetails returned no id — gate will stay disabled")
            return
        cfg.vendor_user_id = uid
        config.save(cfg)
        log.info("bootstrapped vendor_user_id=%s", uid)

    async def _restart_telegram_if_possible(self) -> None:
        cfg = self.state.cfg
        if not (cfg.telegram_bot_token and cfg.telegram_chat_id):
            log.info("no telegram creds — telegram bot idle")
            self._tg_bot = None
            self._notifier = None
            return
        if self._tg_task and not self._tg_task.done() and self._tg_bot:
            self._tg_bot.stop()
            try:
                await self._tg_task
            except Exception:
                pass
        self._tg_bot = TelegramBot(token=cfg.telegram_bot_token, chat_id=cfg.telegram_chat_id)
        # The notifier needs the SavaariClient to call postInterest. We
        # construct (or reuse) it here so both the notifier and poller share
        # one client.
        self._client = SavaariClient(
            vendor_token=cfg.vendor_token,
            base_url=cfg.api_base,
            timeout_s=cfg.request_timeout_s,
            user_agent=cfg.user_agent,
        )
        self._availability = AvailabilityCache(
            self._client, ttl_s=cfg.eligibility_cache_ttl_s
        )
        # Rebuild geo so any UA / base-url changes from the dashboard take
        # effect on the next alert.
        self._build_geo()
        self._notifier = TelegramNotifier(
            self.state,
            self._db_conn,
            self._tg_bot,
            self._client,
            availability=self._availability,
            geocoder=self._geocoder,
            router=self._router,
            analytics=self._analytics,
            escalation=self._escalation,
        )
        self._tg_bot.on_callback = self._notifier.handle_callback
        self._tg_bot.on_message = self._notifier.handle_message
        self._tg_task = asyncio.create_task(self._tg_bot.run_polling(), name="telegram")
        log.info("telegram bot (re)started")

    async def _restart_poller_if_possible(self) -> None:
        cfg = self.state.cfg
        if not cfg.vendor_token:
            log.info("no vendor_token yet — poller idle")
            return
        if self._poller_task and not self._poller_task.done():
            self._poller.stop()
            try:
                await self._poller_task
            except Exception:
                pass
        # Reuse the client created in _restart_telegram_if_possible if any,
        # otherwise build a fresh one.
        if self._client is None or self._client.vendor_token != cfg.vendor_token:
            self._client = SavaariClient(
                vendor_token=cfg.vendor_token,
                base_url=cfg.api_base,
                timeout_s=cfg.request_timeout_s,
                user_agent=cfg.user_agent,
            )
        self._poller = Poller(
            self._client,
            self._db_conn,
            make_events(self.state),
            cfg.poll_interval_s,
            state=self.state,
            notifier=self._notifier,
        )
        self._poller_task = asyncio.create_task(self._poller.run(), name="poller")
        log.info("poller (re)started (notifier=%s)", "on" if self._notifier else "off")

    async def sync_fleet_from_savaari(self) -> dict:
        """Pull FETCH_ALL_CARS + FETCH_ALL_DRIVERS, upsert into DB."""
        cfg = self.state.cfg
        if not cfg.vendor_user_id:
            return {"ok": False, "detail": "vendor_user_id not yet known — boot the poller once first"}
        # Build a temporary client even if Telegram isn't configured.
        client = self._client or SavaariClient(
            vendor_token=cfg.vendor_token,
            base_url=cfg.api_base,
            timeout_s=cfg.request_timeout_s,
            user_agent=cfg.user_agent,
        )
        try:
            cars_payload = await client.fetch_all_cars(vendor_id=cfg.vendor_user_id)
            drivers_payload = await client.fetch_all_drivers(vendor_id=cfg.vendor_user_id)
        except Exception as e:
            log.exception("fleet sync fetch failed")
            return {"ok": False, "detail": f"{type(e).__name__}: {e}"}

        cars = (cars_payload.get("resultset") or {}).get("cars") or []
        drivers = (drivers_payload.get("resultset") or {}).get("drivers") or []

        # Run the upserts inside a single transaction.
        with db.transaction(self._db_conn):
            from . import fleet
            stats = fleet.sync_cars_from_savaari(self._db_conn, cars)
            driver_count = db.upsert_savaari_drivers(self._db_conn, drivers, db._utcnow())
        log.info(
            "fleet sync: %s · drivers cached: %d", stats, driver_count
        )
        return {"ok": True, "cars": stats, "drivers": driver_count}

    # Build a fresh weekly report from the live DB. Returns the dataclass
    # so the route can serve text or html depending on Accept.
    def build_weekly_report(self, days: int = 7) -> "weekly_report.WeeklyReport":
        return weekly_report.build_report(self._db_conn, days=days)

    async def send_weekly_report_now(self, days: int = 7) -> dict:
        if not self._tg_bot:
            return {"ok": False, "detail": "telegram not configured"}
        rep = self.build_weekly_report(days=days)
        try:
            await self._tg_bot.send_message(rep.to_html())
        except Exception as e:
            return {"ok": False, "detail": f"send failed: {e}"}
        return {"ok": True, "lines": len(rep.to_text().splitlines())}

    # Exposed so /api/test-availability can call the gate end-to-end.
    async def test_availability(self, booking_id: str | None = None) -> dict:
        cfg = self.state.cfg
        if not cfg.vendor_user_id:
            return {"ok": False, "detail": "vendor_user_id not yet known"}
        if self._client is None:
            self._client = SavaariClient(
                vendor_token=cfg.vendor_token,
                base_url=cfg.api_base,
                timeout_s=cfg.request_timeout_s,
                user_agent=cfg.user_agent,
            )
        if booking_id is None:
            # Pick the most recently seen open broadcast as a test target.
            row = self._db_conn.execute(
                "SELECT booking_id FROM broadcasts WHERE vanished_at IS NULL "
                "ORDER BY first_seen_at DESC LIMIT 1"
            ).fetchone()
            booking_id = row["booking_id"] if row else "0"
        try:
            data = await self._client.fetch_drivers_with_cars(
                booking_id=booking_id,
                user_id=cfg.vendor_user_id,
                admin_id=cfg.vendor_user_id,
            )
        except Exception as e:
            return {"ok": False, "detail": f"{type(e).__name__}: {e}"}
        rs = data.get("resultset") or {}
        cars = rs.get("carRecordList") or []
        return {
            "ok": True,
            "booking_id": booking_id,
            "eligible_count": len(cars),
            "cars_sample": [
                {k: c.get(k) for k in ("car_number", "driver_number", "car_type")}
                for c in cars[:3]
            ],
        }

    # Exposed so /api/test-alert can fire a synthetic message.
    async def send_test_alert(self) -> str:
        if not self._notifier or not self._tg_bot:
            return "telegram not configured"
        fake = {
            "broadcast_id": f"test-{int(asyncio.get_event_loop().time())}",
            "booking_id": "00000000",
            "car_type": "Wagon R or Equivalent",
            "trip_type_name": "TEST",
            "itinerary": "Test &rarr; Test",
            "total_amt": "1234",
            "vendor_cost": "999",
            "start_date": "today",
            "start_time": "00:00",
            "pick_loc": "Test pickup",
            "drop_loc": "Test drop",
            "auto_cancel_at": "—",
            "has_responded": "NO",
        }
        await self._notifier.alert_new(fake)
        return f"sent test alert {fake['broadcast_id']}"

    def stop(self) -> None:
        if self.loop and self._server:
            self.loop.call_soon_threadsafe(setattr, self._server, "should_exit", True)


def run() -> int:
    cfg = config.load()
    setup_logging(cfg.log_path)
    log.info("data dir: %s", config.data_dir())

    sock = lockfile.acquire_or_redirect()
    if sock is None:
        return 0
    sock.close()  # release immediately so uvicorn can rebind

    state = AppState(cfg=cfg)
    worker = Worker(state)
    worker.start()

    # Auto-open the dashboard for the first run only (no token configured).
    if not cfg.vendor_token:
        try:
            webbrowser.open(DASHBOARD_URL)
        except Exception:
            pass

    tray = TrayApp(state, on_quit=worker.stop)
    try:
        tray.run()
    except KeyboardInterrupt:
        worker.stop()
    if worker.thread:
        worker.thread.join(timeout=10)
    return 0
