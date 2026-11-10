"""Async client for the intervals.icu REST API.

Authentication is HTTP Basic with the literal username ``API_KEY`` and the
user's personal API key as the password. The athlete id is required in the path
of athlete-scoped endpoints. Both values come from the validated access token.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx
from mcp.server.mcpserver.exceptions import ToolError


class IntervalsError(ToolError):
    """An anticipated failure (bad input, API error) with a human-readable message.

    Subclassing ToolError matters: mcp 2.x shows the model only the message of a
    ToolError; any other exception is reported as a bare "Error executing tool ...".
    """


def seg(value: Any) -> str:
    """Make a caller-supplied value safe to use as ONE URL path segment.

    Tool arguments such as activity or event ids end up in the request path. Without
    this, a value like ``../athlete/other`` could redirect the call to a different
    endpoint. Empty values, dot segments and anything containing a slash are rejected;
    everything else is percent-encoded.
    """
    text = str(value).strip()
    if not text or text in {".", ".."} or "/" in text or "\\" in text:
        raise IntervalsError(f"Ungültige ID: {text!r}")
    return quote(text, safe="")


def _clean(params: dict[str, Any] | None) -> dict[str, Any] | None:
    """Drop unset query parameters (httpx would send them as empty strings)."""
    if not params:
        return None
    out = {k: v for k, v in params.items() if v is not None}
    return out or None


class IntervalsClient:
    def __init__(self, base_url: str) -> None:
        self._base_url = base_url

    async def request(
        self,
        method: str,
        path: str,
        api_key: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        content: bytes | None = None,
        content_type: str | None = None,
    ) -> Any:
        headers = {"Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        async with httpx.AsyncClient(
            base_url=self._base_url,
            auth=("API_KEY", api_key),
            timeout=60.0,
            headers=headers,
        ) as client:
            try:
                resp = await client.request(
                    method,
                    path,
                    params=_clean(params),
                    json=json,
                    files=files,
                    content=content,
                )
            except httpx.RequestError as exc:
                raise IntervalsError(f"intervals.icu nicht erreichbar: {exc}") from exc

        if resp.status_code == 401:
            raise IntervalsError(
                "intervals.icu lehnt den API-Key ab (401). Bitte den im "
                "Keycloak-Profil hinterlegten API-Key prüfen."
            )
        if resp.status_code == 403:
            raise IntervalsError(
                "Kein Zugriff (403). Athlete-ID und API-Key müssen zum selben "
                "intervals.icu-Konto gehören; ggf. fehlt dem Key die Schreibberechtigung."
            )
        if resp.status_code == 404:
            raise IntervalsError("Ressource nicht gefunden (404).")
        if resp.status_code == 429:
            raise IntervalsError("Rate-Limit von intervals.icu erreicht (429).")
        if resp.status_code >= 400:
            # For writes the body usually says which field was rejected — keep it.
            raise IntervalsError(
                f"intervals.icu Fehler {resp.status_code}: {resp.text[:500]}"
            )
        if not resp.content:
            return {"ok": True, "status": resp.status_code}
        try:
            return resp.json()
        except ValueError:
            return {"ok": True, "status": resp.status_code, "body": resp.text[:2000]}

    async def _get(
        self, path: str, api_key: str, params: dict[str, Any] | None = None
    ) -> Any:
        return await self.request("GET", path, api_key, params=params)

    # --- athlete-scoped reads ---

    async def get_athlete(self, athlete_id: str, api_key: str) -> Any:
        return await self._get(f"/athlete/{seg(athlete_id)}", api_key)

    async def list_activities(
        self, athlete_id: str, api_key: str, oldest: str, newest: str
    ) -> Any:
        return await self._get(
            f"/athlete/{seg(athlete_id)}/activities",
            api_key,
            {"oldest": oldest, "newest": newest},
        )

    async def get_wellness(
        self, athlete_id: str, api_key: str, oldest: str, newest: str
    ) -> Any:
        return await self._get(
            f"/athlete/{seg(athlete_id)}/wellness",
            api_key,
            {"oldest": oldest, "newest": newest},
        )

    async def list_events(
        self, athlete_id: str, api_key: str, oldest: str, newest: str
    ) -> Any:
        return await self._get(
            f"/athlete/{seg(athlete_id)}/events",
            api_key,
            {"oldest": oldest, "newest": newest},
        )

    async def list_folders(self, athlete_id: str, api_key: str) -> Any:
        return await self._get(f"/athlete/{seg(athlete_id)}/folders", api_key)

    async def list_workouts(self, athlete_id: str, api_key: str) -> Any:
        return await self._get(f"/athlete/{seg(athlete_id)}/workouts", api_key)

    # --- activity-scoped reads ---

    async def get_activity(self, activity_id: str, api_key: str) -> Any:
        return await self._get(f"/activity/{seg(activity_id)}", api_key)

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
        raw = await self._get(
            f"/activity/{seg(activity_id)}/streams",
            api_key,
            {"types": ",".join(types)} if types else None,
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
        return await self._get(f"/activity/{seg(activity_id)}/intervals", api_key)
