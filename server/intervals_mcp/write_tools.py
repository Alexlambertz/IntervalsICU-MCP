"""Write tools: create / update / delete operations against intervals.icu.

All tools act on the logged-in athlete only — the athlete id comes from the access
token, never from a tool argument. Bodies are passed through as free-form objects
because the intervals.icu schemas are large (Activity: 184 fields, Athlete: 164);
call ``get_write_schema`` to look up the valid field names, types and enum values.

Registration is skipped entirely when ``MCP_READ_ONLY`` is set, so a read-only
deployment does not even advertise these tools.

Deliberately NOT exposed:
  * chats / messaging (``/chats/*``)    — sends messages to other people
  * ``/athlete-plans``                  — multi-athlete (coach) operation
  * ``/disconnect-app``                 — would revoke the very access this server uses
  * ``download-workout*`` / ``download-fit-files`` — conversions/downloads, not writes
  * ``PUT /athlete/{id}/wellness``      — duplicate of ``update_wellness`` (by date)
"""

from __future__ import annotations

import base64
import binascii
import json
import mimetypes
from datetime import date as _date
from pathlib import Path
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .config import Settings
from .intervals import IntervalsClient, IntervalsError, seg

Fields = dict[str, Any]
Records = list[dict[str, Any]]

_SCHEMA_FILE = Path(__file__).with_name("write_schemas.json")


def _load_schemas() -> dict[str, dict[str, str]]:
    try:
        return json.loads(_SCHEMA_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _iso_date(value: str, label: str = "date") -> str:
    try:
        return _date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ToolError(f"{label} muss ein ISO-Datum sein (YYYY-MM-DD), erhalten: {value!r}") from exc


def _require(fields: Any, what: str = "fields") -> Any:
    if not fields:
        raise ToolError(f"'{what}' darf nicht leer sein.")
    return fields


def register_write_tools(
    mcp: MCPServer,
    intervals: IntervalsClient,
    credentials: Callable[[], tuple[str, str]],
    settings: Settings,
) -> int:
    """Register all write tools on ``mcp``. Returns the number registered."""
    if settings.read_only:
        return 0

    schemas = _load_schemas()
    count = 0

    # ------------------------------------------------------------------ helpers

    def tool(name: str, description: str, kind: str):
        """Register a tool. kind: 'create' | 'update' | 'delete' (drives MCP hints)."""
        nonlocal count
        count += 1
        return mcp.tool(
            name=name,
            description=description,
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=(kind == "delete"),
                idempotent_hint=(kind in {"update", "delete"}),
                open_world_hint=True,
            ),
        )

    async def api(method: str, path: str, **kw: Any) -> Any:
        _athlete_id, api_key = credentials()
        try:
            return await intervals.request(method, path, api_key, **kw)
        except IntervalsError as exc:
            raise ToolError(str(exc)) from exc

    async def ath(method: str, suffix: str, **kw: Any) -> Any:
        """Call an athlete-scoped endpoint: /athlete/{token athlete}{suffix}."""
        athlete_id, api_key = credentials()
        try:
            return await intervals.request(
                method, f"/athlete/{seg(athlete_id)}{suffix}", api_key, **kw
            )
        except IntervalsError as exc:
            raise ToolError(str(exc)) from exc

    def decode_file(content_base64: str) -> bytes:
        try:
            data = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ToolError("content_base64 ist kein gültiges Base64.") from exc
        if not data:
            raise ToolError("Die Datei ist leer.")
        if len(data) > settings.max_upload_bytes:
            raise ToolError(
                f"Datei zu groß ({len(data)} Bytes, Limit {settings.max_upload_bytes})."
            )
        return data

    def file_part(filename: str, data: bytes) -> dict[str, tuple[str, bytes, str]]:
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return {"file": (filename, data, ctype)}

    # ------------------------------------------------------------- schema lookup

    @mcp.tool(
        name="get_write_schema",
        description=(
            "Look up the valid field names, types and enum values for the free-form "
            "`fields`/`events`/`records` objects accepted by the write tools. Call "
            "without `name` to list the available schemas (Activity, EventEx, Wellness, "
            "WorkoutEx, Folder, Gear, GearReminder, SportSettings, AthleteUpdateDTO, "
            "CustomItem, Interval, ...). Pass `name` to get all fields of one schema, "
            "optionally narrowed with `search` (case-insensitive substring of the field "
            "name)."
        ),
        annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
    )
    async def get_write_schema(name: str | None = None, search: str | None = None) -> dict[str, Any]:
        if not schemas:
            raise ToolError("Schema-Referenz nicht verfügbar (write_schemas.json fehlt).")
        if not name:
            return {"available": sorted(schemas), "usage": "get_write_schema(name='Wellness')"}
        key = next((k for k in schemas if k.lower() == name.lower()), None)
        if key is None:
            raise ToolError(f"Unbekanntes Schema {name!r}. Verfügbar: {', '.join(sorted(schemas))}")
        props = schemas[key]
        if search:
            props = {k: v for k, v in props.items() if search.lower() in k.lower()}
        return {"schema": key, "fields": props}

    # ============================================================== ACTIVITIES

    @tool(
        "update_activity",
        "Update fields of an existing activity (name, description, type, gear, tags, "
        "RPE, feel, custom fields, ...). Only the given fields change. See "
        "get_write_schema('Activity') for all fields.",
        "update",
    )
    async def update_activity(activity_id: str, fields: Fields) -> Any:
        return await api("PUT", f"/activity/{seg(activity_id)}", json=_require(fields))

    @tool(
        "delete_activity",
        "PERMANENTLY delete an activity. Cannot be undone — confirm with the user first.",
        "delete",
    )
    async def delete_activity(activity_id: str) -> Any:
        return await api("DELETE", f"/activity/{seg(activity_id)}")

    @tool(
        "delete_activity_tombstone",
        "Delete the tombstone left behind by a deleted activity, so the activity can be "
        "synced/imported again.",
        "delete",
    )
    async def delete_activity_tombstone(activity_id: str) -> Any:
        return await api("DELETE", f"/activity/{seg(activity_id)}/tombstone")

    @tool(
        "create_manual_activity",
        "Create a manual activity (no file), e.g. a workout logged by hand. Needs at "
        "least start_date_local (ISO datetime), type and name; typically also "
        "moving_time (s), distance (m), icu_training_load. See get_write_schema('Activity').",
        "create",
    )
    async def create_manual_activity(fields: Fields) -> Any:
        return await ath("POST", "/activities/manual", json=_require(fields))

    @tool(
        "create_manual_activities_bulk",
        "Create several manual activities in one call. Each item has the same shape as "
        "create_manual_activity.",
        "create",
    )
    async def create_manual_activities_bulk(activities: Records) -> Any:
        return await ath("POST", "/activities/manual/bulk", json=_require(activities, "activities"))

    @tool(
        "upload_activity_file",
        "Create activities from a recorded file (.fit, .tcx, .gpx, or a .zip/.gz of "
        "those). The file content is passed base64-encoded. Duplicates are detected by "
        f"file hash. Max {settings.max_upload_bytes // 1024} KiB decoded. Optional "
        "paired_event_id pairs the activity with a planned workout.",
        "create",
    )
    async def upload_activity_file(
        filename: str,
        content_base64: str,
        name: str | None = None,
        description: str | None = None,
        device_name: str | None = None,
        external_id: str | None = None,
        paired_event_id: int | None = None,
    ) -> Any:
        return await ath(
            "POST",
            "/activities",
            params={
                "name": name,
                "description": description,
                "device_name": device_name,
                "external_id": external_id,
                "paired_event_id": paired_event_id,
            },
            files=file_part(filename, decode_file(content_base64)),
        )

    @tool(
        "replace_activity_file",
        "Replace an activity's original file (.fit/.tcx/.gpx, optionally gzipped or "
        "zipped; base64-encoded) and reprocess it. Name, description, type, gear, tags "
        "etc. are kept. If the file is unchanged the response has activity=null.",
        "update",
    )
    async def replace_activity_file(
        activity_id: str,
        filename: str,
        content_base64: str,
        keep_intervals: bool = True,
        keep_custom_fields: bool = True,
    ) -> Any:
        return await api(
            "POST",
            f"/activity/{seg(activity_id)}/file",
            params={"keep_intervals": keep_intervals, "keep_custom_fields": keep_custom_fields},
            files=file_part(filename, decode_file(content_base64)),
        )

    @tool(
        "add_activity_comment",
        "Add a comment (chat message) to an activity.",
        "create",
    )
    async def add_activity_comment(activity_id: str, content: str) -> Any:
        if not content.strip():
            raise ToolError("Kommentar darf nicht leer sein.")
        return await api("POST", f"/activity/{seg(activity_id)}/messages", json={"content": content})

    @tool(
        "update_activity_streams",
        "Replace/add per-sample streams of an activity from JSON. `streams` is a list of "
        "{type, data, ...} objects (see get_write_schema('ActivityStream')). Use with care: "
        "this rewrites recorded data.",
        "update",
    )
    async def update_activity_streams(activity_id: str, streams: Records) -> Any:
        return await api(
            "PUT", f"/activity/{seg(activity_id)}/streams", json=_require(streams, "streams")
        )

    @tool(
        "update_activity_streams_csv",
        "Update an activity's streams from CSV text (header row with stream names, one "
        "row per sample). Use with care: this rewrites recorded data.",
        "update",
    )
    async def update_activity_streams_csv(activity_id: str, csv_text: str) -> Any:
        data = _require(csv_text, "csv_text").encode()
        if len(data) > settings.max_upload_bytes:
            raise ToolError("CSV zu groß.")
        return await api(
            "PUT",
            f"/activity/{seg(activity_id)}/streams.csv",
            files={"file": ("streams.csv", data, "text/csv")},
        )

    @tool(
        "update_activity_intervals",
        "Update the intervals of an activity. `intervals` is a list of interval objects "
        "(see get_write_schema('Interval')). With replace_all=true all existing intervals "
        "are replaced, otherwise the given ones are merged in.",
        "update",
    )
    async def update_activity_intervals(
        activity_id: str, intervals: Records, replace_all: bool = False
    ) -> Any:
        return await api(
            "PUT",
            f"/activity/{seg(activity_id)}/intervals",
            params={"all": replace_all},
            json=_require(intervals, "intervals"),
        )

    @tool(
        "update_activity_interval",
        "Create or update a single interval (by id) of an activity.",
        "update",
    )
    async def update_activity_interval(activity_id: str, interval_id: int, fields: Fields) -> Any:
        return await api(
            "PUT",
            f"/activity/{seg(activity_id)}/intervals/{seg(interval_id)}",
            json=_require(fields),
        )

    @tool(
        "delete_activity_intervals",
        "Delete the given intervals from an activity. `intervals` is a list of interval "
        "objects identified by their id.",
        "delete",
    )
    async def delete_activity_intervals(activity_id: str, intervals: Records) -> Any:
        return await api(
            "PUT",
            f"/activity/{seg(activity_id)}/delete-intervals",
            json=_require(intervals, "intervals"),
        )

    @tool(
        "split_activity_interval",
        "Split an interval of an activity at the given index.",
        "update",
    )
    async def split_activity_interval(activity_id: str, split_at: int) -> Any:
        return await api(
            "PUT", f"/activity/{seg(activity_id)}/split-interval", params={"splitAt": split_at}
        )

    # ================================================================== EVENTS

    @tool(
        "create_event",
        "Create a calendar event: planned workout, note, race, holiday/sick/injured "
        "marker, target, etc. Needs start_date_local (ISO datetime, e.g. "
        "2026-11-20T00:00:00), category (WORKOUT, RACE_A/B/C, NOTE, PLAN, HOLIDAY, SICK, "
        "INJURED, SET_EFTP, FITNESS_DAYS, SEASON_START, TARGET, SET_FITNESS) and name; "
        "for workouts also type (Ride, Run, ...). Workout steps can be written in "
        "`description` using intervals.icu workout text syntax, or supplied as a "
        ".zwo/.mrc/.erg/.fit file via file_contents(_base64)+filename. See "
        "get_write_schema('EventEx'). With upsert_on_uid=true an existing event with the "
        "same uid is updated instead.",
        "create",
    )
    async def create_event(fields: Fields, upsert_on_uid: bool = False) -> Any:
        return await ath(
            "POST", "/events", params={"upsertOnUid": upsert_on_uid}, json=_require(fields)
        )

    @tool(
        "create_events_bulk",
        "Create many calendar events at once (e.g. a training block). Each item has the "
        "shape of create_event. upsert=true updates events with matching external_id "
        "instead of creating duplicates; upsert_on_uid does the same by uid.",
        "create",
    )
    async def create_events_bulk(
        events: Records,
        upsert: bool = False,
        upsert_on_uid: bool = False,
        update_plan_applied: bool = False,
    ) -> Any:
        return await ath(
            "POST",
            "/events/bulk",
            params={
                "upsert": upsert,
                "upsertOnUid": upsert_on_uid,
                "updatePlanApplied": update_plan_applied,
            },
            json=_require(events, "events"),
        )

    @tool(
        "update_event",
        "Update fields of one calendar event (move it to another date, rename, change "
        "the workout, ...). Only the given fields change.",
        "update",
    )
    async def update_event(event_id: int, fields: Fields) -> Any:
        return await ath("PUT", f"/events/{seg(event_id)}", json=_require(fields))

    @tool(
        "update_events_in_range",
        "Apply the same field changes to ALL events between two dates (inclusive). "
        "Affects many records — double-check the range first with list_events.",
        "delete",
    )
    async def update_events_in_range(oldest: str, newest: str, fields: Fields) -> Any:
        return await ath(
            "PUT",
            "/events",
            params={"oldest": _iso_date(oldest, "oldest"), "newest": _iso_date(newest, "newest")},
            json=_require(fields),
        )

    @tool(
        "delete_event",
        "Delete one calendar event. With others=true, other events that were added at "
        "the same time (e.g. via a plan) are deleted too; not_before (ISO date) protects "
        "earlier events from that.",
        "delete",
    )
    async def delete_event(
        event_id: int, others: bool = False, not_before: str | None = None
    ) -> Any:
        return await ath(
            "DELETE",
            f"/events/{seg(event_id)}",
            params={
                "others": others,
                "notBefore": _iso_date(not_before, "not_before") if not_before else None,
            },
        )

    @tool(
        "delete_events_in_range",
        "Delete ALL events between two dates (inclusive), optionally only certain "
        "categories (e.g. ['WORKOUT']) or only those created by one athlete. Both dates "
        "are mandatory here on purpose. Cannot be undone — preview with list_events and "
        "confirm with the user first.",
        "delete",
    )
    async def delete_events_in_range(
        oldest: str,
        newest: str,
        categories: list[str] | None = None,
        created_by_id: str | None = None,
    ) -> Any:
        return await ath(
            "DELETE",
            "/events",
            params={
                "oldest": _iso_date(oldest, "oldest"),
                "newest": _iso_date(newest, "newest"),
                "category": ",".join(categories) if categories else None,
                "createdById": created_by_id,
            },
        )

    @tool(
        "delete_events_bulk",
        "Delete specific events by id or external_id. `events` is a list of "
        "{id} or {external_id} objects.",
        "delete",
    )
    async def delete_events_bulk(events: Records) -> Any:
        return await ath("PUT", "/events/bulk-delete", json=_require(events, "events"))

    @tool(
        "mark_event_done",
        "Mark a planned workout as done: creates a manual activity matching the event.",
        "create",
    )
    async def mark_event_done(event_id: int) -> Any:
        return await ath("POST", f"/events/{seg(event_id)}/mark-done")

    @tool(
        "duplicate_events",
        "Duplicate calendar events: {eventIds: [...], numCopies: n, weeksBetween: n}.",
        "create",
    )
    async def duplicate_events(fields: Fields) -> Any:
        return await ath("POST", "/duplicate-events", json=_require(fields))

    @tool(
        "apply_plan",
        "Apply a training plan (a library folder) to the calendar: "
        "{start_date_local: 'YYYY-MM-DD', folder_id: n, extra_workouts?: [...]}.",
        "create",
    )
    async def apply_plan(fields: Fields) -> Any:
        return await ath("POST", "/events/apply-plan", json=_require(fields))

    @tool(
        "apply_plan_changes",
        "Apply the current changes of the athlete's training plan to the calendar.",
        "update",
    )
    async def apply_plan_changes() -> Any:
        return await ath("PUT", "/apply-plan-changes")

    # ================================================================ WELLNESS

    @tool(
        "update_wellness",
        "Create or update the wellness record of one day (date = YYYY-MM-DD). Typical "
        "fields: weight, restingHR, hrv, sleepSecs, sleepScore, sleepQuality, soreness, "
        "fatigue, stress, mood, motivation, readiness, steps, comments, kcalConsumed, "
        "bodyFat, vo2max, spO2, systolic, diastolic. See get_write_schema('Wellness').",
        "update",
    )
    async def update_wellness(date: str, fields: Fields) -> Any:
        return await ath("PUT", f"/wellness/{_iso_date(date)}", json=_require(fields))

    @tool(
        "update_wellness_bulk",
        "Create or update several wellness records at once. Each record needs `id` = "
        "its date (YYYY-MM-DD) plus the fields to set.",
        "update",
    )
    async def update_wellness_bulk(records: Records) -> Any:
        for r in _require(records, "records"):
            _iso_date(str(r.get("id", "")), "records[].id")
        return await ath("PUT", "/wellness-bulk", json=records)

    @tool(
        "upload_wellness_csv",
        "Upload wellness records as CSV text (header row with field names; one row per "
        "day, first column the date).",
        "create",
    )
    async def upload_wellness_csv(csv_text: str, ignore_missing_fields: bool = False) -> Any:
        data = _require(csv_text, "csv_text").encode()
        if len(data) > settings.max_upload_bytes:
            raise ToolError("CSV zu groß.")
        return await ath(
            "POST",
            "/wellness",
            params={"ignoreMissingFields": ignore_missing_fields},
            files={"file": ("wellness.csv", data, "text/csv")},
        )

    # ================================================================ WORKOUTS

    @tool(
        "create_workout",
        "Create a workout in the workout library (a folder or plan). Needs folder_id, "
        "name, type, and the steps in `description` (workout text syntax) or a file via "
        "file_contents(_base64)+filename. See get_write_schema('WorkoutEx').",
        "create",
    )
    async def create_workout(fields: Fields) -> Any:
        return await ath("POST", "/workouts", json=_require(fields))

    @tool(
        "create_workouts_bulk",
        "Create several library workouts at once. Each item has the shape of "
        "create_workout.",
        "create",
    )
    async def create_workouts_bulk(workouts: Records) -> Any:
        return await ath("POST", "/workouts/bulk", json=_require(workouts, "workouts"))

    @tool(
        "update_workout",
        "Update a library workout. Only the given fields change.",
        "update",
    )
    async def update_workout(workout_id: int, fields: Fields) -> Any:
        return await ath("PUT", f"/workouts/{seg(workout_id)}", json=_require(fields))

    @tool(
        "delete_workout",
        "Delete a library workout; others=true also deletes workouts added at the same "
        "time to a plan. Returns the ids of the deleted workouts.",
        "delete",
    )
    async def delete_workout(workout_id: int, others: bool = False) -> Any:
        return await ath("DELETE", f"/workouts/{seg(workout_id)}", params={"others": others})

    @tool(
        "update_plan_workouts",
        "Update a range of workouts on a plan (folder). Currently only "
        "hide_from_athlete can be changed. `oldest`/`newest` are the plan day numbers.",
        "update",
    )
    async def update_plan_workouts(folder_id: int, oldest: int, newest: int, fields: Fields) -> Any:
        return await ath(
            "PUT",
            f"/folders/{seg(folder_id)}/workouts",
            params={"oldest": oldest, "newest": newest},
            json=_require(fields),
        )

    @tool(
        "duplicate_workouts",
        "Duplicate library workouts on a plan: {workoutIds: [...], numCopies: n, "
        "weeksBetween: n}.",
        "create",
    )
    async def duplicate_workouts(fields: Fields) -> Any:
        return await ath("POST", "/duplicate-workouts", json=_require(fields))

    @tool(
        "import_workout_file",
        "Import a workout from a .zwo, .mrc, .erg or .fit file (base64-encoded) into a "
        "library folder. `file_type` is the file extension, e.g. 'zwo'.",
        "create",
    )
    async def import_workout_file(
        folder_id: int, file_type: str, filename: str, content_base64: str
    ) -> Any:
        return await ath(
            "POST",
            f"/folders/{seg(folder_id)}/import-workout",
            params={"type": file_type},
            files=file_part(filename, decode_file(content_base64)),
        )

    # ================================================================= FOLDERS

    @tool(
        "create_folder",
        "Create a workout library folder or a training plan. See "
        "get_write_schema('CreateFolderDTO').",
        "create",
    )
    async def create_folder(fields: Fields) -> Any:
        return await ath("POST", "/folders", json=_require(fields))

    @tool("update_folder", "Update a folder or plan (name, description, ...).", "update")
    async def update_folder(folder_id: int, fields: Fields) -> Any:
        return await ath("PUT", f"/folders/{seg(folder_id)}", json=_require(fields))

    @tool(
        "delete_folder",
        "PERMANENTLY delete a folder or plan INCLUDING ALL ITS WORKOUTS. Cannot be "
        "undone — confirm with the user first.",
        "delete",
    )
    async def delete_folder(folder_id: int) -> Any:
        return await ath("DELETE", f"/folders/{seg(folder_id)}")

    @tool(
        "update_folder_shared_with",
        "Set the list of athletes a folder is shared with. `shared_with` is a list of "
        "{id, canEdit} objects.",
        "update",
    )
    async def update_folder_shared_with(folder_id: int, shared_with: Records) -> Any:
        return await ath("PUT", f"/folders/{seg(folder_id)}/shared-with", json=shared_with)

    @tool("share_folder_with_me", "Share a folder with the calling athlete.", "update")
    async def share_folder_with_me(folder_id: int) -> Any:
        return await ath("POST", f"/folders/{seg(folder_id)}/share-with-me")

    # ==================================================================== GEAR

    @tool(
        "create_gear",
        "Create a piece of gear (bike, shoes, ...) or a component. See "
        "get_write_schema('Gear').",
        "create",
    )
    async def create_gear(fields: Fields) -> Any:
        return await ath("POST", "/gear", json=_require(fields))

    @tool("update_gear", "Update gear or a component.", "update")
    async def update_gear(gear_id: str, fields: Fields) -> Any:
        return await ath("PUT", f"/gear/{seg(gear_id)}", json=_require(fields))

    @tool("delete_gear", "Delete gear or a component. Cannot be undone.", "delete")
    async def delete_gear(gear_id: str) -> Any:
        return await ath("DELETE", f"/gear/{seg(gear_id)}")

    @tool(
        "replace_gear",
        "Retire a component and replace it with a copy that keeps the same reminders. "
        "`fields` describes the new component.",
        "create",
    )
    async def replace_gear(gear_id: str, fields: Fields) -> Any:
        return await ath("POST", f"/gear/{seg(gear_id)}/replace", json=_require(fields))

    @tool(
        "create_gear_reminder",
        "Create a maintenance reminder for a piece of gear. See "
        "get_write_schema('GearReminder').",
        "create",
    )
    async def create_gear_reminder(gear_id: str, fields: Fields) -> Any:
        return await ath("POST", f"/gear/{seg(gear_id)}/reminder", json=_require(fields))

    @tool(
        "update_gear_reminder",
        "Update a gear reminder. reset=true marks the maintenance as done (resets the "
        "counter); snooze_days postpones it.",
        "update",
    )
    async def update_gear_reminder(
        gear_id: str,
        reminder_id: str,
        fields: Fields,
        reset: bool = False,
        snooze_days: int = 0,
    ) -> Any:
        return await ath(
            "PUT",
            f"/gear/{seg(gear_id)}/reminder/{seg(reminder_id)}",
            params={"reset": reset, "snoozeDays": snooze_days},
            json=fields,
        )

    @tool("delete_gear_reminder", "Delete a gear reminder.", "delete")
    async def delete_gear_reminder(gear_id: str, reminder_id: str) -> Any:
        return await ath("DELETE", f"/gear/{seg(gear_id)}/reminder/{seg(reminder_id)}")

    # ================================================ ATHLETE / SETTINGS / ROUTES

    @tool(
        "update_athlete",
        "Update the athlete's profile and settings (name, weight, units, thresholds, "
        "preferences, ...). Only the given fields change. See "
        "get_write_schema('AthleteUpdateDTO').",
        "update",
    )
    async def update_athlete(fields: Fields) -> Any:
        return await ath("PUT", "", json=_require(fields))

    @tool(
        "create_sport_settings",
        "Create sport settings (FTP, LTHR, zones, ... for a sport) with defaults. See "
        "get_write_schema('SportSettings').",
        "create",
    )
    async def create_sport_settings(fields: Fields) -> Any:
        return await ath("POST", "/sport-settings", json=_require(fields))

    @tool(
        "update_sport_settings",
        "Update the settings of one sport, addressed by settings id or activity type "
        "(e.g. 'Ride', 'Run'): FTP, LTHR, max HR, zones, ... recalc_hr_zones=true "
        "recomputes HR zones from the new thresholds.",
        "update",
    )
    async def update_sport_settings(
        settings_id: str, fields: Fields, recalc_hr_zones: bool = False
    ) -> Any:
        return await ath(
            "PUT",
            f"/sport-settings/{seg(settings_id)}",
            params={"recalcHrZones": recalc_hr_zones},
            json=_require(fields),
        )

    @tool(
        "update_sport_settings_multi",
        "Update the settings of several sports at once (list of SportSettings objects, "
        "each with its id).",
        "update",
    )
    async def update_sport_settings_multi(
        settings_list: Records, recalc_hr_zones: bool = False
    ) -> Any:
        return await ath(
            "PUT",
            "/sport-settings",
            params={"recalcHrZones": recalc_hr_zones},
            json=_require(settings_list, "settings_list"),
        )

    @tool("delete_sport_settings", "Delete the settings of one sport.", "delete")
    async def delete_sport_settings(settings_id: str) -> Any:
        return await ath("DELETE", f"/sport-settings/{seg(settings_id)}")

    @tool(
        "update_weather_config",
        "Update the weather forecast settings: {forecasts: [...]}.",
        "update",
    )
    async def update_weather_config(fields: Fields) -> Any:
        return await ath("PUT", "/weather-config", json=_require(fields))

    @tool(
        "update_training_plan",
        "Change the athlete's training plan: {training_plan_id, training_plan_start_date, "
        "training_plan_alias}.",
        "update",
    )
    async def update_training_plan(fields: Fields) -> Any:
        return await ath("PUT", "/training-plan", json=_require(fields))

    @tool("update_route", "Update a saved route (name, description, ...).", "update")
    async def update_route(route_id: int, fields: Fields) -> Any:
        return await ath("PUT", f"/routes/{seg(route_id)}", json=_require(fields))

    # ============================================================ CUSTOM ITEMS

    @tool(
        "create_custom_item",
        "Create a custom item (custom chart, field, zone set, ...). See "
        "get_write_schema('CustomItem').",
        "create",
    )
    async def create_custom_item(fields: Fields) -> Any:
        return await ath("POST", "/custom-item", json=_require(fields))

    @tool("update_custom_item", "Update a custom item.", "update")
    async def update_custom_item(item_id: int, fields: Fields) -> Any:
        return await ath("PUT", f"/custom-item/{seg(item_id)}", json=_require(fields))

    @tool("delete_custom_item", "Delete a custom item.", "delete")
    async def delete_custom_item(item_id: int) -> Any:
        return await ath("DELETE", f"/custom-item/{seg(item_id)}")

    @tool(
        "reorder_custom_items",
        "Reorder custom items: pass the items (with their ids) in the desired order.",
        "update",
    )
    async def reorder_custom_items(items: Records) -> Any:
        return await ath("PUT", "/custom-item-indexes", json=_require(items, "items"))

    @tool(
        "upload_custom_item_image",
        "Upload an image (base64-encoded) for a custom item.",
        "update",
    )
    async def upload_custom_item_image(
        item_id: int, filename: str, content_base64: str
    ) -> Any:
        return await ath(
            "POST",
            f"/custom-item/{seg(item_id)}/image",
            files=file_part(filename, decode_file(content_base64)),
        )

    return count
