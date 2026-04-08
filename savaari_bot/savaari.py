"""HTTP client for the Savaari Vendor backend.

Phase 0 only needs `getNewBusiness`. Later phases will add `postInterest` and
`fetchDriversWithCarsList`. The endpoint shapes were reverse-engineered from
`js/controllers/DashboardBookingController.js` on 2026-04-07.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


class SavaariAuthError(RuntimeError):
    """Raised when the API rejects the vendorToken (or returns empty data
    that smells like an auth failure)."""


@dataclass
class SavaariClient:
    vendor_token: str
    base_url: str = "https://vendor.savaari.com/vendor"
    timeout_s: float = 20.0
    user_agent: str = "Mozilla/5.0"

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout_s,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json, text/plain, */*",
                "Referer": f"{self.base_url}/layout.html",
            },
        )

    async def get_new_business(self) -> dict[str, Any]:
        """Call booking.php?action=getNewBusiness and return the parsed body.

        Raises SavaariAuthError if the response is shaped like an auth failure
        (status:false, or zero counters across the board which is what we get
        when no vendorToken is supplied).
        """
        params = {
            "action": "getNewBusiness",
            "vendorToken": self.vendor_token,
            "booking_id": "0",
        }
        async with self._client() as client:
            resp = await client.get("api/booking/v1/booking.php", params=params)
            resp.raise_for_status()
            data = resp.json()

        if not data.get("status"):
            raise SavaariAuthError(
                f"getNewBusiness returned status=false: {data.get('status_description')!r}"
            )

        rs = data.get("resultset") or {}
        # Heuristic: a valid token returns broadcast_details (often hundreds of
        # rows). An invalid/missing token returns just the zero counters and
        # nothing else. Treat that as auth failure so the caller can refresh.
        if "broadcast_details" not in rs:
            raise SavaariAuthError(
                "resultset missing broadcast_details — token likely invalid"
            )

        return data

    @staticmethod
    def broadcasts(payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Convenience: extract the broadcast_details list from a payload."""
        return list(payload.get("resultset", {}).get("broadcast_details") or [])

    async def vendor_details(self) -> dict[str, Any]:
        """Call profile.php?action=vendordetails. Used at boot to derive the
        vendor's own `user_id` for downstream calls (e.g.
        FETCH_DRIVERS_WITH_CARS_LIST_NPS) so the user only ever has to paste
        the vendorToken.
        """
        params = {"action": "vendordetails", "vendorToken": self.vendor_token}
        async with self._client() as client:
            resp = await client.get("api/profile/v1/profile.php", params=params)
            resp.raise_for_status()
            data = resp.json()
        if not data.get("status"):
            raise SavaariAuthError(
                f"vendordetails returned status=false: {data.get('status_description')!r}"
            )
        return data

    async def fetch_all_cars(
        self, *, vendor_id: str, page_size: int = 1000
    ) -> dict[str, Any]:
        """Pull the vendor's full registered fleet from Savaari.

        Endpoint: driverNcarMGMT.php?action=FETCH_ALL_CARS
        Auth: vendor_id alone is sufficient — verified that this endpoint
        does not check the vendorToken or the PHPSESSID cookie.
        """
        params = {
            "action": "FETCH_ALL_CARS",
            "vendor_id": str(vendor_id),
            "text_search": "",
            "active": "",
            "page": "1",
            "size": str(page_size),
        }
        async with self._client() as client:
            resp = await client.get("api/booking/v1/driverNcarMGMT.php", params=params)
            resp.raise_for_status()
            return resp.json()

    async def fetch_all_drivers(
        self, *, vendor_id: str, page_size: int = 1000
    ) -> dict[str, Any]:
        """Pull the vendor's full driver list from Savaari.

        Same auth surface as fetch_all_cars.
        """
        params = {
            "action": "FETCH_ALL_DRIVERS",
            "vendor_id": str(vendor_id),
            "text_search": "",
            "active": "",
            "page": "1",
            "size": str(page_size),
        }
        async with self._client() as client:
            resp = await client.get("api/booking/v1/driverNcarMGMT.php", params=params)
            resp.raise_for_status()
            return resp.json()

    async def fetch_drivers_with_cars(
        self,
        *,
        booking_id: str,
        user_id: str,
        admin_id: str,
        usertype: str = "Vendor",
    ) -> dict[str, Any]:
        """Call driverNcarMGMT.php?action=FETCH_DRIVERS_WITH_CARS_LIST_NPS.

        Mirrors the dashboard's call when it opens the assign-driver modal.
        Returns the parsed body. The notifier reads `resultset.carRecordList`
        to decide eligibility — its length is the count of (driver, car)
        combinations the vendor has that can serve this specific booking.
        """
        params = {
            "action": "FETCH_DRIVERS_WITH_CARS_LIST_NPS",
            "user_id": user_id,
            "admin_id": admin_id,
            "usertype": usertype,
            "booking_id": str(booking_id),
        }
        async with self._client() as client:
            resp = await client.get("api/booking/v1/driverNcarMGMT.php", params=params)
            resp.raise_for_status()
            return resp.json()

    async def post_interest(
        self,
        broadcast_id: str,
        booking_id: str,
        packed_bookings: str = "",
    ) -> dict[str, Any]:
        """Accept a booking. Mirrors $scope.executeDialogAction in
        DashboardBookingController.js exactly — same URL, same params, same
        order. The server returns a JSON object with `status` and a human
        message; the dashboard considers `status == True` as success.

        This is the only mutating call the bot ever makes. Every invocation
        should be triggered by a deliberate user tap.
        """
        params = {
            "action": "postInterest",
            "vendorToken": self.vendor_token,
            "broadcast_id": str(broadcast_id),
            "booking_id": str(booking_id),
            "packed_bookings": packed_bookings,
        }
        async with self._client() as client:
            resp = await client.get("api/booking/v1/booking.php", params=params)
            resp.raise_for_status()
            return resp.json()
