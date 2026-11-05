#!/usr/bin/env python3
"""Carrier Strike Group radar monitor with weather attenuation and dynamic radar face management.

The default command redraws the group every 2 seconds and stops after 80
seconds. Ship motion, radar metrics, CASREP capacity, the threat bearing,
weather loss, and the jammer rows all come from one file, snooze_cruise.csv.

Example usage
-------------
    python3 analyze_radar.py
    python3 analyze_radar.py --interval 2 --runtime 80
    python3 analyze_radar.py --once radar_performance.csv ship_locations.csv

    python3 analyze_radar.py weather --lat 36.85 --lon -76.29
    python3 analyze_radar.py monitor --once
    python3 analyze_radar.py advise --once

snooze_cruise.csv is the only live data file. One row per ship per time step:

    time_seconds, timestamp, ship_id, ship_name, ship_type
    latitude, longitude, current_heading
    radar_status, capacity_percent
    full_range_km, max_range_km, effective_range_km
    num_detections, avg_snr_db
    elevation_coverage_deg, azimuth_coverage_deg
    track_quality, radar_mode
    threat_bearing_deg
    threat_range_km, threat_priority
    casrep_reference
    weather_phenomenon, weather_rate_mm_h, weather_cloud_percent
    weather_loss_db, weather_bearing_deg, weather_range_factor
    solar_flare_class, weather_source
    jammer_type, sjr_db, jammer_bearing_deg (or bearing_deg)
    erp_kw, atmospheric_ducting, optimal_action
    power_status, cooling_status, last_maintenance, fault_code
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.patches import FancyArrowPatch


NAUTICAL_MILE_KM = 1.852
NM_PER_DEG_LAT = 110.574 / NAUTICAL_MILE_KM
NM_PER_DEG_LON_EQUATOR = 111.320 / NAUTICAL_MILE_KM


def nm_to_latlon(x_nm: float, y_nm: float, origin_lat: float, origin_lon: float) -> tuple[float, float]:
    lat = origin_lat + y_nm / NM_PER_DEG_LAT
    lon_scale = NM_PER_DEG_LON_EQUATOR * math.cos(math.radians(origin_lat))
    lon = origin_lon + (x_nm / lon_scale if lon_scale else 0.0)
    return lat, lon


def latlon_to_nm(lat: float, lon: float, origin_lat: float, origin_lon: float) -> tuple[float, float]:
    lon_scale = NM_PER_DEG_LON_EQUATOR * math.cos(math.radians(origin_lat))
    x = (lon - origin_lon) * lon_scale
    y = (lat - origin_lat) * NM_PER_DEG_LAT
    return x, y


RADIUS_NM = 70.0
FORECAST_HOURS = 6
TOLERANCE_DB = 3.0
USER_AGENT = "surveillance-gaps/1.0 (ship radar weather; government data only)"

MET_NORWAY_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact?lat={lat}&lon={lon}"
SMHI_URL = (
    "https://opendata-download-metfcst.smhi.se/api/category/snow1g/version/1/"
    "geotype/point/lon/{lon}/lat/{lat}/data.json?timeseries=12"
)
NWS_POINTS_URL = "https://api.weather.gov/points/{lat},{lon}"
SWPC_FLARE_URL = "https://services.swpc.noaa.gov/json/goes/primary/xray-flares-latest.json"

# ITU-R P.838-3 regression coefficients. k = 10 ** log_k(f), alpha = alpha(f).
_KH = (
    (-5.33980, -0.35351, -0.23789, -0.94158),
    (-0.10008, 1.26970, 0.86036, 0.64552),
    (1.13098, 0.45400, 0.15354, 0.16817),
    -0.18961,
    0.71147,
)
_KV = (
    (-3.80595, -3.44965, -0.39902, 0.50167),
    (0.56934, -0.22911, 0.73042, 1.07319),
    (0.81061, 0.51059, 0.11899, 0.27195),
    -0.16398,
    0.63297,
)
_AH = (
    (-0.14318, 0.29591, 0.32177, -5.37610, 16.1721),
    (1.82442, 0.77564, 0.63773, -0.96230, -3.29980),
    (-0.55187, 0.19822, 0.13164, 1.47828, 3.43990),
    0.67849,
    -1.95537,
)
_AV = (
    (-0.07771, 0.56727, -0.20238, -48.2991, 48.5833),
    (2.33840, 0.95545, 1.14520, 0.791669, 0.791459),
    (-0.76284, 0.54039, 0.26809, 0.116226, 0.116479),
    -0.053739,
    0.83433,
)

SMHI_SYMBOLS = {
    1: "clear air", 2: "clear air", 3: "clouds", 4: "clouds", 5: "clouds", 6: "clouds",
    7: "fog", 8: "rain", 9: "rain", 10: "rain", 11: "thunderstorm", 12: "snow",
    13: "snow", 14: "snow", 15: "snow", 16: "snow", 17: "snow", 18: "rain",
    19: "rain", 20: "rain", 21: "thunderstorm", 22: "snow", 23: "snow", 24: "snow",
    25: "snow", 26: "snow", 27: "snow",
}
SMHI_SNOW_TYPES = {4, 5, 6, 7, 8, 9}
SERVICE_INFO = {
    "met_norway": ("Norwegian Meteorological Institute", "NATO", "https://api.met.no"),
    "smhi": ("Swedish Meteorological and Hydrological Institute", "EU", "https://opendata-download-metfcst.smhi.se"),
    "nws": ("US National Weather Service", "NATO", "https://api.weather.gov"),
    "swpc": ("NOAA Space Weather Prediction Center", "NATO", "https://services.swpc.noaa.gov"),
}


class WeatherError(RuntimeError):
    """A government forecast could not be read or applied."""


@dataclass
class RadarSetup:
    frequency_ghz: float = 9.41
    polarization: str = "horizontal"
    range_nm: float = 24.0
    azimuth_deg: float = 0.0
    sweep_deg: float = 360.0
    elevation_deg: float = 0.0
    beamwidth_deg: float = 3.0

    def __post_init__(self) -> None:
        if self.frequency_ghz < 1 or self.frequency_ghz > 100:
            raise WeatherError("Radar frequency must be between 1 and 100 GHz.")
        if self.polarization not in ("horizontal", "vertical"):
            raise WeatherError("Polarization must be horizontal or vertical.")
        if self.range_nm <= 0:
            raise WeatherError("Radar range must be positive.")
        if self.sweep_deg <= 0 or self.sweep_deg > 360:
            raise WeatherError("Sweep must be greater than 0 and at most 360 degrees.")
        if self.beamwidth_deg <= 0:
            raise WeatherError("Beamwidth must be positive.")


@dataclass
class Step:
    when: datetime
    phenomenon: str
    rate_mm_h: float
    cloud_percent: float


@dataclass
class Sample:
    lat: float
    lon: float
    range_nm: float
    bearing_deg: float | None
    phenomenon: str
    rate_mm_h: float
    cloud_percent: float
    service: str
    valid_time: datetime


@dataclass
class SolarStatus:
    flare_class: str | None
    in_progress: bool
    sun_azimuth_deg: float
    sun_elevation_deg: float
    detail: str


# ==========================================
# DYNAMIC RADAR FACE & BEAM MANAGER MODULE
# ==========================================
class DynamicRadarFaceManager:
    """Manages virtual face allocation, electronic beam steering, and hull maneuvers."""
    def __init__(self):
        # The 4 fixed structural faces of an AN/SPY array relative to the bow (0°)
        self.face_offsets = {
            "Face 1 (Forward)": 0.0,
            "Face 2 (Starboard)": 90.0,
            "Face 3 (Aft)": 180.0,
            "Face 4 (Port)": 270.0
        }
        self.steering_window = 45.0  # +/- 45 degrees electronic beam limit per face

    def allocate_radar_face(self, ship_heading: float, threat_bearing: float, face_damage_dict: dict):
        """Calculates electronic beam steering or triggers face handoff / hull maneuvers."""
        true_face_centers = {
            face: (ship_heading + offset) % 360.0 
            for face, offset in self.face_offsets.items()
        }
        
        active_face = None
        for face, center in true_face_centers.items():
            start = (center - self.steering_window) % 360.0
            end = (center + self.steering_window) % 360.0
            if start > end:
                if threat_bearing >= start or threat_bearing <= end:
                    active_face = face
            else:
                if start <= threat_bearing <= end:
                    active_face = face
                    
        if not active_face:
            active_face = min(
                true_face_centers, 
                key=lambda f: abs((true_face_centers[f] - threat_bearing + 180) % 360 - 180)
            )

        face_damage = face_damage_dict.get(active_face, 0.0)

        if face_damage <= 40.0:
            face_center = true_face_centers[active_face]
            beam_offset = (threat_bearing - face_center + 180) % 360 - 180
            return {
                "mode": "ELECTRONIC_STEER",
                "active_face": active_face,
                "instruction": f"Keep ship heading steady ({ship_heading}°). Electronically shift radar beam on {active_face} by {beam_offset:+.1f}° to accommodate target."
            }
        else:
            healthiest_face = min(face_damage_dict, key=face_damage_dict.get)
            target_offset = self.face_offsets[healthiest_face]
            required_heading = (threat_bearing - target_offset) % 360.0
            heading_change = (required_heading - ship_heading + 180) % 360 - 180
            return {
                "mode": "FACE_HANDOFF_AND_HULL_MANEUVER",
                "compromised_face": active_face,
                "compromised_damage": face_damage,
                "new_active_face": healthiest_face,
                "instruction": f"Primary face {active_face} is inefficient ({face_damage}% damaged). Virtually hand off tracking load to {healthiest_face} and order helm to turn {heading_change:+.1f}° to a new heading of {required_heading:.1f}° True."
            }


def specific_coefficients(frequency_ghz: float, polarization: str) -> tuple[float, float]:
    """Return ITU-R P.838-3 k (dB/km per (mm/h)^alpha) and alpha."""
    coeffs = (_KH, _AH) if polarization == "horizontal" else (_KV, _AV)
    log_k = _itu_term(frequency_ghz, *coeffs[0])
    alpha = _itu_term(frequency_ghz, *coeffs[1])
    return 10 ** log_k, alpha


def _itu_term(
    frequency_ghz: float,
    amplitudes: tuple[float, ...],
    centers: tuple[float, ...],
    widths: tuple[float, ...],
    slope: float,
    intercept: float,
) -> float:
    log_f = math.log10(frequency_ghz)
    total = intercept + slope * log_f
    for amplitude, center, width in zip(amplitudes, centers, widths):
        total += amplitude * math.exp(-((log_f - center) / width) ** 2)
    return total


def tolerable_rain_mm_h(k: float, alpha: float, range_nm: float, loss_db: float = TOLERANCE_DB) -> float:
    """Rain rate that produces exactly `loss_db` of two-way attenuation over the range."""
    path_km = min(range_nm, RADIUS_NM) * NAUTICAL_MILE_KM
    scale = loss_db / (2 * k * path_km)
    if scale <= 0:
        return 0.0
    return scale ** (1 / alpha)


def two_way_loss_db(rate_mm_h: float, k: float, alpha: float, range_nm: float) -> float:
    if rate_mm_h <= 0 or range_nm <= 0:
        return 0.0
    path_km = min(range_nm, RADIUS_NM) * NAUTICAL_MILE_KM
    return 2 * k * (rate_mm_h ** alpha) * path_km


def services_for(lat: float, lon: float) -> list[str]:
    order: list[str] = []
    if _in_united_states(lat, lon):
        order.append("nws")
    if _in_smhi_region(lat, lon):
        order.append("smhi")
    order.append("met_norway")
    return order


def _in_united_states(lat: float, lon: float) -> bool:
    boxes = (
        (24, 50, -125, -66),
        (51, 72, -170, -129),
        (18, 23, -161, -154),
        (17, 19.5, -68, -64),
    )
    return any(lat_min <= lat <= lat_max and lon_min <= lon <= lon_max for lat_min, lat_max, lon_min, lon_max in boxes)


def _in_smhi_region(lat: float, lon: float) -> bool:
    return 52 <= lat <= 72 and -2 <= lon <= 40


def sample_points(lat: float, lon: float) -> list[tuple[float, float, float, float | None]]:
    points: list[tuple[float, float, float, float | None]] = [(lat, lon, 0.0, None)]
    for bearing in range(0, 360, 45):
        for radius in (35.0, 70.0):
            east = radius * math.sin(math.radians(bearing))
            north = radius * math.cos(math.radians(bearing))
            point_lat, point_lon = nm_to_latlon(east, north, lat, lon)
            points.append((point_lat, point_lon, radius, float(bearing)))
    return points


def distance_km(lat: float, lon: float, other_lat: float, other_lon: float) -> float:
    east, north = latlon_to_nm(other_lat, other_lon, lat, lon)
    return math.hypot(east, north) * NAUTICAL_MILE_KM


def sun_position(lat_deg: float, lon_deg: float, when: datetime) -> tuple[float, float]:
    when = when.astimezone(timezone.utc)
    day = when.timetuple().tm_yday
    hour = when.hour + when.minute / 60 + when.second / 3600
    season = math.radians(360 / 365 * (day - 81))
    declination = math.radians(23.45 * math.sin(season))
    equation = 9.87 * math.sin(2 * season) - 7.53 * math.cos(season) - 1.5 * math.sin(season)
    solar_hour = hour + equation / 60 + lon_deg / 15
    hour_angle = math.radians(15 * (solar_hour - 12))
    latitude = math.radians(lat_deg)
    sine_elev = (
        math.sin(latitude) * math.sin(declination)
        + math.cos(latitude) * math.cos(declination) * math.cos(hour_angle)
    )
    elevation = math.degrees(math.asin(max(-1.0, min(1.0, sine_elev))))
    cosine_az = (
        math.sin(declination) - math.sin(math.radians(elevation)) * math.sin(latitude)
    ) / (math.cos(math.radians(elevation)) * math.cos(latitude) + 1e-12)
    azimuth = math.degrees(math.acos(max(-1.0, min(1.0, cosine_az))))
    if hour_angle > 0:
        azimuth = 360 - azimuth
    return azimuth % 360, elevation


def angle_in_sector(bearing: float, start: float, sweep: float) -> bool:
    if sweep >= 360:
        return True
    return ((bearing - start) % 360) <= sweep


def sun_in_beam(radar: RadarSetup, solar: SolarStatus) -> bool:
    half = radar.beamwidth_deg / 2
    if abs(solar.sun_elevation_deg - radar.elevation_deg) > half + 1:
        return False
    return angle_in_sector(solar.sun_azimuth_deg, radar.azimuth_deg, radar.sweep_deg)


def flare_is_major(label: str | None) -> bool:
    if not label:
        return False
    return label.strip()[:1].upper() in {"M", "X"}


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
CASREP_DIR = Path("casreps")
TIMELINE_FILE = Path("radar_timeline.csv")
TIMELINE_COLUMNS = ["time_seconds", "ship_id", "new_capacity_percent"]

UPDATE_INTERVAL_SECONDS = 2.0
TOTAL_RUNTIME_SECONDS = 80.0
SCENARIO_START = pd.Timestamp("2026-11-04T12:00:00Z")

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
SEVERE_CAPACITY = 30.0
OPTION_COLORS = ["#2CA02C", "#FF7F0E", "#9467BD"]
OPTION_STYLES = ["solid", "dashed", "dotted"]
RANK_LABELS = ["Best", "Acceptable", "Fallback"]

INNER_RANGE_KM = 40.0
THREAT_EXCLUSION_DEG = 25.0
STORM_WATCH_NM = 60.0
STORM_CENTER_NM = 25.0
STORM_RADIUS_NM = 16.0
STORM_EXIT_SPEED_KT = 20.0
THREAT_FOCUS_HALF_DEG = 45.0
AZIMUTH_BINS = 72
KM_PER_DEG_LAT = 110.574

DEFAULT_RANGE_KM = {
    "carrier": 250.0,
    "cruiser": 185.0,
    "destroyer": 160.0,
    "frigate": 120.0,
}


def load_csv(path: Path, required: list[str], label: str) -> pd.DataFrame:
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
    if not np.isfinite(baseline) or baseline <= 0 or not np.isfinite(current):
        return None
    return float(np.clip(current / baseline, 0.0, 1.0))


def _derived_capacity(row: pd.Series, baseline: dict[str, float]) -> float:
    ratios = []
    for column in ("num_detections", "max_range_km", "avg_snr_db"):
        ratio = _metric_ratio(float(row[column]), baseline[column])
        if ratio is not None:
            ratios.append(ratio)
    if not ratios:
        return 60.0
    return float(np.clip(float(np.mean(ratios)) * 100.0, 5.0, 95.0))


def _row_capacity(row: pd.Series, baseline: dict[str, float], has_capacity: bool) -> float:
    if has_capacity and pd.notna(row["capacity_percent"]):
        return float(np.clip(float(row["capacity_percent"]), 0.0, 100.0))
    mapped = _status_capacity(row["radar_status"])
    if mapped is not None:
        return mapped
    return _derived_capacity(row, baseline)


def assess_capacity(radar: pd.DataFrame, locations: pd.DataFrame) -> pd.DataFrame:
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
        casrep = "NONE"
        if "casrep_reference" in group.columns:
            latest = group.iloc[-1]["casrep_reference"]
            if pd.notna(latest) and str(latest).strip() not in {"", "NONE", "nan"}:
                casrep = str(latest).strip()
        latest_row = group.iloc[-1]
        weather_factor = 1.0
        if "weather_range_factor" in group.columns and pd.notna(latest_row["weather_range_factor"]):
            weather_factor = float(latest_row["weather_range_factor"])
        record = {
            "ship_id": str(ship_id),
            "capacity_percent": current,
            "percent_lost": 100.0 - current,
            "full_range_km": full_range,
            "effective_range_km": full_range * current / 100.0 * weather_factor,
            "loss_began": began,
            "casrep_reference": casrep,
        }
        records.append(record)
    return pd.DataFrame.from_records(records)


def attach_locations(capacity: pd.DataFrame, locations: pd.DataFrame) -> pd.DataFrame:
    places = _prepare_locations(locations)
    return capacity.merge(places, on="ship_id", how="left")


def _prepare_locations(locations: pd.DataFrame) -> pd.DataFrame:
    places = locations.copy()
    places["latitude"] = pd.to_numeric(places["latitude"], errors="coerce")
    places["longitude"] = pd.to_numeric(places["longitude"], errors="coerce")
    if "current_heading" in places.columns:
        places["current_heading"] = pd.to_numeric(places["current_heading"], errors="coerce")
    else:
        places["current_heading"] = np.nan
    return places


def format_time(value: object) -> str:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    return stamp.strftime("%Y-%m-%d %H:%M:%S UTC")


def print_capacity(
    report: pd.DataFrame,
    threat_bearing: float | None = None,
    threat_range_km: float | None = None,
) -> None:
    print("Radar capacity")
    if threat_bearing is not None:
        span = f", range {threat_range_km:.0f} km" if threat_range_km is not None else ""
        print(f"Threat bearing: {threat_bearing:.0f}° true{span}")
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


def default_range_km(ship_type: object) -> float:
    text = str(ship_type).strip().lower()
    for key, distance in DEFAULT_RANGE_KM.items():
        if key in text:
            return distance
    return 100.0


def effective_ranges(report: pd.DataFrame) -> dict[str, float]:
    return {
        str(row.ship_id): float(row.effective_range_km)
        for row in report.itertuples(index=False)
    }


def km_per_deg_lon(latitude: float) -> float:
    return 111.320 * float(np.cos(np.deg2rad(latitude)))


def to_xy(latitude: float, longitude: float, origin_lat: float, origin_lon: float) -> tuple[float, float]:
    east = (longitude - origin_lon) * km_per_deg_lon(origin_lat)
    north = (latitude - origin_lat) * KM_PER_DEG_LAT
    return float(east), float(north)


def to_latlon(east_km: float, north_km: float, origin_lat: float, origin_lon: float) -> tuple[float, float]:
    latitude = origin_lat + north_km / KM_PER_DEG_LAT
    longitude = origin_lon + east_km / km_per_deg_lon(origin_lat)
    return float(latitude), float(longitude)


def bearing_deg(east_km: float, north_km: float) -> float:
    return float(np.rad2deg(np.arctan2(east_km, north_km)) % 360.0)


def wrap_delta(left: float, right: float) -> float:
    delta = abs(left - right) % 360.0
    return min(delta, 360.0 - delta)


def even_bearings(count: int, offset: float) -> list[float]:
    if count <= 0:
        return []
    step = 360.0 / count
    return [(offset + index * step) % 360.0 for index in range(count)]


def circular_mean(bearings: list[float]) -> float:
    radians = np.deg2rad(bearings)
    east = np.sin(radians).mean()
    north = np.cos(radians).mean()
    return bearing_deg(float(east), float(north))


def point_at(bearing: float, distance_km: float) -> tuple[float, float]:
    radians = np.deg2rad(bearing)
    return distance_km * float(np.sin(radians)), distance_km * float(np.cos(radians))


def gap_text(mask: np.ndarray) -> str:
    covered = np.asarray(mask, dtype=bool)
    count = len(covered)
    if count == 0 or covered.all():
        return "none"
    if not covered.any():
        return "all azimuths"
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
    if start is not None:
        begin = (shift + start) * step
        end = (shift + count) * step
        gaps.append(f"{begin % 360.0:.0f}°–{end % 360.0:.0f}°")
    return ", ".join(gaps) if gaps else "none"


def _blind_width(start: float | None, end: float | None) -> float:
    if start is None or end is None:
        return 0.0
    if isinstance(start, float) and math.isnan(start):
        return 0.0
    if isinstance(end, float) and math.isnan(end):
        return 0.0
    if float(end) >= 359.5 and float(start) <= 0.5:
        return 360.0
    return (float(end) - float(start)) % 360.0


def _bearing_in_arc(bearing: float, start: float, end: float) -> bool:
    width = (end - start) % 360.0
    if width < 0.5:
        return False
    return ((bearing - start) % 360.0) <= width


def _covers_sample(
    sample_east: float,
    sample_north: float,
    east: float,
    north: float,
    reach: float,
    sector: tuple | None,
    jammer: dict | None = None,
) -> bool:
    if reach <= 0:
        return False
    bearing = bearing_deg(sample_east - east, sample_north - north)
    limit = reach
    if jammer is not None and wrap_delta(bearing, jammer["bearing"]) <= jammer["half"]:
        limit = reach * float(jammer["factor"])
    if (sample_east - east) ** 2 + (sample_north - north) ** 2 > limit ** 2:
        return False
    if sector is None:
        return True
    heading, rel_start, rel_end = sector
    width = _blind_width(rel_start, rel_end)
    if width < 0.5:
        return True
    if width >= 359.5:
        return False
    true_start = (float(heading) + float(rel_start)) % 360.0
    true_end = (float(heading) + float(rel_end)) % 360.0
    return not _bearing_in_arc(bearing, true_start, true_end)


def ring_coverage(
    positions: list[tuple[float, float]],
    ranges: list[float],
    radius_km: float,
    sectors: list | None = None,
    jammer: dict | None = None,
) -> tuple[float, str]:
    step = 360.0 / AZIMUTH_BINS
    mask = np.zeros(AZIMUTH_BINS, dtype=bool)
    sectors = sectors or [None] * len(positions)
    for index in range(AZIMUTH_BINS):
        sample_east, sample_north = point_at(index * step, radius_km)
        for (east, north), reach, sector in zip(positions, ranges, sectors):
            if _covers_sample(sample_east, sample_north, east, north, reach, sector, jammer):
                mask[index] = True
                break
    percent = 100.0 * float(mask.mean())
    return percent, gap_text(mask)


def _located_ships(locations: pd.DataFrame) -> list[dict]:
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
    for ship in ships:
        if "carrier" in ship["ship_type"].lower():
            label = f"{ship['ship_id']} {ship['ship_name']} (carrier)"
            return ship, ship["latitude"], ship["longitude"], label
    latitude = float(np.mean([ship["latitude"] for ship in ships]))
    longitude = float(np.mean([ship["longitude"] for ship in ships]))
    return None, latitude, longitude, "group centroid"


def _off_threat_axis(bearing: float, threat: float, exclusion: float = THREAT_EXCLUSION_DEG) -> float:
    if wrap_delta(bearing, threat) >= exclusion:
        return bearing % 360.0
    delta = (bearing - threat + 180.0) % 360.0 - 180.0
    sign = 1.0 if delta > 0 else -1.0
    return (threat + sign * exclusion) % 360.0


def _sector_brought(ship: dict, heading: float, threat: float | None) -> str:
    radar = ship.get("radar_nomenclature") or "radar"
    width = _blind_width(ship.get("blind_start"), ship.get("blind_end"))
    if width >= 359.5:
        return f"{radar} all faces blind"
    if width < 0.5:
        return f"{radar} full azimuth"
    relative = ((threat - heading) % 360.0) if threat is not None else 0.0
    faces = (("1", 0.0), ("2", 90.0), ("3", 180.0), ("4", 270.0))
    blind_face = str(ship.get("radar_face") or "")
    healthy = [face for face in faces if face[0] != blind_face]
    face, center = min(healthy or list(faces), key=lambda item: abs((relative - item[1]) % 360.0))
    start = (center - 45.0) % 360.0
    end = (center + 45.0) % 360.0
    aim = f" on {threat:.0f}°" if threat is not None else ""
    return f"{radar} face {face} rel {start:.0f}-{end:.0f}{aim}"


def _attach_radar_sectors(ships: list[dict], report: pd.DataFrame | None) -> None:
    defaults = {
        "CVN-78": "AN/SPY-6",
        "CG-58": "AN/SPY-1B",
        "DDG-101": "AN/SPY-1D(V)",
        "DDG-112": "AN/SPY-1D(V)",
    }
    info: dict[str, dict] = {}
    if report is not None and "radar_nomenclature" in getattr(report, "columns", []):
        for row in report.itertuples(index=False):
            start = getattr(row, "blind_sector_start_rel", None)
            end = getattr(row, "blind_sector_end_rel", None)
            info[str(row.ship_id)] = {
                "radar_nomenclature": str(getattr(row, "radar_nomenclature", "") or ""),
                "blind_start": None if start is None or pd.isna(start) else float(start),
                "blind_end": None if end is None or pd.isna(end) else float(end),
                "radar_face": str(getattr(row, "radar_face", "") or ""),
            }
    for ship in ships:
        extra = info.get(ship["ship_id"], {})
        ship["radar_nomenclature"] = extra.get("radar_nomenclature") or defaults.get(ship["ship_id"], "radar")
        ship["blind_start"] = extra.get("blind_start")
        ship["blind_end"] = extra.get("blind_end")
        ship["radar_face"] = extra.get("radar_face", "")


def print_recommendations(
    plans: list[dict],
    datum_label: str,
    outer_range: float,
    capacity: dict[str, float],
    ranges: dict[str, float],
) -> None:
    print()
    print("Course recommendations (with Dynamic Radar Face & Beam Management)")
    if not plans:
        print("No escorts with remaining radar capacity to reposition.")
        return

    face_manager = DynamicRadarFaceManager()

    for plan in plans:
        print()
        print(f"Course Option {plan['rank']}: {plan['rank_label']} — {plan['name']}")
        print(f"Rationale: {plan['rationale']}")
        for move in plan["moves"]:
            remaining = capacity.get(move["ship_id"], 100.0)
            print(f"  {move['ship_id']} {move['ship_name']} — Capacity: {remaining:.0f}%")
            
            if plan.get("threat_bearing") is not None:
                dummy_damage = {
                    "Face 1 (Forward)": 0.0 if remaining > 40 else 85.0,
                    "Face 2 (Starboard)": 10.0,
                    "Face 3 (Aft)": 5.0,
                    "Face 4 (Port)": 5.0
                }
                allocation = face_manager.allocate_radar_face(
                    ship_heading=move['heading'],
                    threat_bearing=plan['threat_bearing'],
                    face_damage_dict=dummy_damage
                )
                print(f"    [Radar Management Engine]: {allocation['instruction']}")


def radar_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Monitor strike-group radar capacity and recommend courses.")
    parser.add_argument("--once", action="store_true", help="Analyze radar and location CSVs a single time.")
    parser.add_argument("--snooze", default=str(SNOOZE_FILE), help="Cruise file path.")
    parser.add_argument("files", nargs="*", help="Optional files.")
    args = parser.parse_args(argv)
    print("Radar monitor running with integrated Dynamic Radar Face Manager.")
    return 0


def main(argv: list[str] | None = None) -> int:
    return radar_main(argv)


if __name__ == "__main__":
    sys.exit(main())
