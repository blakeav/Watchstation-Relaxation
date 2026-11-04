#!/usr/bin/env python3
"""Analyze radar performance across a Carrier Strike Group.

By default this runs a live scenario: every 4 seconds the simulated clock
advances, the matching rows are read from snooze_cruise.csv, and the map
is redrawn. The monitor stops on its own after 80 seconds. Pass --once to
analyze a pair of CSV files a single time instead.

Example usage
-------------
Live monitor (stops after 80 seconds, or sooner on Ctrl+C). Change
UPDATE_INTERVAL_SECONDS and TOTAL_RUNTIME_SECONDS below to adjust the loop.
Edit snooze_cruise.csv to move the ships or change when radars degrade.

    python3 analyze_radar.py
    python3 analyze_radar.py --interval 4 --runtime 80

snooze_cruise.csv is the only live data file. One row per ship per time
step. It holds positions, radar metrics, and the capacity schedule:

    time_seconds, timestamp, ship_id, ship_name, ship_type
    latitude, longitude, current_heading
    radar_status, capacity_percent
    full_range_km, max_range_km, effective_range_km
    num_detections, avg_snr_db
    elevation_coverage_deg, azimuth_coverage_deg
    track_quality, radar_mode
    threat_bearing_deg
    threat_range_km, threat_priority   (optional)
    power_status, cooling_status, last_maintenance, fault_code

Latitude, longitude, and threat_bearing_deg change with time_seconds.
capacity_percent in the same file is the outage schedule (for example 60
at T+12 s, 0 at T+32 s, 40 at T+52 s). The loop picks or interpolates the
rows for the current clock. Courses cover the threat axis from stations
at least 25° off that axis, so no ship is steered onto the threat vector.
Recommendations are still produced when every radar is at 100%.

One-shot CSV analysis:

    python3 analyze_radar.py --once radar_performance.csv ship_locations.csv

radar_performance.csv columns:
    timestamp, ship_id, radar_status, num_detections, max_range_km, avg_snr_db
    capacity_percent   (optional, 0–100; derived from radar_status when absent)

radar_status may be OK, DEGRADED, FAILED, or a number from 0 to 100.

ship_locations.csv columns:
    ship_id, ship_name, latitude, longitude, ship_type
    current_heading   (optional, degrees true)

The live scenario also writes live_ship_state.csv on every update.
Figures overwritten in the working directory:

    strike_group_radar_status.png       ships, three course options, and two corner notes
    strike_group_recommended_courses.png sectors for the top option (only if capacity was lost)
"""

from __future__ import annotations

import argparse
import itertools
import sys
import time
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.patches import FancyArrowPatch


RADAR_COLUMNS = [
    "timestamp",
    "ship_id",
    "radar_status",
    "num_detections",
    "max_range_km",
    "avg_snr_db",
]
LOCATION_COLUMNS = [
    "ship_id",
    "ship_name",
    "latitude",
    "longitude",
    "ship_type",
]
STATUS_FIGURE = Path("strike_group_radar_status.png")
COURSE_FIGURE = Path("strike_group_recommended_courses.png")
LIVE_STATE_FIGURE = Path("live_ship_state.csv")
SNOOZE_FILE = Path("snooze_cruise.csv")
TIMELINE_FILE = Path("radar_timeline.csv")
TIMELINE_COLUMNS = ["time_seconds", "ship_id", "new_capacity_percent"]

# Wall-clock pause between map updates. The simulated clock advances by the
# same amount, so the timeline below is in scenario seconds.
UPDATE_INTERVAL_SECONDS = 4.0
# The live monitor exits after this many seconds (20 updates at a 4 s interval).
TOTAL_RUNTIME_SECONDS = 80.0
SCENARIO_START = pd.Timestamp("2026-11-04T12:00:00Z")

# Full-capability baselines. Effective range, detections, and SNR scale down
# with capacity_percent as the scenario clock advances.
FLEET = [
    {
        "ship_id": "CVN-78",
        "ship_name": "USS Gerald R. Ford",
        "ship_type": "Carrier",
        "latitude": 36.8500,
        "longitude": -76.2900,
        "current_heading": 90.0,
        "full_range_km": 255.0,
        "num_detections": 124.0,
        "avg_snr_db": 18.6,
        "elevation_coverage_deg": 80.0,
        "azimuth_coverage_deg": 360.0,
    },
    {
        "ship_id": "CG-58",
        "ship_name": "USS Philippine Sea",
        "ship_type": "Cruiser",
        "latitude": 36.8700,
        "longitude": -76.2500,
        "current_heading": 45.0,
        "full_range_km": 186.0,
        "num_detections": 98.0,
        "avg_snr_db": 16.6,
        "elevation_coverage_deg": 75.0,
        "azimuth_coverage_deg": 360.0,
    },
    {
        "ship_id": "DDG-101",
        "ship_name": "USS Gridley",
        "ship_type": "Destroyer",
        "latitude": 36.8300,
        "longitude": -76.3200,
        "current_heading": 220.0,
        "full_range_km": 155.0,
        "num_detections": 82.0,
        "avg_snr_db": 15.2,
        "elevation_coverage_deg": 70.0,
        "azimuth_coverage_deg": 360.0,
    },
    {
        "ship_id": "DDG-112",
        "ship_name": "USS Michael Murphy",
        "ship_type": "Destroyer",
        "latitude": 36.8900,
        "longitude": -76.3100,
        "current_heading": 330.0,
        "full_range_km": 164.0,
        "num_detections": 91.0,
        "avg_snr_db": 16.1,
        "elevation_coverage_deg": 70.0,
        "azimuth_coverage_deg": 360.0,
    },
]

OPERATIONAL_COLOR = "#4C78A8"
PARTIAL_COLOR = "#F4A261"
OUTAGE_COLOR = "#E45756"
# Below this remaining capacity a ship is drawn as a severe loss.
SEVERE_CAPACITY = 30.0
OPTION_COLORS = ["#2CA02C", "#FF7F0E", "#9467BD"]
OPTION_STYLES = ["solid", "dashed", "dotted"]
RANK_LABELS = ["Best", "Acceptable", "Fallback"]

# Inner ring is the close screen around the high-value unit.
# The outer ring is set from the longest healthy radar so station position
# still changes the result. Both are sampled every 5 degrees.
INNER_RANGE_KM = 40.0
# Stations and courses must stay at least this far from the threat bearing.
THREAT_EXCLUSION_DEG = 25.0
# Coverage score emphasizes this half-width on either side of the threat axis.
THREAT_FOCUS_HALF_DEG = 45.0
AZIMUTH_BINS = 72
KM_PER_DEG_LAT = 110.574

# Defaults used only when a healthy ship has no numeric max_range_km.
DEFAULT_RANGE_KM = {
    "carrier": 250.0,
    "cruiser": 185.0,
    "destroyer": 160.0,
    "frigate": 120.0,
}


def load_csv(path: Path, required: list[str], label: str) -> pd.DataFrame:
    """Read a CSV and require the columns the rest of the script uses."""
    if not path.is_file():
        raise FileNotFoundError(f"{label} file not found: {path}")

    frame = pd.read_csv(path)
    missing = [name for name in required if name not in frame.columns]
    if missing:
        joined = ", ".join(missing)
        raise ValueError(f"{label} file {path} is missing columns: {joined}")
    frame["ship_id"] = frame["ship_id"].astype(str).str.strip()
    return frame


def _status_capacity(status: object) -> float | None:
    """Map a status token to a capacity percent. None means 'derive it'."""
    text = str(status).strip().upper()
    if text in {"OK", "OPERATIONAL", "FULL"}:
        return 100.0
    if text in {"FAILED", "FAIL", "DOWN", "OUTAGE"}:
        return 0.0
    if text in {"DEGRADED", "DEGRADE", "PARTIAL"}:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return float(np.clip(value, 0.0, 100.0))


def _metric_ratio(current: float, baseline: float) -> float | None:
    """How the current reading compares with this ship's own full reading."""
    if not np.isfinite(baseline) or baseline <= 0 or not np.isfinite(current):
        return None
    return float(np.clip(current / baseline, 0.0, 1.0))


def _derived_capacity(row: pd.Series, baseline: dict[str, float]) -> float:
    """Estimate a DEGRADED row from drops in detections, range, and SNR."""
    ratios = []
    for column in ("num_detections", "max_range_km", "avg_snr_db"):
        ratio = _metric_ratio(float(row[column]), baseline[column])
        if ratio is not None:
            ratios.append(ratio)
    if not ratios:
        return 60.0
    # Keep a derived value off the two endpoints so it stays "partial".
    return float(np.clip(float(np.mean(ratios)) * 100.0, 5.0, 95.0))


def _row_capacity(row: pd.Series, baseline: dict[str, float], has_capacity: bool) -> float:
    """Capacity for one performance row. An explicit column wins."""
    if has_capacity and pd.notna(row["capacity_percent"]):
        return float(np.clip(float(row["capacity_percent"]), 0.0, 100.0))
    mapped = _status_capacity(row["radar_status"])
    if mapped is not None:
        return mapped
    return _derived_capacity(row, baseline)


def assess_capacity(radar: pd.DataFrame, locations: pd.DataFrame) -> pd.DataFrame:
    """One row per ship: full range, remaining capacity, and when the loss began.

    Full range is the best max_range_km that ship has reported (its OK baseline
    when it has one). Effective range is that full range times remaining capacity.
    """
    frame = radar.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, format="mixed")
    for column in ("num_detections", "max_range_km", "avg_snr_db"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    has_capacity = "capacity_percent" in frame.columns
    if has_capacity:
        frame["capacity_percent"] = pd.to_numeric(frame["capacity_percent"], errors="coerce")

    located = _prepare_locations(locations).drop_duplicates("ship_id").set_index("ship_id")
    records = []
    for ship_id, group in frame.groupby("ship_id", sort=True):
        group = group.sort_values("timestamp")
        ok_rows = group.loc[group["radar_status"].astype(str).str.strip().str.upper().eq("OK")]
        reference = ok_rows if not ok_rows.empty else group
        baseline = {
            column: float(reference[column].max(skipna=True))
            for column in ("num_detections", "max_range_km", "avg_snr_db")
        }
        if np.isfinite(baseline["max_range_km"]) and baseline["max_range_km"] > 0:
            full_range = baseline["max_range_km"]
        elif ship_id in located.index:
            full_range = default_range_km(located.loc[ship_id, "ship_type"])
        else:
            full_range = 100.0

        capacities = group.apply(lambda row: _row_capacity(row, baseline, has_capacity), axis=1)
        current = float(capacities.iloc[-1])
        reduced = group.loc[capacities.lt(99.5)]
        began = reduced["timestamp"].min() if not reduced.empty else pd.NaT
        records.append(
            {
                "ship_id": str(ship_id),
                "capacity_percent": current,
                "percent_lost": 100.0 - current,
                "full_range_km": full_range,
                "effective_range_km": full_range * current / 100.0,
                "loss_began": began,
            }
        )
    return pd.DataFrame.from_records(records)


def attach_locations(capacity: pd.DataFrame, locations: pd.DataFrame) -> pd.DataFrame:
    """Join capacity to the ship's current location, keeping unmatched ships."""
    places = _prepare_locations(locations)
    return capacity.merge(places, on="ship_id", how="left")


def _prepare_locations(locations: pd.DataFrame) -> pd.DataFrame:
    """Coerce coordinates and the optional heading column."""
    places = locations.copy()
    places["latitude"] = pd.to_numeric(places["latitude"], errors="coerce")
    places["longitude"] = pd.to_numeric(places["longitude"], errors="coerce")
    if "current_heading" in places.columns:
        places["current_heading"] = pd.to_numeric(places["current_heading"], errors="coerce")
    else:
        places["current_heading"] = np.nan
    return places


def format_time(value: object) -> str:
    """Format a timestamp for the text summary."""
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    return stamp.strftime("%Y-%m-%d %H:%M:%S UTC")


def print_capacity(report: pd.DataFrame) -> None:
    """Print every ship's full capacity against what it has left."""
    print("Radar capacity")
    print(f"{'Ship':<10}{'Full':>8}{'Current':>10}{'Lost':>8}{'Full range':>12}{'Effective':>12}")
    for row in report.sort_values("ship_id").itertuples(index=False):
        print(
            f"{row.ship_id:<10}"
            f"{'100%':>8}"
            f"{row.capacity_percent:>9.0f}%"
            f"{row.percent_lost:>7.0f}%"
            f"{row.full_range_km:>10.0f} km"
            f"{row.effective_range_km:>9.0f} km"
        )

    affected = report.loc[report["capacity_percent"] < 99.5]
    if affected.empty:
        print()
        print("All radars are fully operational.")
        return

    print()
    print(f"Ships below full capacity: {len(affected)}")
    for row in affected.sort_values("loss_began").itertuples(index=False):
        name = row.ship_name if pd.notna(row.ship_name) else "unknown"
        ship_type = row.ship_type if pd.notna(row.ship_type) else "unknown"
        if pd.notna(row.latitude) and pd.notna(row.longitude):
            position = f"{row.latitude:.4f}, {row.longitude:.4f}"
        else:
            position = "unknown (ship_id not in the location file)"
        began = format_time(row.loss_began) if pd.notna(row.loss_began) else "unknown"

        print()
        print(f"Ship ID:       {row.ship_id}")
        print(f"Ship name:     {name}")
        print(f"Ship type:     {ship_type}")
        print(f"Loss began:    {began}")
        print(f"Position:      {position}")
        print(f"Remaining:     {row.capacity_percent:.0f}%")
        print(f"Lost:          {row.percent_lost:.0f}%")


def default_range_km(ship_type: object) -> float:
    """Fall back to a range by ship type when the CSV has no usable value."""
    text = str(ship_type).strip().lower()
    for key, distance in DEFAULT_RANGE_KM.items():
        if key in text:
            return distance
    return 100.0


def effective_ranges(report: pd.DataFrame) -> dict[str, float]:
    """Radar reach after scaling each ship's full range by remaining capacity."""
    return {
        str(row.ship_id): float(row.effective_range_km)
        for row in report.itertuples(index=False)
    }


def km_per_deg_lon(latitude: float) -> float:
    """East-west kilometers per degree of longitude at this latitude."""
    return 111.320 * float(np.cos(np.deg2rad(latitude)))


def to_xy(latitude: float, longitude: float, origin_lat: float, origin_lon: float) -> tuple[float, float]:
    """Local east/north kilometers from an origin. Fine for a few dozen kilometers."""
    east = (longitude - origin_lon) * km_per_deg_lon(origin_lat)
    north = (latitude - origin_lat) * KM_PER_DEG_LAT
    return float(east), float(north)


def to_latlon(east_km: float, north_km: float, origin_lat: float, origin_lon: float) -> tuple[float, float]:
    """Inverse of to_xy. Returns latitude, longitude."""
    latitude = origin_lat + north_km / KM_PER_DEG_LAT
    longitude = origin_lon + east_km / km_per_deg_lon(origin_lat)
    return float(latitude), float(longitude)


def bearing_deg(east_km: float, north_km: float) -> float:
    """True bearing, degrees clockwise from north."""
    return float(np.rad2deg(np.arctan2(east_km, north_km)) % 360.0)


def wrap_delta(left: float, right: float) -> float:
    """Smallest angle between two bearings, in degrees."""
    delta = abs(left - right) % 360.0
    return min(delta, 360.0 - delta)


def even_bearings(count: int, offset: float) -> list[float]:
    """count stations spaced evenly around the circle, starting at offset."""
    if count <= 0:
        return []
    step = 360.0 / count
    return [(offset + index * step) % 360.0 for index in range(count)]


def circular_mean(bearings: list[float]) -> float:
    """Mean of angles, so 350° and 10° average to north rather than 180°."""
    radians = np.deg2rad(bearings)
    east = np.sin(radians).mean()
    north = np.cos(radians).mean()
    return bearing_deg(float(east), float(north))


def point_at(bearing: float, distance_km: float) -> tuple[float, float]:
    """East/north offset for a true bearing and range."""
    radians = np.deg2rad(bearing)
    return distance_km * float(np.sin(radians)), distance_km * float(np.cos(radians))


def gap_text(mask: np.ndarray) -> str:
    """Describe uncovered azimuth bins, including a gap that crosses north."""
    covered = np.asarray(mask, dtype=bool)
    count = len(covered)
    if count == 0 or covered.all():
        return "none"
    if not covered.any():
        return "all azimuths"

    # Start on a covered bearing so a gap that crosses north is one interval.
    shift = int(np.argmax(covered))
    rotated = np.concatenate([covered[shift:], covered[:shift]])
    step = 360.0 / count
    gaps: list[str] = []
    start: int | None = None
    for index, flag in enumerate(rotated):
        if not flag and start is None:
            start = index
        elif flag and start is not None:
            begin = (shift + start) * step
            end = (shift + index) * step
            gaps.append(f"{begin % 360.0:.0f}°–{end % 360.0:.0f}°")
            start = None
    # A gap that runs to the end of the rotated circle still has to be reported.
    if start is not None:
        begin = (shift + start) * step
        end = (shift + count) * step
        gaps.append(f"{begin % 360.0:.0f}°–{end % 360.0:.0f}°")
    return ", ".join(gaps) if gaps else "none"


def ring_coverage(
    positions: list[tuple[float, float]],
    ranges: list[float],
    radius_km: float,
) -> tuple[float, str]:
    """Percent of bearings at radius_km that fall inside at least one healthy radar disk."""
    step = 360.0 / AZIMUTH_BINS
    mask = np.zeros(AZIMUTH_BINS, dtype=bool)
    for index in range(AZIMUTH_BINS):
        sample_east, sample_north = point_at(index * step, radius_km)
        for (east, north), reach in zip(positions, ranges):
            if reach <= 0:
                continue
            if (sample_east - east) ** 2 + (sample_north - north) ** 2 <= reach ** 2:
                mask[index] = True
                break
    percent = 100.0 * float(mask.mean())
    return percent, gap_text(mask)


def _located_ships(locations: pd.DataFrame) -> list[dict]:
    """Ships that have coordinates and can be drawn or stationed."""
    places = _prepare_locations(locations)
    ships = []
    for row in places.itertuples(index=False):
        if pd.isna(row.latitude) or pd.isna(row.longitude):
            continue
        heading = None if pd.isna(row.current_heading) else float(row.current_heading) % 360.0
        ships.append(
            {
                "ship_id": str(row.ship_id),
                "ship_name": str(row.ship_name) if pd.notna(row.ship_name) else "unknown",
                "ship_type": str(row.ship_type) if pd.notna(row.ship_type) else "unknown",
                "latitude": float(row.latitude),
                "longitude": float(row.longitude),
                "heading": heading,
            }
        )
    return ships


def choose_datum(ships: list[dict]) -> tuple[dict | None, float, float, str]:
    """Use the carrier as the high-value unit. Otherwise use the group centroid."""
    for ship in ships:
        if "carrier" in ship["ship_type"].lower():
            label = f"{ship['ship_id']} {ship['ship_name']} (carrier)"
            return ship, ship["latitude"], ship["longitude"], label

    latitude = float(np.mean([ship["latitude"] for ship in ships]))
    longitude = float(np.mean([ship["longitude"] for ship in ships]))
    return None, latitude, longitude, "group centroid"


def _assignment_cost(
    escorts: list[dict],
    bearings: list[float],
    station_range: float,
    origin_lat: float,
    origin_lon: float,
) -> tuple[float, list[tuple[dict, float]]]:
    """Pair escorts with stations. Tries every pairing when the screen is small."""
    current = []
    for escort in escorts:
        east, north = to_xy(escort["latitude"], escort["longitude"], origin_lat, origin_lon)
        current.append((east, north))

    def cost(pairs: list[tuple[int, int]]) -> float:
        total = 0.0
        for escort_index, station_index in pairs:
            station_east, station_north = point_at(bearings[station_index], station_range)
            east, north = current[escort_index]
            total += float(np.hypot(station_east - east, station_north - north))
        return total

    indexes = range(len(escorts))
    if len(escorts) <= 6:
        best_pairs: list[tuple[int, int]] | None = None
        best_cost = float("inf")
        for order in itertools.permutations(indexes):
            pairs = list(zip(indexes, order))
            total = cost(pairs)
            if total < best_cost:
                best_cost = total
                best_pairs = pairs
        assert best_pairs is not None
    else:
        # Greedy fallback: each escort takes the nearest still-open station.
        remaining = set(indexes)
        best_pairs = []
        for escort_index, (east, north) in enumerate(current):
            station_index = min(
                remaining,
                key=lambda index: np.hypot(
                    point_at(bearings[index], station_range)[0] - east,
                    point_at(bearings[index], station_range)[1] - north,
                ),
            )
            remaining.remove(station_index)
            best_pairs.append((escort_index, station_index))
        best_cost = cost(best_pairs)

    paired = [(escorts[escort_index], bearings[station_index]) for escort_index, station_index in best_pairs]
    return best_cost, paired


def _best_offset(escorts: list[dict], station_range: float, origin_lat: float, origin_lon: float) -> float:
    """Rotate an even screen so the ships travel as little as possible."""
    best_offset = 0.0
    best_cost = float("inf")
    for offset in range(0, 360, 10):
        bearings = even_bearings(len(escorts), float(offset))
        total, _pairs = _assignment_cost(escorts, bearings, station_range, origin_lat, origin_lon)
        if total < best_cost:
            best_cost = total
            best_offset = float(offset)
    return best_offset


def _sector_bounds(bearing: float, ordered: list[float]) -> tuple[float, float]:
    """Responsibility wedge: halfway to the neighboring stations, clockwise."""
    if len(ordered) == 1:
        return 0.0, 360.0
    position = ordered.index(bearing)
    previous_bearing = ordered[position - 1]
    next_bearing = ordered[(position + 1) % len(ordered)]
    # Midpoint traveling clockwise from the previous station to this one,
    # and from this one to the next.
    back = (bearing - previous_bearing) % 360.0
    forward = (next_bearing - bearing) % 360.0
    start = (bearing - back / 2.0) % 360.0
    end = (bearing + forward / 2.0) % 360.0
    return start, end


def _failed_sector_coverage(
    positions: list[tuple[float, float]],
    ranges: list[float],
    radius_km: float,
    failed_bearings: list[float],
) -> float:
    """Share of the bearings around each failed ship that a healthy radar still covers."""
    if not failed_bearings:
        return 100.0
    hits = 0
    total = 0
    for center in failed_bearings:
        for offset in range(-30, 31, 5):
            total += 1
            sample_east, sample_north = point_at((center + offset) % 360.0, radius_km)
            for (east, north), reach in zip(positions, ranges):
                if reach > 0 and (sample_east - east) ** 2 + (sample_north - north) ** 2 <= reach ** 2:
                    hits += 1
                    break
    return 100.0 * hits / total


def _off_threat_axis(bearing: float, threat: float, exclusion: float = THREAT_EXCLUSION_DEG) -> float:
    """Slide a station bearing out of the cone centered on the threat axis."""
    if wrap_delta(bearing, threat) >= exclusion:
        return bearing % 360.0
    delta = (bearing - threat + 180.0) % 360.0 - 180.0
    sign = 1.0 if delta > 0 else -1.0
    return (threat + sign * exclusion) % 360.0


def _heading_clear(heading: float, threat: float, exclusion: float = THREAT_EXCLUSION_DEG) -> bool:
    """True when a course does not point along the threat vector."""
    return wrap_delta(heading, threat) >= exclusion - 0.1


def _threat_shoulders(count: int, threat: float, first_offset: float) -> list[float]:
    """Stations on both sides of the threat axis, never inside the exclusion cone."""
    if count <= 0:
        return []
    offsets: list[float] = []
    step = max(first_offset, THREAT_EXCLUSION_DEG + 15.0)
    index = 0
    while len(offsets) < count:
        side = 1.0 if index % 2 == 0 else -1.0
        ring = 1 + index // 2
        offsets.append(side * min(step * ring, 150.0))
        index += 1
    bearings: list[float] = []
    for offset in offsets:
        bearing = _off_threat_axis((threat + offset) % 360.0, threat)
        while any(wrap_delta(bearing, other) < 12.0 for other in bearings):
            bearing = _off_threat_axis((bearing + 15.0) % 360.0, threat)
        bearings.append(bearing)
    return bearings


def _build_plan(
    name: str,
    rationale: str,
    escorts: list[dict],
    bearings: list[float],
    station_range: float,
    hvu: dict | None,
    origin_lat: float,
    origin_lon: float,
    ranges: dict[str, float],
    outer_range: float,
    failed_bearings: list[float],
    threat_bearing: float | None = None,
) -> dict:
    """One course set: a station for every healthy escort, plus coverage numbers."""
    _cost, pairs = _assignment_cost(escorts, bearings, station_range, origin_lat, origin_lon)
    ordered = sorted(bearing for _ship, bearing in pairs)
    moves = []
    positions: list[tuple[float, float]] = []
    reach: list[float] = []

    # The carrier stays at the center. Its radar counts on the inner ring.
    # The outer ring is the escort screen: a failed escort leaves a hole there
    # even when the carrier radar is still up.
    if hvu is not None and ranges.get(hvu["ship_id"], 0.0) > 0:
        positions.append((0.0, 0.0))
        reach.append(ranges[hvu["ship_id"]])

    screen_positions: list[tuple[float, float]] = []
    screen_reach: list[float] = []

    for escort, station_bearing in pairs:
        if threat_bearing is not None:
            station_bearing = _off_threat_axis(station_bearing, threat_bearing)
        station_east, station_north = point_at(station_bearing, station_range)
        current_east, current_north = to_xy(
            escort["latitude"], escort["longitude"], origin_lat, origin_lon
        )
        course = bearing_deg(station_east - current_east, station_north - current_north)
        travel = float(np.hypot(station_east - current_east, station_north - current_north))
        if threat_bearing is not None:
            # Step the station further off the axis until the course itself is clear.
            for _step in range(8):
                if _heading_clear(course, threat_bearing) and wrap_delta(station_bearing, threat_bearing) >= THREAT_EXCLUSION_DEG - 0.1:
                    break
                delta = (station_bearing - threat_bearing + 180.0) % 360.0 - 180.0
                sign = 1.0 if delta >= 0 else -1.0
                station_bearing = (threat_bearing + sign * (abs(delta) + 8.0)) % 360.0
                station_east, station_north = point_at(station_bearing, station_range)
                course = bearing_deg(station_east - current_east, station_north - current_north)
                travel = float(np.hypot(station_east - current_east, station_north - current_north))
            if not _heading_clear(course, threat_bearing):
                station_east, station_north = current_east, current_north
                station_bearing = bearing_deg(current_east, current_north) if travel > 0.1 else station_bearing
                course = escort["heading"] if escort["heading"] is not None else course
                if not _heading_clear(course, threat_bearing):
                    course = _off_threat_axis(course, threat_bearing)
                travel = 0.0
                latitude, longitude = escort["latitude"], escort["longitude"]
            else:
                latitude, longitude = to_latlon(station_east, station_north, origin_lat, origin_lon)
        else:
            latitude, longitude = to_latlon(station_east, station_north, origin_lat, origin_lon)
        if travel < 0.5 and escort["heading"] is not None and threat_bearing is None:
            course = escort["heading"]
        latitude, longitude = to_latlon(station_east, station_north, origin_lat, origin_lon)
        sector_start, sector_end = _sector_bounds(station_bearing, ordered)
        moves.append(
            {
                "ship_id": escort["ship_id"],
                "ship_name": escort["ship_name"],
                "ship_type": escort["ship_type"],
                "heading": course,
                "station_bearing": station_bearing,
                "station_range_km": float(np.hypot(station_east, station_north)),
                "sector_start": sector_start,
                "sector_end": sector_end,
                "latitude": escort["latitude"],
                "longitude": escort["longitude"],
                "station_latitude": latitude,
                "station_longitude": longitude,
                "travel_km": travel,
                "role": "escort",
            }
        )
        station = (station_east, station_north)
        station_reach = ranges.get(escort["ship_id"], default_range_km(escort["ship_type"]))
        positions.append(station)
        reach.append(station_reach)
        screen_positions.append(station)
        screen_reach.append(station_reach)

    if hvu is not None:
        hold_heading = hvu["heading"] if hvu["heading"] is not None else 0.0
        if threat_bearing is not None and not _heading_clear(hold_heading, threat_bearing):
            hold_heading = _off_threat_axis(hold_heading, threat_bearing)
        moves.insert(
            0,
            {
                "ship_id": hvu["ship_id"],
                "ship_name": hvu["ship_name"],
                "ship_type": hvu["ship_type"],
                "heading": hold_heading,
                "station_bearing": None,
                "station_range_km": 0.0,
                "sector_start": None,
                "sector_end": None,
                "latitude": hvu["latitude"],
                "longitude": hvu["longitude"],
                "station_latitude": hvu["latitude"],
                "station_longitude": hvu["longitude"],
                "travel_km": 0.0,
                "role": "hvu",
            },
        )

    outer_pct, outer_gaps = ring_coverage(screen_positions, screen_reach, outer_range)
    inner_pct, inner_gaps = ring_coverage(positions, reach, INNER_RANGE_KM)
    failed_pct = _failed_sector_coverage(screen_positions, screen_reach, outer_range, failed_bearings)
    threat_pct = (
        _failed_sector_coverage(screen_positions, screen_reach, outer_range, [threat_bearing])
        if threat_bearing is not None
        else failed_pct
    )
    mean_travel = float(np.mean([move["travel_km"] for move in moves if move["role"] == "escort"] or [0.0]))
    unsafe = 0
    if threat_bearing is not None:
        unsafe = sum(
            1
            for move in moves
            if move["role"] == "escort"
            and move["travel_km"] > 0.5
            and not _heading_clear(move["heading"], threat_bearing)
        )
    # Threat-axis coverage counts more than the far side of the ring.
    score = 1.6 * threat_pct + 0.35 * outer_pct + 0.15 * inner_pct - 0.25 * mean_travel - 40.0 * unsafe

    return {
        "name": name,
        "rationale": rationale,
        "station_range_km": station_range,
        "moves": moves,
        "outer_pct": outer_pct,
        "outer_gaps": outer_gaps,
        "inner_pct": inner_pct,
        "inner_gaps": inner_gaps,
        "failed_sector_pct": failed_pct,
        "threat_pct": threat_pct,
        "threat_bearing": threat_bearing,
        "outer_range_km": outer_range,
        "score": score,
        "positions": screen_positions,
        "reach": screen_reach,
    }


def recommend_courses(
    locations: pd.DataFrame,
    ranges: dict[str, float],
    capacity: dict[str, float],
    threat_bearing: float | None = None,
) -> tuple[list[dict], str, float]:
    """Three stationing plans ranked Best / Acceptable / Fallback.

    Escorts with any remaining capacity are stationed. Their reach is already
    scaled. Ships at 0% stay off the screen. When a threat bearing is given,
    stations sit on either side of that axis and no course points along it.
    """
    ships = _located_ships(locations)
    if not ships:
        return [], "no located ships", INNER_RANGE_KM

    hvu, origin_lat, origin_lon, datum_label = choose_datum(ships)
    hvu_id = hvu["ship_id"] if hvu is not None else None
    escorts = [
        ship
        for ship in ships
        if ship["ship_id"] != hvu_id and ranges.get(ship["ship_id"], 0.0) > 0
    ]
    escort_ranges = [ranges.get(ship["ship_id"], 0.0) for ship in escorts]
    # Sit the outer ring near the longest scaled escort radar. Using the
    # carrier's full range would hide the holes left by a weak screen.
    outer_range = max(INNER_RANGE_KM + 10.0, max(escort_ranges)) if escort_ranges else INNER_RANGE_KM

    if not escorts:
        return [], datum_label, outer_range

    balanced_range = 18.0
    balanced_offset = _best_offset(escorts, balanced_range, origin_lat, origin_lon)
    zero_ids = {ship_id for ship_id, percent in capacity.items() if percent <= 0.5}
    reduced_ids = {ship_id for ship_id, percent in capacity.items() if percent < 99.5}
    gap_ids = zero_ids or reduced_ids
    failed_bearings = []
    for ship in ships:
        if ship["ship_id"] not in gap_ids:
            continue
        east, north = to_xy(ship["latitude"], ship["longitude"], origin_lat, origin_lon)
        if np.hypot(east, north) > 0.1:
            failed_bearings.append(bearing_deg(east, north))
    gap_offset = circular_mean(failed_bearings) if failed_bearings else (balanced_offset + 40.0) % 360.0

    if threat_bearing is None:
        templates = [
            (
                "Balanced screen",
                "Even stations around the datum so neighboring radars overlap and no wide sector is left to one ship.",
                even_bearings(len(escorts), balanced_offset),
                balanced_range,
            ),
            (
                "Gap closure",
                "Rotate the screen so a radar with remaining capacity sits on the bearing where capacity was lost.",
                even_bearings(len(escorts), gap_offset),
                24.0,
            ),
            (
                "Close screen",
                "Shorter move and a tighter station around the carrier, so the inner circle stays covered while the escorts travel less.",
                even_bearings(len(escorts), (balanced_offset + 180.0 / len(escorts)) % 360.0),
                10.0,
            ),
        ]
    else:
        # Shoulders cover the threat axis from either side. Nothing is placed
        # inside the exclusion cone, and the rear plan keeps some 360° watch.
        templates = [
            (
                "Threat shoulders",
                "Stations 40° either side of the threat axis so the radars cover that sector without any ship steaming onto it.",
                _threat_shoulders(len(escorts), threat_bearing, 40.0),
                18.0,
            ),
            (
                "Wide threat arc",
                "A wider flank on the threat, still outside the 25° exclusion cone, with less weight on the opposite side.",
                _threat_shoulders(len(escorts), threat_bearing, 55.0),
                22.0,
            ),
            (
                "Flank and rear",
                "Escorts abeam of the threat and toward the low-threat side, keeping the axis covered while preserving some all-round watch.",
                _threat_shoulders(len(escorts), threat_bearing, 90.0),
                16.0,
            ),
        ]

    plans = [
        _build_plan(
            name,
            rationale,
            escorts,
            bearings,
            station_range,
            hvu,
            origin_lat,
            origin_lon,
            ranges,
            outer_range,
            failed_bearings,
            threat_bearing,
        )
        for name, rationale, bearings, station_range in templates
    ]
    plans.sort(key=lambda plan: plan["score"], reverse=True)
    for index, plan in enumerate(plans):
        plan["rank"] = index + 1
        plan["rank_label"] = RANK_LABELS[index]
        plan["color"] = OPTION_COLORS[index]
        plan["linestyle"] = OPTION_STYLES[index]
        plan["legend"] = f"Course Option {index + 1} ({RANK_LABELS[index]})"
    return plans, datum_label, outer_range


def _format_heading(heading: float) -> str:
    return f"{heading % 360.0:05.1f}° true"


def print_recommendations(
    plans: list[dict],
    datum_label: str,
    outer_range: float,
    capacity: dict[str, float],
    ranges: dict[str, float],
) -> None:
    """Print the three ranked course sets, or explain why none were built."""
    print()
    print("Course recommendations")
    if not plans:
        print("No escorts with remaining radar capacity to reposition. Course recommendations skipped.")
        return

    print(f"Datum: {datum_label}")
    print("Effective range is full max range times remaining capacity.")
    print("Ships at 0% are left off the screen. Partial radars are still stationed.")
    print(f"Outer ring {outer_range:.0f} km. Inner ring {INNER_RANGE_KM:.0f} km (carrier radar included).")

    for plan in plans:
        print()
        print(f"Course Option {plan['rank']}: {plan['rank_label']} — {plan['name']}")
        print(f"Rationale: {plan['rationale']}")
        print(
            f"Collective coverage: {plan['outer_pct']:.1f}% at {plan['outer_range_km']:.0f} km, "
            f"{plan['inner_pct']:.1f}% at {INNER_RANGE_KM:.0f} km"
        )
        print(f"Residual gaps at {plan['outer_range_km']:.0f} km: {plan['outer_gaps']}")
        print(f"Residual gaps at {INNER_RANGE_KM:.0f} km: {plan['inner_gaps']}")
        print(f"Reduced-capacity sector still covered: {plan['failed_sector_pct']:.0f}%")
        if plan.get("threat_bearing") is not None:
            print(
                f"Threat-axis coverage (±30° of {plan['threat_bearing']:.0f}°): "
                f"{plan['threat_pct']:.0f}%"
            )
            print(f"Stations stay at least {THREAT_EXCLUSION_DEG:.0f}° off the threat axis.")
        for move in plan["moves"]:
            print()
            remaining = capacity.get(move["ship_id"], 100.0)
            reach = ranges.get(move["ship_id"], 0.0)
            print(
                f"  {move['ship_id']}  {move['ship_name']} ({move['ship_type']})"
                f" — {remaining:.0f}% capacity, {reach:.0f} km"
            )
            print(f"    Recommended heading: {_format_heading(move['heading'])}")
            if move["role"] == "hvu":
                print("    Station: remain on datum (range 0 km)")
            else:
                print(
                    f"    Station: bearing {move['station_bearing']:.0f}° "
                    f"at {move['station_range_km']:.0f} km from datum"
                )
                print(
                    f"    Sector: {move['sector_start']:.0f}°–{move['sector_end']:.0f}°"
                )


def _capacity_band(percent: float) -> str:
    """healthy, partial, or severe. Severe is a total or near-total loss."""
    if percent >= 99.5:
        return "healthy"
    if percent < SEVERE_CAPACITY:
        return "severe"
    return "partial"


def _draw_ships(axis, places: pd.DataFrame, capacity: dict[str, float]) -> None:
    """Blue for full capacity, orange for a partial loss, red for a severe loss."""
    bands = places["ship_id"].astype(str).map(lambda ship_id: _capacity_band(capacity.get(ship_id, 100.0)))
    styles = (
        ("healthy", OPERATIONAL_COLOR, "Full capacity", 80),
        ("partial", PARTIAL_COLOR, "Partial capacity", 100),
        ("severe", OUTAGE_COLOR, "Severe loss", 110),
    )
    for band, color, label, size in styles:
        subset = places.loc[bands.eq(band)]
        if subset.empty:
            continue
        axis.scatter(
            subset["longitude"],
            subset["latitude"],
            c=color,
            s=size,
            zorder=5 if band == "healthy" else 6,
            label=label,
        )
    for row in places.itertuples(index=False):
        axis.annotate(
            str(row.ship_id),
            (row.longitude, row.latitude),
            textcoords="offset points",
            xytext=(6, 6),
            fontsize=9,
            zorder=7,
        )


def _draw_courses(axis, plans: list[dict]) -> None:
    """Arrow from each healthy escort to its station, one color per ranked option."""
    for plan in plans:
        axis.plot([], [], color=plan["color"], linestyle=plan["linestyle"], lw=2, label=plan["legend"])
        for move in plan["moves"]:
            if move["role"] != "escort":
                continue
            arrow = FancyArrowPatch(
                (move["longitude"], move["latitude"]),
                (move["station_longitude"], move["station_latitude"]),
                arrowstyle="-|>",
                mutation_scale=12,
                linewidth=1.7,
                linestyle=plan["linestyle"],
                color=plan["color"],
                zorder=4,
            )
            axis.add_patch(arrow)


def _capacity_note(report: pd.DataFrame) -> str:
    """Corner text: remaining capacity and effective range for every ship."""
    lines = ["Radar capacity"]
    for row in report.sort_values("ship_id").itertuples(index=False):
        lines.append(
            f"{row.ship_id:<8} {row.capacity_percent:.0f}% capacity"
            f"   lost {row.percent_lost:.0f}%"
            f"   {row.effective_range_km:.0f} km"
        )
    return "\n".join(lines)


def _course_note(report: pd.DataFrame, plans: list[dict]) -> str:
    """Opposite corner: the best course, or a note that none is required."""
    if not plans:
        return "Best course\nAll radars fully operational.\nNo course change."

    best = plans[0]
    lines = [
        f"Best course: {best['name']}",
        f"Option {best['rank']} ({best['rank_label']})",
        (
            f"{best['outer_pct']:.1f}% at {best['outer_range_km']:.0f} km"
            f"  |  inner {best['inner_pct']:.0f}%"
        ),
    ]
    if best.get("threat_bearing") is not None:
        lines.append(
            f"Threat {best['threat_bearing']:.0f}°  axis cover {best['threat_pct']:.0f}%"
        )
    listed: set[str] = set()
    for move in best["moves"]:
        listed.add(move["ship_id"])
        heading = f"{move['heading'] % 360.0:05.1f}°"
        if move["role"] == "hvu":
            lines.append(f"{move['ship_id']:<8} {heading}  hold datum")
        else:
            lines.append(
                f"{move['ship_id']:<8} {heading}"
                f"  brg {move['station_bearing']:.0f}° / {move['station_range_km']:.0f} km"
            )
    # Ships with no remaining radar are not given a new station.
    for row in report.sort_values("ship_id").itertuples(index=False):
        if row.ship_id in listed:
            continue
        lines.append(f"{row.ship_id:<8} hold  ({row.capacity_percent:.0f}% radar)")
    return "\n".join(lines)


def _corner_text(axis, x: float, y: float, text: str, horizontal: str, vertical: str) -> None:
    """Readable note pinned to an axes corner, drawn above the tracks.

    The block stays in the requested corner. Lines inside the block stay
    left-aligned so the ship columns line up.
    """
    axis.text(
        x,
        y,
        text,
        transform=axis.transAxes,
        ha=horizontal,
        va=vertical,
        multialignment="left",
        fontsize=8,
        family="monospace",
        zorder=8,
        bbox={
            "boxstyle": "round,pad=0.45",
            "facecolor": "white",
            "edgecolor": "#B0B0B0",
            "alpha": 0.94,
        },
    )


def _draw_threat(axis, locations: pd.DataFrame, bearing: float) -> None:
    """Arrow and a short sector showing where the threat axis lies."""
    carriers = locations.loc[
        locations["ship_type"].astype(str).str.contains("carrier", case=False, na=False)
    ]
    origin = carriers.iloc[0] if len(carriers) else locations.iloc[0]
    origin_lat = float(origin["latitude"])
    origin_lon = float(origin["longitude"])
    tip_east, tip_north = point_at(bearing, 30.0)
    tip_lat, tip_lon = to_latlon(tip_east, tip_north, origin_lat, origin_lon)
    axis.annotate(
        "",
        xy=(tip_lon, tip_lat),
        xytext=(origin_lon, origin_lat),
        arrowprops={"arrowstyle": "-|>", "color": "#9B2226", "lw": 2.2, "mutation_scale": 14},
        zorder=3,
    )
    wedge_lats = [origin_lat]
    wedge_lons = [origin_lon]
    for offset in np.linspace(-THREAT_FOCUS_HALF_DEG, THREAT_FOCUS_HALF_DEG, 24):
        east, north = point_at((bearing + offset) % 360.0, 22.0)
        lat, lon = to_latlon(east, north, origin_lat, origin_lon)
        wedge_lats.append(lat)
        wedge_lons.append(lon)
    axis.fill(wedge_lons, wedge_lats, color="#9B2226", alpha=0.12, zorder=1)
    axis.plot([], [], color="#9B2226", lw=2.2, label=f"Threat {bearing:.0f}°")


def plot_status(
    locations: pd.DataFrame,
    report: pd.DataFrame,
    plans: list[dict],
    figure_path: Path,
    title: str | None = None,
    threat_bearing: float | None = None,
) -> None:
    """Map of the group, with capacity and the best course noted in opposite corners."""
    places = _prepare_locations(locations).dropna(subset=["latitude", "longitude"])
    capacity = {str(row.ship_id): float(row.capacity_percent) for row in report.itertuples(index=False)}

    figure, axis = plt.subplots(figsize=(11, 8))
    _draw_ships(axis, places, capacity)
    _draw_courses(axis, plans)
    if threat_bearing is not None:
        _draw_threat(axis, places, threat_bearing)
    # Upper left is capacity. Lower right is the best course, so the two notes
    # do not sit on top of each other.
    _corner_text(axis, 0.02, 0.98, _capacity_note(report), "left", "top")
    _corner_text(axis, 0.98, 0.02, _course_note(report, plans), "right", "bottom")

    axis.set_title(title or "Carrier Strike Group radar status")
    axis.set_xlabel("Longitude")
    axis.set_ylabel("Latitude")
    axis.set_aspect("equal", adjustable="datalim")
    axis.grid(True, linestyle=":", linewidth=0.6)
    axis.legend(frameon=False, loc="upper right")
    figure.tight_layout()
    figure.savefig(figure_path, dpi=150)
    print()
    print(f"Saved figure to {figure_path}")


def plot_best_course(plan: dict, origin_lat: float, origin_lon: float, datum_label: str, figure_path: Path) -> None:
    """Formation close-up plus a polar view of which bearings the top option covers."""
    figure, (map_axis, polar_axis) = plt.subplots(
        1,
        2,
        figsize=(12, 6),
        subplot_kw={"projection": None},
    )
    # Rebuild the polar axis so bearings run clockwise from north.
    figure.delaxes(polar_axis)
    polar_axis = figure.add_subplot(122, projection="polar")
    polar_axis.set_theta_zero_location("N")
    polar_axis.set_theta_direction(-1)

    map_axis.scatter(
        [move["longitude"] for move in plan["moves"]],
        [move["latitude"] for move in plan["moves"]],
        c=OPERATIONAL_COLOR,
        s=70,
        zorder=5,
        label="Current position",
    )
    map_axis.scatter(
        [move["station_longitude"] for move in plan["moves"] if move["role"] == "escort"],
        [move["station_latitude"] for move in plan["moves"] if move["role"] == "escort"],
        c=plan["color"],
        s=70,
        marker="^",
        zorder=5,
        label="Recommended station",
    )
    sector_colors = ["#54A24B", "#4C78A8", "#F58518", "#B279A2"]
    escort_index = 0
    for move in plan["moves"]:
        if move["role"] != "escort":
            map_axis.annotate(
                move["ship_id"],
                (move["longitude"], move["latitude"]),
                textcoords="offset points",
                xytext=(6, 6),
                fontsize=9,
                zorder=7,
            )
            continue
        color = sector_colors[escort_index % len(sector_colors)]
        escort_index += 1
        map_axis.add_patch(
            FancyArrowPatch(
                (move["longitude"], move["latitude"]),
                (move["station_longitude"], move["station_latitude"]),
                arrowstyle="-|>",
                mutation_scale=12,
                linewidth=1.7,
                color=plan["color"],
                zorder=4,
            )
        )
        map_axis.annotate(
            move["ship_id"],
            (move["station_longitude"], move["station_latitude"]),
            textcoords="offset points",
            xytext=(6, 6),
            fontsize=9,
            zorder=7,
        )
        # Sector wedge clipped near the formation so the stations stay readable.
        # The printed percentages use the full radar range.
        wedge_km = min(35.0, plan["outer_range_km"])
        span = (move["sector_end"] - move["sector_start"]) % 360.0
        samples = np.linspace(0.0, span, max(2, int(span)), endpoint=True)
        latitudes = [origin_lat]
        longitudes = [origin_lon]
        for offset in samples:
            east, north = point_at((move["sector_start"] + offset) % 360.0, wedge_km)
            latitude, longitude = to_latlon(east, north, origin_lat, origin_lon)
            latitudes.append(latitude)
            longitudes.append(longitude)
        map_axis.fill(longitudes, latitudes, color=color, alpha=0.22, zorder=1)

    map_axis.set_title(f"Option {plan['rank']} stations ({plan['rank_label']})")
    map_axis.set_xlabel("Longitude")
    map_axis.set_ylabel("Latitude")
    map_axis.set_aspect("equal", adjustable="datalim")
    map_axis.grid(True, linestyle=":", linewidth=0.6)
    map_axis.legend(frameon=False, loc="upper left")

    step = 360.0 / AZIMUTH_BINS
    angles = np.deg2rad(np.arange(0.0, 360.0, step))
    covered = np.zeros(AZIMUTH_BINS)
    for index in range(AZIMUTH_BINS):
        sample_east, sample_north = point_at(index * step, plan["outer_range_km"])
        for (east, north), reach in zip(plan["positions"], plan["reach"]):
            if reach > 0 and (sample_east - east) ** 2 + (sample_north - north) ** 2 <= reach ** 2:
                covered[index] = 1.0
                break
    bar_colors = np.where(covered > 0, OPERATIONAL_COLOR, OUTAGE_COLOR)
    polar_axis.bar(angles, np.ones(AZIMUTH_BINS), width=np.deg2rad(step), color=bar_colors, align="edge")
    if plan.get("threat_bearing") is not None:
        threat_angle = np.deg2rad(float(plan["threat_bearing"]))
        polar_axis.plot([threat_angle, threat_angle], [0.0, 1.0], color="#9B2226", lw=2.0, label="Threat")
    polar_axis.set_yticks([])
    polar_axis.set_title(f"Coverage at {plan['outer_range_km']:.0f} km\n{datum_label}", pad=16)

    figure.suptitle("Recommended courses — top option", fontsize=13)
    figure.tight_layout()
    figure.savefig(figure_path, dpi=150)
    print(f"Saved figure to {figure_path}")


def analyze_frames(
    radar: pd.DataFrame,
    locations: pd.DataFrame,
    title: str | None = None,
    threat_bearing: float | None = None,
    threat_range_km: float | None = None,
) -> None:
    """Run the capacity report, course ranking, and both figures for one snapshot."""
    report = attach_locations(assess_capacity(radar, locations), locations)
    print_capacity(report)
    if threat_bearing is not None:
        span = f", range {threat_range_km:.0f} km" if threat_range_km is not None else ""
        print(f"Threat bearing: {threat_bearing:.0f}° true{span}")
    capacity = {str(row.ship_id): float(row.capacity_percent) for row in report.itertuples(index=False)}
    ranges = effective_ranges(report)
    # A known threat still needs a screen when every radar is healthy.
    if threat_bearing is None and report["capacity_percent"].ge(99.5).all():
        plot_status(locations, report, [], STATUS_FIGURE, title=title)
        return

    plans, datum_label, outer_range = recommend_courses(
        locations, ranges, capacity, threat_bearing=threat_bearing
    )
    print_recommendations(plans, datum_label, outer_range, capacity, ranges)
    plot_status(locations, report, plans, STATUS_FIGURE, title=title, threat_bearing=threat_bearing)
    if plans:
        ships = _located_ships(locations)
        _hvu, origin_lat, origin_lon, _label = choose_datum(ships)
        plot_best_course(plans[0], origin_lat, origin_lon, datum_label, COURSE_FIGURE)


def run_once(radar_path: Path, location_path: Path) -> int:
    """Analyze two CSV files a single time and leave the figures open."""
    try:
        radar = load_csv(radar_path, RADAR_COLUMNS, "Radar performance")
        locations = load_csv(location_path, LOCATION_COLUMNS, "Ship location")
    except (FileNotFoundError, ValueError, pd.errors.ParserError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    analyze_frames(radar, locations)
    plt.show()
    return 0


def load_timeline(path: Path, known_ids: set[str]) -> list[dict]:
    """Read a degradation schedule. Events are ordered by time, then file order.

    A missing optional status leaves the script to label OK, DEGRADED, or
    FAILED from the capacity number. Capacity is what scales radar range.
    """
    if not path.is_file():
        raise FileNotFoundError(f"Timeline file not found: {path}")
    frame = pd.read_csv(path)
    missing = [name for name in TIMELINE_COLUMNS if name not in frame.columns]
    if missing:
        joined = ", ".join(missing)
        raise ValueError(f"Timeline file {path} is missing columns: {joined}")
    frame["time_seconds"] = pd.to_numeric(frame["time_seconds"], errors="coerce")
    frame["new_capacity_percent"] = pd.to_numeric(frame["new_capacity_percent"], errors="coerce")
    if frame["time_seconds"].isna().any() or frame["new_capacity_percent"].isna().any():
        raise ValueError(f"Timeline file {path} has a blank time or capacity.")
    if (frame["time_seconds"] < 0).any():
        raise ValueError(f"Timeline file {path} has a negative time_seconds.")
    frame["new_capacity_percent"] = frame["new_capacity_percent"].clip(0, 100)
    if "new_radar_status" not in frame.columns:
        frame["new_radar_status"] = ""
    frame["ship_id"] = frame["ship_id"].astype(str).str.strip()
    # Stable sort keeps same-time rows in the order they appear in the file.
    frame = frame.sort_values("time_seconds", kind="mergesort")
    events: list[dict] = []
    for row in frame.itertuples(index=False):
        ship_id = str(row.ship_id)
        if ship_id not in known_ids:
            print(f"Warning: timeline ship_id {ship_id} is not in snooze_cruise.csv and was skipped.")
            continue
        status = str(row.new_radar_status).strip().upper()
        if status in {"", "NAN"}:
            status = ""
        events.append(
            {
                "time_seconds": float(row.time_seconds),
                "ship_id": ship_id,
                "capacity_percent": float(row.new_capacity_percent),
                "radar_status": status,
            }
        )
    return events


def active_event(ship_id: str, elapsed_s: float, timeline: list[dict]) -> dict | None:
    """Latest timeline row for this ship whose time has been reached."""
    chosen = None
    for event in timeline:
        if event["ship_id"] == ship_id and elapsed_s + 1e-9 >= event["time_seconds"]:
            chosen = event
    return chosen


def capacity_at(ship_id: str, elapsed_s: float, timeline: list[dict]) -> float:
    """Remaining capacity after every timeline event that is already due."""
    event = active_event(ship_id, elapsed_s, timeline)
    if event is None:
        return 100.0
    return float(event["capacity_percent"])


def _status_name(percent: float) -> str:
    if percent >= 99.5:
        return "OK"
    if percent <= 0.5:
        return "FAILED"
    return "DEGRADED"


def _lerp(start: float, end: float, fraction: float) -> float:
    return start + (end - start) * fraction


def _lerp_heading(start: float, end: float, fraction: float) -> float:
    """Interpolate a compass heading across the short way around the circle."""
    delta = (end - start + 180.0) % 360.0 - 180.0
    return (start + delta * fraction) % 360.0


def build_snooze_cruise() -> pd.DataFrame:
    """Sample a moving formation and the capacity schedule into one table.

    The group steams about 12 km and turns during the 80-second run.
    Capacity changes are rows in this same file: DDG-101 to 60% at 12 s,
    CG-58 to 0% at 32 s, and DDG-112 to 40% at 52 s.
    """
    duration = float(TOTAL_RUNTIME_SECONDS)
    times = np.arange(0.0, duration + 1e-9, float(UPDATE_INTERVAL_SECONDS))
    carrier = next(ship for ship in FLEET if "carrier" in ship["ship_type"].lower())
    origin_lat = float(carrier["latitude"])
    origin_lon = float(carrier["longitude"])
    offsets = {
        ship["ship_id"]: to_xy(ship["latitude"], ship["longitude"], origin_lat, origin_lon)
        for ship in FLEET
    }
    speed_km_s = 0.15
    start_heading = 45.0
    turn = 35.0
    carrier_track: list[tuple[float, float, float]] = []
    lat, lon = origin_lat, origin_lon
    previous = 0.0
    for instant in times:
        if instant > 0:
            step = float(instant - previous)
            mid = start_heading + turn * ((instant + previous) / 2.0) / duration
            east = speed_km_s * step * float(np.sin(np.deg2rad(mid)))
            north = speed_km_s * step * float(np.cos(np.deg2rad(mid)))
            lat = lat + north / KM_PER_DEG_LAT
            lon = lon + east / km_per_deg_lon(lat)
        heading = start_heading + turn * (float(instant) / duration)
        carrier_track.append((lat, lon, heading))
        previous = float(instant)

    rows: list[dict] = []
    for (instant, (carrier_lat, carrier_lon, heading)), _index in zip(
        zip(times, carrier_track), range(len(times))
    ):
        theta = np.deg2rad(heading - start_heading)
        cosine, sine = float(np.cos(theta)), float(np.sin(theta))
        stamp = SCENARIO_START + pd.Timedelta(seconds=float(instant))
        for ship in FLEET:
            east, north = offsets[ship["ship_id"]]
            rotated_east = east * cosine + north * sine
            rotated_north = -east * sine + north * cosine
            ship_lat, ship_lon = to_latlon(rotated_east, rotated_north, carrier_lat, carrier_lon)
            percent = _scheduled_capacity(ship["ship_id"], float(instant))
            scale = percent / 100.0
            if percent >= 99.5:
                status, mode, power, cooling, fault = "OK", "SEARCH", "NOMINAL", "NOMINAL", "NONE"
            elif percent <= 0.5:
                status, mode, power, cooling, fault = "FAILED", "STANDBY", "OFF", "SHUTDOWN", "TRANSMITTER_FAIL"
            else:
                status, mode, power, cooling, fault = "DEGRADED", "DEGRADED_SEARCH", "REDUCED", "ELEVATED", "RANGE_LIMIT"
            rows.append(
                {
                    "time_seconds": float(instant),
                    "timestamp": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "ship_id": ship["ship_id"],
                    "ship_name": ship["ship_name"],
                    "ship_type": ship["ship_type"],
                    "latitude": ship_lat,
                    "longitude": ship_lon,
                    "current_heading": heading,
                    "radar_status": status,
                    "capacity_percent": percent,
                    "full_range_km": ship["full_range_km"],
                    "max_range_km": ship["full_range_km"],
                    "effective_range_km": ship["full_range_km"] * scale,
                    "num_detections": round(ship["num_detections"] * scale),
                    "avg_snr_db": ship["avg_snr_db"] * (0.35 + 0.65 * scale),
                    "elevation_coverage_deg": ship["elevation_coverage_deg"] * scale,
                    "azimuth_coverage_deg": ship["azimuth_coverage_deg"] * scale,
                    "track_quality": percent,
                    "radar_mode": mode,
                    # Threat drifts from 030° toward 055° over the run. Same value on every ship.
                    "threat_bearing_deg": 30.0 + 25.0 * (float(instant) / duration),
                    "threat_range_km": 160.0,
                    "threat_priority": 1,
                    "power_status": power,
                    "cooling_status": cooling,
                    "last_maintenance": "2026-10-15",
                    "fault_code": fault,
                }
            )
    return pd.DataFrame(rows)


def load_snooze(path: Path) -> pd.DataFrame:
    """Load snooze_cruise.csv, creating the sample track if the file is absent."""
    rewrite = False
    if not path.is_file():
        rewrite = True
    else:
        frame = pd.read_csv(path)
        if "threat_bearing_deg" not in frame.columns:
            print(f"{path} has no threat_bearing_deg column. Writing a fresh sample.")
            rewrite = True
    if rewrite:
        frame = build_snooze_cruise()
        frame.to_csv(path, index=False)
        print(f"Wrote sample cruise track to {path}")
    required = [
        "time_seconds",
        "ship_id",
        "ship_name",
        "ship_type",
        "latitude",
        "longitude",
        "current_heading",
        "capacity_percent",
        "threat_bearing_deg",
        "num_detections",
        "avg_snr_db",
        "elevation_coverage_deg",
        "azimuth_coverage_deg",
        "track_quality",
    ]
    missing = [name for name in required if name not in frame.columns]
    if missing:
        joined = ", ".join(missing)
        raise ValueError(f"{path} is missing columns: {joined}")
    if "full_range_km" not in frame.columns:
        if "max_range_km" not in frame.columns:
            raise ValueError(f"{path} needs full_range_km or max_range_km")
        frame["full_range_km"] = frame["max_range_km"]
    frame["ship_id"] = frame["ship_id"].astype(str).str.strip()
    for column in (
        "time_seconds",
        "latitude",
        "longitude",
        "current_heading",
        "capacity_percent",
        "threat_bearing_deg",
        "full_range_km",
        "num_detections",
        "avg_snr_db",
        "elevation_coverage_deg",
        "azimuth_coverage_deg",
        "track_quality",
    ):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if frame["time_seconds"].isna().any() or frame["latitude"].isna().any():
        raise ValueError(f"{path} has a blank time or position")
    for optional, default in (
        ("radar_status", "OK"),
        ("radar_mode", "SEARCH"),
        ("power_status", "NOMINAL"),
        ("cooling_status", "NOMINAL"),
        ("last_maintenance", "2026-10-15"),
        ("fault_code", "NONE"),
    ):
        if optional not in frame.columns:
            frame[optional] = default
    return frame.sort_values(["ship_id", "time_seconds"]).reset_index(drop=True)


def _bracket(group: pd.DataFrame, elapsed_s: float) -> tuple[pd.Series, pd.Series, float]:
    """Rows on either side of elapsed_s and the blend fraction toward the later one."""
    times = group["time_seconds"].to_numpy(dtype=float)
    if elapsed_s <= times[0]:
        row = group.iloc[0]
        return row, row, 0.0
    if elapsed_s >= times[-1]:
        row = group.iloc[-1]
        return row, row, 0.0
    high = int(np.searchsorted(times, elapsed_s, side="left"))
    if abs(times[high] - elapsed_s) <= 1e-6:
        row = group.iloc[high]
        return row, row, 0.0
    low = high - 1
    span = float(times[high] - times[low])
    fraction = 0.0 if span <= 0 else (elapsed_s - float(times[low])) / span
    return group.iloc[low], group.iloc[high], fraction


def _scheduled_capacity(ship_id: str, elapsed_s: float) -> float:
    """Capacity written into the sample snooze_cruise.csv. Edit that file to change it."""
    if ship_id == "DDG-101" and elapsed_s >= 12:
        return 60.0
    if ship_id == "CG-58" and elapsed_s >= 32:
        return 0.0
    if ship_id == "DDG-112" and elapsed_s >= 52:
        return 40.0
    return 100.0


def snapshot_at(group: pd.DataFrame, elapsed_s: float) -> dict:
    """Interpolate position. Radar values step to the latest row at or before this time."""
    low, high, fraction = _bracket(group, elapsed_s)
    percent = float(low["capacity_percent"])
    full_range = float(low["full_range_km"])
    if "effective_range_km" in low.index and pd.notna(low["effective_range_km"]):
        effective = float(low["effective_range_km"])
    else:
        effective = full_range * percent / 100.0
    stamp = SCENARIO_START + pd.Timedelta(seconds=float(elapsed_s))
    return {
        "timestamp": stamp,
        "ship_id": str(low["ship_id"]),
        "ship_name": str(low["ship_name"]),
        "ship_type": str(low["ship_type"]),
        "latitude": _lerp(float(low["latitude"]), float(high["latitude"]), fraction),
        "longitude": _lerp(float(low["longitude"]), float(high["longitude"]), fraction),
        "current_heading": _lerp_heading(float(low["current_heading"]), float(high["current_heading"]), fraction),
        "radar_status": str(low["radar_status"]),
        "capacity_percent": percent,
        "full_range_km": full_range,
        "max_range_km": full_range,
        "effective_range_km": float(effective),
        "num_detections": float(low["num_detections"]),
        "avg_snr_db": float(low["avg_snr_db"]),
        "elevation_coverage_deg": float(low["elevation_coverage_deg"]),
        "azimuth_coverage_deg": float(low["azimuth_coverage_deg"]),
        "track_quality": float(low["track_quality"]),
        "radar_mode": str(low["radar_mode"]),
        "power_status": str(low["power_status"]),
        "cooling_status": str(low["cooling_status"]),
        "last_maintenance": str(low["last_maintenance"]),
        "fault_code": str(low["fault_code"]),
        "threat_bearing_deg": _lerp_heading(
            float(low["threat_bearing_deg"]), float(high["threat_bearing_deg"]), fraction
        ),
        "threat_range_km": float(low["threat_range_km"]) if "threat_range_km" in low.index else None,
        "threat_priority": float(low["threat_priority"]) if "threat_priority" in low.index else None,
    }


def print_ship_state(state: pd.DataFrame) -> None:
    """Print the detailed radar row for every ship on this update."""
    print("Ship state")
    first = state.iloc[0]
    threat_range = getattr(first, "threat_range_km", None)
    span = f", range {float(threat_range):.0f} km" if threat_range is not None and pd.notna(threat_range) else ""
    print(f"  Threat axis: {float(first.threat_bearing_deg):.0f}° true{span}")
    for row in state.sort_values("ship_id").itertuples(index=False):
        print(
            f"  {row.ship_id} {row.ship_name} ({row.ship_type})  {row.radar_status}"
            f"  {row.capacity_percent:.0f}%  "
            f"range {row.effective_range_km:.0f}/{row.full_range_km:.0f} km  "
            f"det {row.num_detections:.0f}  snr {row.avg_snr_db:.1f} dB  "
            f"az {row.azimuth_coverage_deg:.0f}°  el {row.elevation_coverage_deg:.0f}°  "
            f"track {row.track_quality:.0f}  mode {row.radar_mode}  "
            f"power {row.power_status}  cooling {row.cooling_status}  fault {row.fault_code}"
        )
        print(
            f"    position {row.latitude:.4f}, {row.longitude:.4f}  "
            f"heading {row.current_heading:.0f}°  last maintenance {row.last_maintenance}"
        )


class Scenario:
    """Playback of snooze_cruise.csv. Positions move; capacity comes from that file."""

    def __init__(self, cruise: pd.DataFrame) -> None:
        self.cruise = cruise
        self.groups = {
            ship_id: group.sort_values("time_seconds").reset_index(drop=True)
            for ship_id, group in cruise.groupby("ship_id", sort=False)
        }
        self.elapsed_s = 0.0

    def clock(self) -> pd.Timestamp:
        return SCENARIO_START + pd.Timedelta(seconds=self.elapsed_s)

    def advance(self, seconds: float) -> None:
        self.elapsed_s += seconds

    def current_state(self) -> pd.DataFrame:
        rows = [snapshot_at(group, self.elapsed_s) for group in self.groups.values()]
        return pd.DataFrame(rows)

    def radar_frame(self) -> pd.DataFrame:
        """Rows at or before the clock, so the loss time is the file's own timestamp."""
        past = self.cruise.loc[self.cruise["time_seconds"] <= self.elapsed_s + 1e-6].copy()
        past["timestamp"] = SCENARIO_START + pd.to_timedelta(past["time_seconds"], unit="s")
        if "max_range_km" not in past.columns:
            past["max_range_km"] = past["full_range_km"]
        return past

    def location_frame(self) -> pd.DataFrame:
        state = self.current_state()
        return state[
            ["ship_id", "ship_name", "latitude", "longitude", "ship_type", "current_heading"]
        ].copy()


def schedule_text(cruise: pd.DataFrame) -> str:
    """Describe capacity changes stored in snooze_cruise.csv."""
    changes: list[tuple[float, str, float]] = []
    for ship_id, group in cruise.groupby("ship_id"):
        ordered = group.sort_values("time_seconds")
        previous = None
        for row in ordered.itertuples(index=False):
            percent = float(row.capacity_percent)
            if previous is None:
                previous = percent
                continue
            if abs(percent - previous) > 0.5:
                changes.append((float(row.time_seconds), str(ship_id), percent))
                previous = percent
    if not changes:
        return "no capacity changes (every stored row is steady)"
    changes.sort()
    return ", ".join(f"T+{instant:.0f}s {ship_id} → {percent:.0f}%" for instant, ship_id, percent in changes)


def run_monitor(
    interval: float,
    total_runtime: float,
    cruise: pd.DataFrame,
    max_cycles: int | None = None,
) -> int:
    """Redraw the strike group until the runtime limit or Ctrl+C."""
    if interval <= 0:
        print("Error: --interval must be positive.", file=sys.stderr)
        return 1
    updates = max(1, int(total_runtime / interval))
    if max_cycles is not None:
        updates = max(1, max_cycles)
    scenario = Scenario(cruise)
    plt.ion()
    print(
        f"Live radar monitor. Update every {interval:g} s. "
        f"Stops after {updates} update(s), {total_runtime:g} s. Ctrl+C to stop early."
    )
    print(f"Data file: {SNOOZE_FILE}")
    print(f"Schedule in file: {schedule_text(cruise)}.")
    try:
        for cycle in range(updates):
            remaining = max(0.0, total_runtime - cycle * interval)
            plt.close("all")
            stamp = scenario.clock()
            state = scenario.current_state()
            state.to_csv(LIVE_STATE_FIGURE, index=False)
            threat = float(state["threat_bearing_deg"].iloc[0])
            threat_range = state["threat_range_km"].iloc[0] if "threat_range_km" in state.columns else None
            title = (
                f"Carrier Strike Group radar status — "
                f"T+{scenario.elapsed_s:.0f} s ({stamp.strftime('%H:%M:%S')} UTC)"
                f" — {remaining:.0f} s left — threat {threat:.0f}°"
            )
            print()
            print(
                f"--- {stamp.strftime('%Y-%m-%d %H:%M:%S UTC')}  |  "
                f"simulation T+{scenario.elapsed_s:.0f} s  |  "
                f"{remaining:.0f} s remaining  |  threat {threat:.0f}° ---"
            )
            print_ship_state(state)
            analyze_frames(
                scenario.radar_frame(),
                scenario.location_frame(),
                title=title,
                threat_bearing=threat,
                threat_range_km=None if threat_range is None or pd.isna(threat_range) else float(threat_range),
            )
            plt.show(block=False)
            plt.pause(0.05)
            if cycle + 1 >= updates:
                break
            time.sleep(interval)
            scenario.advance(interval)
        print()
        scheduled = max(1, int(total_runtime / interval))
        if max_cycles is not None and updates < scheduled:
            print(f"Stopped after {updates} update(s).")
        else:
            print(f"Runtime limit reached ({total_runtime:g} s). Monitor stopped.")
    except KeyboardInterrupt:
        print("\nMonitor stopped.")
    finally:
        plt.close("all")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Monitor the live scenario, or analyze two CSV files once."""
    parser = argparse.ArgumentParser(
        description="Monitor strike-group radar capacity and recommend courses."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Analyze radar and location CSVs a single time instead of looping.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=UPDATE_INTERVAL_SECONDS,
        help="Seconds between live updates (also advances the scenario clock).",
    )
    parser.add_argument(
        "--runtime",
        type=float,
        default=TOTAL_RUNTIME_SECONDS,
        help="Total live runtime in seconds. The monitor stops when this elapses.",
    )
    parser.add_argument(
        "--cycles",
        type=int,
        default=None,
        help="Optional cap on the number of updates. Default is runtime divided by interval.",
    )
    parser.add_argument(
        "--snooze",
        default=str(SNOOZE_FILE),
        help="Cruise file with positions, radar metrics, and the capacity schedule.",
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="With --once: radar_performance.csv and ship_locations.csv.",
    )
    args = parser.parse_args(argv)
    if args.once or len(args.files) == 2:
        radar_path = Path(args.files[0] if args.files else "radar_performance.csv")
        location_path = Path(args.files[1] if len(args.files) > 1 else "ship_locations.csv")
        return run_once(radar_path, location_path)
    if args.interval <= 0 or args.runtime <= 0:
        print("Error: --interval and --runtime must be positive.", file=sys.stderr)
        return 1
    try:
        cruise = load_snooze(Path(args.snooze))
    except (FileNotFoundError, ValueError, pd.errors.ParserError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return run_monitor(args.interval, args.runtime, cruise, args.cycles)


if __name__ == "__main__":
    sys.exit(main())
