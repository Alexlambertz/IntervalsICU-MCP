"""MCP server definition: auth wiring, tools, and transport."""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse

from .aggregation import summarise_activity_streams
from .auth import KeycloakTokenVerifier
from .config import Settings
from .intervals import IntervalsClient, IntervalsError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("intervals_mcp")

settings = Settings.from_env()
intervals = IntervalsClient(settings.intervals_base_url)

mcp = MCPServer(
    name="intervals-icu",
    instructions=(
        "Access the authenticated user's intervals.icu training data: activities, "
        "wellness/recovery metrics, planned workouts and athlete profile. The athlete "
        "is fixed by the login — tools never take an athlete id."
    ),
    token_verifier=KeycloakTokenVerifier(settings),
    auth=AuthSettings(
        issuer_url=settings.oidc_issuer,  # published in protected-resource metadata
        resource_server_url=settings.resource_url,
        # Advertised as `scopes_supported`; bounds what MCP clients request so
        # they don't fall back to the authorization server's full scope catalog.
        required_scopes=list(settings.scopes),
        validate_token_resource=False,  # audience is validated inside the verifier
    ),
)


class CredentialsMissing(Exception):
    """The access token lacks the intervals.icu attributes."""


def _credentials() -> tuple[str, str]:
    """Extract (athlete_id, api_key) from the validated access token claims."""
    token = get_access_token()
    claims: dict[str, Any] = (token.claims if token else None) or {}
    athlete_id = claims.get(settings.athlete_claim)
    api_key = claims.get(settings.apikey_claim)
    if not athlete_id or not api_key:
        raise CredentialsMissing(
            "Im Keycloak-Profil fehlen die Attribute "
            f"'{settings.athlete_claim}' und/oder '{settings.apikey_claim}'. "
            "Bitte Athlete-ID und API-Key am Benutzer hinterlegen."
        )
    return str(athlete_id), str(api_key)


@mcp.tool(
    description=(
        "List the logged-in athlete's activities (rides, runs, swims, walks …) "
        "between two dates. Dates are ISO (YYYY-MM-DD), inclusive."
    )
)
async def list_activities(oldest: str, newest: str) -> list[dict[str, Any]]:
    athlete_id, api_key = _credentials()
    try:
        return await intervals.list_activities(athlete_id, api_key, oldest, newest)
    except IntervalsError as exc:
        raise RuntimeError(str(exc)) from exc


@mcp.tool(
    description="Get the full detail record for a single activity by its id."
)
async def get_activity(activity_id: str) -> dict[str, Any]:
    _athlete_id, api_key = _credentials()
    try:
        return await intervals.get_activity(activity_id, api_key)
    except IntervalsError as exc:
        raise RuntimeError(str(exc)) from exc


@mcp.tool(
    description=(
        "Get daily wellness / recovery records (resting HR, HRV, sleep, weight, "
        "fitness/fatigue/form) between two ISO dates, inclusive."
    )
)
async def get_wellness(oldest: str, newest: str) -> list[dict[str, Any]]:
    athlete_id, api_key = _credentials()
    try:
        return await intervals.get_wellness(athlete_id, api_key, oldest, newest)
    except IntervalsError as exc:
        raise RuntimeError(str(exc)) from exc


@mcp.tool(
    description=(
        "List calendar events / planned workouts between two ISO dates, inclusive."
    )
)
async def list_events(oldest: str, newest: str) -> list[dict[str, Any]]:
    athlete_id, api_key = _credentials()
    try:
        return await intervals.list_events(athlete_id, api_key, oldest, newest)
    except IntervalsError as exc:
        raise RuntimeError(str(exc)) from exc


@mcp.tool(
    description=(
        "Get detailed per-sample time-series for one activity, aggregated into "
        "fixed-length time blocks (default 60s) so the result stays compact. Each "
        "block carries average/max power, average/max heart rate, speed (km/h), "
        "cadence, torque, cumulative distance, elevation gain, altitude, "
        "temperature, direction of travel, the headwind component (m/s, "
        "positive=head), and watts-per-heartbeat (cardiac drift). Also returns "
        "activity context and activity-level weather. Use a larger block_seconds "
        "(e.g. 300) for long activities. Optionally restrict `types` to specific "
        "streams (e.g. ['watts','heartrate'])."
    )
)
async def get_activity_streams(
    activity_id: str,
    block_seconds: int = 60,
    types: list[str] | None = None,
) -> dict[str, Any]:
    if block_seconds < 1:
        raise RuntimeError("block_seconds muss mindestens 1 sein.")
    _athlete_id, api_key = _credentials()
    try:
        activity = await intervals.get_activity(activity_id, api_key)
        streams = await intervals.get_streams(activity_id, api_key, types)
    except IntervalsError as exc:
        raise RuntimeError(str(exc)) from exc
    try:
        return summarise_activity_streams(activity, streams, block_seconds)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc


@mcp.tool(
    description=(
        "Get the activity's detected intervals / laps analysis (per-interval "
        "duration, power, heart rate, cadence, speed, etc.) — a compact, "
        "structured breakdown of the efforts within an activity."
    )
)
async def get_activity_intervals(activity_id: str) -> dict[str, Any]:
    _athlete_id, api_key = _credentials()
    try:
        return await intervals.get_intervals(activity_id, api_key)
    except IntervalsError as exc:
        raise RuntimeError(str(exc)) from exc


@mcp.tool(
    description="Get the logged-in athlete's profile, including FTP and training zones."
)
async def get_athlete() -> dict[str, Any]:
    athlete_id, api_key = _credentials()
    try:
        return await intervals.get_athlete(athlete_id, api_key)
    except IntervalsError as exc:
        raise RuntimeError(str(exc)) from exc


@mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
async def healthz(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def main() -> None:
    logger.info(
        "Starting intervals.icu MCP server on %s:%s%s (issuer=%s)",
        settings.host,
        settings.port,
        settings.mcp_path,
        settings.oidc_issuer,
    )
    mcp.run(
        transport="streamable-http",
        host=settings.host,
        port=settings.port,
        streamable_http_path=settings.mcp_path,
    )
