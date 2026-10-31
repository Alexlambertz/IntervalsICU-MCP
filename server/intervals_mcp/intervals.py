"""Thin async client for the intervals.icu REST API.

Authentication is HTTP Basic with the literal username ``API_KEY`` and the
user's personal API key as the password. The athlete id is required in the path
of athlete-scoped endpoints. Both values come from the validated access token.
"""

from __future__ import annotations

from typing import Any

import httpx


class IntervalsError(RuntimeError):
    """Raised when intervals.icu returns an error, with a human-readable message."""


class IntervalsClient:
    def __init__(self, base_url: str) -> None:
        self._base_url = base_url

    async def _request(
        self,
        path: str,
        api_key: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        async with httpx.AsyncClient(
            base_url=self._base_url,
            auth=("API_KEY", api_key),
            timeout=30.0,
            headers={"Accept": "application/json"},
        ) as client:
            try:
                resp = await client.get(path, params=params)
            except httpx.RequestError as exc:
                raise IntervalsError(f"intervals.icu nicht erreichbar: {exc}") from exc

        if resp.status_code == 401:
            raise IntervalsError(
                "intervals.icu lehnt den API-Key ab (401). Bitte den im "
                "Keycloak-Profil hinterlegten API-Key prüfen."
            )
        if resp.status_code == 403:
            raise IntervalsError(
                "Kein Zugriff auf diese Athlete-ID (403). Athlete-ID und API-Key "
                "müssen zum selben intervals.icu-Konto gehören."
            )
        if resp.status_code == 404:
            raise IntervalsError("Ressource nicht gefunden (404).")
        if resp.status_code == 429:
            raise IntervalsError("Rate-Limit von intervals.icu erreicht (429).")
        if resp.status_code >= 400:
            raise IntervalsError(
                f"intervals.icu Fehler {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json()

    # --- athlete-scoped endpoints ---

    async def get_athlete(self, athlete_id: str, api_key: str) -> Any:
        return await self._request(f"/athlete/{athlete_id}", api_key)

    async def list_activities(
        self, athlete_id: str, api_key: str, oldest: str, newest: str
    ) -> Any:
        return await self._request(
            f"/athlete/{athlete_id}/activities",
            api_key,
            params={"oldest": oldest, "newest": newest},
        )

    async def get_wellness(
        self, athlete_id: str, api_key: str, oldest: str, newest: str
    ) -> Any:
        return await self._request(
            f"/athlete/{athlete_id}/wellness",
            api_key,
            params={"oldest": oldest, "newest": newest},
        )

    async def list_events(
        self, athlete_id: str, api_key: str, oldest: str, newest: str
    ) -> Any:
        return await self._request(
            f"/athlete/{athlete_id}/events",
            api_key,
            params={"oldest": oldest, "newest": newest},
        )

    # --- activity-scoped endpoints ---

    async def get_activity(self, activity_id: str, api_key: str) -> Any:
        return await self._request(f"/activity/{activity_id}", api_key)

    async def get_streams(
        self,
        activity_id: str,
        api_key: str,
        types: list[str] | None = None,
    ) -> dict[str, list[Any]]:
        """Return per-sample streams as {stream_type: [values]}.

        Most streams are a flat list in "data". The latlng stream is special:
        intervals.icu puts latitude in "data" and longitude in a parallel
        "data2", so it is zipped back into [lat, lon] pairs here.
        """
        params = {"types": ",".join(types)} if types else None
        raw = await self._request(
            f"/activity/{activity_id}/streams", api_key, params=params
        )
        out: dict[str, list[Any]] = {}
        for stream in raw:
            data = stream.get("data") or []
            data2 = stream.get("data2")
            if data2:
                out[stream["type"]] = [
                    [a, b] if a is not None and b is not None else None
                    for a, b in zip(data, data2)
                ]
            else:
                out[stream["type"]] = data
        return out

    async def get_intervals(self, activity_id: str, api_key: str) -> Any:
        """Return the activity's detected intervals / laps analysis."""
        return await self._request(f"/activity/{activity_id}/intervals", api_key)
