#!/usr/bin/env python3
"""Generate server/intervals_mcp/write_schemas.json from the intervals.icu OpenAPI spec.

The write tools accept free-form field objects (the Activity / Athlete schemas have
well over 100 fields each). This script distils the official OpenAPI request-body
schemas into a compact field reference that the `get_write_schema` tool serves to
the model, so it can discover valid field names, types and enum values.

Usage:
    python scripts/gen_write_schemas.py                 # download the live spec
    python scripts/gen_write_schemas.py path/to/spec.json
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

SPEC_URL = "https://intervals.icu/api/v1/docs"
OUT = Path(__file__).resolve().parent.parent / "server" / "intervals_mcp" / "write_schemas.json"

# Schemas used as write request bodies (keys are what get_write_schema accepts).
SCHEMAS = [
    "Activity", "Interval", "EventEx", "Event", "Wellness", "WorkoutEx", "Workout",
    "Folder", "CreateFolderDTO", "SharedWith", "Gear", "GearReminder", "SportSettings",
    "AthleteUpdateDTO", "CustomItem", "WeatherConfig", "AthleteRoute",
    "AthleteTrainingPlanUpdate", "ActivityStream", "DuplicateEventsDTO",
    "DuplicateWorkoutsDTO", "ApplyPlanDTO", "DoomedEvent", "NewActivityMsg",
]


def describe(prop: dict) -> str:
    if "$ref" in prop:
        return prop["$ref"].rsplit("/", 1)[-1]
    t = prop.get("type", "any")
    if t == "array":
        items = prop.get("items", {})
        inner = items["$ref"].rsplit("/", 1)[-1] if "$ref" in items else items.get("type", "any")
        t = f"array<{inner}>"
    if prop.get("format"):
        t += f":{prop['format']}"
    if prop.get("enum"):
        t += " enum(" + "|".join(str(e) for e in prop["enum"]) + ")"
    desc = (prop.get("description") or "").strip().replace("\n", " ")
    if desc:
        t += " - " + desc[:160]
    return t


def main() -> None:
    if len(sys.argv) > 1:
        spec = json.loads(Path(sys.argv[1]).read_text())
    else:
        req = urllib.request.Request(SPEC_URL, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            spec = json.loads(resp.read())

    comps = spec["components"]["schemas"]
    out = {}
    for name in SCHEMAS:
        schema = comps.get(name)
        if not schema:
            print(f"warning: schema {name} not found", file=sys.stderr)
            continue
        out[name] = {k: describe(v) for k, v in schema.get("properties", {}).items()}

    OUT.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KiB, {len(out)} schemas)")


if __name__ == "__main__":
    main()
