"""MCP server definition: auth wiring, tools, and transport."""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse

from .aggregation import summarise_activity_streams
from .auth import KeycloakTokenVerifier
from .config import Settings
from .intervals import IntervalsClient, IntervalsError
from .write_tools import register_write_tools

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


class CredentialsMissing(ToolError):
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
        raise ToolError(str(exc)) from exc


@mcp.tool(
    description="Get the full detail record for a single activity by its id."
)
async def get_activity(activity_id: str) -> dict[str, Any]:
    _athlete_id, api_key = _credentials()
    try:
        return await intervals.get_activity(activity_id, api_key)
    except IntervalsError as exc:
        raise ToolError(str(exc)) from exc


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
        raise ToolError(str(exc)) from exc


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
        raise ToolError(str(exc)) from exc


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
        raise ToolError("block_seconds muss mindestens 1 sein.")
    _athlete_id, api_key = _credentials()
    try:
        activity = await intervals.get_activity(activity_id, api_key)
        streams = await intervals.get_streams(activity_id, api_key, types)
    except IntervalsError as exc:
        raise ToolError(str(exc)) from exc
    try:
        return summarise_activity_streams(activity, streams, block_seconds)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


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
        raise ToolError(str(exc)) from exc


@mcp.tool(
    description="Get the logged-in athlete's profile, including FTP and training zones."
)
async def get_athlete() -> dict[str, Any]:
    athlete_id, api_key = _credentials()
    try:
        return await intervals.get_athlete(athlete_id, api_key)
    except IntervalsError as exc:
        raise ToolError(str(exc)) from exc


# Keys of a folder worth showing. `children` (every workout inside) and `shareToken`
# (a sharing secret) are deliberately left out: the list stays compact and leaks nothing.
_FOLDER_KEYS = (
    "id", "name", "type", "description", "num_workouts", "start_date_local",
    "duration_weeks", "visibility", "canEdit",
)
_WORKOUT_KEYS = (
    "id", "folder_id", "name", "type", "description", "moving_time",
    "icu_training_load", "icu_intensity", "distance", "indoor", "tags", "day", "days",
)


@mcp.tool(
    description=(
        "List the athlete's workout library folders and training plans (id, name, type, "
        "description, number of workouts, ...). Use the folder id with create_workout "
        "or list_workouts. The workouts inside a folder are not included."
    ),
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
async def list_folders() -> list[dict[str, Any]]:
    athlete_id, api_key = _credentials()
    try:
        folders = await intervals.list_folders(athlete_id, api_key)
    except IntervalsError as exc:
        raise ToolError(str(exc)) from exc
    return [{k: f[k] for k in _FOLDER_KEYS if f.get(k) is not None} for f in folders]


@mcp.tool(
    description=(
        "List workouts in the athlete's workout library, optionally only those of one "
        "folder/plan (folder_id from list_folders). Includes the workout description "
        "(the step text). Capped at `limit` results (default 100)."
    ),
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
async def list_workouts(folder_id: int | None = None, limit: int = 100) -> dict[str, Any]:
    athlete_id, api_key = _credentials()
    try:
        workouts = await intervals.list_workouts(athlete_id, api_key)
    except IntervalsError as exc:
        raise ToolError(str(exc)) from exc
    if folder_id is not None:
        workouts = [w for w in workouts if w.get("folder_id") == folder_id]
    shown = workouts[: max(1, limit)]
    return {
        "total": len(workouts),
        "returned": len(shown),
        "workouts": [{k: w[k] for k in _WORKOUT_KEYS if w.get(k) is not None} for w in shown],
    }


# Write tools (create/update/delete). Which ones are active depends on MCP_READ_ONLY
# (none) and MCP_ENABLE_ADVANCED_WRITES (core only vs. everything).
WRITE_TOOLS_ACTIVE, WRITE_TOOLS_HIDDEN = register_write_tools(
    mcp, intervals, _credentials, settings
)


@mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
async def healthz(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def main() -> None:
    if settings.read_only:
        write_info = "disabled (MCP_READ_ONLY)"
    else:
        write_info = (
            f"{WRITE_TOOLS_ACTIVE} active, {WRITE_TOOLS_HIDDEN} advanced hidden "
            "(MCP_ENABLE_ADVANCED_WRITES=true to enable)"
            if WRITE_TOOLS_HIDDEN
            else f"{WRITE_TOOLS_ACTIVE} active (advanced enabled)"
        )
    logger.info(
        "Starting intervals.icu MCP server on %s:%s%s (issuer=%s, write tools: %s)",
        settings.host,
        settings.port,
        settings.mcp_path,
        settings.oidc_issuer,
        write_info,
    )
    mcp.run(
        transport="streamable-http",
        host=settings.host,
        port=settings.port,
        streamable_http_path=settings.mcp_path,
    )
