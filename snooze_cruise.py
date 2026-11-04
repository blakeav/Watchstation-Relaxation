"""Weather impact on a ship radar, using government forecasts only.

Samples a 70 nautical mile disk around the ship and compares precipitation
with the two-way loss a radar can tolerate. Forecasts come from national
meteorological services:

- Swedish Meteorological and Hydrological Institute (EU), for the Nordic seas
- US National Weather Service (NATO), for United States forecast waters
- Norwegian Meteorological Institute (NATO), for every other sea area
- NOAA Space Weather Prediction Center (NATO), for solar flares

No commercial weather aggregator is used.

The whole program is this one file. Ocean stations, synthesized ships, and the
forecast checks are included. Copy the file anywhere and run it. Opening it runs a weather scan before any other command:

    python snooze_cruise.py
    python snooze_cruise.py monitor
    python snooze_cruise.py advise
    python snooze_cruise.py --lat 58 --lon 1.5
"""

from __future__ import annotations

import argparse
import json
import math
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
        completed = subprocess.run(
            [
                "curl.exe",
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


EVENT_RADIUS_NM = 60.0
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
            f"{density:.2f} g/m³, {loss:.1f} dB two-way"
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
                f"{MEDIUM_DROPLET_G_M3:.2f} g/m³, {loss:.1f} dB two-way"
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
            "The published disk is the 60 nmi ocean sample. Leaving that disk does not leave the sunlit side."
        ),
        source=source,
        valid_time=valid_time.isoformat(),
        loss_db=0.0,
        station=name,
        sun_azimuth_deg=sun_azimuth_deg,
        sun_elevation_deg=sun_elevation_deg,
    )


def distance_nm(lat: float, lon: float, other_lat: float, other_lon: float) -> float:
    east, north = latlon_to_nm(other_lat, other_lon, lat, lon)
    return math.hypot(east, north)


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


def monitor_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Poll NATO and EU government forecasts at ocean stations. "
            "Write interfering weather as coordinates plus a 60 nmi radius."
        )
    )
    parser.add_argument("--stations", type=Path, default=None, help="Optional ocean station JSON")
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
    stations = load_stations(args.stations)
    if args.max_stations > 0:
        stations = stations[: args.max_stations]
    if not stations:
        print("No ocean stations to check.", file=sys.stderr)
        return 1

    while True:
        now = datetime.now(timezone.utc)
        print(
            f"{now.strftime('%Y-%m-%d %H:%M')} UTC  checking {len(stations)} ocean stations...",
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
            print(format_cycle(observations, events, failures, flare_detail, args.events))
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
) -> str:
    lines = [
        f"Stations read: {len(observations)}. Interference disks: {len(events)}.",
        f"Solar flare: {flare_detail}",
        *_weather_lines(observations),
    ]
    if events:
        lines.append("Interference:")
        for event in events:
            lines.append(
                f"  {event.phenomenon} is occurring at {event.station}, "
                f"{format_position(event.lat, event.lon)}, {event.radius_nm:.0f} nmi. {event.detail}"
            )
    else:
        lines.append("No ocean station is over the radar-interference threshold.")
    for failure in failures:
        lines.append(f"Failed: {failure}")
    lines.append(f"Wrote {path}")
    return "\n".join(lines)


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
        "0.05 g/m³, visibility of the order of 300 m"
        if math.isclose(radar.fog_density_g_m3, MEDIUM_DROPLET_G_M3)
        else "0.5 g/m³, visibility of the order of 50 m"
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
        f"medium small-droplet density of {MEDIUM_DROPLET_G_M3:.2f} g/m³. "
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
        radius_nm=60.0,
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


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "monitor":
        return monitor_main(args[1:])
    print("Initial weather scan:", file=sys.stderr)
    scan_code = monitor_main(["--once"])
    if scan_code != 0 or not args:
        return scan_code
    if args[0] == "advise":
        return advise_main(args[1:])
    return forecast_main(args)


if __name__ == "__main__":
    sys.exit(main())
