#!/usr/bin/env python3
"""Carrier Strike Group radar monitor with weather attenuation.

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

CASREP texts are also saved under casreps/. capacity_percent and radar_status
in snooze_cruise are parsed from those messages. Courses cover the threat
axis from stations at least 25 degrees off that axis, stay in the water, and
limit time spent inside any storm within 60 nautical miles of the carrier.
A CASREP names the radar and the relative sector that is degraded. A course
may be a turn in place, or a move followed by a turn, whichever restores
more coverage with less disruption.

The 2-second loop does not call government weather APIs. Those services are
the weather, monitor, and advise commands, and monitor will not poll faster
than once a minute. The loop reads weather columns already stored in
snooze_cruise. That split is deliberate: a 2-second poll would fight the
forecast tools' own rate limit.
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
    1: "clear air",
    2: "clear air",
    3: "clouds",
    4: "clouds",
    5: "clouds",
    6: "clouds",
    7: "fog",
    8: "rain",
    9: "rain",
    10: "rain",
    11: "thunderstorm",
    12: "snow",
    13: "snow",
    14: "snow",
    15: "snow",
    16: "snow",
    17: "snow",
    18: "rain",
    19: "rain",
    20: "rain",
    21: "thunderstorm",
    22: "snow",
    23: "snow",
    24: "snow",
    25: "snow",
    26: "snow",
    27: "snow",
}
SMHI_SNOW_TYPES = {4, 5, 6, 7, 8, 9}
SERVICE_INFO = {
    "met_norway": (
        "Norwegian Meteorological Institute",
        "NATO",
        "https://api.met.no",
    ),
    "smhi": (
        "Swedish Meteorological and Hydrological Institute",
        "EU",
        "https://opendata-download-metfcst.smhi.se",
    ),
    "nws": (
        "US National Weather Service",
        "NATO",
        "https://api.weather.gov",
    ),
    "swpc": (
        "NOAA Space Weather Prediction Center",
        "NATO",
        "https://services.swpc.noaa.gov",
    ),
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
    """National services to try, most local first. MET Norway covers any ocean point."""
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
    """Ship plus eight bearings at 35 and 70 nautical miles."""
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
    """Solar azimuth (from north) and elevation in degrees. Accurate to a few degrees."""
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


def build_report(
    ship_lat: float,
    ship_lon: float,
    radar: RadarSetup,
    samples: list[Sample],
    solar: SolarStatus,
) -> str:
    center = next((sample for sample in samples if sample.range_nm == 0), None)
    if center is None:
        raise WeatherError("The ship forecast sample is missing.")
    k, alpha = specific_coefficients(radar.frequency_ghz, radar.polarization)
    path_nm = min(radar.range_nm, RADIUS_NM)
    threshold = tolerable_rain_mm_h(k, alpha, path_nm)
    bearing_losses = _bearing_losses(samples, center, radar, k, alpha)
    center_loss = two_way_loss_db(center.rate_mm_h, k, alpha, path_nm)
    scanned = {
        bearing: loss
        for bearing, loss in bearing_losses.items()
        if angle_in_sector(bearing, radar.azimuth_deg, radar.sweep_deg)
    }
    sector_loss = max(scanned.values()) if scanned else center_loss
    weather_over = center_loss > TOLERANCE_DB or sector_loss > TOLERANCE_DB
    solar_hit = solar.in_progress and flare_is_major(solar.flare_class) and sun_in_beam(radar, solar)
    issue = _issue(center, samples, bearing_losses, weather_over, solar_hit)
    worst_bearing, worst_loss = _worst(center_loss, bearing_losses)
    lines = [
        f"Meteorological issue: {issue}",
        (
            f"Radar tolerance: {TOLERANCE_DB:.1f} dB two-way, "
            f"which is {threshold:.2f} mm/h water equivalent over {path_nm:.0f} nmi "
            f"at {radar.frequency_ghz:.2f} GHz {radar.polarization} polarization (ITU-R P.838-3)."
        ),
        f"Ship: {format_position(ship_lat, ship_lon)}",
        (
            f"Scan: azimuth {radar.azimuth_deg:.0f} degrees, sweep {radar.sweep_deg:.0f} degrees, "
            f"elevation {radar.elevation_deg:.0f} degrees, beamwidth {radar.beamwidth_deg:.0f} degrees, "
            f"range {radar.range_nm:.0f} nmi."
        ),
        f"Forecast window: the worst hour in the next {FORECAST_HOURS} hours at each point.",
        f"Samples: {len(samples)} government forecasts inside {RADIUS_NM:.0f} nmi.",
        _loss_line(center, samples, worst_bearing, worst_loss, center_loss),
        _judgment(issue, weather_over, solar_hit, threshold, center),
        f"Solar flare: {solar.detail}",
    ]
    advice = _recommendation(
        radar,
        samples,
        center,
        center_loss,
        bearing_losses,
        weather_over,
        solar_hit,
        solar,
        k,
        alpha,
        threshold,
    )
    if advice:
        lines.append(advice)
    lines.append("Sources: " + _sources(samples))
    return "\n".join(lines)


def collect_forecast(
    ship_lat: float,
    ship_lon: float,
    radar: RadarSetup,
    now: datetime | None = None,
) -> str:
    moment = now or datetime.now(timezone.utc)
    points = sample_points(ship_lat, ship_lon)
    print(
        f"Requesting {len(points)} forecasts within {RADIUS_NM:.0f} nmi of the ship...",
        file=sys.stderr,
    )
    samples = _fetch_all(points, moment)
    solar = fetch_solar(ship_lat, ship_lon, radar, moment)
    return build_report(ship_lat, ship_lon, radar, samples, solar)


def _fetch_all(
    points: list[tuple[float, float, float, float | None]],
    now: datetime,
) -> list[Sample]:
    def load(point: tuple[float, float, float, float | None]) -> Sample:
        lat, lon, radius, bearing = point
        return fetch_sample(lat, lon, radius, bearing, now)

    with ThreadPoolExecutor(max_workers=2) as pool:
        return list(pool.map(load, points))


def fetch_sample(
    lat: float,
    lon: float,
    range_nm: float,
    bearing_deg: float | None,
    now: datetime,
) -> Sample:
    errors: list[str] = []
    for service in services_for(lat, lon):
        try:
            steps, served_lat, served_lon = FETCHERS[service](lat, lon)
        except WeatherError as exc:
            errors.append(str(exc))
            continue
        if service == "smhi" and distance_km(lat, lon, served_lat, served_lon) > 25:
            errors.append("SMHI returned a grid point more than 25 km from the sample.")
            continue
        step = select_step(steps, now)
        return Sample(
            lat=lat,
            lon=lon,
            range_nm=range_nm,
            bearing_deg=bearing_deg,
            phenomenon=step.phenomenon,
            rate_mm_h=step.rate_mm_h,
            cloud_percent=step.cloud_percent,
            service=service,
            valid_time=step.when,
        )
    detail = "; ".join(errors) if errors else "no government forecast service is configured"
    raise WeatherError(f"No government forecast for {lat:.3f}, {lon:.3f}: {detail}")


def select_step(steps: list[Step], now: datetime) -> Step:
    window = [step for step in steps if _in_forecast_window(step.when, now)]
    chosen = window or steps
    if not chosen:
        raise WeatherError("The forecast did not include a time step.")
    rainy = [step for step in chosen if step.rate_mm_h >= 0.1]
    if rainy:
        return max(rainy, key=lambda step: step.rate_mm_h)
    return max(chosen, key=lambda step: step.cloud_percent)


def _in_forecast_window(when: datetime, now: datetime) -> bool:
    return now - timedelta(minutes=30) <= when <= now + timedelta(hours=FORECAST_HOURS)


def fetch_solar(lat: float, lon: float, radar: RadarSetup, now: datetime) -> SolarStatus:
    azimuth, elevation = sun_position(lat, lon, now)
    try:
        payload = get_json(SWPC_FLARE_URL)
    except WeatherError as exc:
        return SolarStatus(None, False, azimuth, elevation, f"NOAA SWPC could not be read ({exc}).")
    event = payload[0] if isinstance(payload, list) and payload else {}
    if not isinstance(event, dict) or not event:
        return SolarStatus(
            None,
            False,
            azimuth,
            elevation,
            "NOAA SWPC reported no X-ray flare event.",
        )
    max_class = str(event.get("max_class") or event.get("current_class") or "").strip() or None
    begin = _optional_time(event.get("begin_time"))
    end = _optional_time(event.get("end_time"))
    in_progress = False
    if begin is not None and (end is None or begin <= now <= end):
        in_progress = flare_is_major(str(event.get("current_class") or max_class or ""))
    detail = _flare_detail(event, max_class, in_progress, end)
    status = SolarStatus(max_class, in_progress, azimuth, elevation, detail)
    if status.in_progress and sun_in_beam(radar, status):
        status.detail += (
            f" The sun is in the beam at azimuth {azimuth:.0f} degrees, elevation {elevation:.0f} degrees."
        )
    elif status.in_progress:
        status.detail += (
            f" The sun is outside the beam (azimuth {azimuth:.0f} degrees, elevation {elevation:.0f} degrees)."
        )
    return status


def _flare_detail(event: dict, max_class: str | None, in_progress: bool, end: datetime | None) -> str:
    label = max_class or "unknown"
    if in_progress:
        return f"NOAA SWPC M/X flare {label} is in progress."
    if end is not None:
        return (
            f"NOAA SWPC latest event max class {label} ended {end.strftime('%Y-%m-%d %H:%M')} UTC. "
            "It is not an in-progress M or X flare."
        )
    current = event.get("current_class")
    if current:
        return f"NOAA SWPC current X-ray class is {current}. It is not an in-progress M or X flare."
    return f"NOAA SWPC latest event class is {label}. It is not an in-progress M or X flare."


def parse_met_norway(payload: dict) -> tuple[list[Step], float, float]:
    series = payload.get("properties", {}).get("timeseries", [])
    geometry = payload.get("geometry", {}).get("coordinates", [None, None])
    if not series:
        raise WeatherError("MET Norway returned no time series.")
    steps: list[Step] = []
    for entry in series:
        data = entry.get("data", {})
        instant = data.get("instant", {}).get("details", {})
        hour = data.get("next_1_hours") or {}
        six = data.get("next_6_hours") or {}
        block = hour or six
        summary = block.get("summary", {}) if isinstance(block, dict) else {}
        details = block.get("details", {}) if isinstance(block, dict) else {}
        amount = _float(details.get("precipitation_amount"))
        hours = 1.0 if hour else 6.0
        rate = amount / hours
        cloud = _float(instant.get("cloud_area_fraction"))
        temperature = instant.get("air_temperature")
        phenomenon = _met_symbol(str(summary.get("symbol_code") or ""), rate, cloud, temperature)
        steps.append(Step(parse_time(entry["time"]), phenomenon, rate, cloud))
    lon = float(geometry[0]) if geometry and geometry[0] is not None else 0.0
    lat = float(geometry[1]) if geometry and len(geometry) > 1 and geometry[1] is not None else 0.0
    return steps, lat, lon


def parse_smhi(payload: dict) -> tuple[list[Step], float, float]:
    series = payload.get("timeSeries", [])
    geometry = payload.get("geometry", {}).get("coordinates", [None, None])
    if not series:
        raise WeatherError("SMHI returned no time series.")
    steps: list[Step] = []
    for entry in series:
        data = entry.get("data", {})
        end = parse_time(entry["time"])
        start = _optional_time(entry.get("intervalParametersStartTime")) or (end - timedelta(hours=1))
        hours = max((end - start).total_seconds() / 3600, 1.0)
        amount = _float(data.get("precipitation_amount_mean"))
        rate = amount / hours
        oktas = _float(data.get("cloud_area_fraction"))
        cloud = min(oktas, 8.0) / 8 * 100
        symbol = int(_float(data.get("symbol_code")))
        precip_type = int(_float(data.get("predominant_precipitation_type_at_surface")))
        frozen = _float(data.get("precipitation_frozen_part"))
        phenomenon = _smhi_phenomenon(symbol, precip_type, frozen, rate)
        steps.append(Step(end, phenomenon, rate, cloud))
    lon = float(geometry[0])
    lat = float(geometry[1])
    return steps, lat, lon


def parse_nws_grid(payload: dict) -> tuple[list[Step], float, float]:
    props = payload.get("properties", {})
    precip = props.get("quantitativePrecipitation", {}).get("values", [])
    sky = props.get("skyCover", {}).get("values", [])
    weather = props.get("weather", {}).get("values", [])
    geometry = payload.get("geometry", {})
    lat, lon = _geometry_point(geometry)
    steps: list[Step] = []
    for item in precip:
        value = item.get("value")
        if value is None:
            continue
        start, end = split_valid_time(item["validTime"])
        hours = max((end - start).total_seconds() / 3600, 1.0)
        rate = float(value) / hours
        cloud = _cover_at(sky, start)
        phenomenon = _nws_phenomenon(_weather_at(weather, start), rate, cloud)
        steps.append(Step(start, phenomenon, rate, cloud))
    if not steps:
        for item in sky:
            start, _end = split_valid_time(item["validTime"])
            cloud = _float(item.get("value"))
            phenomenon = _nws_phenomenon(_weather_at(weather, start), 0.0, cloud)
            steps.append(Step(start, phenomenon, 0.0, cloud))
    if not steps:
        raise WeatherError("NWS grid forecast did not include precipitation or cloud cover.")
    return steps, lat, lon


def _geometry_point(geometry: dict) -> tuple[float, float]:
    coords = geometry.get("coordinates")
    if geometry.get("type") == "Point" and isinstance(coords, list) and len(coords) >= 2:
        return float(coords[1]), float(coords[0])
    return 0.0, 0.0


def _met_symbol(symbol: str, rate: float, cloud: float, temperature: float | None) -> str:
    base = symbol.lower().split("_", 1)[0]
    if "snow" in base or "sleet" in base:
        phenomenon = "snow"
    elif "thunder" in base:
        phenomenon = "thunderstorm"
    elif "rain" in base or "drizzle" in base:
        phenomenon = "rain"
    elif "fog" in base:
        phenomenon = "fog"
    elif "cloud" in base or base == "fair":
        phenomenon = "clouds"
    else:
        phenomenon = "clear air"
    if rate >= 0.1 and phenomenon in {"clear air", "clouds", "fog"}:
        if temperature is not None and float(temperature) <= 0:
            return "snow"
        return "rain"
    if phenomenon == "clear air" and cloud >= 25:
        return "clouds"
    return phenomenon


def _smhi_phenomenon(symbol: int, precip_type: int, frozen: float, rate: float) -> str:
    phenomenon = SMHI_SYMBOLS.get(symbol, "clouds")
    if rate >= 0.1:
        if precip_type in SMHI_SNOW_TYPES or frozen >= 50 or phenomenon == "snow":
            return "snow"
        if precip_type == 2 or symbol in {11, 21}:
            return "thunderstorm"
        if phenomenon in {"clear air", "clouds", "fog"}:
            return "rain"
    return phenomenon


def _nws_phenomenon(items: list[dict], rate: float, cloud: float) -> str:
    names = " ".join(str(item.get("weather") or "").lower() for item in items)
    if any(word in names for word in ("snow", "sleet", "blizzard", "flurr")):
        return "snow"
    if "thunder" in names:
        return "thunderstorm"
    if any(word in names for word in ("rain", "drizzle", "shower")):
        return "rain"
    if "fog" in names or "mist" in names:
        return "fog"
    if rate >= 0.1:
        return "rain"
    if cloud >= 25:
        return "clouds"
    return "clear air"


def _weather_at(values: list[dict], moment: datetime) -> list[dict]:
    for item in values:
        start, end = split_valid_time(item["validTime"])
        if start <= moment < end:
            value = item.get("value") or []
            return [entry for entry in value if isinstance(entry, dict)]
    return []


def _cover_at(values: list[dict], moment: datetime) -> float:
    for item in values:
        start, end = split_valid_time(item["validTime"])
        if start <= moment < end and item.get("value") is not None:
            return float(item["value"])
    return 0.0


def split_valid_time(value: str) -> tuple[datetime, datetime]:
    stamp, _, duration = value.partition("/")
    start = parse_time(stamp)
    hours = parse_iso_duration_hours(duration or "PT1H")
    return start, start + timedelta(hours=hours)


def parse_iso_duration_hours(value: str) -> float:
    digits = ""
    days = hours = minutes = 0.0
    for char in value:
        if char.isdigit() or char == ".":
            digits += char
            continue
        if not digits:
            continue
        amount = float(digits)
        digits = ""
        if char == "D":
            days = amount
        elif char == "H":
            hours = amount
        elif char == "M" and "T" in value:
            minutes = amount
    return max(days * 24 + hours + minutes / 60, 1 / 60)


def _bearing_losses(
    samples: list[Sample],
    center: Sample,
    radar: RadarSetup,
    k: float,
    alpha: float,
) -> dict[float, float]:
    bearings = sorted({sample.bearing_deg for sample in samples if sample.bearing_deg is not None})
    losses: dict[float, float] = {}
    length = min(radar.range_nm, RADIUS_NM)
    for bearing in bearings:
        along = [sample for sample in samples if sample.bearing_deg == bearing]
        along.sort(key=lambda sample: sample.range_nm)
        points = [(0.0, center.rate_mm_h)] + [(sample.range_nm, sample.rate_mm_h) for sample in along]
        losses[bearing] = _integrated_loss(points, length, k, alpha)
    return losses


def _integrated_loss(
    points: list[tuple[float, float]],
    length_nm: float,
    k: float,
    alpha: float,
) -> float:
    if length_nm <= 0 or not points:
        return 0.0
    clipped = [(distance, rate) for distance, rate in points if distance <= length_nm]
    if not clipped or clipped[0][0] > 0:
        clipped.insert(0, (0.0, points[0][1]))
    if clipped[-1][0] < length_nm:
        clipped.append((length_nm, _interp(points, length_nm)))
    loss = 0.0
    for (start, start_rate), (end, end_rate) in zip(clipped, clipped[1:]):
        start_gamma = k * (start_rate ** alpha) if start_rate > 0 else 0.0
        end_gamma = k * (end_rate ** alpha) if end_rate > 0 else 0.0
        loss += (start_gamma + end_gamma) * (end - start) * NAUTICAL_MILE_KM
    return loss


def _interp(points: list[tuple[float, float]], distance: float) -> float:
    ordered = sorted(points)
    if distance <= ordered[0][0]:
        return ordered[0][1]
    for (start, start_rate), (end, end_rate) in zip(ordered, ordered[1:]):
        if start <= distance <= end and end > start:
            weight = (distance - start) / (end - start)
            return start_rate + (end_rate - start_rate) * weight
    return ordered[-1][1]


def _issue(
    center: Sample,
    samples: list[Sample],
    bearing_losses: dict[float, float],
    weather_over: bool,
    solar_hit: bool,
) -> str:
    if weather_over:
        ranked = sorted(
            samples,
            key=lambda sample: (
                sample.rate_mm_h,
                bearing_losses.get(sample.bearing_deg or -1, 0.0),
            ),
            reverse=True,
        )
        for sample in ranked:
            if sample.rate_mm_h >= 0.1 and sample.phenomenon != "clear air":
                return sample.phenomenon
        return center.phenomenon if center.phenomenon != "clear air" else "rain"
    if solar_hit:
        return "solar flare"
    order = ("thunderstorm", "snow", "rain", "fog", "clouds", "clear air")
    present = {sample.phenomenon for sample in samples}
    for name in order:
        if name in present:
            return name
    return "clear air"


def _worst(center_loss: float, bearing_losses: dict[float, float]) -> tuple[float | None, float]:
    if not bearing_losses:
        return None, center_loss
    bearing, loss = max(bearing_losses.items(), key=lambda item: item[1])
    if center_loss >= loss:
        return None, center_loss
    return bearing, loss


def _loss_line(
    center: Sample,
    samples: list[Sample],
    worst_bearing: float | None,
    worst_loss: float,
    center_loss: float,
) -> str:
    if worst_bearing is None:
        return (
            f"Strongest loss: {center_loss:.1f} dB at the ship "
            f"({center.phenomenon}, {center.rate_mm_h:.1f} mm/h, cloud cover {center.cloud_percent:.0f}%)."
        )
    match = next(sample for sample in samples if sample.bearing_deg == worst_bearing)
    return (
        f"Strongest loss: {worst_loss:.1f} dB on bearing {worst_bearing:.0f} "
        f"at {match.range_nm:.0f} nmi ({match.phenomenon}, {match.rate_mm_h:.1f} mm/h)."
    )


def _judgment(
    issue: str,
    weather_over: bool,
    solar_hit: bool,
    threshold: float,
    center: Sample,
) -> str:
    if solar_hit and not weather_over:
        return "Judgment: the solar flare is in the radar beam, above what this scan can tolerate."
    if weather_over:
        return (
            f"Judgment: {issue} is above the tolerated loss. "
            f"The tolerated precipitation rate is {threshold:.2f} mm/h."
        )
    if issue == "clouds":
        return (
            "Judgment: cloud cover stays within the tolerated loss. "
            f"Forecast precipitation at the ship is {center.rate_mm_h:.1f} mm/h, "
            f"under the {threshold:.2f} mm/h threshold."
        )
    return "Judgment: this forecast stays within the tolerated loss."


def _recommendation(
    radar: RadarSetup,
    samples: list[Sample],
    center: Sample,
    center_loss: float,
    bearing_losses: dict[float, float],
    weather_over: bool,
    solar_hit: bool,
    solar: SolarStatus,
    k: float,
    alpha: float,
    threshold: float,
) -> str | None:
    if not weather_over and not solar_hit:
        return None
    path_nm = min(radar.range_nm, RADIUS_NM)
    move = _move_recommendation(samples, center_loss, k, alpha, path_nm, threshold)
    steer = _steer_recommendation(radar, bearing_losses, solar if solar_hit else None)
    if center_loss > TOLERANCE_DB and move:
        return move
    if weather_over and steer:
        extra = ""
        if solar_hit:
            extra = " The recommended sector also keeps the sun outside the beam."
        return steer + extra
    if solar_hit:
        lifted = _sun_recommendation(radar, solar)
        if lifted:
            return lifted
    if weather_over:
        return (
            f"Every sampled heading and every point inside {RADIUS_NM:.0f} nmi "
            f"stays above the {TOLERANCE_DB:.1f} dB tolerated loss."
        )
    return None


def _move_recommendation(
    samples: list[Sample],
    center_loss: float,
    k: float,
    alpha: float,
    path_nm: float,
    threshold: float,
) -> str | None:
    if center_loss <= TOLERANCE_DB:
        return None
    candidates = []
    for sample in samples:
        if sample.bearing_deg is None or sample.range_nm <= 0 or sample.range_nm > RADIUS_NM:
            continue
        loss = two_way_loss_db(sample.rate_mm_h, k, alpha, path_nm)
        if loss <= TOLERANCE_DB:
            candidates.append((sample.range_nm, sample.rate_mm_h, sample.bearing_deg, sample, loss))
    if not candidates:
        return None
    _radius, _rate, bearing, sample, loss = min(candidates)
    return (
        f"Recommendation: move the radar to {format_position(sample.lat, sample.lon)}, "
        f"{sample.range_nm:.0f} nmi on bearing {bearing:.0f}. "
        f"Precipitation there is {sample.rate_mm_h:.1f} mm/h ({loss:.1f} dB), "
        f"under the {threshold:.2f} mm/h threshold."
    )


def _steer_recommendation(
    radar: RadarSetup,
    bearing_losses: dict[float, float],
    solar: SolarStatus | None,
) -> str | None:
    if not bearing_losses:
        return None
    bearings = sorted(bearing_losses)
    sweep = radar.sweep_deg if radar.sweep_deg < 360 else 90.0
    best: tuple[float, float] | None = None
    for start in bearings:
        covered = [bearing for bearing in bearings if angle_in_sector(bearing, start, sweep)]
        if not covered:
            continue
        if solar is not None and angle_in_sector(solar.sun_azimuth_deg, start, sweep):
            continue
        worst = max(bearing_losses[bearing] for bearing in covered)
        if worst > TOLERANCE_DB:
            continue
        if best is None or worst < best[0]:
            best = (worst, start)
    if best is None:
        return None
    worst, start = best
    return (
        f"Recommendation: point the radar to azimuth {start:.0f} degrees, "
        f"elevation {radar.elevation_deg:.0f} degrees, sweep {sweep:.0f} degrees. "
        f"The strongest loss in that sector is {worst:.1f} dB, under the {TOLERANCE_DB:.1f} dB tolerance."
    )


def _sun_recommendation(radar: RadarSetup, solar: SolarStatus) -> str | None:
    if radar.sweep_deg < 360:
        opposite = (solar.sun_azimuth_deg + 180) % 360
        start = (opposite - radar.sweep_deg / 2) % 360
        return (
            f"Recommendation: point the radar to azimuth {start:.0f} degrees, "
            f"sweep {radar.sweep_deg:.0f} degrees, elevation {radar.elevation_deg:.0f} degrees, "
            f"away from the sun at azimuth {solar.sun_azimuth_deg:.0f}."
        )
    lifted = solar.sun_elevation_deg + radar.beamwidth_deg / 2 + 2
    if lifted > 45:
        return None
    return (
        f"Recommendation: raise the radar elevation to {lifted:.0f} degrees "
        f"so the beam clears the sun at elevation {solar.sun_elevation_deg:.0f} degrees."
    )


def _sources(samples: list[Sample]) -> str:
    used = []
    seen = set()
    for sample in samples:
        if sample.service in seen:
            continue
        seen.add(sample.service)
        name, alliance, url = SERVICE_INFO[sample.service]
        used.append(f"{name} ({alliance}), {url}")
    name, alliance, url = SERVICE_INFO["swpc"]
    used.append(f"{name} ({alliance}), {url}")
    return "; ".join(used)


def format_position(lat: float, lon: float) -> str:
    north = "N" if lat >= 0 else "S"
    east = "E" if lon >= 0 else "W"
    return f"{abs(lat):.4f} {north}, {abs(lon):.4f} {east}"


def get_json(url: str) -> dict | list:
    delay = 1.0
    last: WeatherError | None = None
    for attempt in range(4):
        try:
            body = _read_body(url)
        except WeatherError as exc:
            last = exc
            if attempt == 3 or not _retryable(exc):
                raise
            time.sleep(delay)
            delay *= 2
            continue
        try:
            return json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            snippet = _snippet(body)
            raise WeatherError(f"Response from {_host(url)} was not JSON ({snippet}).") from exc
    if last is not None:
        raise last
    raise WeatherError(f"{_host(url)} request failed.")


def _read_body(url: str) -> bytes:
    try:
        return _urllib_bytes(url)
    except WeatherError as exc:
        if "certificate" not in str(exc).lower() and "ssl" not in str(exc).lower():
            raise
        return _curl_bytes(url)


def _retryable(exc: WeatherError) -> bool:
    text = str(exc)
    return any(code in text for code in ("HTTP 429", "HTTP 502", "HTTP 503", "HTTP 504"))


def _snippet(body: bytes) -> str:
    text = body[:160].decode("utf-8", errors="replace").strip().replace("\n", " ")
    if text.startswith("<!") or text.startswith("<html"):
        return "HTML error page"
    return text


def _urllib_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = _snippet(exc.read(300))
        raise WeatherError(f"{_host(url)} returned HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, ssl.SSLError) as exc:
        raise WeatherError(f"{_host(url)} request failed: {exc}") from exc


def _curl_bytes(url: str) -> bytes:
    path = Path(tempfile.mkstemp(suffix=".json")[1])
    try:
        curl_bin = shutil.which("curl") or shutil.which("curl.exe")
        if curl_bin is None:
            path.unlink(missing_ok=True)
            raise WeatherError(f"{_host(url)} request failed: curl is not installed")
        completed = subprocess.run(
            [
                curl_bin,
                "--ssl-no-revoke",
                "-sS",
                "-A",
                USER_AGENT,
                "-H",
                "Accept: application/json",
                "--max-time",
                "45",
                "-w",
                "%{http_code}",
                "-o",
                str(path),
                url,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        path.unlink(missing_ok=True)
        raise WeatherError(f"{_host(url)} request failed: {exc}") from exc
    body = path.read_bytes()
    path.unlink(missing_ok=True)
    code_text = (completed.stdout or "").strip()
    code = int(code_text) if code_text.isdigit() else 0
    if completed.returncode != 0 and code == 0:
        raise WeatherError(f"{_host(url)} request failed: {completed.stderr.strip()}")
    if code >= 400 or code == 0:
        raise WeatherError(f"{_host(url)} returned HTTP {code}: {_snippet(body)}")
    return body


def _host(url: str) -> str:
    return url.split("/")[2]


def parse_time(value: str) -> datetime:
    text = value.replace("Z", "+00:00")
    if text.endswith("+00:00") and text.count("+") == 1:
        moment = datetime.fromisoformat(text)
    else:
        moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _optional_time(value: object) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    return parse_time(value)


def _float(value: object) -> float:
    if value is None or value == "":
        return 0.0
    return float(value)


def _fetch_met_norway(lat: float, lon: float) -> tuple[list[Step], float, float]:
    payload = get_json(MET_NORWAY_URL.format(lat=f"{lat:.4f}", lon=f"{lon:.4f}"))
    if not isinstance(payload, dict):
        raise WeatherError("MET Norway returned an unexpected document.")
    return parse_met_norway(payload)


def _fetch_smhi(lat: float, lon: float) -> tuple[list[Step], float, float]:
    payload = get_json(SMHI_URL.format(lat=f"{lat:.4f}", lon=f"{lon:.4f}"))
    if not isinstance(payload, dict):
        raise WeatherError("SMHI returned an unexpected document.")
    return parse_smhi(payload)


def _fetch_nws(lat: float, lon: float) -> tuple[list[Step], float, float]:
    points = get_json(NWS_POINTS_URL.format(lat=f"{lat:.4f}", lon=f"{lon:.4f}"))
    if not isinstance(points, dict):
        raise WeatherError("NWS point lookup returned an unexpected document.")
    grid_url = points.get("properties", {}).get("forecastGridData")
    if not grid_url:
        raise WeatherError("NWS point lookup did not include a grid forecast.")
    grid = get_json(grid_url)
    if not isinstance(grid, dict):
        raise WeatherError("NWS grid forecast returned an unexpected document.")
    return parse_nws_grid(grid)


FETCHERS = {
    "met_norway": _fetch_met_norway,
    "smhi": _fetch_smhi,
    "nws": _fetch_nws,
}


def forecast_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Check government weather within 70 nmi of a ship and say whether it "
            "exceeds the radar's tolerated loss. Recommends a new azimuth or a new "
            "position when that brings the radar back under the threshold."
        )
    )
    parser.add_argument("--lat", type=float, required=True, help="Ship latitude in degrees")
    parser.add_argument("--lon", type=float, required=True, help="Ship longitude in degrees")
    parser.add_argument("--range-nm", type=float, default=24.0, help="Instrumented radar range in nautical miles")
    parser.add_argument("--frequency-ghz", type=float, default=9.41, help="Radar frequency in GHz")
    parser.add_argument("--polarization", choices=("horizontal", "vertical"), default="horizontal")
    parser.add_argument("--azimuth", type=float, default=0.0, help="Scan start azimuth, degrees from north")
    parser.add_argument("--sweep", type=float, default=360.0, help="Scan width in degrees")
    parser.add_argument("--elevation", type=float, default=0.0, help="Beam elevation in degrees")
    parser.add_argument("--beamwidth", type=float, default=3.0, help="Beamwidth in degrees")
    args = parser.parse_args(argv)
    radar = RadarSetup(
        frequency_ghz=args.frequency_ghz,
        polarization=args.polarization,
        range_nm=args.range_nm,
        azimuth_deg=args.azimuth % 360,
        sweep_deg=args.sweep,
        elevation_deg=args.elevation,
        beamwidth_deg=args.beamwidth,
    )
    print(collect_forecast(args.lat, args.lon, radar))
    return 0


EVENT_RADIUS_NM = 256.0
OVERCAST_PERCENT = 90.0
FOG_AREA_PERCENT = 50.0
MEDIUM_DROPLET_G_M3 = 0.05
THICK_FOG_G_M3 = 0.5
SOLAR = "solar flare"
MET_NORWAY_COMPLETE = (
    "https://api.met.no/weatherapi/locationforecast/2.0/complete?lat={lat}&lon={lon}"
)
PACKAGE_ROOT = Path(__file__).resolve().parent
DEFAULT_EVENTS = PACKAGE_ROOT / "output" / "weather_events.json"
DEFAULT_STATUS = PACKAGE_ROOT / "output" / "station_status.json"
def default_track_path() -> Path:
    """Track CSV beside this file, or in the user's Downloads folder."""
    beside_script = Path(__file__).resolve().parent / "snooze_cruise.csv"
    if beside_script.exists():
        return beside_script
    return Path.home() / "Downloads" / "snooze_cruise.csv"

BUILTIN_STATIONS = (
    ("North Sea", 58.0, 1.5),
    ("Norwegian Sea", 66.5, 2.0),
    ("Northern North Sea", 61.0, 1.0),
    ("Skagerrak", 57.8, 9.2),
    ("Baltic Sea", 58.3, 20.2),
    ("Southern Baltic", 55.2, 17.5),
    ("Bay of Biscay", 45.5, -6.0),
    ("West of Portugal", 39.5, -12.0),
    ("Balearic Sea", 38.8, 3.5),
    ("Tyrrhenian Sea", 39.5, 12.0),
    ("Ionian Sea", 36.2, 18.2),
    ("South of Crete", 34.2, 25.5),
    ("Denmark Strait", 66.0, -27.0),
    ("Mid Atlantic", 47.0, -30.0),
    ("Azores waters", 36.0, -35.0),
    ("Western Atlantic", 37.0, -72.0),
    ("Southwest Atlantic approaches", 30.0, -70.0),
    ("Labrador Sea", 58.0, -54.0),
    ("Caribbean Sea", 16.0, -78.0),
    ("Gulf of Mexico", 25.5, -90.0),
    ("Northeast Pacific", 40.0, -150.0),
    ("Gulf of Alaska", 54.0, -148.0),
    ("Central Pacific", 10.0, -160.0),
    ("South Pacific", -25.0, -130.0),
    ("Northwest Pacific", 32.0, 160.0),
    ("Philippine Sea", 18.0, 135.0),
    ("South China Sea", 13.0, 114.0),
    ("Bay of Bengal", 14.0, 88.0),
    ("Arabian Sea", 14.0, 64.0),
    ("Central Indian Ocean", -8.0, 80.0),
    ("South Atlantic", -32.0, -15.0),
    ("Southern Ocean", -52.0, 20.0),
    ("Tasman Sea", -38.0, 162.0),
    ("Coral Sea", -16.0, 156.0),
    ("Barents Sea", 75.0, 35.0),
    ("Bering Sea", 56.0, -175.0),
)

BUILTIN_SHIPS = (
    ("SIMULANT", "SIM-1", 58.0, 1.5),
    ("PLACEHOLDER", "SIM-2", 47.0, -30.0),
    ("DRILL", "SIM-3", 37.0, -72.0),
    ("PRACTICE", "SIM-4", 36.2, 18.2),
)
SWPC_SOURCE = "NOAA Space Weather Prediction Center (NATO), https://services.swpc.noaa.gov"


@dataclass(frozen=True)
class RadarContext:
    frequency_ghz: float = 9.41
    polarization: str = "horizontal"
    range_nm: float = 24.0
    tolerance_db: float = 3.0
    fog_density_g_m3: float = MEDIUM_DROPLET_G_M3
    radius_nm: float = EVENT_RADIUS_NM

    def __post_init__(self) -> None:
        if self.frequency_ghz < 1 or self.frequency_ghz > 200:
            raise ValueError("Frequency must be from 1 to 200 GHz for ITU-R P.840.")
        if self.polarization not in ("horizontal", "vertical"):
            raise ValueError("Polarization must be horizontal or vertical.")
        if self.range_nm <= 0 or self.radius_nm <= 0 or self.tolerance_db <= 0:
            raise ValueError("Range, radius, and tolerance must be positive.")
        if not (
            math.isclose(self.fog_density_g_m3, MEDIUM_DROPLET_G_M3)
            or math.isclose(self.fog_density_g_m3, THICK_FOG_G_M3)
        ):
            raise ValueError("Fog density must be the ITU-R P.840 medium (0.05) or thick (0.5) value.")


@dataclass(frozen=True)
class Observation:
    name: str
    lat: float
    lon: float
    phenomenon: str
    rate_mm_h: float
    cloud_percent: float
    low_cloud_percent: float | None
    fog_percent: float
    temperature_c: float | None
    service: str
    source: str
    valid_time: datetime


@dataclass(frozen=True)
class WeatherEvent:
    lat: float
    lon: float
    radius_nm: float
    phenomenon: str
    detail: str
    criterion: str
    source: str
    valid_time: str
    loss_db: float
    station: str = ""
    sun_azimuth_deg: float | None = None
    sun_elevation_deg: float | None = None


@dataclass(frozen=True)
class EventFile:
    generated_at: str
    radius_nm: float
    events: list[WeatherEvent]
    sources: list[str]
    note: str


@dataclass(frozen=True)
class Station:
    name: str
    lat: float
    lon: float


@dataclass
class OceanStep:
    when: datetime
    phenomenon: str
    rate_mm_h: float
    cloud_percent: float
    low_cloud_percent: float | None
    fog_percent: float
    temperature_c: float | None


@dataclass(frozen=True)
class Ship:
    name: str
    hull: str
    lat: float
    lon: float


def droplet_coefficient(frequency_ghz: float, temperature_c: float) -> float:
    """ITU-R P.840 specific attenuation, (dB/km) / (g/m^3)."""
    temperature_k = temperature_c + 273.15
    if temperature_k < 230 or temperature_k > 320:
        temperature_k = 273.15
    theta = 300.0 / temperature_k
    epsilon0 = 77.66 + 103.3 * (theta - 1.0)
    epsilon1 = 0.0671 * epsilon0
    epsilon2 = 3.52
    relaxation = theta - 1.0
    primary = 20.20 - 146.0 * relaxation + 316.0 * relaxation * relaxation
    secondary = 39.8 * primary
    if primary <= 0 or secondary <= 0:
        return 0.0
    real = (
        (epsilon0 - epsilon1) / (1.0 + (frequency_ghz / primary) ** 2)
        + (epsilon1 - epsilon2) / (1.0 + (frequency_ghz / secondary) ** 2)
        + epsilon2
    )
    imag = (
        frequency_ghz * (epsilon0 - epsilon1) / (primary * (1.0 + (frequency_ghz / primary) ** 2))
        + frequency_ghz * (epsilon1 - epsilon2) / (secondary * (1.0 + (frequency_ghz / secondary) ** 2))
    )
    if imag <= 0:
        return 0.0
    eta = (2.0 + real) / imag
    return (0.819 * frequency_ghz) / (imag * (1.0 + eta * eta))


def droplet_loss_db(
    frequency_ghz: float,
    temperature_c: float,
    density_g_m3: float,
    range_nm: float,
) -> float:
    if density_g_m3 <= 0 or range_nm <= 0:
        return 0.0
    coefficient = droplet_coefficient(frequency_ghz, temperature_c)
    return 2.0 * coefficient * density_g_m3 * range_nm * NAUTICAL_MILE_KM


def classify_observation(observation: Observation, radar: RadarContext) -> WeatherEvent | None:
    """Return a disk when a cited threshold is exceeded at this ocean point."""
    temperature = 0.0 if observation.temperature_c is None else observation.temperature_c
    candidates: list[tuple[float, int, str, str, str]] = []
    kind = _precip_kind(observation)
    if kind is not None and observation.rate_mm_h > 0:
        k, alpha = specific_coefficients(radar.frequency_ghz, radar.polarization)
        loss = two_way_loss_db(observation.rate_mm_h, k, alpha, radar.range_nm)
        detail = f"{observation.rate_mm_h:.2f} mm/h liquid equivalent, {loss:.1f} dB two-way"
        candidates.append((loss, _priority(kind), kind, detail, _precip_criterion(radar, kind)))

    fog_percent = observation.fog_percent
    if observation.phenomenon == "fog":
        fog_percent = max(fog_percent, 100.0)
    if fog_percent >= FOG_AREA_PERCENT:
        loss = droplet_loss_db(radar.frequency_ghz, temperature, radar.fog_density_g_m3, radar.range_nm)
        density = radar.fog_density_g_m3
        label = "medium" if math.isclose(density, MEDIUM_DROPLET_G_M3) else "thick"
        detail = (
            f"fog over {fog_percent:.0f}% of the area, ITU-R P.840 {label} fog "
            f"{density:.2f} g/m3, {loss:.1f} dB two-way"
        )
        candidates.append((loss, _priority("fog"), "fog", detail, _fog_criterion(radar)))
    else:
        cover = _overcast_percent(observation)
        if cover >= OVERCAST_PERCENT:
            loss = droplet_loss_db(
                radar.frequency_ghz,
                temperature,
                MEDIUM_DROPLET_G_M3,
                radar.range_nm,
            )
            detail = (
                f"cloud cover {cover:.0f}%, ITU-R P.840 medium small-droplet water "
                f"{MEDIUM_DROPLET_G_M3:.2f} g/m3, {loss:.1f} dB two-way"
            )
            candidates.append((loss, _priority("clouds"), "clouds", detail, _cloud_criterion(radar)))

    exceeding = [item for item in candidates if item[0] > radar.tolerance_db]
    if not exceeding:
        return None
    loss, _rank, kind, detail, criterion = max(exceeding)
    return WeatherEvent(
        lat=observation.lat,
        lon=observation.lon,
        radius_nm=radar.radius_nm,
        phenomenon=kind,
        detail=detail,
        criterion=criterion,
        source=observation.source,
        valid_time=observation.valid_time.isoformat(),
        loss_db=loss,
        station=observation.name,
    )


def solar_event(
    name: str,
    lat: float,
    lon: float,
    valid_time: datetime,
    flare_class: str,
    source: str,
    sun_azimuth_deg: float,
    sun_elevation_deg: float,
    radar: RadarContext,
) -> WeatherEvent | None:
    """Dayside sample cell while an M or X flare is in progress."""
    if sun_elevation_deg <= 0:
        return None
    return WeatherEvent(
        lat=lat,
        lon=lon,
        radius_nm=radar.radius_nm,
        phenomenon=SOLAR,
        detail=(
            f"NOAA SWPC {flare_class} flare is in progress. "
            f"Sun azimuth {sun_azimuth_deg:.0f} degrees, elevation {sun_elevation_deg:.0f} degrees."
        ),
        criterion=(
            "NOAA SWPC radio-blackout scale: M-class is R1 or higher and X-class is R3 or higher. "
            "The published disk is the ocean sample radius. Leaving that disk does not leave the sunlit side."
        ),
        source=source,
        valid_time=valid_time.isoformat(),
        loss_db=0.0,
        station=name,
        sun_azimuth_deg=sun_azimuth_deg,
        sun_elevation_deg=sun_elevation_deg,
    )


def distance_nm(lat: float, lon: float, other_lat: float, other_lon: float) -> float:
    _heading, distance = bearing_nm(lat, lon, other_lat, other_lon)
    return distance


def bearing_nm(lat: float, lon: float, other_lat: float, other_lon: float) -> tuple[int, float]:
    """Heading and distance from one point to another. Heading 0 is north."""
    east, north = latlon_to_nm(other_lat, other_lon, lat, lon)
    distance = math.hypot(east, north)
    if distance < 0.05:
        return 0, 0.0
    heading = int(round(math.degrees(math.atan2(east, north)))) % 360
    return heading, distance


def compass_point(heading: int) -> str:
    names = (
        "north",
        "northeast",
        "east",
        "southeast",
        "south",
        "southwest",
        "west",
        "northwest",
    )
    return names[int((heading % 360) + 22.5) // 45 % 8]


def escape_course(events: list[WeatherEvent], lat: float, lon: float) -> tuple[int, float] | None:
    """Shortest heading and distance that ends outside every disk."""
    circles: list[tuple[float, float, float]] = []
    inside = False
    for event in events:
        east, north = latlon_to_nm(event.lat, event.lon, lat, lon)
        circles.append((east, north, event.radius_nm))
        if math.hypot(east, north) < event.radius_nm:
            inside = True
    if not inside:
        return None
    best_heading: int | None = None
    best_distance: float | None = None
    for heading in range(360):
        intervals: list[tuple[float, float]] = []
        for east, north, radius in circles:
            intervals.extend(_ray_intervals(east, north, radius, heading))
        distance = _forward_exit(_merge_intervals(intervals))
        if distance is None:
            continue
        if (
            best_distance is None
            or distance < best_distance - 1e-4
            or (abs(distance - best_distance) <= 1e-4 and (best_heading is None or heading < best_heading))
        ):
            best_heading = heading
            best_distance = distance
    if best_heading is None or best_distance is None:
        return None
    return best_heading, round_up_nm(best_distance)


def solar_escape(sun_azimuth_deg: float, sun_elevation_deg: float) -> tuple[int, float]:
    """Head away from the sun. One degree of elevation is 60 nautical miles."""
    heading = int(round((sun_azimuth_deg + 180.0) % 360.0)) % 360
    return heading, round_up_nm(max(0.0, sun_elevation_deg) * 60.0)


def round_up_nm(distance: float) -> float:
    if distance <= 0:
        return 0.0
    return max(0.1, math.ceil(distance * 10.0 - 1e-9) / 10.0)


def write_event_file(
    path: Path,
    events: list[WeatherEvent],
    *,
    generated_at: datetime,
    radar: RadarContext,
    sources: list[str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": generated_at.astimezone(timezone.utc).isoformat(),
        "radius_nm": radar.radius_nm,
        "note": (
            "Ocean interference disks from NATO and EU government forecasts. "
            "The ship advisor applies these disks to synthesized positions."
        ),
        "reference_radar": {
            "frequency_ghz": radar.frequency_ghz,
            "polarization": radar.polarization,
            "range_nm": radar.range_nm,
            "tolerance_db": radar.tolerance_db,
            "fog_density_g_m3": radar.fog_density_g_m3,
        },
        "sources": sources,
        "events": [_event_dict(event) for event in events],
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(path)


def read_event_file(path: Path) -> EventFile:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Weather event file must be a JSON object.")
    events = payload.get("events", [])
    if not isinstance(events, list):
        raise ValueError("Weather event file has no event list.")
    sources = payload.get("sources", [])
    return EventFile(
        generated_at=str(payload.get("generated_at") or ""),
        radius_nm=float(payload.get("radius_nm") or EVENT_RADIUS_NM),
        events=[_event_from_dict(item) for item in events if isinstance(item, dict)],
        sources=[str(item) for item in sources] if isinstance(sources, list) else [],
        note=str(payload.get("note") or ""),
    )


def containing(events: list[WeatherEvent], lat: float, lon: float) -> list[WeatherEvent]:
    return [event for event in events if distance_nm(lat, lon, event.lat, event.lon) < event.radius_nm]


def station_records(
    observations: list[Observation],
    events: list[WeatherEvent],
    failures: list[str],
) -> dict[str, dict]:
    records: dict[str, dict] = {}
    for observation in observations:
        records[observation.name] = {
            "lat": observation.lat,
            "lon": observation.lon,
            "status": "clear",
            "weather": observation.phenomenon,
            "detail": (
                f"{observation.phenomenon}, {observation.rate_mm_h:.2f} mm/h, "
                f"cloud {observation.cloud_percent:.0f}%"
            ),
        }
    for event in events:
        record = records.setdefault(
            event.station,
            {
                "lat": event.lat,
                "lon": event.lon,
                "status": "clear",
                "weather": event.phenomenon,
                "detail": event.detail,
            },
        )
        if record["status"] == "clear":
            record["status"] = event.phenomenon
        elif event.phenomenon not in record["status"].split(", "):
            record["status"] = f"{record['status']}, {event.phenomenon}"
        weather = str(record.get("weather") or event.phenomenon)
        if event.phenomenon not in weather.split(", "):
            record["weather"] = f"{weather}, {event.phenomenon}"
        record["detail"] = event.detail
        record["lat"] = event.lat
        record["lon"] = event.lon
    for failure in failures:
        name, _, detail = failure.partition(":")
        name = name.strip() or failure
        if name not in records:
            records[name] = {
                "lat": None,
                "lon": None,
                "status": "no forecast",
                "weather": "no forecast",
                "detail": detail.strip(),
            }
    return records


def format_status_changes(previous: dict, current: dict) -> str:
    """Report interference that entered a location, cleared, or changed type."""
    lines = []
    for name in sorted(set(previous) | set(current)):
        old = str(previous.get(name, {}).get("status") or "not checked")
        new = str(current.get(name, {}).get("status") or "not checked")
        if old == new:
            continue
        record = current.get(name) or previous.get(name) or {}
        place = _status_place(record)
        detail = str(record.get("detail") or "")
        if not _interfering(old) and _interfering(new):
            lines.append(f"ENTERED {name}, {place}: {new} is occurring. {detail}".rstrip())
        elif _interfering(old) and new == "clear":
            lines.append(
                f"CLEARED {name}, {place}: {old} has cleared. Weather occurring: {_weather_name(record)}."
            )
        elif _interfering(old) and _interfering(new):
            lines.append(f"CHANGED {name}, {place}: {old} is now {new}. {detail}".rstrip())
    return "\n".join(lines)


def _weather_name(record: dict) -> str:
    weather = str(record.get("weather") or "").strip()
    if weather:
        return weather
    detail = str(record.get("detail") or "").strip()
    if detail:
        return detail.split(",", 1)[0].strip()
    return "unknown"


def _interfering(status: str) -> bool:
    return status not in {"", "clear", "not checked", "no forecast"}


def _status_place(record: dict) -> str:
    lat = record.get("lat")
    lon = record.get("lon")
    if lat is None or lon is None:
        return "position unavailable"
    return format_position(float(lat), float(lon))


def load_status(path: Path) -> dict:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    stations = payload.get("stations", {}) if isinstance(payload, dict) else {}
    return stations if isinstance(stations, dict) else {}


def save_status(path: Path, records: dict[str, dict], generated_at: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": generated_at.astimezone(timezone.utc).isoformat(),
        "stations": records,
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(path)


def load_track_ships(path: Path) -> list[Ship]:
    """Latest latitude and longitude for each ship in a track CSV."""
    latest: dict[str, tuple[tuple[str, str], str, float, float]] = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            if not isinstance(row, dict):
                continue
            hull = str(row.get("ship_id") or "").strip()
            lat_text = row.get("latitude")
            lon_text = row.get("longitude")
            if not hull or lat_text in (None, "") or lon_text in (None, ""):
                continue
            lat = float(lat_text)
            lon = float(lon_text)
            if not -90 <= lat <= 90 or not -180 <= lon <= 180:
                raise ValueError(f"{hull} has a position outside latitude or longitude bounds.")
            key = (str(row.get("timestamp") or ""), str(row.get("time_seconds") or ""))
            name = str(row.get("ship_name") or hull).strip() or hull
            previous = latest.get(hull)
            if previous is None or key >= previous[0]:
                latest[hull] = (key, name, lat, lon)
    if not latest:
        raise ValueError(f"No ship positions in {path}.")
    return [Ship(item[1], hull, item[2], item[3]) for hull, item in sorted(latest.items())]


def stations_for_ships(ships: list[Ship], radius_nm: float) -> list[Station]:
    """Sample the center and two rings inside the radius of each ship group."""
    return [sample for station in _ship_centers(ships, radius_nm) for sample in _ring_samples(station, radius_nm)]


def _ship_centers(ships: list[Ship], radius_nm: float) -> list[Station]:
    clusters: list[list[Ship]] = []
    for ship in ships:
        placed = False
        for cluster in clusters:
            lat, lon = _cluster_center(cluster)
            if distance_nm(lat, lon, ship.lat, ship.lon) <= radius_nm:
                cluster.append(ship)
                placed = True
                break
        if not placed:
            clusters.append([ship])
    return [
        Station(", ".join(ship.hull for ship in cluster), *_cluster_center(cluster))
        for cluster in clusters
    ]


def _ring_samples(center: Station, radius_nm: float) -> list[Station]:
    samples = [Station(f"{center.name} center", center.lat, center.lon)]
    for fraction in (0.5, 0.85):
        distance = radius_nm * fraction
        for bearing in range(0, 360, 45):
            east = distance * math.sin(math.radians(bearing))
            north = distance * math.cos(math.radians(bearing))
            lat, lon = nm_to_latlon(east, north, center.lat, center.lon)
            lon = ((lon + 180.0) % 360.0) - 180.0
            samples.append(
                Station(f"{center.name} {compass_point(bearing)} {distance:.0f} nmi", lat, lon)
            )
    return samples


def _cluster_center(ships: list[Ship]) -> tuple[float, float]:
    return (
        sum(ship.lat for ship in ships) / len(ships),
        sum(ship.lon for ship in ships) / len(ships),
    )


def format_track_positions(ships: list[Ship], radius_nm: float) -> str:
    lines = [f"Latest track positions, {radius_nm:.0f} nmi radius:"]
    for ship in ships:
        lines.append(f"  {ship.hull} {ship.name}, {format_position(ship.lat, ship.lon)}")
    return "\n".join(lines)


def monitor_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Poll NATO and EU government forecasts within 256 nmi of the latest "
            "ship positions. Write interfering weather with its direction from the ships."
        )
    )
    parser.add_argument(
        "--track",
        type=Path,
        default=None,
        help="CSV whose latest latitude and longitude are used for each ship",
    )
    parser.add_argument("--stations", type=Path, default=None, help="Optional ocean station JSON")
    parser.add_argument(
        "--global-stations",
        action="store_true",
        help="Scan the fixed ocean list instead of the ship track",
    )
    parser.add_argument("--events", type=Path, default=DEFAULT_EVENTS, help="Event file for the ship advisor")
    parser.add_argument("--interval", type=int, default=1800, help="Seconds between polls (minimum 60)")
    parser.add_argument("--once", action="store_true", help="Poll one time and exit")
    parser.add_argument(
        "--changes-only",
        action="store_true",
        help="Print a location when interference enters it, clears, or changes type",
    )
    parser.add_argument("--max-stations", type=int, default=0, help="Use only the first N stations")
    parser.add_argument("--frequency-ghz", type=float, default=9.41)
    parser.add_argument("--polarization", choices=("horizontal", "vertical"), default="horizontal")
    parser.add_argument("--range-nm", type=float, default=24.0, help="Path length used for attenuation")
    parser.add_argument("--tolerance-db", type=float, default=3.0)
    parser.add_argument("--radius-nm", type=float, default=EVENT_RADIUS_NM)
    parser.add_argument(
        "--fog-density",
        type=float,
        choices=(0.05, 0.5),
        default=0.05,
        help="ITU-R P.840 fog water density: 0.05 medium or 0.5 thick",
    )
    args = parser.parse_args(argv)
    if args.track is None:
        args.track = default_track_path()
    if not args.once and args.interval < 60:
        parser.error("--interval must be at least 60 seconds so government forecasts are not polled in a tight loop.")
    radar = RadarContext(
        frequency_ghz=args.frequency_ghz,
        polarization=args.polarization,
        range_nm=args.range_nm,
        tolerance_db=args.tolerance_db,
        fog_density_g_m3=args.fog_density,
        radius_nm=args.radius_nm,
    )
    while True:
        try:
            stations, ships = _monitor_targets(args)
        except (OSError, ValueError, csv.Error) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if args.max_stations > 0:
            stations = stations[: args.max_stations]
        if not stations:
            print("No positions to check.", file=sys.stderr)
            return 1
        now = datetime.now(timezone.utc)
        label = "ship-radius samples" if ships is not None else "ocean stations"
        print(
            f"{now.strftime('%Y-%m-%d %H:%M')} UTC  checking {len(stations)} {label}...",
            file=sys.stderr,
        )
        previous = load_status(DEFAULT_STATUS) if args.changes_only else {}
        observations, failures, sources = collect_stations(stations, now, radar)
        flare_class, flare_on, flare_detail = fetch_flare(now)
        events = build_events(observations, radar, flare_class if flare_on else None, SWPC_SOURCE)
        if flare_on:
            sources = [*sources, SWPC_SOURCE]
        records = station_records(observations, events, failures)
        write_event_file(args.events, events, generated_at=now, radar=radar, sources=sources)
        save_status(DEFAULT_STATUS, records, now)
        if args.changes_only:
            changes = format_status_changes(previous, records)
            print(changes if changes else "NO_STATUS_CHANGE")
        else:
            report = format_cycle(
                observations,
                events,
                failures,
                flare_detail,
                args.events,
                ships=ships,
            )
            if ships is not None:
                report = f"{format_track_positions(ships, radar.radius_nm)}\n{report}"
            print(report)
        if failures and not observations and args.once:
            return 1
        if args.once:
            return 0
        time.sleep(args.interval)


def advise_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read interference disks and synthesized ship positions. "
            "Alert a ship that is inside a disk with a heading and distance."
        )
    )
    parser.add_argument("--ships", type=Path, default=None)
    parser.add_argument("--events", type=Path, default=DEFAULT_EVENTS)
    parser.add_argument("--interval", type=int, default=1800, help="Seconds between checks (minimum 60)")
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Add a snow disk on the first ship so the alert format can be seen without live weather",
    )
    args = parser.parse_args(argv)
    if not args.once and args.interval < 60:
        parser.error("--interval must be at least 60 seconds.")
    ships = load_ships(args.ships)
    if not ships:
        print("No synthesized ships are listed.", file=sys.stderr)
        return 1

    while True:
        if not args.events.exists():
            print(f"No weather file at {args.events}. Start the ocean monitor first.")
            if args.once:
                return 1
            time.sleep(args.interval)
            continue
        event_file = read_event_file(args.events)
        events = list(event_file.events)
        if args.demo:
            events.append(_demo_event(ships[0]))
        print(format_watch(ships, events, event_file, args.demo))
        if args.once:
            return 0
        time.sleep(args.interval)


def _monitor_targets(args: argparse.Namespace) -> tuple[list[Station], list[Ship] | None]:
    if args.stations is not None:
        return load_stations(args.stations), None
    if args.global_stations:
        return load_stations(None), None
    if not args.track.exists():
        raise ValueError(f"Ship track not found: {args.track}")
    ships = load_track_ships(args.track)
    return stations_for_ships(ships, args.radius_nm), ships


def load_stations(path: Path | None) -> list[Station]:
    if path is None:
        return [Station(name, lat, lon) for name, lat, lon in BUILTIN_STATIONS]
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("stations", payload) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("Station file must be a list or an object with a stations list.")
    stations: list[Station] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        lat = float(row["lat"])
        lon = float(row["lon"])
        if not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise ValueError(f"Station {row.get('name')} is outside latitude or longitude bounds.")
        stations.append(Station(str(row.get("name") or "ocean"), lat, lon))
    return stations


def collect_stations(
    stations: list[Station],
    now: datetime,
    radar: RadarContext,
) -> tuple[list[Observation], list[str], list[str]]:
    def load(station: Station) -> tuple[Observation | None, str | None]:
        try:
            return fetch_observation(station, now, radar), None
        except WeatherError as exc:
            return None, f"{station.name}: {exc}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        loaded = list(pool.map(load, stations))
    observations = [item for item, _error in loaded if item is not None]
    failures = [error for _item, error in loaded if error]
    sources: list[str] = []
    for observation in observations:
        if observation.source not in sources:
            sources.append(observation.source)
    return observations, failures, sources


def build_events(
    observations: list[Observation],
    radar: RadarContext,
    flare_class: str | None,
    flare_source: str,
) -> list[WeatherEvent]:
    events: list[WeatherEvent] = []
    for observation in observations:
        event = classify_observation(observation, radar)
        if event is not None:
            events.append(event)
        if flare_class:
            azimuth, elevation = sun_position(observation.lat, observation.lon, observation.valid_time)
            solar = solar_event(
                observation.name,
                observation.lat,
                observation.lon,
                observation.valid_time,
                flare_class,
                flare_source,
                azimuth,
                elevation,
                radar,
            )
            if solar is not None:
                events.append(solar)
    return events


def fetch_observation(station: Station, now: datetime, radar: RadarContext) -> Observation:
    errors: list[str] = []
    for service in services_for(station.lat, station.lon):
        try:
            steps = OCEAN_FETCHERS[service](station.lat, station.lon)
        except WeatherError as exc:
            errors.append(str(exc))
            continue
        step = choose_step(steps, now, station, service, radar)
        name, alliance, url = SERVICE_INFO[service]
        return Observation(
            name=station.name,
            lat=station.lat,
            lon=station.lon,
            phenomenon=step.phenomenon,
            rate_mm_h=step.rate_mm_h,
            cloud_percent=step.cloud_percent,
            low_cloud_percent=step.low_cloud_percent,
            fog_percent=step.fog_percent,
            temperature_c=step.temperature_c,
            service=service,
            source=f"{name} ({alliance}), {url}",
            valid_time=step.when,
        )
    detail = "; ".join(errors) if errors else "no government forecast service is configured"
    raise WeatherError(f"No government forecast for {station.lat:.3f}, {station.lon:.3f}: {detail}")


def choose_step(
    steps: list[OceanStep],
    now: datetime,
    station: Station,
    service: str,
    radar: RadarContext,
) -> OceanStep:
    window = [step for step in steps if _in_forecast_window(step.when, now)]
    pool = window or steps
    if not pool:
        raise WeatherError("The forecast did not include a time step.")

    def score(step: OceanStep) -> tuple[float, float, float]:
        event = classify_observation(_preview(station, service, step), radar)
        loss = event.loss_db if event is not None else 0.0
        return (loss, step.rate_mm_h, step.fog_percent)

    return max(pool, key=score)


def fetch_flare(now: datetime) -> tuple[str | None, bool, str]:
    try:
        payload = get_json(SWPC_FLARE_URL)
    except WeatherError as exc:
        return None, False, f"NOAA SWPC could not be read ({exc})."
    event = payload[0] if isinstance(payload, list) and payload else {}
    if not isinstance(event, dict) or not event:
        return None, False, "NOAA SWPC reported no X-ray flare event."
    max_class = str(event.get("max_class") or event.get("current_class") or "").strip() or None
    begin = _optional_time(event.get("begin_time"))
    end = _optional_time(event.get("end_time"))
    current = str(event.get("current_class") or max_class or "")
    in_progress = bool(begin is not None and (end is None or begin <= now <= end) and flare_is_major(current))
    label = max_class or "unknown"
    if in_progress:
        return label, True, f"NOAA SWPC M/X flare {label} is in progress."
    if end is not None:
        return label, False, (
            f"NOAA SWPC latest event max class {label} ended {end.strftime('%Y-%m-%d %H:%M')} UTC."
        )
    return label, False, f"NOAA SWPC latest event class is {label}. It is not an in-progress M or X flare."


def parse_met_norway_ocean(payload: dict) -> list[OceanStep]:
    series = payload.get("properties", {}).get("timeseries", [])
    if not series:
        raise WeatherError("MET Norway returned no time series.")
    steps: list[OceanStep] = []
    for entry in series:
        data = entry.get("data", {})
        instant = data.get("instant", {}).get("details", {})
        hour = data.get("next_1_hours") or {}
        six = data.get("next_6_hours") or {}
        block = hour or six
        summary = block.get("summary", {}) if isinstance(block, dict) else {}
        details = block.get("details", {}) if isinstance(block, dict) else {}
        amount = _float(details.get("precipitation_amount"))
        period = 1.0 if hour else 6.0
        rate = amount / period
        cloud = _float(instant.get("cloud_area_fraction"))
        low = instant.get("cloud_area_fraction_low")
        fog = instant.get("fog_area_fraction")
        temperature = instant.get("air_temperature")
        temp = None if temperature is None else float(temperature)
        steps.append(
            OceanStep(
                when=parse_time(entry["time"]),
                phenomenon=_met_symbol(str(summary.get("symbol_code") or ""), rate, cloud, temp),
                rate_mm_h=rate,
                cloud_percent=cloud,
                low_cloud_percent=None if low is None else _float(low),
                fog_percent=0.0 if fog is None else _float(fog),
                temperature_c=temp,
            )
        )
    return steps


def format_cycle(
    observations: list[Observation],
    events: list[WeatherEvent],
    failures: list[str],
    flare_detail: str,
    path: Path,
    ships: list[Ship] | None = None,
) -> str:
    lines = [
        f"Stations read: {len(observations)}. Interference disks: {len(events)}.",
        f"Solar flare: {flare_detail}",
    ]
    if ships:
        lines.extend(format_direction_miles(ships, observations, events))
    else:
        lines.extend(_weather_lines(observations))
    if events:
        lines.append("Interference:")
        for event in events:
            place = _event_place(event, ships)
            lines.append(
                f"  {event.phenomenon} is occurring {place}. {event.detail}"
            )
    elif ships is not None:
        lines.append("No sample inside the ship radius is over the radar-interference threshold.")
    else:
        lines.append("No ocean station is over the radar-interference threshold.")
    if ships and events:
        lines.extend(format_avoidance(ships, events))
    for failure in failures:
        lines.append(f"Failed: {failure}")
    lines.append(f"Wrote {path}")
    return "\n".join(lines)


def format_avoidance(ships: list[Ship], events: list[WeatherEvent]) -> list[str]:
    """Heading and nautical miles to sail until the ships are clear of interference."""
    weather = [event for event in events if event.phenomenon != SOLAR]
    solar = [event for event in events if event.phenomenon == SOLAR]
    lines = [
        *_avoidance_lines(ships, weather, solar=False),
        *_avoidance_lines(ships, solar, solar=True),
    ]
    if not lines:
        return []
    return ["Course to sail clear:", *lines]


def _avoidance_lines(ships: list[Ship], events: list[WeatherEvent], solar: bool) -> list[str]:
    grouped: dict[tuple[int, float], list[str]] = {}
    blocked: list[str] = []
    for ship in ships:
        if not containing(events, ship.lat, ship.lon):
            continue
        if solar:
            nearest = min(events, key=lambda event: distance_nm(ship.lat, ship.lon, event.lat, event.lon))
            azimuth = nearest.sun_azimuth_deg if nearest.sun_azimuth_deg is not None else 0.0
            elevation = nearest.sun_elevation_deg if nearest.sun_elevation_deg is not None else 0.0
            course = solar_escape(azimuth, elevation)
        else:
            course = escape_course(events, ship.lat, ship.lon)
        if course is None:
            blocked.append(ship.hull)
            continue
        heading, distance = course
        grouped.setdefault((heading, distance), []).append(ship.hull)
    lines = []
    for heading, distance in sorted(grouped):
        hulls = ", ".join(grouped[(heading, distance)])
        if solar:
            lines.append(
                f"  {hulls}: steer heading {heading:03d} for {distance:.1f} nautical miles "
                "to put the sun below the horizon."
            )
        else:
            lines.append(
                f"  {hulls}: steer heading {heading:03d} for {distance:.1f} nautical miles "
                "to sail around the interfering weather."
            )
    for hull in blocked:
        lines.append(f"  {hull}: no single heading clears every interfering weather disk.")
    return lines


def _event_place(event: WeatherEvent, ships: list[Ship] | None) -> str:
    position = f"{event.station}, {format_position(event.lat, event.lon)}, {event.radius_nm:.0f} nmi"
    if not ships:
        return f"at {position}"
    relations = [
        (ship, *bearing_nm(ship.lat, ship.lon, event.lat, event.lon))
        for ship in ships
    ]
    if all(distance < 1.0 for _ship, _heading, distance in relations):
        return f"at the ships, {position}"
    if _same_direction(relations):
        _ship, heading, distance = relations[0]
        return (
            f"{distance:.0f} nmi {compass_point(heading)} of the ships, bearing {heading:03d}, {position}"
        )
    parts = []
    for ship, heading, distance in relations:
        if distance < 1.0:
            parts.append(f"at {ship.hull}")
        else:
            parts.append(
                f"{distance:.0f} nmi {compass_point(heading)} of {ship.hull}, bearing {heading:03d}"
            )
    return f"{'; '.join(parts)}, {position}"


def _same_direction(relations: list[tuple[Ship, int, float]]) -> bool:
    headings = [heading for _ship, heading, _distance in relations]
    distances = [distance for _ship, _heading, distance in relations]
    if max(distances) - min(distances) > 20.0:
        return False
    base = headings[0]
    return all(_heading_gap(base, heading) <= 15 for heading in headings)


def _heading_gap(left: int, right: int) -> int:
    gap = abs(left - right) % 360
    return min(gap, 360 - gap)


def _weather_lines(observations: list[Observation]) -> list[str]:
    groups: dict[str, list[str]] = {}
    for observation in observations:
        place = f"{observation.name} ({format_position(observation.lat, observation.lon)})"
        groups.setdefault(observation.phenomenon, []).append(place)
    lines = ["Weather occurring:"]
    for phenomenon in sorted(groups):
        lines.append(f"  {phenomenon}: {', '.join(groups[phenomenon])}")
    if not groups:
        lines.append("  no forecast")
    return lines


def format_direction_miles(
    ships: list[Ship],
    observations: list[Observation],
    events: list[WeatherEvent],
) -> list[str]:
    """Weather type and distance from the ships along each compass bearing."""
    if not ships:
        return []
    origin_lat = sum(ship.lat for ship in ships) / len(ships)
    origin_lon = sum(ship.lon for ship in ships) / len(ships)
    buckets: dict[str, list[tuple[float, str, bool]]] = {}
    for observation in observations:
        heading, distance = bearing_nm(origin_lat, origin_lon, observation.lat, observation.lon)
        key = "at the ships" if distance < 1.0 else compass_point(heading)
        interfering = any(
            distance_nm(observation.lat, observation.lon, event.lat, event.lon) < 1.0
            for event in events
        )
        buckets.setdefault(key, []).append((distance, observation.phenomenon, interfering))
    lines = ["Miles from the ships:"]
    order = (
        "at the ships",
        "north",
        "northeast",
        "east",
        "southeast",
        "south",
        "southwest",
        "west",
        "northwest",
    )
    for key in order:
        rows = buckets.get(key)
        if not rows:
            continue
        parts = []
        for distance, phenomenon, interfering in sorted(rows):
            place = "0 nmi" if distance < 1.0 else f"{distance:.0f} nmi"
            if interfering:
                place += ", interfering"
            parts.append(f"{phenomenon} at {place}")
        lines.append(f"  {key}: {', '.join(parts)}")
    if len(lines) == 1:
        lines.append("  no forecast")
    return lines


def load_ships(path: Path | None) -> list[Ship]:
    if path is None:
        return [Ship(name, hull, lat, lon) for name, hull, lat, lon in BUILTIN_SHIPS]
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("ships", []) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("Ship file must contain a ships list.")
    ships: list[Ship] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ships.append(
            Ship(
                name=str(row.get("name") or "SYNTHETIC"),
                hull=str(row.get("hull") or "SIM-0"),
                lat=float(row["lat"]),
                lon=float(row["lon"]),
            )
        )
    return ships


def format_watch(ships: list[Ship], events: list[WeatherEvent], event_file: EventFile, demo: bool) -> str:
    lines = [
        "Ship positions are synthesized. They are not live US Navy tracks.",
        (
            f"Weather file: {event_file.generated_at or 'undated'}, "
            f"{len(event_file.events)} disks, radius {event_file.radius_nm:.0f} nmi."
        ),
    ]
    if demo:
        lines.append("A demonstration snow disk was added on the first ship.")
    alerts = [text for text in (format_alert(ship, events) for ship in ships) if text]
    if alerts:
        lines.extend(alerts)
    else:
        lines.append("No synthesized ship is inside an interference radius.")
    return "\n".join(lines)


def format_alert(ship: Ship, events: list[WeatherEvent]) -> str | None:
    hits = containing(events, ship.lat, ship.lon)
    if not hits:
        return None
    lines = [
        f"ALERT {ship.hull} {ship.name}",
        f"Synthesized position {format_position(ship.lat, ship.lon)} is inside an interference radius.",
    ]
    for event in hits:
        place = format_position(event.lat, event.lon)
        lines.append(
            f"Weather occurring: {event.phenomenon} at {place}, {event.radius_nm:.0f} nmi radius. "
            f"{event.detail} Source: {event.source}."
        )
    weather = [event for event in events if event.phenomenon != SOLAR]
    solar = [event for event in hits if event.phenomenon == SOLAR]
    if any(event.phenomenon != SOLAR for event in hits):
        course = escape_course(weather, ship.lat, ship.lon)
        if course is None:
            lines.append("No heading leaves every weather disk.")
        else:
            heading, distance = course
            lines.append(
                f"Steer heading {heading:03d} for {distance:.1f} nautical miles to leave the weather radius."
            )
    if solar:
        nearest = min(solar, key=lambda event: distance_nm(ship.lat, ship.lon, event.lat, event.lon))
        azimuth = nearest.sun_azimuth_deg if nearest.sun_azimuth_deg is not None else 0.0
        elevation = nearest.sun_elevation_deg if nearest.sun_elevation_deg is not None else 0.0
        heading, distance = solar_escape(azimuth, elevation)
        lines.append(
            f"Solar flare: steer heading {heading:03d} for {distance:.1f} nautical miles "
            "to put the sun below the horizon. The flare can also end before the ship arrives."
        )
    return "\n".join(lines)


def _precip_kind(observation: Observation) -> str | None:
    if observation.phenomenon in {"rain", "snow", "thunderstorm"}:
        return observation.phenomenon
    if observation.rate_mm_h <= 0:
        return None
    if observation.temperature_c is not None and observation.temperature_c <= 0:
        return "snow"
    return "rain"


def _overcast_percent(observation: Observation) -> float:
    if observation.low_cloud_percent is not None:
        return observation.low_cloud_percent
    if observation.phenomenon == "clouds":
        return observation.cloud_percent
    return 0.0


def _priority(kind: str) -> int:
    order = {"thunderstorm": 5, "snow": 4, "rain": 3, "fog": 2, "clouds": 1}
    return order.get(kind, 0)


def _radar_clause(radar: RadarContext) -> str:
    return (
        f"{radar.tolerance_db:.0f} dB two-way over {radar.range_nm:.0f} nmi "
        f"at {radar.frequency_ghz:.2f} GHz {radar.polarization} polarization"
    )


def _precip_criterion(radar: RadarContext, kind: str) -> str:
    text = f"ITU-R P.838-3 specific attenuation exceeds {_radar_clause(radar)}."
    if kind == "snow":
        text += (
            " Snow uses the melted rate. Dry snow attenuates less than rain of the same "
            "melted rate (Gunn and East, 1954, McGill University)."
        )
    return text


def _fog_criterion(radar: RadarContext) -> str:
    density = (
        "0.05 g/m3, visibility of the order of 300 m"
        if math.isclose(radar.fog_density_g_m3, MEDIUM_DROPLET_G_M3)
        else "0.5 g/m3, visibility of the order of 50 m"
    )
    return (
        "MET Norway defines fog_area_fraction as visibility under 1 km. "
        f"An area is flagged at {FOG_AREA_PERCENT:.0f}% coverage or a fog symbol from SMHI or NWS. "
        f"ITU-R P.840 {density} exceeds {_radar_clause(radar)}."
    )


def _cloud_criterion(radar: RadarContext) -> str:
    return (
        f"Overcast is {OVERCAST_PERCENT:.0f}% or more, above 7 oktas (87.5%). "
        "The point forecast has no liquid-water content, so ITU-R P.840 is applied with its "
        f"medium small-droplet density of {MEDIUM_DROPLET_G_M3:.2f} g/m3. "
        f"A disk is published only when that loss exceeds {_radar_clause(radar)}."
    )


def _ray_intervals(east: float, north: float, radius: float, heading_deg: float) -> list[tuple[float, float]]:
    ux = math.sin(math.radians(heading_deg))
    uy = math.cos(math.radians(heading_deg))
    dot = ux * east + uy * north
    gap = dot * dot - (east * east + north * north - radius * radius)
    if gap < 0:
        return []
    root = math.sqrt(gap)
    start = dot - root
    end = dot + root
    if end <= 1e-9:
        return []
    return [(max(0.0, start), end)]


def _merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end + 1e-6:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _forward_exit(intervals: list[tuple[float, float]]) -> float | None:
    for start, end in intervals:
        if start <= 1e-4:
            return max(0.0, end)
    return None


def _event_dict(event: WeatherEvent) -> dict:
    return {
        "station": event.station,
        "lat": event.lat,
        "lon": event.lon,
        "radius_nm": event.radius_nm,
        "phenomenon": event.phenomenon,
        "detail": event.detail,
        "criterion": event.criterion,
        "source": event.source,
        "valid_time": event.valid_time,
        "loss_db": round(event.loss_db, 3),
        "sun_azimuth_deg": event.sun_azimuth_deg,
        "sun_elevation_deg": event.sun_elevation_deg,
    }


def _event_from_dict(item: dict) -> WeatherEvent:
    return WeatherEvent(
        lat=float(item["lat"]),
        lon=float(item["lon"]),
        radius_nm=float(item.get("radius_nm") or EVENT_RADIUS_NM),
        phenomenon=str(item.get("phenomenon") or ""),
        detail=str(item.get("detail") or ""),
        criterion=str(item.get("criterion") or ""),
        source=str(item.get("source") or ""),
        valid_time=str(item.get("valid_time") or ""),
        loss_db=float(item.get("loss_db") or 0.0),
        station=str(item.get("station") or ""),
        sun_azimuth_deg=_optional_float(item.get("sun_azimuth_deg")),
        sun_elevation_deg=_optional_float(item.get("sun_elevation_deg")),
    )


def _optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _preview(station: Station, service: str, step: OceanStep) -> Observation:
    name, alliance, url = SERVICE_INFO[service]
    return Observation(
        name=station.name,
        lat=station.lat,
        lon=station.lon,
        phenomenon=step.phenomenon,
        rate_mm_h=step.rate_mm_h,
        cloud_percent=step.cloud_percent,
        low_cloud_percent=step.low_cloud_percent,
        fog_percent=step.fog_percent,
        temperature_c=step.temperature_c,
        service=service,
        source=f"{name} ({alliance}), {url}",
        valid_time=step.when,
    )


def _fetch_met_complete(lat: float, lon: float) -> list[OceanStep]:
    try:
        payload = get_json(MET_NORWAY_COMPLETE.format(lat=f"{lat:.4f}", lon=f"{lon:.4f}"))
    except WeatherError:
        payload = None
    if isinstance(payload, dict):
        return parse_met_norway_ocean(payload)
    steps, _lat, _lon = _fetch_met_norway(lat, lon)
    return [_from_legacy(step.when, step.phenomenon, step.rate_mm_h, step.cloud_percent) for step in steps]


def _fetch_ocean_legacy(service: str, lat: float, lon: float) -> list[OceanStep]:
    steps, served_lat, served_lon = FETCHERS[service](lat, lon)
    if service == "smhi" and distance_km(lat, lon, served_lat, served_lon) > 25:
        raise WeatherError("SMHI returned a grid point more than 25 km from the ocean sample.")
    return [_from_legacy(step.when, step.phenomenon, step.rate_mm_h, step.cloud_percent) for step in steps]


def _from_legacy(when: datetime, phenomenon: str, rate: float, cloud: float) -> OceanStep:
    return OceanStep(
        when=when,
        phenomenon=phenomenon,
        rate_mm_h=rate,
        cloud_percent=cloud,
        low_cloud_percent=None,
        fog_percent=100.0 if phenomenon == "fog" else 0.0,
        temperature_c=None,
    )


def _demo_event(ship: Ship) -> WeatherEvent:
    return WeatherEvent(
        lat=ship.lat,
        lon=ship.lon,
        radius_nm=EVENT_RADIUS_NM,
        phenomenon="snow",
        detail="8.00 mm/h liquid equivalent, demonstration disk, not a live forecast.",
        criterion="Demonstration disk for the alert format.",
        source="synthesized demonstration",
        valid_time="",
        loss_db=0.0,
        station="demonstration",
    )


OCEAN_FETCHERS = {
    "met_norway": _fetch_met_complete,
    "smhi": lambda lat, lon: _fetch_ocean_legacy("smhi", lat, lon),
    "nws": lambda lat, lon: _fetch_ocean_legacy("nws", lat, lon),
}



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

# Wall-clock pause between map updates. The simulated clock advances by the
# same amount, so the timeline below is in scenario seconds.
UPDATE_INTERVAL_SECONDS = 2.0
# The live monitor exits after this many seconds (40 updates at a 2 s interval).
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
# Storms farther than this from the carrier do not change the course ranking.
STORM_WATCH_NM = 60.0
# snooze_cruise stores a weather bearing, not a storm center. The disk sits
# on that bearing, inside 60 nm, with its near edge close to the screen so
# a station on the storm bearing steps back outside. The carrier is left on
# datum when the disk does not cover it.
STORM_CENTER_NM = 25.0
STORM_RADIUS_NM = 16.0
# Speed used only to turn a path through a storm into hours.
STORM_EXIT_SPEED_KT = 20.0
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
        for column in (
            "weather_phenomenon",
            "weather_rate_mm_h",
            "weather_cloud_percent",
            "weather_loss_db",
            "weather_bearing_deg",
            "weather_range_factor",
            "solar_flare_class",
            "weather_source",
        ):
            if column in group.columns:
                record[column] = latest_row[column]
        for column in (
            "radar_nomenclature",
            "blind_sector_start_rel",
            "blind_sector_end_rel",
            "radar_face",
        ):
            if column in group.columns:
                record[column] = latest_row[column]
        records.append(record)
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


def print_capacity(
    report: pd.DataFrame,
    threat_bearing: float | None = None,
    threat_range_km: float | None = None,
) -> None:
    """Print every ship's full capacity against what it has left."""
    print("Radar capacity")
    if threat_bearing is not None:
        span = f", range {threat_range_km:.0f} km" if threat_range_km is not None else ""
        print(f"Threat bearing: {threat_bearing:.0f}° true{span}")
    if "weather_phenomenon" in report.columns and not report.empty:
        sample = report.iloc[0]
        print(
            f"Weather: {sample.weather_phenomenon}, "
            f"{float(sample.weather_rate_mm_h):.1f} mm/h, "
            f"{float(sample.weather_loss_db):.1f} dB two-way on bearing {float(sample.weather_bearing_deg):.0f}°"
        )
        away = (float(sample.weather_bearing_deg) + 180.0) % 360.0
        if threat_bearing is not None:
            away = _off_threat_axis(away, threat_bearing)
        print(f"Weather avoidance heading: {away:.0f}° (kept off the threat axis)")
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
        casrep = getattr(row, "casrep_reference", "NONE")
        if casrep and str(casrep) not in {"NONE", "nan"}:
            print(f"CASREP:        {casrep}")
        if threat_bearing is not None:
            print(f"Threat bearing: {threat_bearing:.0f}° true")


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


def _blind_width(start: float | None, end: float | None) -> float:
    """Degrees of arc that a CASREP says are degraded. 0 means the radar is whole."""
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
    """True when bearing lies inside the clockwise arc from start to end."""
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
    """True when this radar's remaining range and healthy faces cover the sample."""
    if reach <= 0:
        return False
    bearing = bearing_deg(sample_east - east, sample_north - north)
    limit = reach
    # Jamming shortens range only inside the sector around the jammer bearing.
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
    """Percent of bearings at radius_km that fall inside at least one healthy radar disk."""
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
    sectors: list | None = None,
    jammer: dict | None = None,
) -> float:
    """Share of the bearings around each failed ship that a healthy radar still covers."""
    if not failed_bearings:
        return 100.0
    sectors = sectors or [None] * len(positions)
    hits = 0
    total = 0
    for center in failed_bearings:
        for offset in range(-30, 31, 5):
            total += 1
            sample_east, sample_north = point_at((center + offset) % 360.0, radius_km)
            for (east, north), reach, sector in zip(positions, ranges, sectors):
                if _covers_sample(sample_east, sample_north, east, north, reach, sector, jammer):
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


def _away_from(threat: float, other: float | None) -> float:
    """Bearing farthest from the threat and, when given, the jammer."""
    if other is None:
        return (threat + 180.0) % 360.0
    first = (threat + ((other - threat) % 360.0) / 2.0) % 360.0
    second = (first + 180.0) % 360.0

    def clearance(bearing: float) -> float:
        return min(wrap_delta(bearing, threat), wrap_delta(bearing, other))

    return first if clearance(first) >= clearance(second) else second


def _bow_for_sector(
    ship: dict,
    threat: float | None,
    keep_heading: bool = False,
    jammer_bearing: float | None = None,
) -> float:
    """Bow that puts a healthy face on the threat and a blind face away from the jammer."""
    current = ship["heading"] if ship.get("heading") is not None else 0.0
    current = float(current) % 360.0
    width = _blind_width(ship.get("blind_start"), ship.get("blind_end"))
    if keep_heading or threat is None or width < 0.5 or width >= 359.5:
        return current
    if jammer_bearing is not None:
        blind_center = (float(ship["blind_start"]) + width / 2.0) % 360.0
        heading = (_away_from(threat, jammer_bearing) - blind_center) % 360.0
        true_start = (heading + float(ship["blind_start"])) % 360.0
        true_end = (heading + float(ship["blind_end"])) % 360.0
        if not _bearing_in_arc(threat, true_start, true_end):
            return heading
    healthy = 360.0 - width
    center_rel = (float(ship["blind_end"]) + healthy / 2.0) % 360.0
    return (threat - center_rel) % 360.0


def _sector_brought(ship: dict, heading: float, threat: float | None) -> str:
    """Name the healthy array face the recommended bow points toward the threat."""
    radar = ship.get("radar_nomenclature") or "radar"
    width = _blind_width(ship.get("blind_start"), ship.get("blind_end"))
    if width >= 359.5:
        return f"{radar} all faces blind"
    if width < 0.5:
        return f"{radar} full azimuth"
    relative = ((threat - heading) % 360.0) if threat is not None else 0.0
    faces = (("1", 0.0), ("2", 90.0), ("3", 180.0), ("4", 270.0))
    blind_face = str(ship.get("radar_face") or "")

    def gap(center: float) -> float:
        delta = abs(relative - center) % 360.0
        return min(delta, 360.0 - delta)

    healthy = [face for face in faces if face[0] != blind_face]
    face, center = min(healthy or list(faces), key=lambda item: gap(item[1]))
    start = (center - 45.0) % 360.0
    end = (center + 45.0) % 360.0
    aim = f" on {threat:.0f}°" if threat is not None else ""
    return f"{radar} face {face} rel {start:.0f}-{end:.0f}{aim}"


def _land_hits(moves: list[dict], origin_lat: float, origin_lon: float) -> int:
    """Stations or transits that still touch land after the water search."""
    hits = 0
    for move in moves:
        if move["travel_km"] <= 0.5:
            continue
        start_east, start_north = to_xy(move["latitude"], move["longitude"], origin_lat, origin_lon)
        end_east, end_north = to_xy(move["station_latitude"], move["station_longitude"], origin_lat, origin_lon)
        if not _track_in_water(start_east, start_north, end_east, end_north, origin_lat, origin_lon):
            hits += 1
    return hits


def _jammed_face_penalty(moves: list[dict], jammer: dict | None, threat: float | None) -> int:
    """Count ships whose degraded face still points at the jammer or the threat."""
    if not jammer:
        return 0
    penalty = 0
    for move in moves:
        center = move.get("blind_true")
        if center is None:
            continue
        near_jammer = wrap_delta(float(center), jammer["bearing"]) < 30.0
        near_threat = threat is not None and wrap_delta(float(center), threat) < 30.0
        if near_jammer or near_threat:
            penalty += 1
    return penalty


# Lower Chesapeake and the Virginia Capes. Rings are land. The formation's
# track sits in the roads; stations south of the channel, inland of the
# Elizabeth River, on the Cape Henry spit, or on the Hampton bank are ashore.
LAND_RINGS = (
    ((36.55, -76.70), (36.55, -75.95), (36.80, -75.95), (36.80, -76.70)),
    ((36.80, -76.40), (36.80, -76.90), (37.25, -76.90), (37.25, -76.48), (37.00, -76.40)),
    ((36.80, -76.10), (36.80, -75.90), (37.02, -75.90), (36.97, -76.02), (36.88, -76.10)),
    ((37.00, -76.55), (37.00, -76.34), (37.50, -76.45), (37.50, -76.80)),
    ((37.15, -76.25), (37.15, -75.75), (37.70, -75.60), (37.70, -76.40)),
)


def _ring_contains(ring: tuple, latitude: float, longitude: float) -> bool:
    """Ray cast. The ring is a closed land outline in latitude, longitude."""
    inside = False
    previous = len(ring) - 1
    for index, (lat_i, lon_i) in enumerate(ring):
        lat_j, lon_j = ring[previous]
        if (lon_i > longitude) != (lon_j > longitude):
            edge = (lat_j - lat_i) * (longitude - lon_i) / (lon_j - lon_i) + lat_i
            if latitude < edge:
                inside = not inside
        previous = index
    return inside


def _on_land(latitude: float, longitude: float) -> bool:
    """True when this point lies on the coastline approximation."""
    return any(_ring_contains(ring, latitude, longitude) for ring in LAND_RINGS)


def _track_in_water(
    start_east: float,
    start_north: float,
    end_east: float,
    end_north: float,
    origin_lat: float,
    origin_lon: float,
) -> bool:
    """True when the station and the straight transit both stay off land."""
    steps = 16
    for step in range(steps + 1):
        fraction = step / steps
        east = start_east + (end_east - start_east) * fraction
        north = start_north + (end_north - start_north) * fraction
        latitude, longitude = to_latlon(east, north, origin_lat, origin_lon)
        if _on_land(latitude, longitude):
            return False
    return True


def _station_in_water(
    station_east: float,
    station_north: float,
    current_east: float,
    current_north: float,
    origin_lat: float,
    origin_lon: float,
    threat_bearing: float | None,
) -> tuple[float, float]:
    """Keep the station. If it or the path crosses land, take the nearest water point."""
    if _track_in_water(current_east, current_north, station_east, station_north, origin_lat, origin_lon):
        return station_east, station_north
    base_bearing = bearing_deg(station_east, station_north)
    base_range = float(np.hypot(station_east, station_north))
    ranges = [base_range] if base_range > 1.0 else []
    for extra in (8.0, 12.0, 16.0, 20.0, 24.0):
        if all(abs(extra - chosen) > 0.5 for chosen in ranges):
            ranges.append(extra)
    best: tuple[float, float] | None = None
    best_key: tuple | None = None
    for range_km in ranges:
        for delta in (0, 8, -8, 16, -16, 24, -24, 36, -36, 50, -50, 70, -70, 90, -90, 120, -120, 160, -160):
            bearing = (base_bearing + delta) % 360.0
            east, north = point_at(bearing, range_km)
            travel = float(np.hypot(east - current_east, north - current_north))
            course = bearing_deg(east - current_east, north - current_north) if travel > 0.05 else bearing
            if threat_bearing is not None and travel > 0.5:
                if wrap_delta(bearing, threat_bearing) < THREAT_EXCLUSION_DEG - 0.1:
                    continue
                if not _heading_clear(course, threat_bearing):
                    continue
            if not _track_in_water(current_east, current_north, east, north, origin_lat, origin_lon):
                continue
            key = (abs(delta), abs(range_km - base_range), travel)
            if best_key is None or key < best_key:
                best_key = key
                best = (east, north)
    if best is None:
        return current_east, current_north
    return best


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


def _attach_radar_sectors(ships: list[dict], report: pd.DataFrame | None) -> None:
    """Copy the radar and blind sector parsed from each ship's CASREP."""
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


def storms_from_report(report: pd.DataFrame | None) -> list[dict]:
    """Storm disks within 60 nm of the carrier, read from snooze_cruise weather columns.

    Clear air is not a storm. Rain, snow, thunderstorm, and fog on a reported
    bearing become one disk. The same bearing is not added twice.
    """
    if report is None or "weather_phenomenon" not in report.columns or report.empty:
        return []
    storms: list[dict] = []
    seen: set[tuple[str, float]] = set()
    for row in report.itertuples(index=False):
        phenomenon = str(getattr(row, "weather_phenomenon", "")).strip().lower()
        if phenomenon not in {"rain", "snow", "thunderstorm", "fog"}:
            continue
        rate = getattr(row, "weather_rate_mm_h", 0.0)
        rate = 0.0 if rate is None or (isinstance(rate, float) and math.isnan(rate)) else float(rate)
        if phenomenon != "fog" and rate < 0.1:
            continue
        bearing_value = getattr(row, "weather_bearing_deg", None)
        if bearing_value is None or pd.isna(bearing_value):
            continue
        bearing = float(bearing_value) % 360.0
        key = (phenomenon, round(bearing, 1))
        if key in seen or STORM_CENTER_NM > STORM_WATCH_NM:
            continue
        seen.add(key)
        east, north = point_at(bearing, STORM_CENTER_NM * NAUTICAL_MILE_KM)
        storms.append(
            {
                "phenomenon": phenomenon,
                "bearing": bearing,
                "east_km": east,
                "north_km": north,
                "radius_km": STORM_RADIUS_NM * NAUTICAL_MILE_KM,
                "center_nm": STORM_CENTER_NM,
                "radius_nm": STORM_RADIUS_NM,
            }
        )
    return storms


def _inside_storm(east: float, north: float, storms: list[dict]) -> dict | None:
    """The storm disk containing this point, if the point is still inside one."""
    for storm in storms:
        if (east - storm["east_km"]) ** 2 + (north - storm["north_km"]) ** 2 <= storm["radius_km"] ** 2:
            return storm
    return None


def _leave_storm(
    east: float,
    north: float,
    current_east: float,
    current_north: float,
    storms: list[dict],
    threat_bearing: float | None,
) -> tuple[float, float]:
    """Move a station just outside any storm without putting it on the threat axis."""
    for _step in range(18):
        storm = _inside_storm(east, north, storms)
        if storm is None:
            break
        center_east = storm["east_km"]
        center_north = storm["north_km"]
        reach = storm["radius_km"] + NAUTICAL_MILE_KM
        delta_east = east - center_east
        delta_north = north - center_north
        distance = math.hypot(delta_east, delta_north)
        if distance < 0.05:
            away = (storm["bearing"] + 180.0) % 360.0
            if threat_bearing is not None:
                away = _off_threat_axis(away, threat_bearing)
            east, north = point_at(away, reach)
        else:
            east = center_east + delta_east / distance * reach
            north = center_north + delta_north / distance * reach
        station_bearing = bearing_deg(east, north)
        course = bearing_deg(east - current_east, north - current_north)
        blocked = threat_bearing is not None and (
            wrap_delta(station_bearing, threat_bearing) < THREAT_EXCLUSION_DEG
            or not _heading_clear(course, threat_bearing)
        )
        if not blocked:
            continue
        # Slide around the storm on the side that stays farther from the threat.
        angle = math.atan2(east - center_east, north - center_north)
        options = []
        for sign in (1.0, -1.0):
            turned = angle + sign * math.radians(12.0)
            candidate = (
                center_east + math.sin(turned) * reach,
                center_north + math.cos(turned) * reach,
            )
            options.append(candidate)
        if threat_bearing is None:
            east, north = options[0]
        else:
            east, north = max(
                options,
                key=lambda point: wrap_delta(bearing_deg(point[0], point[1]), threat_bearing),
            )
    return east, north


def _storm_path_km(
    start_east: float,
    start_north: float,
    end_east: float,
    end_north: float,
    storms: list[dict],
) -> float:
    """Kilometers of this track that lie inside a storm, plus any depth still inside at the end."""
    if not storms:
        return 0.0
    span = math.hypot(end_east - start_east, end_north - start_north)
    steps = 48
    if span < 0.05:
        inside = 0.0
    else:
        hits = 0
        for step in range(steps):
            fraction = (step + 0.5) / steps
            east = start_east + (end_east - start_east) * fraction
            north = start_north + (end_north - start_north) * fraction
            if _inside_storm(east, north, storms) is not None:
                hits += 1
        inside = span * hits / steps
    end_storm = _inside_storm(end_east, end_north, storms)
    if end_storm is not None:
        distance = math.hypot(end_east - end_storm["east_km"], end_north - end_storm["north_km"])
        inside += end_storm["radius_km"] - distance
    return inside


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
    storms: list[dict] | None = None,
    turn_only: bool = False,
    keep_heading: bool = False,
    jammer: dict | None = None,
) -> dict:
    """One course set: a station for every healthy escort, plus coverage numbers."""
    storms = storms or []
    _cost, pairs = _assignment_cost(escorts, bearings, station_range, origin_lat, origin_lon)
    moves = []
    positions: list[tuple[float, float]] = []
    reach: list[float] = []
    position_sectors: list = []

    # The carrier stays at the center. Its radar counts on the inner ring.
    # The outer ring is the escort screen: a failed escort leaves a hole there
    # even when the carrier radar is still up.
    if hvu is not None and ranges.get(hvu["ship_id"], 0.0) > 0:
        positions.append((0.0, 0.0))
        reach.append(ranges[hvu["ship_id"]])
        position_sectors.append(None)

    screen_positions: list[tuple[float, float]] = []
    screen_reach: list[float] = []
    screen_sectors: list = []

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
        if storms:
            station_east, station_north = _leave_storm(
                station_east, station_north, current_east, current_north, storms, threat_bearing
            )
            course = bearing_deg(station_east - current_east, station_north - current_north)
            travel = float(np.hypot(station_east - current_east, station_north - current_north))
            station_bearing = bearing_deg(station_east, station_north)
            if threat_bearing is not None and travel > 0.5 and (
                not _heading_clear(course, threat_bearing)
                or wrap_delta(station_bearing, threat_bearing) < THREAT_EXCLUSION_DEG - 0.1
            ):
                station_east, station_north = current_east, current_north
                station_bearing = bearing_deg(current_east, current_north) if travel > 0.1 else station_bearing
                course = escort["heading"] if escort["heading"] is not None else course
                if threat_bearing is not None and not _heading_clear(course, threat_bearing):
                    course = _off_threat_axis(course, threat_bearing)
                travel = 0.0
        if not turn_only:
            station_east, station_north = _station_in_water(
                station_east,
                station_north,
                current_east,
                current_north,
                origin_lat,
                origin_lon,
                threat_bearing,
            )
            course = bearing_deg(station_east - current_east, station_north - current_north)
            travel = float(np.hypot(station_east - current_east, station_north - current_north))
            station_bearing = bearing_deg(station_east, station_north)
            if travel < 0.5:
                station_east, station_north = current_east, current_north
                travel = 0.0
                if float(np.hypot(current_east, current_north)) > 0.1:
                    station_bearing = bearing_deg(current_east, current_north)
        if travel < 0.5 and escort["heading"] is not None and threat_bearing is None and not turn_only:
            course = escort["heading"]
        if turn_only:
            station_east, station_north = current_east, current_north
            travel = 0.0
            if float(np.hypot(current_east, current_north)) > 0.1:
                station_bearing = bearing_deg(current_east, current_north)
        bow = _bow_for_sector(
            escort,
            threat_bearing,
            keep_heading,
            None if jammer is None else jammer["bearing"],
        )
        if (
            travel < 0.5
            and _blind_width(escort.get("blind_start"), escort.get("blind_end")) < 0.5
            and threat_bearing is not None
            and not _heading_clear(bow, threat_bearing)
        ):
            bow = _off_threat_axis(bow, threat_bearing)
        transit = bow if travel < 0.5 else course
        blind_width = _blind_width(escort.get("blind_start"), escort.get("blind_end"))
        blind_true = None
        if 0.5 <= blind_width < 359.5:
            blind_true = (bow + float(escort["blind_start"]) + blind_width / 2.0) % 360.0
        latitude, longitude = to_latlon(station_east, station_north, origin_lat, origin_lon)
        moves.append(
            {
                "ship_id": escort["ship_id"],
                "ship_name": escort["ship_name"],
                "ship_type": escort["ship_type"],
                "heading": bow,
                "course_heading": transit,
                "action": "turn only" if travel < 0.5 else "move then turn",
                "radar_sector": _sector_brought(escort, bow, threat_bearing),
                "blind_true": blind_true,
                "station_bearing": station_bearing,
                "station_range_km": float(np.hypot(station_east, station_north)),
                "sector_start": None,
                "sector_end": None,
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
        sector = (bow, escort.get("blind_start"), escort.get("blind_end"))
        screen_sectors.append(sector)
        position_sectors.append(sector)

    final_bearings = sorted(move["station_bearing"] for move in moves)
    for move in moves:
        start, end = _sector_bounds(move["station_bearing"], final_bearings)
        move["sector_start"] = start
        move["sector_end"] = end

    if hvu is not None:
        hold_heading = hvu["heading"] if hvu["heading"] is not None else 0.0
        if threat_bearing is not None and not _heading_clear(hold_heading, threat_bearing):
            hold_heading = _off_threat_axis(hold_heading, threat_bearing)
        hvu_east, hvu_north = 0.0, 0.0
        hvu_travel = 0.0
        if storms and not turn_only and _inside_storm(0.0, 0.0, storms) is not None:
            # The carrier is the unit that must leave a nearby storm first.
            hvu_east, hvu_north = _leave_storm(0.0, 0.0, 0.0, 0.0, storms, threat_bearing)
            hvu_east, hvu_north = _station_in_water(
                hvu_east, hvu_north, 0.0, 0.0, origin_lat, origin_lon, threat_bearing
            )
            hvu_travel = float(np.hypot(hvu_east, hvu_north))
            if hvu_travel > 0.5:
                hold_heading = bearing_deg(hvu_east, hvu_north)
                if threat_bearing is not None and not _heading_clear(hold_heading, threat_bearing):
                    hvu_east, hvu_north = 0.0, 0.0
                    hvu_travel = 0.0
                    hold_heading = _off_threat_axis(hold_heading, threat_bearing)
        hvu_lat, hvu_lon = to_latlon(hvu_east, hvu_north, origin_lat, origin_lon)
        if hvu_travel > 0.5 and positions:
            positions[0] = (hvu_east, hvu_north)
        bow = _bow_for_sector(
            hvu,
            threat_bearing,
            keep_heading,
            None if jammer is None else jammer["bearing"],
        )
        if hvu_travel > 0.5:
            transit = hold_heading
        else:
            transit = bow
            if (
                _blind_width(hvu.get("blind_start"), hvu.get("blind_end")) < 0.5
                and threat_bearing is not None
                and not _heading_clear(bow, threat_bearing)
            ):
                bow = _off_threat_axis(bow, threat_bearing)
                transit = bow
        hvu_blind = _blind_width(hvu.get("blind_start"), hvu.get("blind_end"))
        hvu_blind_true = None
        if 0.5 <= hvu_blind < 359.5:
            hvu_blind_true = (bow + float(hvu["blind_start"]) + hvu_blind / 2.0) % 360.0
        if position_sectors:
            position_sectors[0] = (bow, hvu.get("blind_start"), hvu.get("blind_end"))
        moves.insert(
            0,
            {
                "ship_id": hvu["ship_id"],
                "ship_name": hvu["ship_name"],
                "ship_type": hvu["ship_type"],
                "heading": bow,
                "course_heading": transit,
                "action": "turn only" if hvu_travel < 0.5 else "move then turn",
                "radar_sector": _sector_brought(hvu, bow, threat_bearing),
                "blind_true": hvu_blind_true,
                "station_bearing": bearing_deg(hvu_east, hvu_north) if hvu_travel > 0.5 else None,
                "station_range_km": hvu_travel,
                "sector_start": None,
                "sector_end": None,
                "latitude": hvu["latitude"],
                "longitude": hvu["longitude"],
                "station_latitude": hvu_lat if hvu_travel > 0.5 else hvu["latitude"],
                "station_longitude": hvu_lon if hvu_travel > 0.5 else hvu["longitude"],
                "travel_km": hvu_travel,
                "role": "hvu",
            },
        )

    outer_pct, outer_gaps = ring_coverage(screen_positions, screen_reach, outer_range, screen_sectors, jammer)
    inner_pct, inner_gaps = ring_coverage(positions, reach, INNER_RANGE_KM, position_sectors, jammer)
    failed_pct = _failed_sector_coverage(
        screen_positions, screen_reach, outer_range, failed_bearings, screen_sectors, jammer
    )
    threat_pct = (
        _failed_sector_coverage(
            screen_positions, screen_reach, outer_range, [threat_bearing], screen_sectors, jammer
        )
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
            and not _heading_clear(move.get("course_heading", move["heading"]), threat_bearing)
        )
    speed_km_h = STORM_EXIT_SPEED_KT * NAUTICAL_MILE_KM
    storm_hours = 0.0
    cvn_storm_hours = 0.0
    if storms:
        for move in moves:
            start_east, start_north = to_xy(move["latitude"], move["longitude"], origin_lat, origin_lon)
            end_east, end_north = to_xy(
                move["station_latitude"], move["station_longitude"], origin_lat, origin_lon
            )
            hours = _storm_path_km(start_east, start_north, end_east, end_north, storms) / speed_km_h
            weight = 3.0 if move["role"] == "hvu" else 1.0
            if move["role"] == "hvu":
                cvn_storm_hours = hours
            storm_hours += weight * hours
    # Threat coverage still leads. Storm time breaks ties toward the faster exit.
    score = (
        1.6 * threat_pct
        + 0.35 * outer_pct
        + 0.15 * inner_pct
        - 0.25 * mean_travel
        - 40.0 * unsafe
        - 15.0 * storm_hours
        - 8.0 * _jammed_face_penalty(moves, jammer, threat_bearing)
        - 80.0 * _land_hits(moves, origin_lat, origin_lon)
    )

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
        "turn_only": turn_only,
        "storm_hours": storm_hours,
        "cvn_storm_hours": cvn_storm_hours,
        "storm_count": len(storms),
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
    weather_report: pd.DataFrame | None = None,
    jammer: dict | None = None,
) -> tuple[list[dict], str, float]:
    """Three stationing plans ranked Best / Acceptable / Fallback.

    Escorts with any remaining capacity are stationed. Their reach is already
    scaled. Ships at 0% stay off the screen. When a threat bearing is given,
    stations sit on either side of that axis and no course points along it.
    A storm within 60 nm of the carrier, taken from snooze_cruise, lowers a
    plan that keeps ships inside it. The carrier's time counts three times.
    """
    ships = _located_ships(locations)
    if not ships:
        return [], "no located ships", INNER_RANGE_KM
    _attach_radar_sectors(ships, weather_report)

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
    storms = storms_from_report(weather_report)

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
            storms,
            jammer=jammer,
        )
        for name, rationale, bearings, station_range in templates
    ]
    turn_bearings = (
        _threat_shoulders(len(escorts), threat_bearing, 40.0)
        if threat_bearing is not None
        else even_bearings(len(escorts), 0.0)
    )
    plans.append(
        _build_plan(
            "Turn in place",
            "Hold station and train a healthy array face on the threat. A move is used only when turning cannot match that coverage.",
            escorts,
            turn_bearings,
            18.0,
            hvu,
            origin_lat,
            origin_lon,
            ranges,
            outer_range,
            failed_bearings,
            threat_bearing,
            storms,
            turn_only=True,
            jammer=jammer,
        )
    )
    baseline = _build_plan(
        "Current heading",
        "",
        escorts,
        turn_bearings,
        18.0,
        hvu,
        origin_lat,
        origin_lon,
        ranges,
        outer_range,
        failed_bearings,
        threat_bearing,
        storms,
        turn_only=True,
        keep_heading=True,
        jammer=jammer,
    )
    if jammer is not None:
        jammer_note = (
            f" Jammer on {jammer['bearing']:.0f}° at {jammer['erp']:.0f} kW, "
            f"SJR {jammer['sjr']:.1f} dB"
            f"{', ducting on' if jammer['ducting'] else ''}. "
            f"Degraded faces are kept off that bearing and off the threat. "
            f"Stored optimal_action is {jammer['action']}."
        )
        for plan in plans:
            plan["rationale"] = plan["rationale"] + jammer_note
            plan["jammer_text"] = (
                f"Jammer {jammer['bearing']:.0f}°  {jammer['erp']:.0f} kW  "
                f"SJR {jammer['sjr']:.1f} dB"
            )
    water_note = " Stations and the tracks between them stay in the water."
    for plan in plans:
        if water_note not in plan["rationale"]:
            plan["rationale"] = plan["rationale"] + water_note
        plan["coverage_gain"] = plan["threat_pct"] - baseline["threat_pct"]
    plans.sort(key=lambda plan: plan["score"], reverse=True)
    turn_plan = next(plan for plan in plans if plan.get("turn_only"))
    move_plans = [plan for plan in plans if not plan.get("turn_only")]
    if move_plans and turn_plan["threat_pct"] + 0.5 >= max(plan["threat_pct"] for plan in move_plans):
        plans = [turn_plan] + [plan for plan in plans if plan is not turn_plan]
    plans = plans[:3]
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
        if plan.get("jammer_text"):
            print(f"Jamming: {plan['jammer_text']}")
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
            gain = plan.get("coverage_gain")
            if gain is not None:
                print(f"Threat-axis change versus current headings: {gain:+.0f} points")
            print(f"Stations stay at least {THREAT_EXCLUSION_DEG:.0f}° off the threat axis.")
        if plan.get("storm_count"):
            if plan.get("storm_hours"):
                print(
                    f"Time in storms within {STORM_WATCH_NM:.0f} nm of the carrier: "
                    f"{plan['storm_hours']:.2f} h weighted "
                    f"(CVN {plan['cvn_storm_hours']:.2f} h, counted three times)."
                )
            else:
                print(
                    f"No recommended track stays inside a storm within {STORM_WATCH_NM:.0f} nm of the carrier."
                )
        for move in plan["moves"]:
            print()
            remaining = capacity.get(move["ship_id"], 100.0)
            reach = ranges.get(move["ship_id"], 0.0)
            print(
                f"  {move['ship_id']}  {move['ship_name']} ({move['ship_type']})"
                f" — {remaining:.0f}% capacity, {reach:.0f} km"
            )
            print(f"    Recommended heading: {_format_heading(move['heading'])}")
            print(f"    Action: {move.get('action', 'move then turn')}")
            if move.get("action") == "move then turn":
                print(f"    Transit course: {_format_heading(move.get('course_heading', move['heading']))}")
            print(f"    Radar sector: {move.get('radar_sector', 'full azimuth')}")
            if move["role"] == "hvu" and move["travel_km"] < 0.5:
                print("    Station: remain on datum (range 0 km)")
            elif move["role"] == "hvu":
                print(
                    f"    Station: leave the storm, bearing {move['station_bearing']:.0f}° "
                    f"at {move['station_range_km']:.0f} km from datum"
                )
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
            if move["travel_km"] < 0.5:
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
    if "weather_phenomenon" in report.columns and not report.empty:
        sample = report.iloc[0]
        lines.append(
            f"Weather {sample.weather_phenomenon}  {float(sample.weather_loss_db):.1f} dB"
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
    if best.get("storm_hours"):
        lines.append(f"Storm time {best['storm_hours']:.2f} h within {STORM_WATCH_NM:.0f} nm")
    if best.get("jammer_text"):
        lines.append(best["jammer_text"])
    listed: set[str] = set()
    for move in best["moves"]:
        listed.add(move["ship_id"])
        heading = f"{move['heading'] % 360.0:05.1f}°"
        action = move.get("action", "turn only")
        short = "turn" if action == "turn only" else "move+turn"
        sector = move.get("radar_sector", "")
        face = ""
        if "face" in sector:
            face = " " + sector.split("face", 1)[1].split("rel", 1)[0].strip()
        if move["role"] == "hvu" and move["travel_km"] < 0.5:
            lines.append(f"{move['ship_id']:<8} {heading}  {short}{face}")
        elif move["role"] == "hvu":
            lines.append(
                f"{move['ship_id']:<8} {heading}  {short} "
                f"{move['station_range_km']:.0f} km"
            )
        else:
            lines.append(
                f"{move['ship_id']:<8} {heading}  {short}{face}"
                f"  brg {move['station_bearing']:.0f}°"
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
            if move["travel_km"] > 0.5:
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
    jammer: dict | None = None,
) -> None:
    """Run the capacity report, course ranking, and both figures for one snapshot."""
    report = attach_locations(assess_capacity(radar, locations), locations)
    print_capacity(report, threat_bearing=threat_bearing, threat_range_km=threat_range_km)
    capacity = {str(row.ship_id): float(row.capacity_percent) for row in report.itertuples(index=False)}
    ranges = effective_ranges(report)
    # A known threat still needs a screen when every radar is healthy.
    if threat_bearing is None and report["capacity_percent"].ge(99.5).all():
        plot_status(locations, report, [], STATUS_FIGURE, title=title)
        return

    plans, datum_label, outer_range = recommend_courses(
        locations, ranges, capacity, threat_bearing=threat_bearing, weather_report=report, jammer=jammer
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


def _casrep_events() -> list[dict]:
    """Facts used to write the CASREP messages. Capacity is read back from the text."""
    return [
        {
            "ship_id": "DDG-101",
            "time_seconds": 12.0,
            "capacity_percent": 60,
            "radar_status": "DEGRADED",
            "category": "2",
            "nomenclature": "AN/SPY-1D(V)",
            "serial": "SPY1D-101-04",
            "description": (
                "INTERMITTENT FAILURE OF TRANSMITTER MODULES ON ARRAY FACE 2. "
                "COOLANT PUMP DISCHARGE PRESSURE BELOW LIMIT. SEARCH VOLUME REDUCED."
            ),
            "impact": (
                "AIR SEARCH RANGE AND TRACK CAPACITY REDUCED TO ABOUT 60 PERCENT. "
                "SHIP CAN STILL COVER AN ASSIGNED SECTOR AT SHORTER RANGE."
            ),
            "parts": [
                ("5840-01-555-0142", "TRANSMITTER MODULE", 6),
                ("4320-01-441-2208", "COOLANT PUMP", 1),
            ],
            "etr_hours": 18,
            "assistance": "NOT REQUIRED. SHIP'S FORCE EFFECTING REPAIRS.",
            "fault_code": "XMTR_MODULE",
            "seq": "001",
            "affected_start_rel": 90,
            "affected_end_rel": 180,
            "radar_face": "2",
        },
        {
            "ship_id": "CG-58",
            "time_seconds": 32.0,
            "capacity_percent": 0,
            "radar_status": "FAILED",
            "category": "4",
            "nomenclature": "AN/SPY-1B",
            "serial": "SPY1B-058-02",
            "description": (
                "PRIMARY TRANSMITTER TRIPPED OFF LINE. HIGH-VOLTAGE POWER SUPPLY "
                "WILL NOT RESET. ARRAY IS RADIATING NO ENERGY."
            ),
            "impact": (
                "AIR-SEARCH RADAR IS NOT AVAILABLE. NO REMAINING CAPABILITY ON THIS "
                "SYSTEM. SHIP CANNOT CONTRIBUTE TO THE GROUP AIR PICTURE."
            ),
            "parts": [
                ("6130-01-318-7740", "HIGH VOLTAGE POWER SUPPLY", 1),
                ("5840-01-227-6619", "TRANSMITTER ASSEMBLY", 1),
            ],
            "etr_hours": 72,
            "assistance": "REQUEST TECH ASSIST FROM CSG STAFF. PARTS NOT ON BOARD.",
            "fault_code": "TRANSMITTER_FAIL",
            "seq": "001",
            "affected_start_rel": 0,
            "affected_end_rel": 360,
            "radar_face": "1-4",
        },
        {
            "ship_id": "DDG-112",
            "time_seconds": 52.0,
            "capacity_percent": 40,
            "radar_status": "DEGRADED",
            "category": "3",
            "nomenclature": "AN/SPY-1D(V)",
            "serial": "SPY1D-112-07",
            "description": (
                "SIGNAL PROCESSOR OVERTEMP. ONE OF TWO PROCESSOR STRINGS SECURED. "
                "DETECTION THRESHOLD RAISED TO LIMIT FALSE TRACKS."
            ),
            "impact": (
                "TRACK QUALITY AND INSTRUMENTED RANGE REDUCED. ABOUT 40 PERCENT "
                "OF DESIGN CAPACITY REMAINS. SECTOR WATCH STILL POSSIBLE."
            ),
            "parts": [
                ("5840-01-602-1184", "SIGNAL PROCESSOR CARD", 2),
                ("4130-01-509-3301", "PROCESSOR COOLING UNIT", 1),
            ],
            "etr_hours": 36,
            "assistance": "NOT REQUIRED UNLESS SECOND STRING FAILS.",
            "fault_code": "PROCESSOR_OVERTEMP",
            "seq": "001",
            "affected_start_rel": 180,
            "affected_end_rel": 270,
            "radar_face": "3",
        },
    ]


def _dtg(elapsed_s: float) -> str:
    """Navy date-time group for a scenario offset, down to the second."""
    stamp = SCENARIO_START + pd.Timedelta(seconds=float(elapsed_s))
    months = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
    return (
        f"{stamp.day:02d}{stamp.hour:02d}{stamp.minute:02d}{stamp.second:02d}Z "
        f"{months[stamp.month - 1]} {stamp.year % 100:02d}"
    )


def _position_text(latitude: float, longitude: float) -> str:
    """Latitude and longitude in the form used in a naval message."""
    ns = "N" if latitude >= 0 else "S"
    ew = "E" if longitude >= 0 else "W"
    return f"{abs(latitude):05.2f}{ns} {abs(longitude):06.2f}{ew}"


def render_casrep(event: dict, ship: dict, latitude: float, longitude: float) -> str:
    """One INITIAL CASREP. Status and remaining percent are lines the parser reads."""
    reference = f"CASREP-{event['ship_id']}-{event['seq']}"
    parts = "\n".join(
        f"   {index}. NSN {nsn}  {name}  QTY {qty}"
        for index, (nsn, name, qty) in enumerate(event["parts"], start=1)
    )
    return "\n".join(
        [
            "UNCLAS",
            f"MSGID/CASREP/{ship['ship_name'].upper()}/{event['seq']}",
            f"REF/{reference}",
            "CASREP/INITIAL",
            f"1. UNIT: {ship['ship_name'].upper()} ({event['ship_id']})",
            f"2. DTG: {_dtg(event['time_seconds'])}",
            f"3. POSITION: {_position_text(latitude, longitude)}",
            "4. EQUIPMENT:",
            f"   A. NOMENCLATURE: {event['nomenclature']}",
            "   B. EIC: RADA",
            f"   C. SERIAL: {event['serial']}",
            f"5. CASUALTY CATEGORY: {event['category']}",
            f"6. DESCRIPTION: {event['description']}",
            f"7. MISSION IMPACT: {event['impact']}",
            f"8. REMAINING CAPABILITY: {int(event['capacity_percent'])} PERCENT",
            f"9. RADAR STATUS: {event['radar_status']}",
            f"RADAR: {event['nomenclature']}",
            (
                f"AFFECTED SECTOR: {int(event['affected_start_rel']):03d}-"
                f"{int(event['affected_end_rel']):03d} RELATIVE FACE {event['radar_face']}"
            ),
            "10. PARTS REQUIRED:",
            parts,
            f"11. ESTIMATED TIME TO REPAIR: {int(event['etr_hours'])} HOURS",
            f"12. TECHNICAL ASSISTANCE: {event['assistance']}",
            "13. REMARKS: SHIP REMAINS IN COMPANY. DO NOT CLOSE THE THREAT AXIS.",
            f"BT  {reference}",
        ]
    )


def parse_casrep(text: str) -> dict:
    """Pull ship, time, status, and remaining capacity out of a CASREP body."""
    ship_match = re.search(r"UNIT:.*\(([A-Z]+-\d+)\)", text)
    ref_match = re.search(r"REF/(CASREP-[A-Z]+-\d+-\d+)", text)
    time_match = re.search(r"DTG:\s*(\d{2})(\d{2})(\d{2})(\d{2})Z", text)
    capacity_match = re.search(r"REMAINING CAPABILITY:\s*(\d+)\s*PERCENT", text)
    status_match = re.search(r"RADAR STATUS:\s*(OK|DEGRADED|FAILED)", text)
    radar_match = re.search(r"^RADAR:\s*(.+)$", text, re.M)
    sector_match = re.search(
        r"AFFECTED SECTOR:\s*(\d+)-(\d+)\s+RELATIVE(?:\s+FACE\s+(\S+))?",
        text,
    )
    if not all((ship_match, ref_match, time_match, capacity_match, status_match, radar_match, sector_match)):
        raise ValueError("CASREP is missing ship, radar, sector, reference, DTG, status, or remaining capability")
    day = int(time_match.group(1))
    hour = int(time_match.group(2))
    minute = int(time_match.group(3))
    second = int(time_match.group(4))
    stamp = SCENARIO_START
    elapsed = ((day - stamp.day) * 86400) + hour * 3600 + minute * 60 + second - (
        stamp.hour * 3600 + stamp.minute * 60 + stamp.second
    )
    return {
        "ship_id": ship_match.group(1),
        "casrep_reference": ref_match.group(1),
        "time_seconds": float(elapsed),
        "capacity_percent": float(capacity_match.group(1)),
        "radar_status": status_match.group(1),
        "radar_nomenclature": radar_match.group(1).strip(),
        "blind_start_rel": float(sector_match.group(1)),
        "blind_end_rel": float(sector_match.group(2)),
        "radar_face": (sector_match.group(3) or "").strip(),
    }


def write_casreps(
    events: list[dict],
    positions: dict[tuple[str, float], tuple[float, float]],
    folder: Path = CASREP_DIR,
) -> list[dict]:
    """Write each CASREP, then return the status and capacity parsed from the files."""
    folder.mkdir(parents=True, exist_ok=True)
    fleet = {ship["ship_id"]: ship for ship in FLEET}
    parsed: list[dict] = []
    index_lines = ["CASREP log", ""]
    for event in events:
        ship = fleet[event["ship_id"]]
        latitude, longitude = positions[(event["ship_id"], event["time_seconds"])]
        body = render_casrep(event, ship, latitude, longitude)
        reference = f"CASREP-{event['ship_id']}-{event['seq']}"
        path = folder / f"{reference}.txt"
        path.write_text(body + "\n", encoding="utf-8")
        record = parse_casrep(path.read_text(encoding="utf-8"))
        record["fault_code"] = event["fault_code"]
        parsed.append(record)
        index_lines.append(
            f"{record['casrep_reference']}  T+{record['time_seconds']:.0f}s  "
            f"{record['ship_id']}  {record['radar_nomenclature']}  {record['radar_status']}  "
            f"{record['capacity_percent']:.0f}%  face {record['radar_face']}  "
            f"rel {record['blind_start_rel']:.0f}-{record['blind_end_rel']:.0f}"
        )
    (folder / "INDEX.txt").write_text("\n".join(index_lines) + "\n", encoding="utf-8")
    return parsed


def _active_casrep(casreps: list[dict], ship_id: str, elapsed_s: float) -> dict | None:
    """Latest parsed CASREP at or before this time."""
    active = [
        item
        for item in casreps
        if item["ship_id"] == ship_id and item["time_seconds"] <= elapsed_s + 1e-6
    ]
    if not active:
        return None
    return max(active, key=lambda item: item["time_seconds"])


def _capacity_from_casreps(casreps: list[dict], ship_id: str, elapsed_s: float) -> tuple[float, str]:
    """Latest CASREP at or before this time. No report means full capacity."""
    latest = _active_casrep(casreps, ship_id, elapsed_s)
    if latest is None:
        return 100.0, "NONE"
    return float(latest["capacity_percent"]), str(latest["casrep_reference"])


def _ship_at_time(
    ship_id: str,
    elapsed_s: float,
    times: np.ndarray,
    carrier_track: list[tuple[float, float, float]],
    offsets: dict[str, tuple[float, float]],
    start_heading: float,
) -> tuple[float, float]:
    """Formation position of one ship at a scenario time, used in the CASREP."""
    index = int(np.searchsorted(times, elapsed_s, side="left"))
    index = min(max(index, 0), len(times) - 1)
    carrier_lat, carrier_lon, heading = carrier_track[index]
    theta = np.deg2rad(heading - start_heading)
    cosine, sine = float(np.cos(theta)), float(np.sin(theta))
    east, north = offsets[ship_id]
    rotated_east = east * cosine + north * sine
    rotated_north = -east * sine + north * cosine
    return to_latlon(rotated_east, rotated_north, carrier_lat, carrier_lon)


def build_snooze_cruise() -> pd.DataFrame:
    """Sample a moving formation. Radar capacity is read from the CASREP files.

    The group steams about 12 km and turns during the 80-second run.
    CASREPs are written to casreps/, then remaining capability is parsed
    back out and stored in this table: DDG-101 at 12 s, CG-58 at 32 s,
    DDG-112 at 52 s.
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

    events = _casrep_events()
    positions = {
        (event["ship_id"], event["time_seconds"]): _ship_at_time(
            event["ship_id"], event["time_seconds"], times, carrier_track, offsets, start_heading
        )
        for event in events
    }
    casreps = write_casreps(events, positions)
    fault_by_ref = {item["casrep_reference"]: item["fault_code"] for item in casreps}

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
            percent, casrep_reference = _capacity_from_casreps(casreps, ship["ship_id"], float(instant))
            detail = _active_casrep(casreps, ship["ship_id"], float(instant))
            radar_name = detail["radar_nomenclature"] if detail else {
                "CVN-78": "AN/SPY-6",
                "CG-58": "AN/SPY-1B",
                "DDG-101": "AN/SPY-1D(V)",
                "DDG-112": "AN/SPY-1D(V)",
            }.get(ship["ship_id"], "radar")
            status = "OK"
            for item in casreps:
                if item["casrep_reference"] == casrep_reference:
                    status = item["radar_status"]
            scale = percent / 100.0
            if percent >= 99.5:
                mode, power, cooling, fault = "SEARCH", "NOMINAL", "NOMINAL", "NONE"
            elif percent <= 0.5:
                mode, power, cooling, fault = "STANDBY", "OFF", "SHUTDOWN", fault_by_ref.get(casrep_reference, "TRANSMITTER_FAIL")
            else:
                mode, power, cooling, fault = "DEGRADED_SEARCH", "REDUCED", "ELEVATED", fault_by_ref.get(casrep_reference, "RANGE_LIMIT")
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
                    "casrep_reference": casrep_reference,
                    "radar_nomenclature": radar_name,
                    "blind_sector_start_rel": None if detail is None else detail["blind_start_rel"],
                    "blind_sector_end_rel": None if detail is None else detail["blind_end_rel"],
                    "radar_face": "" if detail is None else detail["radar_face"],
                    "power_status": power,
                    "cooling_status": cooling,
                    "last_maintenance": "2026-10-15",
                    "fault_code": fault,
                }
            )
    return attach_scenario_weather(pd.DataFrame(rows))


def attach_scenario_weather(frame: pd.DataFrame) -> pd.DataFrame:
    """Write ITU-R rain loss into snooze_cruise.

    The 4-second loop does not call a forecast service. monitor and weather
    do that, and monitor will not poll faster than once a minute. A rain
    cell on bearing 080 starts at T+20 s. Loss uses a 24 nmi path, matching
    the forecast tool's default range, and the range factor is not allowed
    to fall below 0.25 so the CASREP capacity changes stay visible.
    """
    k, alpha = specific_coefficients(9.41, "horizontal")
    phenomena: list[str] = []
    rates: list[float] = []
    clouds: list[float] = []
    losses: list[float] = []
    factors: list[float] = []
    for row in frame.itertuples(index=False):
        elapsed = float(row.time_seconds)
        if elapsed < 20:
            rate, phenomenon, cloud = 0.0, "clear air", 15.0
        elif elapsed < 48:
            rate, phenomenon, cloud = 4.0, "rain", 90.0
        else:
            rate, phenomenon, cloud = 8.0, "rain", 100.0
        path_nm = min(24.0, RADIUS_NM)
        loss = two_way_loss_db(rate, k, alpha, path_nm)
        factor = 1.0 if loss <= 0 else max(0.25, 10 ** (-loss / 40.0))
        phenomena.append(phenomenon)
        rates.append(rate)
        clouds.append(cloud)
        losses.append(loss)
        factors.append(factor)
    frame = frame.copy()
    frame["weather_phenomenon"] = phenomena
    frame["weather_rate_mm_h"] = rates
    frame["weather_cloud_percent"] = clouds
    frame["weather_loss_db"] = losses
    frame["weather_bearing_deg"] = 80.0
    frame["weather_range_factor"] = factors
    frame["solar_flare_class"] = "none"
    frame["weather_source"] = "snooze_cruise ITU-R P.838-3 scenario"
    frame["effective_range_km"] = frame["effective_range_km"] * frame["weather_range_factor"]
    return frame


def _write_casrep_sectors(frame: pd.DataFrame, path: Path) -> pd.DataFrame:
    """Rewrite the CASREP texts with radar and sector lines, then store what was parsed.

    The messages are the source of the radar name and the blind sector. Rows
    before a CASREP keep a healthy radar. The full table, including rows that
    are not ship tracks, is written back to snooze_cruise.
    """
    events = _casrep_events()
    fleet = {ship["ship_id"]: ship for ship in FLEET}
    positions: dict[tuple[str, float], tuple[float, float]] = {}
    for event in events:
        rows = frame.loc[frame["ship_id"] == event["ship_id"]]
        if "latitude" in rows.columns:
            rows = rows.loc[rows["latitude"].notna() & rows["time_seconds"].notna()]
        if rows.empty:
            ship = fleet[event["ship_id"]]
            latitude, longitude = float(ship["latitude"]), float(ship["longitude"])
        else:
            index = (rows["time_seconds"] - event["time_seconds"]).abs().idxmin()
            latitude = float(rows.loc[index, "latitude"])
            longitude = float(rows.loc[index, "longitude"])
        positions[(event["ship_id"], event["time_seconds"])] = (latitude, longitude)
    parsed = {item["ship_id"]: item for item in write_casreps(events, positions)}
    names: list[str] = []
    starts: list[float] = []
    ends: list[float] = []
    faces: list[str] = []
    defaults = {
        "CVN-78": "AN/SPY-6",
        "CG-58": "AN/SPY-1B",
        "DDG-101": "AN/SPY-1D(V)",
        "DDG-112": "AN/SPY-1D(V)",
    }
    for row in frame.itertuples(index=False):
        ship_id = str(row.ship_id).strip()
        detail = parsed.get(ship_id)
        elapsed = getattr(row, "time_seconds", None)
        active = (
            detail is not None
            and elapsed is not None
            and not (isinstance(elapsed, float) and math.isnan(elapsed))
            and float(elapsed) + 1e-6 >= detail["time_seconds"]
        )
        if active and detail is not None:
            names.append(detail["radar_nomenclature"])
            starts.append(float(detail["blind_start_rel"]))
            ends.append(float(detail["blind_end_rel"]))
            faces.append(detail["radar_face"])
        else:
            names.append(defaults.get(ship_id, ""))
            starts.append(float("nan"))
            ends.append(float("nan"))
            faces.append("")
    frame = frame.copy()
    frame["radar_nomenclature"] = names
    frame["blind_sector_start_rel"] = starts
    frame["blind_sector_end_rel"] = ends
    frame["radar_face"] = faces
    frame.to_csv(path, index=False)
    return frame


def load_snooze(path: Path) -> pd.DataFrame:
    """Load snooze_cruise.csv, creating the sample track if the file is absent."""
    rewrite = False
    if not path.is_file():
        rewrite = True
    else:
        frame = pd.read_csv(path)
        if any(
            name not in frame.columns
            for name in ("threat_bearing_deg", "casrep_reference", "weather_loss_db")
        ):
            print(f"{path} is missing threat, CASREP, or weather columns. Writing a fresh sample.")
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
    frame = _write_casrep_sectors(frame, path)
    if "bearing_deg" in frame.columns and "jammer_bearing_deg" not in frame.columns:
        frame["jammer_bearing_deg"] = frame["bearing_deg"]
    jammer_rows = frame.iloc[0:0]
    if "jammer_type" in frame.columns:
        kind = frame["record_kind"].astype(str) if "record_kind" in frame.columns else ""
        jammer_rows = frame.loc[
            frame["jammer_type"].notna()
            & (kind.eq("jammer") if "record_kind" in frame.columns else frame["latitude"].isna())
        ].copy()
    # Jammer and casualty rows in a merged snooze_cruise have no track. The
    # loop uses the strike-group rows, which carry weather and radar fields.
    frame = frame.loc[
        frame["time_seconds"].notna()
        & frame["latitude"].notna()
        & frame["longitude"].notna()
        & ~frame["ship_id"].str.lower().isin(["", "nan", "none"])
    ].copy()
    if frame.empty:
        raise ValueError(f"{path} has no ship track with a time and a position")
    for optional, default in (
        ("radar_status", "OK"),
        ("radar_mode", "SEARCH"),
        ("power_status", "NOMINAL"),
        ("cooling_status", "NOMINAL"),
        ("last_maintenance", "2026-10-15"),
        ("fault_code", "NONE"),
        ("casrep_reference", "NONE"),
    ):
        if optional not in frame.columns:
            frame[optional] = default
    track = frame.sort_values(["ship_id", "time_seconds"]).reset_index(drop=True)
    track.attrs["jammers"] = jammer_rows.reset_index(drop=True)
    return track


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
        "casrep_reference": (
            str(low["casrep_reference"])
            if "casrep_reference" in low.index and pd.notna(low["casrep_reference"])
            else "NONE"
        ),
        "threat_bearing_deg": _lerp_heading(
            float(low["threat_bearing_deg"]), float(high["threat_bearing_deg"]), fraction
        ),
        "threat_range_km": float(low["threat_range_km"]) if "threat_range_km" in low.index else None,
        "threat_priority": float(low["threat_priority"]) if "threat_priority" in low.index else None,
        "weather_phenomenon": str(low["weather_phenomenon"]) if "weather_phenomenon" in low.index else "clear air",
        "weather_rate_mm_h": float(low["weather_rate_mm_h"]) if "weather_rate_mm_h" in low.index else 0.0,
        "weather_cloud_percent": float(low["weather_cloud_percent"]) if "weather_cloud_percent" in low.index else 0.0,
        "weather_loss_db": float(low["weather_loss_db"]) if "weather_loss_db" in low.index else 0.0,
        "weather_bearing_deg": float(low["weather_bearing_deg"]) if "weather_bearing_deg" in low.index else 0.0,
        "weather_range_factor": float(low["weather_range_factor"]) if "weather_range_factor" in low.index else 1.0,
        "solar_flare_class": str(low["solar_flare_class"]) if "solar_flare_class" in low.index else "none",
        "weather_source": str(low["weather_source"]) if "weather_source" in low.index else "",
        "radar_nomenclature": str(low["radar_nomenclature"]) if "radar_nomenclature" in low.index else "",
        "blind_sector_start_rel": (
            float(low["blind_sector_start_rel"])
            if "blind_sector_start_rel" in low.index and pd.notna(low["blind_sector_start_rel"])
            else None
        ),
        "blind_sector_end_rel": (
            float(low["blind_sector_end_rel"])
            if "blind_sector_end_rel" in low.index and pd.notna(low["blind_sector_end_rel"])
            else None
        ),
        "radar_face": str(low["radar_face"]) if "radar_face" in low.index and pd.notna(low["radar_face"]) else "",
    }


def print_ship_state(state: pd.DataFrame) -> None:
    """Print the detailed radar row for every ship on this update."""
    print("Ship state")
    first = state.iloc[0]
    threat_range = getattr(first, "threat_range_km", None)
    span = f", range {float(threat_range):.0f} km" if threat_range is not None and pd.notna(threat_range) else ""
    print(f"  Threat axis: {float(first.threat_bearing_deg):.0f}° true{span}")
    if hasattr(first, "weather_phenomenon"):
        print(
            f"  Weather: {first.weather_phenomenon}  "
            f"{float(first.weather_rate_mm_h):.1f} mm/h  "
            f"{float(first.weather_loss_db):.1f} dB on {float(first.weather_bearing_deg):.0f}°"
        )
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
            f"  CASREP {row.casrep_reference}"
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


def jammer_at(table: pd.DataFrame | None, elapsed_s: float) -> dict | None:
    """One jammer row from snooze_cruise for this simulation time.

    Jammer records are not tied to a ship, so the row index follows the
    2-second clock. Poor SJR or high ERP shortens range around that bearing.
    """
    if table is None or len(table) == 0:
        return None
    index = int(elapsed_s / UPDATE_INTERVAL_SECONDS) % len(table)
    row = table.iloc[index]
    if "jammer_bearing_deg" in row.index and pd.notna(row["jammer_bearing_deg"]):
        bearing = float(row["jammer_bearing_deg"])
    elif "bearing_deg" in row.index and pd.notna(row["bearing_deg"]):
        bearing = float(row["bearing_deg"])
    else:
        return None
    sjr = float(row["sjr_db"])
    erp = float(row["erp_kw"])
    ducting = bool(int(float(row["atmospheric_ducting"]))) if pd.notna(row["atmospheric_ducting"]) else False
    sjr_bad = min(1.0, max(0.0, (3.0 - sjr) / 25.0))
    erp_bad = min(1.0, max(0.0, erp / 400.0))
    strength = max(sjr_bad, 0.65 * erp_bad)
    if ducting:
        strength = min(1.0, strength * 1.2)
    action = row["optimal_action"]
    return {
        "bearing": bearing % 360.0,
        "sjr": sjr,
        "erp": erp,
        "ducting": ducting,
        "factor": max(0.4, 1.0 - 0.55 * strength),
        "half": 45.0 if ducting else 30.0,
        "jammer_type": row["jammer_type"],
        "action": int(float(action)) if pd.notna(action) else 0,
    }


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
    jammers = cruise.attrs.get("jammers")
    plt.ion()
    print(
        f"Live radar monitor. Update every {interval:g} s. "
        f"Stops after {updates} update(s), {total_runtime:g} s. Ctrl+C to stop early."
    )
    print(f"Data file: {SNOOZE_FILE}")
    print(f"Schedule in file: {schedule_text(cruise)}.")
    print(f"CASREP texts: {CASREP_DIR}/")
    announced: set[str] = set()
    try:
        for cycle in range(updates):
            remaining = max(0.0, total_runtime - cycle * interval)
            plt.close("all")
            stamp = scenario.clock()
            state = scenario.current_state()
            state.to_csv(LIVE_STATE_FIGURE, index=False)
            threat = float(state["threat_bearing_deg"].iloc[0])
            threat_range = state["threat_range_km"].iloc[0] if "threat_range_km" in state.columns else None
            jammer = jammer_at(jammers, scenario.elapsed_s)
            jammer_title = f" — jammer {jammer['bearing']:.0f}°" if jammer else ""
            title = (
                f"Carrier Strike Group radar status — "
                f"T+{scenario.elapsed_s:.0f} s ({stamp.strftime('%H:%M:%S')} UTC)"
                f" — {remaining:.0f} s left — threat {threat:.0f}°{jammer_title}"
            )
            print()
            print(
                f"--- {stamp.strftime('%Y-%m-%d %H:%M:%S UTC')}  |  "
                f"simulation T+{scenario.elapsed_s:.0f} s  |  "
                f"{remaining:.0f} s remaining  |  threat {threat:.0f}°"
                + (
                    f"  |  jammer {jammer['bearing']:.0f}°  {jammer['erp']:.0f} kW  "
                    f"SJR {jammer['sjr']:.1f} dB ---"
                    if jammer
                    else " ---"
                )
            )
            print_ship_state(state)
            for row in state.itertuples(index=False):
                reference = str(row.casrep_reference)
                if reference not in {"NONE", "nan"} and reference not in announced:
                    announced.add(reference)
                    print(
                        f"CASREP now in effect: {reference}  "
                        f"{row.ship_name}  {row.radar_status}  {row.capacity_percent:.0f}%"
                    )
                    print(f"  Full text: {CASREP_DIR / (reference + '.txt')}")
            analyze_frames(
                scenario.radar_frame(),
                scenario.location_frame(),
                title=title,
                threat_bearing=threat,
                threat_range_km=None if threat_range is None or pd.isna(threat_range) else float(threat_range),
                jammer=jammer,
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


def radar_main(argv: list[str] | None = None) -> int:
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


def main(argv: list[str] | None = None) -> int:
    """Strike-group loop by default. weather, monitor, and advise keep the forecast tools."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["weather"]:
        return forecast_main(args[1:])
    if args[:1] == ["monitor"]:
        return monitor_main(args[1:])
    if args[:1] == ["advise"]:
        return advise_main(args[1:])
    return radar_main(args)


if __name__ == "__main__":
    sys.exit(main())
