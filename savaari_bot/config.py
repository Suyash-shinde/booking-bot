"""Runtime configuration for the bot.

Phase 0 keeps configuration minimal: a single TOML file under the user's data
dir, plus environment-variable overrides. Later phases will surface these
fields in the FastAPI dashboard.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def data_dir() -> Path:
    """Return the per-user data directory where db, config and logs live.

    Windows: %APPDATA%\\SavaariBot
    Linux/macOS: ~/.local/share/savaari_bot   (xdg-ish, no extra dependency)
    """
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "SavaariBot"
    return Path.home() / ".local" / "share" / "savaari_bot"


@dataclass
class Config:
    vendor_token: str = ""
    poll_interval_s: float = 10.0
    api_base: str = "https://vendor.savaari.com/vendor"
    request_timeout_s: float = 20.0
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
    # Reserved for later phases — kept here so the schema is stable.
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    fare_floor: int = 0
    # Whether `fare_floor` is compared against gross fare (total_amt) or
    # against the bot's own *net* profit estimate. Default to "net" — the
    # whole point of computing profit is to filter on it.
    fare_floor_basis: str = "net"  # "net" | "gross"
    paused: bool = False
    # Safety: when True, postInterest calls are *logged* but not actually sent.
    # The Telegram message still updates as if it succeeded so the user can
    # rehearse the full flow without taking any real bookings. Default ON.
    dry_run_accept: bool = True

    # ----- profit estimator (Phase 2) -----
    # Fuel: per-km cost defaults, used when a car type has no override below.
    # Driver: percentage of `earned` (vendor_cost + night_charge if any), NOT
    # per-km — that's how Indian fleet operators actually pay. A driver does
    # not earn extra for the deadhead trip back to base. Tune both in the
    # Settings panel.
    fuel_rate_default: float = 8.5      # ₹/km, fuel + maintenance
    driver_pct_default: float = 25.0    # % of earned booking value
    # Per-car-type overrides keyed by car_type_id (string, as Savaari sends).
    fuel_rate_per_car_type: dict[str, float] = field(default_factory=dict)
    driver_pct_per_car_type: dict[str, float] = field(default_factory=dict)

    # ----- driver/car availability gate (Phase 3) -----
    # vendor_user_id is auto-discovered from vendordetails on boot. The user
    # never has to set it manually; we persist it so subsequent runs skip
    # the bootstrap call.
    vendor_user_id: str = ""
    # Off by default — turning the gate on with an empty fleet would
    # silently suppress every alert.
    require_eligible_car: bool = False
    # When the gate is OFF we still want to show the eligibility count in
    # the alert if available, but only if this is True (annotate-only mode).
    annotate_eligibility: bool = True
    eligibility_cache_ttl_s: float = 60.0

    # ----- deadhead / geocoding (Phase 4) -----
    # Off by default. Once the user adds at least one car with a known
    # location, they can flip this on. The notifier degrades gracefully:
    # if geocoding fails or no cars exist, alerts go out without a deadhead
    # line, exactly like Phase 2.
    enable_deadhead: bool = False
    # Nominatim's usage policy requires a real User-Agent identifying the
    # app + a contact. Tune this in the Settings panel before enabling.
    nominatim_base: str = "https://nominatim.openstreetmap.org"
    nominatim_user_agent: str = "savaari_bot (personal use; set contact in settings)"
    # Public OSRM demo. For heavier use, set this to a self-hosted instance.
    osrm_base: str = "https://router.project-osrm.org"

    # ----- analytics (Phase 5) -----
    # Whether to attach a competition tag (🟢/🟡/🔥) to every alert. On by
    # default once we have any history; degrades cleanly to "no history yet"
    # before there's enough data.
    annotate_competition: bool = True
    # Rolling window in days used by the route_stats query.
    analytics_window_days: int = 14
    # How often to recompute the route stats. 5 min is plenty — most users
    # will look at the dashboard and weekly report, not stare live.
    analytics_refresh_s: float = 300.0

    # ----- escalation curves (Phase 6) -----
    # Annotate every alert with a "WAIT / GRAB / OK" hint based on the
    # bucket's escalation distribution. Default on; degrades cleanly to
    # "no escalation history yet" until enough samples accumulate.
    annotate_escalation: bool = True
    # If True, alerts where the model says "wait" are not sent at all
    # (the user gets the next re-broadcast at a higher fare). Default OFF
    # because it can suppress real opportunities — opt-in once the model
    # has 2+ weeks of data.
    suppress_below_p50: bool = False

    @property
    def db_path(self) -> Path:
        return data_dir() / "savaari.sqlite3"

    @property
    def log_path(self) -> Path:
        return data_dir() / "savaari.log"

    @property
    def config_path(self) -> Path:
        return data_dir() / "config.toml"


_PROFIT_NESTED_KEYS = {
    "fuel_rate_default",
    "driver_pct_default",
    "fuel_rate_per_car_type",
    "driver_pct_per_car_type",
}


def _apply_dict(cfg: Config, raw: dict[str, Any]) -> None:
    for key, value in raw.items():
        if key == "profit" and isinstance(value, dict):
            for pk, pv in value.items():
                if pk in _PROFIT_NESTED_KEYS:
                    if pk.endswith("_per_car_type") and isinstance(pv, dict):
                        setattr(cfg, pk, {str(k): float(v) for k, v in pv.items()})
                    else:
                        setattr(cfg, pk, type(getattr(cfg, pk))(pv))
            continue
        if not hasattr(cfg, key):
            continue
        current = getattr(cfg, key)
        if isinstance(current, bool):
            setattr(cfg, key, bool(value))
        elif isinstance(current, (int, float)) and not isinstance(current, bool):
            setattr(cfg, key, type(current)(value))
        else:
            setattr(cfg, key, value)


def load() -> Config:
    """Load config from TOML file (if present), then env-var overrides."""
    cfg = Config()
    data_dir().mkdir(parents=True, exist_ok=True)

    path = cfg.config_path
    if path.exists():
        with path.open("rb") as f:
            raw = tomllib.load(f)
        _apply_dict(cfg, raw)

    # Env overrides — handy for development.
    env_map = {
        "SAVAARI_VENDOR_TOKEN": "vendor_token",
        "SAVAARI_POLL_INTERVAL": "poll_interval_s",
        "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
        "TELEGRAM_CHAT_ID": "telegram_chat_id",
    }
    for env_key, attr in env_map.items():
        if env_key in os.environ:
            current = getattr(cfg, attr)
            raw_val = os.environ[env_key]
            if isinstance(current, (int, float)):
                setattr(cfg, attr, type(current)(raw_val))
            else:
                setattr(cfg, attr, raw_val)
    return cfg


def save(cfg: Config) -> None:
    """Persist a minimal subset of fields to TOML.

    We deliberately write only the fields that are user-tunable; secrets like
    vendor_token are written too because the bot needs to survive restarts.
    """
    data_dir().mkdir(parents=True, exist_ok=True)
    def _esc(s: str) -> str:
        # TOML basic-string escape: backslashes and double quotes only.
        return s.replace("\\", "\\\\").replace('"', '\\"')

    lines = [
        f'vendor_token = "{_esc(cfg.vendor_token)}"',
        f"poll_interval_s = {cfg.poll_interval_s}",
        f'telegram_bot_token = "{_esc(cfg.telegram_bot_token)}"',
        f'telegram_chat_id = "{_esc(cfg.telegram_chat_id)}"',
        f"fare_floor = {cfg.fare_floor}",
        f'fare_floor_basis = "{cfg.fare_floor_basis}"',
        f"paused = {'true' if cfg.paused else 'false'}",
        f"dry_run_accept = {'true' if cfg.dry_run_accept else 'false'}",
        f'vendor_user_id = "{_esc(cfg.vendor_user_id)}"',
        f"require_eligible_car = {'true' if cfg.require_eligible_car else 'false'}",
        f"annotate_eligibility = {'true' if cfg.annotate_eligibility else 'false'}",
        f"eligibility_cache_ttl_s = {cfg.eligibility_cache_ttl_s}",
        f"enable_deadhead = {'true' if cfg.enable_deadhead else 'false'}",
        f'nominatim_base = "{_esc(cfg.nominatim_base)}"',
        f'nominatim_user_agent = "{_esc(cfg.nominatim_user_agent)}"',
        f'osrm_base = "{_esc(cfg.osrm_base)}"',
        f"annotate_competition = {'true' if cfg.annotate_competition else 'false'}",
        f"analytics_window_days = {cfg.analytics_window_days}",
        f"analytics_refresh_s = {cfg.analytics_refresh_s}",
        f"annotate_escalation = {'true' if cfg.annotate_escalation else 'false'}",
        f"suppress_below_p50 = {'true' if cfg.suppress_below_p50 else 'false'}",
        "",
        "[profit]",
        f"fuel_rate_default = {cfg.fuel_rate_default}",
        f"driver_pct_default = {cfg.driver_pct_default}",
    ]
    if cfg.fuel_rate_per_car_type:
        lines.append("")
        lines.append("[profit.fuel_rate_per_car_type]")
        for cid, rate in sorted(cfg.fuel_rate_per_car_type.items()):
            lines.append(f'"{_esc(str(cid))}" = {rate}')
    if cfg.driver_pct_per_car_type:
        lines.append("")
        lines.append("[profit.driver_pct_per_car_type]")
        for cid, pct in sorted(cfg.driver_pct_per_car_type.items()):
            lines.append(f'"{_esc(str(cid))}" = {pct}')
    cfg.config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
