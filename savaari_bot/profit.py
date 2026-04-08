"""Profit estimator for Savaari broadcasts.

The model is intentionally simple — two coefficients per car type:

    fuel_rate    ₹/km   covers fuel + per-km maintenance
    driver_pct   % of   covers driver pay as a percentage of `earned`
                 earned (vendor_cost + night_charge if applicable)

Toll is NOT modelled. Real tolls vary wildly by route and a flat per-km
estimate misled more than it helped. Set fuel_rate slightly higher if
you want to fold a rough toll allowance into the estimate.

Driver pay is a percentage of the booking value, NOT per km. Indian
fleet operators almost always pay drivers a cut of what they collect,
not by distance. A direct consequence: the deadhead trip back to base
adds fuel cost but does NOT add driver cost — the driver's pay is
locked to the booking, not the actual km driven.

Inputs are taken straight from a `broadcast_details[i]` row. Output is
a small dataclass that the notifier turns into a one-line summary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import Config
from .db import _to_int


@dataclass
class ProfitEstimate:
    estimated_km: int
    earned: int          # vendor_cost (+ night charge if applicable)
    fuel_cost: int
    driver_cost: int     # round(earned * driver_pct / 100)
    driver_pct: float    # the percentage actually used for this booking
    net: int
    # Phase 4 additions — populated when fleet + geo are configured.
    deadhead_km: float = 0.0
    deadhead_cost: int = 0

    def short(self) -> str:
        """One-line summary suitable for the Telegram alert."""
        # Negative net is intentionally rendered without parentheses so the
        # ₹- sign is unmistakable on a phone screen.
        parts = [
            f"fuel ₹{self.fuel_cost:,}",
            f"driver ₹{self.driver_cost:,} ({self.driver_pct:.0f}%)",
        ]
        if self.deadhead_cost:
            parts.append(f"deadhead ₹{self.deadhead_cost:,}/{self.deadhead_km:.0f}km")
        return (
            f"Net ≈ ₹{self.net:,} ("
            + " / ".join(parts)
            + f" · {self.estimated_km}km)"
        )


def apply_deadhead(p: ProfitEstimate, cfg: Config, deadhead_km: float, car_id: str = "") -> ProfitEstimate:
    """Return a copy of `p` with deadhead distance + fuel cost subtracted.

    Only fuel is charged on deadhead. Driver pay is locked to the booking
    value (a percentage), so an extra empty drive does not increase what
    you owe the driver. If you actually pay drivers per km, raise the
    fuel_rate to compensate.
    """
    fuel_rate = float(cfg.fuel_rate_per_car_type.get(car_id, cfg.fuel_rate_default))
    cost = round(deadhead_km * fuel_rate)
    return ProfitEstimate(
        estimated_km=p.estimated_km,
        earned=p.earned,
        fuel_cost=p.fuel_cost,
        driver_cost=p.driver_cost,
        driver_pct=p.driver_pct,
        net=p.net - int(cost),
        deadhead_km=float(deadhead_km),
        deadhead_cost=int(cost),
    )


def _rate_for(car_id: str, overrides: dict[str, float], default: float) -> float:
    if not car_id:
        return default
    return float(overrides.get(car_id, default))


def estimate(b: dict[str, Any], cfg: Config) -> ProfitEstimate:
    car_id = str(b.get("car_type_id") or "").strip()

    fuel_rate = _rate_for(car_id, cfg.fuel_rate_per_car_type, cfg.fuel_rate_default)
    driver_pct = _rate_for(car_id, cfg.driver_pct_per_car_type, cfg.driver_pct_default)

    package_kms = _to_int(b.get("package_kms")) or 0
    num_days = _to_int(b.get("num_days")) or 1
    min_km_per_day = _to_int(b.get("min_km_per_day")) or 0
    estimated_km = max(min_km_per_day * num_days, package_kms, 0)

    vendor_cost = _to_int(b.get("vendor_cost")) or 0
    night_charge = _to_int(b.get("night_charge")) or 0
    nightcharge_status = str(b.get("nightcharge_status") or "0").strip() not in ("", "0", "false", "False")
    earned = vendor_cost + (night_charge if nightcharge_status else 0)

    fuel_cost = round(estimated_km * fuel_rate)
    driver_cost = round(earned * driver_pct / 100.0)

    net = earned - fuel_cost - driver_cost

    return ProfitEstimate(
        estimated_km=int(estimated_km),
        earned=int(earned),
        fuel_cost=int(fuel_cost),
        driver_cost=int(driver_cost),
        driver_pct=float(driver_pct),
        net=int(net),
    )
