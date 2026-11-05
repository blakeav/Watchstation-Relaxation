"""Advise which published radar band a ship should be using.

Fog and rain losses follow ITU-R P.840 and ITU-R P.838-3. Band edges follow
the IMO radar performance standards and the Furuno and Lockheed Martin
figures cited on each scanner. Near land, household receivers are protected
by sector blanking and by taking the high-power sets off the air. A step
inside a radar allocation does not move the transmission onto, or off, a
television or a baby monitor.

Copy this file anywhere and run it with Python 3.11 or newer. It does not import the rest of this project.

    python radar_frequency_advice.py
    python radar_frequency_advice.py --fog-percent 100 --visibility-m 50 --range-nm 24
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass

NAUTICAL_MILE_KM = 1.852
RADIUS_NM = 70.0
TOLERANCE_DB = 3.0

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


def two_way_loss_db(rate_mm_h: float, k: float, alpha: float, range_nm: float) -> float:
    if rate_mm_h <= 0 or range_nm <= 0:
        return 0.0
    path_km = min(range_nm, RADIUS_NM) * NAUTICAL_MILE_KM
    return 2 * k * (rate_mm_h ** alpha) * path_km


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


# ITU-R P.840-9 examples: medium fog, visibility of the order of 300 m;
# thick fog, visibility of the order of 50 m.
MEDIUM_VISIBILITY_M = 300.0
THICK_VISIBILITY_M = 50.0
MEDIUM_WATER_G_M3 = 0.05
THICK_WATER_G_M3 = 0.5
# MET Norway publishes fog where visibility is under 1 km.
FOG_VISIBILITY_M = 1000.0
# UNCLOS territorial sea. This starts the coastal check. It is not a
# measured distance at which a television or monitor fails.
COASTAL_NM = 12.0
MAX_RANGE_NM = 70.0
# A listed carrier matches within this window. Radar edges in this program are 30 MHz apart.
MATCH_GHZ = 0.0005
IMO_S_GHZ = (2.9, 3.1)
IMO_X_GHZ = (9.2, 9.5)
# IEEE Std 521 radar letter bands. C-band sits between S-band and X-band.
C_BAND_LOW_GHZ = 4.0
C_BAND_HIGH_GHZ = 8.0

# Published distress, safety, and first-responder allocations. Values are MHz.
# A radar recommendation is never allowed to land inside one of these spans.
def _tone(center_mhz: float, name: str, half_khz: float = 12.5) -> tuple[float, float, str]:
    half_mhz = half_khz / 1000.0
    return (center_mhz - half_mhz, center_mhz + half_mhz, name)


TABOO_SPANS: tuple[tuple[float, float, str], ...] = (
    (0.490, 0.510, "490–510 kHz maritime distress and search-and-rescue telegraphy"),
    _tone(0.518, "518 kHz NAVTEX maritime safety", 3),
    (2.170, 2.194, "2170–2194 kHz international maritime distress, calling, and safety"),
    _tone(3.023, "3023 kHz aeronautical on-scene search and rescue", 3),
    _tone(4.125, "4125 kHz distress voice", 3),
    _tone(4.1775, "4177.5 kHz NBDP distress", 3),
    _tone(4.2075, "4207.5 kHz DSC distress", 3),
    _tone(4.2095, "4209.5 kHz NAVTEX", 3),
    _tone(5.680, "5680 kHz aeronautical on-scene search and rescue", 3),
    _tone(6.215, "6215 kHz distress voice", 3),
    _tone(6.268, "6268 kHz NBDP distress", 3),
    _tone(6.312, "6312 kHz DSC distress", 3),
    _tone(8.291, "8291 kHz distress voice", 3),
    (8.354, 8.374, "8354–8374 kHz emergency search and rescue"),
    _tone(8.3765, "8376.5 kHz NBDP distress", 3),
    _tone(8.4145, "8414.5 kHz DSC distress", 3),
    _tone(12.290, "12290 kHz distress voice", 3),
    _tone(12.520, "12520 kHz NBDP distress", 3),
    _tone(12.577, "12577 kHz DSC distress", 3),
    _tone(16.420, "16420 kHz distress voice", 3),
    _tone(16.695, "16695 kHz NBDP distress", 3),
    _tone(16.8045, "16804.5 kHz DSC distress", 3),
    (121.4, 121.6, "121.4–121.6 MHz aeronautical emergency, 121.5 MHz international distress"),
    _tone(123.1, "123.1 MHz aeronautical search and rescue"),
    _tone(156.300, "VHF channel 6, intership safety"),
    _tone(156.525, "VHF channel 70, DSC distress"),
    _tone(156.650, "VHF channel 13, bridge-to-bridge safety"),
    (156.7, 156.9, "156.7–156.9 MHz marine distress and safety, VHF channel 16"),
    _tone(161.975, "AIS channel 1, including AIS-SART"),
    _tone(162.025, "AIS channel 2, including AIS-SART"),
    (242.8, 243.2, "242.8–243.2 MHz military aeronautical emergency and survival craft"),
    (380.0, 385.0, "European emergency-service band 380–385 MHz"),
    (390.0, 395.0, "European emergency-service band 390–395 MHz"),
    (406.0, 406.1, "406–406.1 MHz COSPAS-SARSAT distress beacons"),
    (758.0, 775.0, "US 700 MHz public safety and FirstNet"),
    (788.0, 805.0, "US 700 MHz public safety and FirstNet uplink"),
    (806.0, 815.0, "US 800 MHz public safety"),
    (851.0, 860.0, "US 800 MHz public safety"),
    (1427.0, 1431.0, "FCC public-safety telemetry 1427–1431 MHz"),
    (1544.0, 1545.0, "1544–1545 MHz COSPAS-SARSAT space-to-Earth"),
    (
        4940.0,
        4990.0,
        "4.940–4.990 GHz public safety for law enforcement, fire, and emergency medical services; not a consumer Wi-Fi band",
    ),
    (5850.0, 5925.0, "FCC public safety 5850–5925 MHz"),
)

# Carriers the operator asked to treat as friendly. A carrier that already
# falls in a taboo span stays taboo. Taboo is decided first.
CHANNEL_HALF_HZ = 12_500
FRIENDLY_CHANNELS: tuple[tuple[float, str], ...] = (
    (121.500, "VHF civil and military emergency and guard"),
    (243.000, "UHF military emergency and guard"),
    (126.200, "military tower common"),
    (242.400, "Army helicopter common"),
    (242.500, "Army helicopter common"),
    (246.800, "USAF common Cheerleader"),
    (252.100, "Air Force Reserves command-post common"),
    (252.525, "USAF common Triple 25"),
    (255.400, "military flight service station"),
    (257.800, "military tower common at civilian airports"),
    (282.800, "military search and rescue"),
    (299.500, "USAF common Cheap Suit"),
    (300.600, "USAF common and air-to-air"),
    (300.650, "USAF common and air-to-air"),
    (303.000, "USAF common and air-to-air Winchester"),
    (303.050, "USAF common and air-to-air Winchester"),
    (4.724, "HFGCS 4724.0 kHz"),
    (6.712, "HFGCS 6712.0 kHz"),
    (8.992, "HFGCS 8992.0 kHz"),
    (11.175, "HFGCS 11175.0 kHz primary"),
    (13.215, "HFGCS 13215.0 kHz"),
    (15.016, "HFGCS 15016.0 kHz"),
    (396.875, "ISR channel 1"),
    (397.125, "ISR channel 2"),
    (397.175, "ISR channel 3"),
    (397.375, "ISR channel 4"),
    (397.425, "ISR channel 5"),
    (397.475, "ISR channel 6"),
    (397.550, "ISR channel 7"),
    (397.950, "ISR channel 8"),
    (398.050, "ISR channel 9"),
    (399.425, "ISR channel 10, CAP prohibited"),
    (399.475, "ISR channel 11, CAP prohibited"),
    (399.725, "ISR channel 12"),
    (352.600, "aerial refueling"),
    (360.200, "Navy control towers"),
    (364.200, "NORAD air intercept command and control"),
    (372.200, "Air Force dispatch common"),
    (375.200, "Air Force weather"),
    (376.025, "Air Mobility Command common"),
    (381.300, "Air Force ACC command posts"),
    (384.100, "NORAD air intercept command secondary"),
    (385.000, "Navy port control common"),
    (385.900, "Air Force ACC logistics"),
)

SOURCES = (
    "ITU-R P.840-9, attenuation due to clouds and fog: specific attenuation "
    "Kl times liquid-water density. Medium fog is about 0.05 g/m³ (visibility "
    "of the order of 300 m); thick fog is about 0.5 g/m³ (about 50 m).",
    "ITU-R P.838-3, specific attenuation due to rain, as already used for the "
    f"{TOLERANCE_DB:.0f} dB two-way gate in this project.",
    "IMO radar performance standards: X-band 9.2–9.5 GHz for discrimination; "
    "S-band 2.9–3.1 GHz so detection holds in fog, rain, and sea clutter. "
    "https://www.imorules.com/GUID-A699FBD5-D49D-4437-BD35-76492CB21D30.html",
    "Furuno operator's guide: X-band attenuates more in precipitation; S-band "
    "is the inclement-weather band. https://www.furunousa.com/-/media/sites/furuno/reference_materials/furunoradarguide_lr.pdf",
    "Furuno FAR-2228 specification: 9410 MHz ± 30 MHz, 25 kW, horizontal polarization.",
    "Furuno FAR-21x7 brochure: S-band 3050 MHz ± 30 MHz, 30 kW on the FAR-2137S; "
    "the S-band scanner is the one specified for adverse weather.",
    "Furuno installation guidance: sector blanking stops transmission on a chosen "
    "bearing. That is the published control for energy leaving the scanner, "
    "including energy toward nearby structures.",
    "Lockheed Martin AN/SPY-1, public characteristics in ITU-R M.1465 and the "
    "Naval Postgraduate School radar notes: S-band tuning span 3100–3500 MHz, "
    "vertical polarization. https://faculty.nps.edu/jenn/EC4610/Vol4v7.2.pdf",
    "US DOD Director, Operational Test and Evaluation, FY2023 report: Raytheon "
    "AN/SPY-6 is the Navy 3D air and missile defense radar and is used in "
    "littoral and electromagnetically contested water. A public channel step "
    "was not stated in that report.",
    "UNCLOS article 3: the territorial sea extends 12 nautical miles. Inside "
    "that distance this program starts the coastal blanking check.",
    "ITU Radio Regulations Appendix 15 distress and safety frequencies, including "
    "2182 kHz, VHF channel 16, 121.5 MHz, and 406 MHz COSPAS-SARSAT beacons. "
    "These are taboo and can never be switched to.",
    "FCC public-safety allocations: 700 MHz FirstNet and narrowband, 800 MHz "
    "public safety, 4940–4990 MHz, and 5850–5925 MHz. "
    "https://www.fcc.gov/public-safety-licensing",
    "European emergency services use 380–385 MHz and 390–395 MHz (ECC/DEC/(08)05 "
    "and the TETRA emergency allocation). Those spans are taboo.",
    "IEEE Std 521 letter bands: C-band is 4–8 GHz, between S-band and X-band. "
    "With no interference and no loss past the tolerance gate, a tunable scanner "
    "is placed on the published channel closest to that range. The scanner is not "
    "moved outside its own allocation, and it is not moved into a taboo span. "
    "The marine radar bands 2.9–3.1 GHz and 9.2–9.5 GHz are not on the taboo list. "
    "Search-and-rescue transponders answer a navigation radar in the 9.2–9.5 GHz band, "
    "so that scanner stays there. Shared land-mobile pools such as the whole of "
    "150–174 MHz are not marked taboo, because they are not reserved for emergency services.",
)


@dataclass(frozen=True)
class RadarSet:
    role: str
    manufacturer: str
    model: str
    frequency_ghz: float
    low_ghz: float
    high_ghz: float
    tunable: bool
    polarization: str
    weather_alternate: str | None
    coastal: str
    coordination: str
    note: str


@dataclass(frozen=True)
class Conditions:
    fog_percent: float = 0.0
    visibility_m: float | None = None
    temperature_c: float = 0.0
    rain_mm_h: float = 0.0
    range_nm: float = 24.0
    emi: str = "none"
    interferer_ghz: float | None = None
    land_nm: float | None = None
    land_bearing_deg: float | None = None
    extra_taboo_ghz: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        if not 0 <= self.fog_percent <= 100:
            raise ValueError("Fog coverage must be from 0 to 100 percent.")
        if self.visibility_m is not None and self.visibility_m <= 0:
            raise ValueError("Visibility must be positive.")
        if not -40 <= self.temperature_c <= 40:
            raise ValueError("Temperature must be from -40 to 40 C.")
        if self.rain_mm_h < 0:
            raise ValueError("Rain rate cannot be negative.")
        if not 0 < self.range_nm <= MAX_RANGE_NM:
            raise ValueError(f"Range must be from 0 to {MAX_RANGE_NM:.0f} nautical miles.")
        if self.emi not in {"none", "in_band", "broadband"}:
            raise ValueError("EMI must be none, in_band, or broadband.")
        if self.land_nm is not None and self.land_nm < 0:
            raise ValueError("Distance to land cannot be negative.")
        if self.land_bearing_deg is not None and not 0 <= self.land_bearing_deg < 360:
            raise ValueError("Land bearing must be from 0 up to 360 degrees.")
        if any(frequency <= 0 for frequency in self.extra_taboo_ghz):
            raise ValueError("Taboo frequencies must be positive.")


@dataclass(frozen=True)
class Advice:
    role: str
    model: str
    action: str
    frequency_ghz: float
    loss_db: float
    status: str
    text: str


# Order is the order the user named. The surface scanner is the X-band
# navigation set. The 2D scanner is the S-band set that replaces it in weather.
SETS: dict[str, RadarSet] = {
    "2d": RadarSet(
        role="2D scanner",
        manufacturer="Furuno",
        model="FAR-2137S class S-band",
        frequency_ghz=3.050,
        low_ghz=3.020,
        high_ghz=3.080,
        tunable=True,
        polarization="horizontal",
        weather_alternate=None,
        coastal="navigation",
        coordination="permitted",
        note="Published transceiver 3050 MHz ± 30 MHz, 30 kW. This is the adverse-weather scanner.",
    ),
    "3d": RadarSet(
        role="3D scanner",
        manufacturer="Raytheon",
        model="AN/SPY-6",
        frequency_ghz=3.300,
        low_ghz=3.300,
        high_ghz=3.300,
        tunable=False,
        polarization="horizontal",
        weather_alternate=None,
        coastal="inhibit",
        coordination="guarded",
        note=(
            "S-band 3D air and missile defense set. Loss is computed at 3.300 GHz "
            "because the FY2023 DOT&E report does not publish a channel step. "
            "The program will not invent one."
        ),
    ),
    "surface": RadarSet(
        role="surface scanner",
        manufacturer="Furuno",
        model="FAR-2228 X-band",
        frequency_ghz=9.410,
        low_ghz=9.380,
        high_ghz=9.440,
        tunable=True,
        polarization="horizontal",
        weather_alternate="2d",
        coastal="navigation",
        coordination="permitted",
        note="Published transceiver 9410 MHz ± 30 MHz, 25 kW, horizontal polarization.",
    ),
    "air": RadarSet(
        role="air defence scanner",
        manufacturer="Lockheed Martin",
        model="AN/SPY-1",
        frequency_ghz=3.300,
        low_ghz=3.100,
        high_ghz=3.500,
        tunable=True,
        polarization="vertical",
        weather_alternate=None,
        coastal="inhibit",
        coordination="guarded",
        note=(
            "Public ITU-R M.1465 span 3100–3500 MHz, vertical polarization. "
            "The nominal 3.300 GHz is the middle of that span, not a claim that "
            "the set is parked there."
        ),
    ),
    "fire": RadarSet(
        role="fire watch",
        manufacturer="Raytheon",
        model="X-band fire-control class",
        frequency_ghz=9.410,
        low_ghz=9.410,
        high_ghz=9.410,
        tunable=False,
        polarization="horizontal",
        weather_alternate="2d",
        coastal="inhibit",
        coordination="guarded",
        note=(
            "Fire-control sets of this class stay in X-band for the narrow beam. "
            "No public tuning window is applied here, and the set is not given "
            "a search or illumination plan."
        ),
    ),
}


def liquid_water_g_m3(visibility_m: float) -> float:
    """Interpolate the two liquid-water examples in ITU-R P.840.

    The recommendation gives 0.05 g/m³ near 300 m visibility and 0.5 g/m³
    near 50 m. Values between those anchors follow a straight line on a
    log-log plot. Outside the anchors the same line is extended and labeled
    as an extrapolation in the report.
    """
    if visibility_m <= 0:
        raise ValueError("Visibility must be positive.")
    slope = _log_ratio(MEDIUM_WATER_G_M3, THICK_WATER_G_M3) / _log_ratio(
        THICK_VISIBILITY_M, MEDIUM_VISIBILITY_M
    )
    return THICK_WATER_G_M3 * (THICK_VISIBILITY_M / visibility_m) ** slope


def _log_ratio(left: float, right: float) -> float:
    return math.log(left / right)


def fog_density(conditions: Conditions) -> tuple[float, str]:
    """Return g/m³ and a short description of which ITU anchor was used."""
    if conditions.fog_percent <= 0:
        return 0.0, "no fog coverage was entered"
    if conditions.visibility_m is None:
        return (
            MEDIUM_WATER_G_M3,
            "visibility was not entered, so the ITU-R P.840 medium-fog value 0.05 g/m³ is used",
        )
    if conditions.visibility_m >= FOG_VISIBILITY_M:
        return (
            0.0,
            f"visibility {conditions.visibility_m:.0f} m is at or beyond 1 km, which MET Norway does not class as fog",
        )
    density = liquid_water_g_m3(conditions.visibility_m)
    if conditions.visibility_m < THICK_VISIBILITY_M or conditions.visibility_m > MEDIUM_VISIBILITY_M:
        anchor = "extrapolated from the ITU-R P.840 50 m and 300 m examples"
    else:
        anchor = "interpolated between the ITU-R P.840 50 m and 300 m examples"
    return density, f"{density:.3f} g/m³, {anchor}"


def band_loss_db(frequency_ghz: float, polarization: str, conditions: Conditions) -> tuple[float, float]:
    """Fog loss scaled by path coverage, then rain loss over the whole path."""
    density, _detail = fog_density(conditions)
    fog = 0.0
    if density > 0 and conditions.fog_percent > 0:
        full = droplet_loss_db(frequency_ghz, conditions.temperature_c, density, conditions.range_nm)
        fog = full * (conditions.fog_percent / 100.0)
    k, alpha = specific_coefficients(frequency_ghz, polarization)
    rain = two_way_loss_db(conditions.rain_mm_h, k, alpha, conditions.range_nm)
    return fog, rain


def coverage_percent_for_tolerance(
    frequency_ghz: float,
    visibility_m: float,
    range_nm: float,
    temperature_c: float = 0.0,
) -> float | None:
    """Path coverage at which fog alone reaches the 3 dB two-way gate.

    None means the full path stays inside the gate at this visibility.
    """
    density = 0.0 if visibility_m >= FOG_VISIBILITY_M else liquid_water_g_m3(visibility_m)
    full = droplet_loss_db(frequency_ghz, temperature_c, density, range_nm)
    if full <= TOLERANCE_DB:
        return None
    return 100.0 * TOLERANCE_DB / full


def _near_land(conditions: Conditions) -> bool:
    return conditions.land_nm is not None and conditions.land_nm <= COASTAL_NM


def _far_edge(radar: RadarSet, interferer_ghz: float | None) -> float:
    if interferer_ghz is None:
        if abs(radar.frequency_ghz - radar.low_ghz) <= abs(radar.frequency_ghz - radar.high_ghz):
            return radar.high_ghz
        return radar.low_ghz
    if abs(interferer_ghz - radar.low_ghz) <= abs(interferer_ghz - radar.high_ghz):
        return radar.high_ghz
    return radar.low_ghz


def _in_window(radar: RadarSet, frequency_ghz: float | None) -> bool:
    if frequency_ghz is None:
        return True
    return radar.low_ghz - 1e-9 <= frequency_ghz <= radar.high_ghz + 1e-9


def _listed(frequency_ghz: float, points: tuple[float, ...]) -> bool:
    return any(abs(frequency_ghz - point) <= MATCH_GHZ for point in points)


def taboo_reason(frequency_ghz: float, extra_taboo_ghz: tuple[float, ...] = ()) -> str | None:
    """Name the emergency or operator ban that forbids this frequency, if any."""
    hz = round(frequency_ghz * 1_000_000_000)
    for low_mhz, high_mhz, name in TABOO_SPANS:
        if round(low_mhz * 1_000_000) <= hz <= round(high_mhz * 1_000_000):
            return name
    if _listed(frequency_ghz, extra_taboo_ghz):
        return "operator taboo list"
    return None


def _in_imo_radar_band(frequency_ghz: float) -> bool:
    low_s, high_s = IMO_S_GHZ
    low_x, high_x = IMO_X_GHZ
    return low_s <= frequency_ghz <= high_s or low_x <= frequency_ghz <= high_x


def _friendly_assignment(frequency_ghz: float) -> bool:
    return any(abs(frequency_ghz - radar.frequency_ghz) <= MATCH_GHZ for radar in SETS.values())


def friendly_channel_name(
    frequency_ghz: float,
    extra_taboo_ghz: tuple[float, ...] = (),
) -> str | None:
    """Return the friendly-channel name when taboo has not already claimed it."""
    hz = round(frequency_ghz * 1_000_000_000)
    for mhz, name in FRIENDLY_CHANNELS:
        if taboo_reason(mhz / 1000.0, extra_taboo_ghz) is not None:
            continue
        center_hz = round(mhz * 1_000_000)
        if abs(hz - center_hz) <= CHANNEL_HALF_HZ:
            return name
    return None


def frequency_status(
    frequency_ghz: float,
    radar: RadarSet,
    conditions: Conditions,
) -> tuple[str, str]:
    """Classify a candidate transmit frequency.

    Taboo wins over every other label. A taboo frequency can never be switched to.
    A listed friendly carrier is friendly only when it is outside every taboo span.
    Guarded is the federal scanner's own published span. A ship assignment is
    friendly. Permitted is an IMO maritime radar channel inside a navigation window.
    """
    banned = taboo_reason(frequency_ghz, conditions.extra_taboo_ghz)
    if banned is not None:
        return "taboo", banned
    named = friendly_channel_name(frequency_ghz, conditions.extra_taboo_ghz)
    if named is not None:
        return "friendly", named
    if not _in_window(radar, frequency_ghz):
        return "taboo", "outside this scanner's published window"
    if radar.coordination == "guarded":
        return "guarded", "authorized only inside this scanner's published span"
    if _friendly_assignment(frequency_ghz):
        return "friendly", "already assigned to a scanner on this ship"
    if radar.coordination == "permitted" and _in_imo_radar_band(frequency_ghz):
        return "permitted", "IMO maritime radar allocation and this scanner's published window"
    return "taboo", "no published authority for this scanner to transmit here"


def _legal_retune(radar: RadarSet, conditions: Conditions) -> float | None:
    """Far edge of the published window, skipping taboo frequencies and the interferer."""
    if radar.low_ghz == radar.high_ghz:
        return None
    preferred = _far_edge(radar, conditions.interferer_ghz)
    other = radar.low_ghz if abs(preferred - radar.high_ghz) <= 1e-9 else radar.high_ghz
    candidates = [preferred]
    if abs(other - preferred) > 1e-6:
        candidates.append(other)
    for frequency in candidates:
        status, _why = frequency_status(frequency, radar, conditions)
        if status == "taboo":
            continue
        if conditions.interferer_ghz is not None and abs(frequency - conditions.interferer_ghz) <= MATCH_GHZ:
            continue
        return frequency
    return None


def advise(conditions: Conditions) -> list[Advice]:
    losses = {
        key: band_loss_db(radar.frequency_ghz, radar.polarization, conditions)
        for key, radar in SETS.items()
    }
    use_s_band = _weather_prefers_s_band(losses)
    prefer_c_band = conditions.emi == "none" and sum(losses["2d"]) <= TOLERANCE_DB
    handoff_ghz = _s_band_frequency(conditions, prefer_c_band)
    lines: list[Advice] = []
    for key, radar in SETS.items():
        lines.append(_advise_one(key, radar, conditions, losses, use_s_band, handoff_ghz))
    return lines


def distance_to_c_band(frequency_ghz: float) -> float:
    """How far a frequency sits from IEEE C-band, 4–8 GHz."""
    if frequency_ghz < C_BAND_LOW_GHZ:
        return C_BAND_LOW_GHZ - frequency_ghz
    if frequency_ghz > C_BAND_HIGH_GHZ:
        return frequency_ghz - C_BAND_HIGH_GHZ
    return 0.0


def _nearest_c_band(radar: RadarSet, conditions: Conditions) -> float | None:
    """Published channel closest to C-band. None when the nominal is already closest."""
    if not radar.tunable or radar.low_ghz == radar.high_ghz:
        return None
    best = radar.frequency_ghz
    best_distance = distance_to_c_band(best)
    for frequency in (radar.low_ghz, radar.high_ghz):
        status, _why = frequency_status(frequency, radar, conditions)
        if status == "taboo":
            continue
        distance = distance_to_c_band(frequency)
        if distance + 1e-9 < best_distance:
            best = frequency
            best_distance = distance
    if abs(best - radar.frequency_ghz) <= 1e-6:
        return None
    return best


def _c_band_reason(radar: RadarSet, frequency_ghz: float) -> str:
    return (
        f"Nothing is degrading this scanner, so use {frequency_ghz:.3f} GHz, "
        f"the published channel closest to IEEE C-band "
        f"({C_BAND_LOW_GHZ:.0f}–{C_BAND_HIGH_GHZ:.0f} GHz). "
        f"It is {distance_to_c_band(frequency_ghz):.3f} GHz from that band and stays inside "
        f"{radar.low_ghz:.3f}–{radar.high_ghz:.3f} GHz."
    )


def _s_band_frequency(conditions: Conditions, prefer_c_band: bool = False) -> float:
    """Frequency the S-band navigation scanner should actually be on."""
    radar = SETS["2d"]
    if conditions.emi == "in_band" and _in_window(radar, conditions.interferer_ghz):
        edge = _legal_retune(radar, conditions)
        if edge is not None:
            return edge
    if prefer_c_band:
        nearest = _nearest_c_band(radar, conditions)
        if nearest is not None:
            return nearest
    return radar.frequency_ghz


def _weather_prefers_s_band(losses: dict[str, tuple[float, float]]) -> bool:
    surface = sum(losses["surface"])
    s_band = sum(losses["2d"])
    return surface > TOLERANCE_DB and s_band < surface


def _advise_one(
    key: str,
    radar: RadarSet,
    conditions: Conditions,
    losses: dict[str, tuple[float, float]],
    use_s_band: bool,
    handoff_ghz: float,
) -> Advice:
    fog_loss, rain_loss = losses[key]
    total = fog_loss + rain_loss
    frequency = radar.frequency_ghz
    action = "hold"
    reasons: list[str] = []

    if use_s_band and radar.weather_alternate == "2d":
        alternate = SETS["2d"]
        frequency = handoff_ghz
        action = "change_band"
        c_band_note = ""
        if conditions.emi == "none" and abs(handoff_ghz - alternate.frequency_ghz) > 1e-6:
            c_band_note = "That is the published S-band channel closest to IEEE C-band, 4–8 GHz. "
        reasons.append(
            f"Two-way loss at {radar.frequency_ghz:.3f} GHz is {total:.1f} dB "
            f"over {conditions.range_nm:.0f} nmi, past the {TOLERANCE_DB:.0f} dB gate. "
            f"Put detection on the {alternate.model} at {handoff_ghz:.3f} GHz, "
            f"where the same path is {sum(losses['2d']):.1f} dB. "
            f"{c_band_note}"
            "An X-band scanner cannot be tuned down to S-band; the other scanner is selected. "
            f"{_loss_parts(fog_loss, rain_loss)}"
        )
    elif total > TOLERANCE_DB:
        reasons.append(
            f"Two-way loss at {radar.frequency_ghz:.3f} GHz is {total:.1f} dB, "
            f"past the {TOLERANCE_DB:.0f} dB gate. This set has no second published band "
            f"to move to. {_loss_parts(fog_loss, rain_loss)} "
            "Expect a shorter useful range and keep the S-band 2D scanner as the search picture."
        )
    else:
        reasons.append(
            f"Two-way loss at {radar.frequency_ghz:.3f} GHz is {total:.1f} dB "
            f"({_loss_parts(fog_loss, rain_loss)}), inside the {TOLERANCE_DB:.0f} dB gate."
        )

    if key == "2d" and handoff_ghz != radar.frequency_ghz and action == "hold":
        frequency = handoff_ghz
        action = "retune"
        if conditions.emi == "in_band":
            reasons.append(
                f"In-band interference: move to {frequency:.3f} GHz, inside the "
                f"published {radar.low_ghz:.3f}–{radar.high_ghz:.3f} GHz window."
            )
        else:
            reasons.append(_c_band_reason(radar, frequency))
    elif conditions.emi == "in_band" and radar.tunable and action != "change_band":
        if _in_window(radar, conditions.interferer_ghz):
            edge = _legal_retune(radar, conditions)
            if edge is None:
                reasons.append(
                    "Every published edge is taboo or sits on the interferer, so the "
                    "frequency is not changed. A taboo frequency can never be switched to."
                )
            else:
                frequency = edge
                action = "retune"
                reasons.append(
                    f"In-band interference: move to {frequency:.3f} GHz, inside the "
                    f"published {radar.low_ghz:.3f}–{radar.high_ghz:.3f} GHz window."
                )
        else:
            reasons.append(
                "The reported interferer is outside this set's published window, so a step "
                "inside the window does not leave it. Keep the allocated frequency."
            )
    elif conditions.emi == "broadband" and action != "change_band":
        reasons.append(
            "Broadband interference follows a small frequency step. Keep the allocated "
            "frequency and treat the affected sector as unusable rather than chasing the noise."
        )
    elif conditions.emi == "in_band" and not radar.tunable and action != "change_band":
        reasons.append(
            "No public channel step is on file for this set. Leave the frequency where it is."
        )

    if _near_land(conditions):
        bearing = _bearing_phrase(conditions.land_bearing_deg)
        if radar.coastal == "inhibit":
            action = "inhibit"
            frequency = radar.frequency_ghz
            reasons.append(
                f"Land is {conditions.land_nm:.1f} nmi away, inside the 12 nmi territorial sea. "
                f"Inhibit this set {bearing}. Its frequency stays {radar.frequency_ghz:.3f} GHz. "
                "Televisions and baby monitors are not in this allocation, so changing frequency "
                "does not keep them from overload. The control is to stop radiating toward shore."
            )
        else:
            reasons.append(
                f"Land is {conditions.land_nm:.1f} nmi away. Keep this navigation scanner available "
                f"on short range, and blank only the shore arc {bearing} when that blank does not "
                "hide traffic. Do not retune toward a broadcast or monitor band."
            )

    if action == "hold" and key != "2d" and conditions.emi == "none" and total <= TOLERANCE_DB:
        nearest = _nearest_c_band(radar, conditions)
        if nearest is not None:
            frequency = nearest
            action = "retune"
            reasons.append(_c_band_reason(radar, frequency))
        elif not radar.tunable:
            reasons.append(
                f"IEEE C-band is {C_BAND_LOW_GHZ:.0f}–{C_BAND_HIGH_GHZ:.0f} GHz. "
                f"This set publishes only {radar.frequency_ghz:.3f} GHz, "
                f"{distance_to_c_band(radar.frequency_ghz):.3f} GHz from that band."
            )

    if action == "hold" and frequency != radar.frequency_ghz:
        action = "change_band"
    radiating = SETS["2d"] if action == "change_band" else radar
    status, why = frequency_status(frequency, radiating, conditions)
    outside_window = not _in_window(radiating, frequency)
    if action in {"retune", "change_band"} and (status == "taboo" or outside_window):
        if status == "taboo":
            refusal = f"{frequency:.3f} GHz is taboo ({why}) and can never be switched to."
        else:
            refusal = (
                f"{frequency:.3f} GHz is outside {radiating.role}'s published window "
                "and is not a radar retune."
            )
        reasons.append(f"{refusal} The scanner stays on {radar.frequency_ghz:.3f} GHz.")
        frequency = radar.frequency_ghz
        action = "hold"
        status, why = frequency_status(frequency, radar, conditions)
    if status == "taboo":
        action = "inhibit"
        reasons.append(
            f"{frequency:.3f} GHz is taboo ({why}). Transmission on a taboo frequency is refused."
        )
    else:
        reasons.append(f"{frequency:.3f} GHz is {status}: {why}.")
    return Advice(
        role=radar.role,
        model=f"{radar.manufacturer} {radar.model}",
        action=action,
        frequency_ghz=frequency,
        loss_db=total,
        status=status,
        text=" ".join(reasons),
    )


def _loss_parts(fog_loss: float, rain_loss: float) -> str:
    return f"fog {fog_loss:.1f} dB plus rain {rain_loss:.1f} dB"


def _bearing_phrase(bearing: float | None) -> str:
    if bearing is None:
        return "on the bearings toward land"
    return f"on bearing {bearing:.0f} degrees"


def fog_threshold_line(conditions: Conditions) -> str:
    """State the fog coverage that crosses 3 dB, from the ITU density in use."""
    visibility = conditions.visibility_m
    if visibility is None or visibility >= FOG_VISIBILITY_M:
        visibility = THICK_VISIBILITY_M
        assumed = (
            " Thick fog, visibility of the order of 50 m, is the case used here"
            " because a shorter visibility was not entered."
        )
    else:
        assumed = ""
    x_cover = coverage_percent_for_tolerance(SETS["surface"].frequency_ghz, visibility, conditions.range_nm, conditions.temperature_c)
    s_cover = coverage_percent_for_tolerance(SETS["2d"].frequency_ghz, visibility, conditions.range_nm, conditions.temperature_c)
    _density, detail = fog_density(
        Conditions(
            fog_percent=100,
            visibility_m=visibility,
            temperature_c=conditions.temperature_c,
            range_nm=conditions.range_nm,
        )
    )
    if x_cover is None:
        x_text = (
            f"At {SETS['surface'].frequency_ghz:.3f} GHz and {conditions.range_nm:.0f} nmi, "
            f"100% of this fog stays inside {TOLERANCE_DB:.0f} dB, so coverage alone does not call for a band change."
        )
    else:
        x_text = (
            f"At {SETS['surface'].frequency_ghz:.3f} GHz and {conditions.range_nm:.0f} nmi, "
            f"this fog crosses {TOLERANCE_DB:.0f} dB two-way when it fills {x_cover:.0f}% of the path."
        )
    if s_cover is None:
        s_text = (
            f"At {SETS['2d'].frequency_ghz:.3f} GHz the same fog stays inside {TOLERANCE_DB:.0f} dB "
            "even when it fills the path."
        )
    else:
        s_text = (
            f"At {SETS['2d'].frequency_ghz:.3f} GHz the same fog crosses {TOLERANCE_DB:.0f} dB "
            f"at {s_cover:.0f}% path coverage."
        )
    return f"Fog water {detail}. {x_text} {s_text}{assumed} Entered fog coverage is {conditions.fog_percent:.0f}%."


def format_report(conditions: Conditions, rows: list[Advice] | None = None) -> str:
    advice = advise(conditions) if rows is None else rows
    blocks = [
        "Radar frequency advice",
        fog_threshold_line(conditions),
        (
            "Electromagnetic interference is handled inside the published window for that scanner. "
            "A move from X-band to S-band means selecting the S-band scanner."
        ),
        (
            "Approaching land: frequency is not the control for televisions and baby monitors. "
            f"Inside {COASTAL_NM:.0f} nmi, air defence, 3D search, and fire watch are inhibited "
            "toward shore. Navigation scanners stay on at short range."
        ),
        (
            "Friendly means a listed guard, common, or intra-squad carrier, or a frequency "
            "already assigned to a scanner on this ship. Taboo still wins when a carrier "
            "is also in a taboo span. "
            "Guarded means only that federal scanner may use its published span. "
            "Permitted means an IMO maritime radar channel inside the navigation window. "
            "Taboo means distress, safety, first-responder, or an operator ban. "
            "A taboo frequency can never be switched to."
        ),
        taboo_catalog(),
        friendly_catalog(),
        "",
    ]
    for row in advice:
        blocks.append(f"{row.role} ({row.model})")
        blocks.append(
            f"Action: {row.action.replace('_', ' ')} at {row.frequency_ghz:.3f} GHz, {row.status}."
        )
        blocks.append(row.text)
        blocks.append("")
    blocks.append("Sources")
    blocks.extend(f"- {source}" for source in SOURCES)
    return "\n".join(blocks)


def friendly_catalog() -> str:
    """List requested friendly carriers, leaving taboo overlaps on the taboo side."""
    friendly: list[str] = []
    stayed: list[str] = []
    for mhz, name in FRIENDLY_CHANNELS:
        banned = taboo_reason(mhz / 1000.0)
        label = _fmt_mhz(mhz)
        if banned is None:
            friendly.append(f"- {label}: {name}")
        else:
            stayed.append(f"- {label}: {name}. Remains taboo ({banned}).")
    lines = [
        "Friendly carriers:",
        *friendly,
        "Requested carriers that stay taboo because taboo takes priority:",
        *stayed,
    ]
    return "\n".join(lines)


def taboo_catalog() -> str:
    lines = ["Taboo spans, which can never be switched to:"]
    for low_mhz, high_mhz, name in TABOO_SPANS:
        lines.append(f"- {_fmt_mhz(low_mhz)} to {_fmt_mhz(high_mhz)}: {name}")
    return "\n".join(lines)


def _fmt_mhz(value: float) -> str:
    if value >= 1000:
        return f"{value / 1000:.3f} GHz"
    if value >= 1:
        return f"{value:.3f} MHz"
    return f"{value * 1000:.1f} kHz"


def _parse_ghz_list(value: str | None) -> tuple[float, ...]:
    if not value:
        return ()
    return tuple(float(part.strip()) for part in value.split(",") if part.strip())


def _parse_args(argv: list[str] | None) -> Conditions:
    parser = argparse.ArgumentParser(
        description="Advise ship radar bands from fog, rain, interference, and distance to land."
    )
    parser.add_argument("--fog-percent", type=float, default=0.0, help="Percent of the radar path in fog")
    parser.add_argument("--visibility-m", type=float, default=None, help="Meteorological visibility in meters")
    parser.add_argument("--temperature-c", type=float, default=0.0, help="Fog temperature, default 0 C from ITU-R P.840")
    parser.add_argument("--rain-mm-h", type=float, default=0.0)
    parser.add_argument("--range-nm", type=float, default=24.0)
    parser.add_argument("--emi", choices=("none", "in_band", "broadband"), default="none")
    parser.add_argument("--interferer-ghz", type=float, default=None)
    parser.add_argument("--land-nm", type=float, default=None, help="Distance to the nearest shore")
    parser.add_argument("--land-bearing", type=float, default=None, help="Bearing of the shore, degrees true")
    parser.add_argument(
        "--taboo",
        default=None,
        help="Extra taboo frequencies in GHz, comma-separated. These can never be switched to.",
    )
    args = parser.parse_args(argv)
    return Conditions(
        fog_percent=args.fog_percent,
        visibility_m=args.visibility_m,
        temperature_c=args.temperature_c,
        rain_mm_h=args.rain_mm_h,
        range_nm=args.range_nm,
        emi=args.emi,
        interferer_ghz=args.interferer_ghz,
        land_nm=args.land_nm,
        land_bearing_deg=args.land_bearing,
        extra_taboo_ghz=_parse_ghz_list(args.taboo),
    )


def main(argv: list[str] | None = None) -> int:
    try:
        conditions = _parse_args(argv)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(format_report(conditions))
    return 0


if __name__ == "__main__":
    sys.exit(main())
