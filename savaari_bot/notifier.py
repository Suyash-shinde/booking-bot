"""Bridges the poller and the Telegram bot.

Contains:

  - alert formatting (HTML, escape-safe)
  - filter logic (fare floor, dedup against the alerts table)
  - callback dispatcher (confirm / skip)
  - the actual postInterest invocation, gated on cfg.dry_run_accept
"""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from . import db, fleet
from .analytics import AnalyticsCache, CompetitionTag, tag_for
from .availability import AvailabilityCache, Eligibility
from .escalation import EscalationCache, EscalationHint, hint_for
from .fleet import CarPick
from .geo import Geocoder, Router
from .profit import ProfitEstimate, apply_deadhead, estimate as estimate_profit
from .savaari import SavaariClient
from .state import AppState
from .telegram import CallbackQuery, IncomingMessage, TelegramBot

log = logging.getLogger("savaari_bot.notifier")

# Inline keyboard callback_data is capped at 64 bytes by Telegram. broadcast
# IDs from Savaari are 8-digit numbers, so "c:<id>" / "s:<id>" fit easily.
PREFIX_CONFIRM = "c:"
PREFIX_SKIP = "s:"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _predict_trip_end(b: dict[str, Any]) -> str | None:
    """Best-effort estimate of when the assigned car will be free again.

    Inputs from a Savaari broadcast row:
      pickup_time:  "2026-04-08 06:30:00"  (server-local, no tz)
      num_days:     "1" / "3" etc.
      package_kms:  "296"
      min_km_per_day:"250"

    Strategy: pickup + max(num_days * 24h, package_kms / 50 km/h). Falls
    back to None if pickup_time can't be parsed. The returned timestamp is
    a naive ISO string in the same shape Savaari uses, which is what
    fleet_cars.busy_until_ts compares against.
    """
    raw = b.get("pickup_time") or b.get("start_date_format")
    if not raw:
        return None
    try:
        # Savaari uses "YYYY-MM-DD HH:MM:SS"; tolerate "T" too.
        s = str(raw).replace("T", " ")
        dt = datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S")
    except Exception:
        try:
            dt = datetime.strptime(str(raw).strip(), "%Y-%m-%d")
        except Exception:
            return None
    try:
        num_days = max(int(float(b.get("num_days") or 1)), 1)
    except (TypeError, ValueError):
        num_days = 1
    try:
        package_kms = float(b.get("package_kms") or 0)
    except (TypeError, ValueError):
        package_kms = 0.0
    by_days = num_days * 24
    by_km = package_kms / 50.0  # rough average speed including breaks
    hours = max(by_days, by_km, 1.0)
    end = dt + timedelta(hours=hours)
    return end.isoformat(timespec="seconds")


def _esc(v: Any) -> str:
    return html.escape(str(v or "").strip())


def _fare(b: dict[str, Any]) -> int | None:
    """Gross fare (what the customer paid Savaari). Used for the price-up
    detection and historical analytics — NOT for what the user sees."""
    return db._to_int(b.get("total_amt") or b.get("gross_amount"))


def _vendor_cost(b: dict[str, Any]) -> int | None:
    """The amount Savaari pays the vendor. This is the only number that
    matters for fleet-owner decision-making, so it's the headline."""
    return db._to_int(b.get("vendor_cost"))


def _format_alert(
    b: dict[str, Any],
    *,
    title: str = "New Booking Alert! 🚖",
    # Phase 2-6 annotation params accepted but ignored. The features
    # still RUN — profit drives the fare floor, deadhead drives best-car
    # tracking, eligibility/escalation drive optional suppression — they
    # just don't get rendered into the Telegram body. Keeping the
    # parameters here means call sites don't break.
    profit: ProfitEstimate | None = None,
    eligibility: Eligibility | None = None,
    car_pick: CarPick | None = None,
    competition: CompetitionTag | None = None,
    escalation: EscalationHint | None = None,
) -> str:
    """Plain notifier-style message that mirrors the original Chrome
    extension's format."""
    booking_id = _esc(b.get("booking_id"))
    car = _esc(b.get("car_type"))
    trip_type = _esc(b.get("trip_type_name"))
    num_days = db._to_int(b.get("num_days")) or 0
    package_kms = db._to_int(b.get("package_kms")) or 0
    if num_days or package_kms:
        bits = []
        if num_days:
            bits.append(f"{num_days} day{'s' if num_days != 1 else ''}")
        if package_kms:
            bits.append(f"{package_kms} km")
        trip_type = f"{trip_type} ({', '.join(bits)})" if trip_type else ", ".join(bits)
    itin = _esc(b.get("itinerary")).replace("&amp;rarr;", "→").replace("&rarr;", "→")
    start_date = _esc(b.get("start_date"))
    start_time = _esc(b.get("start_time"))
    when = f"{start_date} at {start_time}".strip()
    pickup = _esc(b.get("pick_loc"))
    drop = _esc(b.get("drop_loc"))

    fare = _vendor_cost(b)
    if fare is None:
        fare = _fare(b)
    fare_str = f"₹{fare:,}" if fare is not None else "—"

    exclusions = _esc(b.get("exclusions"))

    lines = [
        f"<b>{title}</b>",
        f"Booking ID: {booking_id}",
        f"Trip Type: {trip_type}",
        f"Car: {car}",
        f"Itinerary: {itin}",
        f"Start: {when}",
        f"Pickup: {pickup}",
        f"Drop: {drop}",
        f"Fare: {fare_str}",
    ]
    if exclusions:
        lines.append(f"Not Included: {exclusions}")
    return "\n".join(lines)


class TelegramNotifier:
    """The thing the poller calls. One instance per process."""

    def __init__(
        self,
        state: AppState,
        conn,
        bot: TelegramBot,
        client: SavaariClient,
        availability: AvailabilityCache | None = None,
        geocoder: Geocoder | None = None,
        router: Router | None = None,
        analytics: AnalyticsCache | None = None,
        escalation: EscalationCache | None = None,
    ):
        self.state = state
        self.conn = conn
        self.bot = bot
        self.client = client
        self.availability = availability
        self.geocoder = geocoder
        self.router = router
        self.analytics = analytics
        self.escalation = escalation
        # In-process dedup so concurrent ticks (and concurrent button taps)
        # don't double-send / double-accept on the same broadcast.
        self._inflight: set[str] = set()
        self._lock = asyncio.Lock()

    # ---------- new broadcast ----------

    async def alert_new(self, b: dict[str, Any]) -> None:
        bid = str(b.get("broadcast_id") or "")
        if not bid:
            return
        fare = _fare(b)
        profit = estimate_profit(b, self.state.cfg)
        # Deadhead is consulted *before* the fare floor so that the floor
        # check uses the deadhead-adjusted net. We pick the best car here
        # if both fleet and geo are configured; otherwise car_pick=None.
        car_pick = await self._maybe_pick_best_car(b)
        if car_pick is not None and car_pick.distance_km is not None:
            profit = apply_deadhead(
                profit, self.state.cfg, car_pick.distance_km,
                car_id=str(b.get("car_type_id") or ""),
            )
        if self._below_floor(fare, profit):
            return  # below floor — don't ping

        # Phase 6: escalation hint. Computed *after* deadhead so the
        # advice uses the current displayed fare. May suppress the alert
        # entirely if the user opted into wait-mode.
        escalation = self._maybe_escalation_hint(b, fare)
        if (
            self.state.cfg.suppress_below_p50
            and escalation is not None
            and escalation.advice == "wait"
        ):
            log.info("suppressed broadcast %s — escalation says wait", bid)
            return

        # Driver/car availability gate. Only consult if either:
        #   - the user wants alerts gated on real eligibility, or
        #   - they want the count annotated even when un-gated.
        eligibility = await self._maybe_check_eligibility(b)
        cfg = self.state.cfg
        if cfg.require_eligible_car and eligibility is not None:
            # If we got a clean answer of zero, suppress. If the call errored,
            # fail OPEN — never silently lose an alert because of a transport
            # blip.
            if eligibility.known and eligibility.eligible_count == 0:
                log.info("suppressed broadcast %s — no eligible cars", bid)
                return

        async with self._lock:
            if bid in self._inflight:
                return
            existing = db.get_alert(self.conn, bid)
            if existing:
                return  # already alerted (restart-safe)
            self._inflight.add(bid)

        try:
            competition = self._maybe_competition_tag(b)
            text = _format_alert(
                b,
                title="New Booking Alert! 🚖",
                profit=profit,
                eligibility=eligibility,
                car_pick=car_pick,
                competition=competition,
                escalation=escalation,
            )
            # Notifier-only mode: no inline keyboard. The bot is plain
            # send-and-forget; the user reads the message and acts in
            # the Savaari dashboard.
            try:
                msg = await self.bot.send_message(text)
            except Exception:
                log.exception("send_message failed for broadcast %s", bid)
                return
            # Persist Phase 4.5 fields so handle_callback's auto-position
            # update knows which car to move and where to.
            picked_car_id = car_pick.car.id if car_pick is not None else None
            predicted_end_ts = _predict_trip_end(b)
            drop_loc_text = (b.get("drop_loc") or "").strip() or None
            db.insert_alert(
                self.conn,
                broadcast_id=bid,
                booking_id=str(b.get("booking_id") or ""),
                chat_id=str(self.state.cfg.telegram_chat_id),
                message_id=int(msg["message_id"]),
                fare=fare,
                now=_now(),
                picked_car_id=picked_car_id,
                predicted_end_ts=predicted_end_ts,
                drop_loc_text=drop_loc_text,
            )
        finally:
            self._inflight.discard(bid)

    async def _auto_relocate_picked_car(self, alert_row) -> None:
        """Move the picked car to drop_loc and mark it busy."""
        car_id = alert_row["picked_car_id"]
        drop = alert_row["drop_loc_text"]
        end_ts = alert_row["predicted_end_ts"]
        car = fleet.get_car(self.conn, int(car_id))
        if car is None:
            log.warning("auto-relocate: picked car %s not found", car_id)
            return
        # If we have a drop location, geocode it. Otherwise leave the
        # car's location as-is and only update busy_until_ts.
        new_lat, new_lng, new_text = car.location_lat, car.location_lng, car.location_text
        if drop and self.geocoder is not None:
            try:
                g = await self.geocoder.geocode(drop)
                if g is not None:
                    new_lat, new_lng = g.lat, g.lng
                    new_text = drop
            except Exception:
                log.exception("auto-relocate geocode failed")
        fleet.upsert_car(
            self.conn,
            id=car.id,
            label=car.label,
            car_type_id=car.car_type_id,
            location_text=new_text,
            location_lat=new_lat,
            location_lng=new_lng,
            busy_until_ts=end_ts or car.busy_until_ts,
            notes=car.notes,
        )
        log.info(
            "auto-relocated car %s to %r busy_until=%s",
            car.label, (new_text or "")[:60], end_ts,
        )

    def _maybe_escalation_hint(
        self, b: dict[str, Any], fare: int | None
    ) -> EscalationHint | None:
        cfg = self.state.cfg
        if not cfg.annotate_escalation or self.escalation is None:
            return None
        try:
            stat = self.escalation.get_by_route(
                str(b.get("source_city") or ""),
                str(b.get("dest_city") or ""),
                str(b.get("car_type_id") or ""),
            )
        except Exception:
            log.exception("escalation lookup failed")
            return None
        return hint_for(stat, fare)

    def _maybe_competition_tag(self, b: dict[str, Any]) -> CompetitionTag | None:
        cfg = self.state.cfg
        if not cfg.annotate_competition or self.analytics is None:
            return None
        try:
            stat = self.analytics.get_by_route(
                source_city=str(b.get("source_city") or ""),
                dest_city=str(b.get("dest_city") or ""),
                car_type_id=str(b.get("car_type_id") or ""),
            )
        except Exception:
            log.exception("analytics lookup failed")
            return None
        return tag_for(stat)

    async def _maybe_pick_best_car(self, b: dict[str, Any]) -> CarPick | None:
        cfg = self.state.cfg
        if not cfg.enable_deadhead or not self.geocoder or not self.router:
            return None
        try:
            return await fleet.best_car_for(
                self.conn,
                booking=b,
                geocoder=self.geocoder,
                router=self.router,
            )
        except Exception:
            log.exception("best_car_for crashed")
            return None

    async def _maybe_check_eligibility(
        self, b: dict[str, Any]
    ) -> Eligibility | None:
        cfg = self.state.cfg
        if not (cfg.require_eligible_car or cfg.annotate_eligibility):
            return None
        if not self.availability or not cfg.vendor_user_id:
            return None
        booking_id = str(b.get("booking_id") or "")
        if not booking_id:
            return None
        return await self.availability.get(
            booking_id=booking_id,
            user_id=cfg.vendor_user_id,
            admin_id=cfg.vendor_user_id,
        )

    def _below_floor(self, fare: int | None, profit: ProfitEstimate) -> bool:
        floor = self.state.cfg.fare_floor
        if not floor:
            return False
        basis = (self.state.cfg.fare_floor_basis or "net").lower()
        if basis == "gross":
            return fare is not None and fare < floor
        # Default: net.
        return profit.net < floor

    # ---------- price bump ----------

    async def alert_price_up(self, b: dict[str, Any], old: int, new: int) -> None:
        bid = str(b.get("broadcast_id") or "")
        if not bid:
            return
        row = db.get_alert(self.conn, bid)
        if not row:
            # No existing alert — treat as new instead.
            await self.alert_new(b)
            return
        if row["status"] != "pending":
            # Already actioned — don't disturb the message.
            db.update_alert_fare(self.conn, bid, new)
            return

        profit = estimate_profit(b, self.state.cfg)
        car_pick = await self._maybe_pick_best_car(b)
        if car_pick is not None and car_pick.distance_km is not None:
            profit = apply_deadhead(
                profit, self.state.cfg, car_pick.distance_km,
                car_id=str(b.get("car_type_id") or ""),
            )
        eligibility = await self._maybe_check_eligibility(b)
        competition = self._maybe_competition_tag(b)
        escalation = self._maybe_escalation_hint(b, new)
        text = _format_alert(
            b,
            title="Price Increased! 📈",
            profit=profit,
            eligibility=eligibility,
            car_pick=car_pick,
            competition=competition,
            escalation=escalation,
        )
        # Notifier-only mode: send a SEPARATE message for the bump (matching
        # the original Chrome extension behaviour) instead of editing the
        # original message in place. The user wants to be loudly notified
        # when a rate goes up — an in-place edit is too subtle.
        try:
            msg = await self.bot.send_message(text)
            db.update_alert_fare(self.conn, bid, new)
            # Track the new message id so any subsequent bump on the same
            # broadcast can be edited if we ever switch back to in-place
            # edits later. Doesn't affect current behaviour.
            self.conn.execute(
                "UPDATE alerts SET message_id=?, sent_at=? WHERE broadcast_id=?",
                (int(msg["message_id"]), _now(), bid),
            )
        except Exception:
            log.exception("send_message failed for price-up on broadcast %s", bid)

    # ---------- text command dispatch ----------

    async def handle_message(self, msg: IncomingMessage) -> None:
        if str(msg.chat_id) != str(self.state.cfg.telegram_chat_id):
            log.warning("message from unexpected chat %s ignored", msg.chat_id)
            return
        text = (msg.text or "").strip()
        if not text.startswith("/"):
            return  # we only react to slash commands
        parts = text.split()
        cmd = parts[0].lower().split("@", 1)[0]  # strip @botname suffix
        args = parts[1:]
        try:
            if cmd == "/help" or cmd == "/start":
                await self._cmd_help()
            elif cmd == "/cars":
                await self._cmd_cars()
            elif cmd == "/where":
                await self._cmd_where(args)
            elif cmd == "/pause":
                self.state.paused = True
                await self.bot.send_message("⏸ Paused.")
            elif cmd == "/resume":
                self.state.paused = False
                await self.bot.send_message("▶ Resumed.")
            elif cmd == "/status":
                await self._cmd_status()
            elif cmd == "/free":
                await self._cmd_free(args)
            else:
                await self.bot.send_message(
                    f"Unknown command <code>{html.escape(cmd)}</code>. Try /help."
                )
        except Exception:
            log.exception("command %s crashed", cmd)
            try:
                await self.bot.send_message(f"⚠️ command failed: {html.escape(cmd)}")
            except Exception:
                pass

    async def _cmd_help(self) -> None:
        await self.bot.send_message(
            "<b>Commands</b>\n"
            "/cars — list your fleet with current locations\n"
            "/where &lt;label&gt; &lt;new location&gt; — move a car (geocodes)\n"
            "/free &lt;label&gt; — clear busy_until on a car\n"
            "/pause — stop polling Savaari\n"
            "/resume — start polling again\n"
            "/status — quick health line"
        )

    async def _cmd_cars(self) -> None:
        cars = fleet.list_cars(self.conn)
        if not cars:
            await self.bot.send_message("<i>No cars registered yet. Add them in the dashboard.</i>")
            return
        lines = ["<b>Fleet</b>"]
        now = _now()
        for c in cars:
            busy = ""
            if c.busy_until_ts and c.busy_until_ts > now:
                busy = f" · 🛑 busy until {html.escape(c.busy_until_ts)}"
            coord = ""
            if c.location_lat is not None and c.location_lng is not None:
                coord = f" ({c.location_lat:.3f},{c.location_lng:.3f})"
            lines.append(
                f"• <b>{html.escape(c.label)}</b> — "
                f"{html.escape((c.location_text or '?')[:60])}{coord}{busy}"
            )
        await self.bot.send_message("\n".join(lines))

    async def _cmd_where(self, args: list[str]) -> None:
        if len(args) < 2:
            await self.bot.send_message(
                "Usage: <code>/where &lt;label substring&gt; &lt;new location&gt;</code>\n"
                "Example: <code>/where KA-01 Pune Airport</code>"
            )
            return
        needle = args[0]
        loc_text = " ".join(args[1:])
        match = self._find_car_by_label(needle)
        if match is None:
            await self.bot.send_message(
                f"No car matching <code>{html.escape(needle)}</code>. Try /cars."
            )
            return
        new_lat, new_lng = match.location_lat, match.location_lng
        warn = ""
        if self.geocoder is not None:
            g = await self.geocoder.geocode(loc_text)
            if g is not None:
                new_lat, new_lng = g.lat, g.lng
            else:
                warn = "\n<i>(could not geocode — coords unchanged)</i>"
        fleet.upsert_car(
            self.conn,
            id=match.id,
            label=match.label,
            car_type_id=match.car_type_id,
            location_text=loc_text,
            location_lat=new_lat,
            location_lng=new_lng,
            busy_until_ts=None,  # /where also frees the car
            notes=match.notes,
        )
        await self.bot.send_message(
            f"📍 <b>{html.escape(match.label)}</b> moved to "
            f"<i>{html.escape(loc_text)}</i>{warn}"
        )

    async def _cmd_free(self, args: list[str]) -> None:
        if not args:
            await self.bot.send_message("Usage: <code>/free &lt;label substring&gt;</code>")
            return
        match = self._find_car_by_label(args[0])
        if match is None:
            await self.bot.send_message(f"No car matching <code>{html.escape(args[0])}</code>.")
            return
        fleet.upsert_car(
            self.conn,
            id=match.id,
            label=match.label,
            car_type_id=match.car_type_id,
            location_text=match.location_text,
            location_lat=match.location_lat,
            location_lng=match.location_lng,
            busy_until_ts=None,
            notes=match.notes,
        )
        await self.bot.send_message(f"✅ <b>{html.escape(match.label)}</b> marked free.")

    async def _cmd_status(self) -> None:
        snap = self.state.snapshot()
        lp = snap["last_poll"]
        flag = "⏸ paused" if snap["paused"] else ("⚠ auth failed" if snap["auth_failed"] else "● running")
        await self.bot.send_message(
            f"<b>Savaari Bot</b> {flag}\n"
            f"Last poll: {snap['last_ok_at'] or '—'}\n"
            f"{lp['total_broadcasts']} broadcasts · "
            f"{lp['new_count']} new · {lp['price_up_count']} price-up"
        )

    def _find_car_by_label(self, needle: str):
        needle_l = needle.lower()
        for c in fleet.list_cars(self.conn):
            if needle_l in c.label.lower():
                return c
        return None

    # ---------- callback dispatch ----------

    async def handle_callback(self, cbq: CallbackQuery) -> None:
        # Lightweight auth: only the configured chat can drive the bot.
        if str(cbq.chat_id) != str(self.state.cfg.telegram_chat_id):
            log.warning("callback from unexpected chat %s ignored", cbq.chat_id)
            await self.bot.answer_callback_query(cbq.id, "unauthorized")
            return

        data = cbq.data or ""
        if data.startswith(PREFIX_CONFIRM):
            await self._on_confirm(cbq, data[len(PREFIX_CONFIRM):])
        elif data.startswith(PREFIX_SKIP):
            await self._on_skip(cbq, data[len(PREFIX_SKIP):])
        else:
            await self.bot.answer_callback_query(cbq.id, "unknown action")

    async def _on_skip(self, cbq: CallbackQuery, broadcast_id: str) -> None:
        row = db.get_alert(self.conn, broadcast_id)
        if not row:
            await self.bot.answer_callback_query(cbq.id, "no record")
            return
        db.set_alert_status(self.conn, broadcast_id, "skipped", _now())
        await self.bot.edit_message_text(
            row["chat_id"],
            int(row["message_id"]),
            cbq.data and f"⏭ Skipped — {row['booking_id']}" or "Skipped",
            buttons=[],
        )
        await self.bot.answer_callback_query(cbq.id, "skipped")

    async def _on_confirm(self, cbq: CallbackQuery, broadcast_id: str) -> None:
        row = db.get_alert(self.conn, broadcast_id)
        if not row:
            await self.bot.answer_callback_query(cbq.id, "no record")
            return
        booking_id = row["booking_id"]

        # Atomic claim — wins exactly once across taps + restarts.
        if not db.claim_alert_pending(self.conn, broadcast_id, _now()):
            await self.bot.answer_callback_query(cbq.id, "already handled")
            return

        await self.bot.answer_callback_query(cbq.id, "confirming…")
        await self.bot.edit_message_text(
            row["chat_id"],
            int(row["message_id"]),
            f"⏳ Confirming booking #{booking_id}…",
            buttons=[],
        )

        dry_run = self.state.cfg.dry_run_accept
        result_text = ""
        result_ok = False
        try:
            if dry_run:
                log.warning(
                    "DRY RUN — would call postInterest broadcast=%s booking=%s",
                    broadcast_id,
                    booking_id,
                )
                result_ok = True
                result_text = "(dry run — postInterest not actually called)"
            else:
                result = await self.client.post_interest(broadcast_id, booking_id)
                result_ok = bool(result.get("status"))
                result_text = (
                    result.get("message")
                    or result.get("status_description")
                    or "(no message)"
                )
        except Exception as e:
            log.exception("postInterest crashed")
            result_text = f"{type(e).__name__}: {e}"

        db.insert_accept_log(
            self.conn,
            broadcast_id=broadcast_id,
            booking_id=booking_id,
            now=_now(),
            result_ok=result_ok,
            result_text=result_text,
            source="telegram_tap",
            dry_run=dry_run,
        )
        db.set_alert_status(
            self.conn,
            broadcast_id,
            "confirmed" if result_ok else "failed",
            _now(),
        )

        # Phase 4.5: on a successful confirm (real or dry-run), move the
        # picked car to the booking's drop location and lock it as busy
        # until the predicted trip end. This keeps Phase 4's deadhead math
        # accurate without the user having to do any manual updates.
        if result_ok and row["picked_car_id"]:
            try:
                await self._auto_relocate_picked_car(row)
            except Exception:
                log.exception("auto-relocate failed for booking %s", booking_id)

        icon = "✅" if result_ok else "❌"
        prefix = "DRY-RUN " if dry_run else ""
        text = f"{icon} {prefix}Booking #{booking_id}\n<i>{html.escape(result_text)}</i>"
        try:
            await self.bot.edit_message_text(
                row["chat_id"], int(row["message_id"]), text, buttons=[]
            )
        except Exception:
            log.exception("could not edit confirmation message")
