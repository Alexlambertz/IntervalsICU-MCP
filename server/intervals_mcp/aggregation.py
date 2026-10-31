"""Reduce per-second intervals.icu streams to fixed-length time blocks.

Raw streams are recorded roughly once per second, so a two-hour ride is several
thousand samples per channel — far more than an LLM needs (or can usefully read)
to reason about a workout. This module buckets the samples into blocks (one
minute by default) and emits one aggregated row per block, plus a few derived
signals (direction of travel, headwind component, watts-per-heartbeat).

Ported from the intervals.icu-activities skill's fetch_streams.py.
"""

from __future__ import annotations

import math
from typing import Any

# Activity-level weather fields worth reporting alongside the blocks.
WEATHER_FIELDS = [
    "has_weather", "average_weather_temp", "min_weather_temp", "max_weather_temp",
    "average_feels_like", "min_feels_like", "max_feels_like", "average_clouds",
    "average_wind_speed", "average_wind_gust", "prevailing_wind_deg",
    "headwind_percent", "tailwind_percent", "max_rain", "max_snow",
]

# Activity-level context fields worth returning with the block data.
CONTEXT_FIELDS = [
    "id", "name", "type", "start_date_local", "distance", "moving_time",
    "elapsed_time", "total_elevation_gain", "icu_training_load", "icu_intensity",
    "icu_ftp", "icu_weighted_avg_watts", "icu_average_watts", "average_heartrate",
    "max_heartrate", "athlete_max_hr", "lthr", "decoupling", "calories",
    "icu_efficiency_factor", "device_name", "trainer",
]


def _mean(values: list[Any]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _peak(values: list[Any]) -> Any:
    vals = [v for v in values if v is not None]
    return max(vals) if vals else None


def _bearing(p1: list[float] | None, p2: list[float] | None) -> float | None:
    """Compass bearing in degrees from point 1 to point 2, both [lat, lon]."""
    if not p1 or not p2:
        return None
    lat1, lon1 = math.radians(p1[0]), math.radians(p1[1])
    lat2, lon2 = math.radians(p2[0]), math.radians(p2[1])
    dlon = lon2 - lon1
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _headwind_component(
    course_deg: float | None, wind_from_deg: float | None, wind_speed: float | None
) -> float | None:
    """Wind component opposing travel in m/s (positive head, negative tail)."""
    if course_deg is None or wind_from_deg is None or wind_speed is None:
        return None
    return wind_speed * math.cos(math.radians(wind_from_deg - course_deg))


def _elevation_gain(altitudes: list[Any]) -> float:
    vals = [a for a in altitudes if a is not None]
    if len(vals) < 2:
        return 0.0
    return sum(max(0.0, b - a) for a, b in zip(vals, vals[1:]))


def _wind_label(hw: float | None) -> str | None:
    if hw is None:
        return None
    if hw > 1.5:
        return "head"
    if hw < -1.5:
        return "tail"
    return "cross"


def _blocks(streams: dict[str, list[Any]], block_seconds: int, weather: dict) -> list[dict]:
    time = streams.get("time") or []
    if not time:
        raise ValueError("Diese Aktivität hat keinen 'time'-Stream (keine aufgezeichneten Rohdaten).")
    n = len(time)

    def chan(name: str) -> list[Any]:
        data = streams.get(name) or []
        return data if len(data) == n else []

    watts = chan("watts")
    hr = chan("heartrate")
    speed = chan("velocity_smooth")
    cadence = chan("cadence")
    altitude = chan("altitude")
    distance = chan("distance")
    temp = chan("temp")
    latlng = chan("latlng")
    torque = chan("torque")

    wind_speed = weather.get("average_wind_speed")
    wind_deg = weather.get("prevailing_wind_deg")

    buckets: dict[int, list[int]] = {}
    for i, t in enumerate(time):
        if t is None:
            continue
        buckets.setdefault(int(t) // block_seconds, []).append(i)

    rows: list[dict] = []
    for block_idx in sorted(buckets):
        idx = buckets[block_idx]
        last = idx[-1]

        course = None
        if latlng:
            fixes = [latlng[i] for i in idx if latlng[i] is not None]
            if len(fixes) >= 2:
                course = _bearing(fixes[0], fixes[-1])

        avg_speed = _mean([speed[i] for i in idx]) if speed else None
        avg_w = _mean([watts[i] for i in idx]) if watts else None
        avg_hr = _mean([hr[i] for i in idx]) if hr else None
        avg_cad = _mean([cadence[i] for i in idx]) if cadence else None
        avg_torque = _mean([torque[i] for i in idx]) if torque else None
        avg_alt = _mean([altitude[i] for i in idx]) if altitude else None
        avg_temp = _mean([temp[i] for i in idx]) if temp else None

        row: dict[str, Any] = {
            "block": block_idx + 1,
            "start_s": block_idx * block_seconds,
            "elapsed": f"{(block_idx * block_seconds) // 60}:{(block_idx * block_seconds) % 60:02d}",
            "samples": len(idx),
            "avg_watts": round(avg_w) if avg_w is not None else None,
            "max_watts": _peak([watts[i] for i in idx]) if watts else None,
            "avg_hr": round(avg_hr) if avg_hr is not None else None,
            "max_hr": _peak([hr[i] for i in idx]) if hr else None,
            "avg_speed_kmh": round(avg_speed * 3.6, 1) if avg_speed is not None else None,
            "avg_cadence": round(avg_cad) if avg_cad is not None else None,
            "avg_torque": round(avg_torque, 1) if avg_torque is not None else None,
            "distance_km": round(distance[last] / 1000, 2) if distance and distance[last] is not None else None,
            "elev_gain_m": round(_elevation_gain([altitude[i] for i in idx]), 1) if altitude else None,
            "avg_altitude_m": round(avg_alt, 1) if avg_alt is not None else None,
            "avg_temp_c": round(avg_temp, 1) if avg_temp is not None else None,
            "course_deg": round(course) if course is not None else None,
        }

        hw = _headwind_component(course, wind_deg, wind_speed)
        row["headwind_ms"] = round(hw, 2) if hw is not None else None
        row["wind_label"] = _wind_label(hw)
        row["w_per_bpm"] = round(avg_w / avg_hr, 2) if avg_w and avg_hr else None

        rows.append(row)

    return rows


def summarise_activity_streams(
    activity: dict, streams: dict[str, list[Any]], block_seconds: int
) -> dict[str, Any]:
    """Build the full time-blocked response for one activity."""
    weather = {k: activity.get(k) for k in WEATHER_FIELDS if activity.get(k) is not None}
    context = {k: activity.get(k) for k in CONTEXT_FIELDS if activity.get(k) is not None}
    blocks = _blocks(streams, block_seconds, weather)
    return {
        "activity": context,
        "weather": weather,
        "block_seconds": block_seconds,
        "available_streams": sorted(streams.keys()),
        "num_blocks": len(blocks),
        "blocks": blocks,
        "notes": (
            "Blocks aggregate per-second samples. Speed is km/h; headwind_ms is the "
            "prevailing-wind component along the direction of travel (positive=head, "
            "negative=tail); w_per_bpm (power/HR) exposes cardiac drift. Weather is "
            "activity-level, not per sample."
        ),
    }
