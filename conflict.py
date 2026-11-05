"""Notional real-time spectrum deconfliction for a hackathon demo.

Frequencies, ship positions, and schedules come from the JSON files in
``data/``. They are fictional and are not a real frequency plan.

A request conflicts with another ship's assignment only when all three are true:

* the frequency ranges overlap after a 10 MHz guard (a gap of exactly 10 MHz
  is allowed; anything closer conflicts)
* the time windows overlap (a window that starts when the other ends does not)
* the ships are within 50 nautical miles

The requesting ship is not reported as a conflict with its own assignments.
Suggested alternatives stay inside that emitter's tunable range, and they
avoid the ship's own assignments plus every nearby ship. When the tunable
range has no clean band of the requested width, the result recommends
delaying until the blocking window ends.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
SHIPS_PATH = DATA_DIR / "ships.json"
PLAN_PATH = DATA_DIR / "spectrum_plan.json"

GUARD_MHZ = 10.0
CONFLICT_RANGE_NM = 50.0
# Mean Earth radius (km) converted to nautical miles (1 nm = 1.852 km).
EARTH_RADIUS_NM = 6371.0088 / 1.852

# Step used while scanning an emitter's tunable range for a free band.
SEARCH_STEP_MHZ = 10.0
MAX_ALTERNATIVES = 2

SEVERITY_RANK = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two WGS84 points, in nautical miles."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return EARTH_RADIUS_NM * c


def freq_ranges_conflict(
    a_min: float,
    a_max: float,
    b_min: float,
    b_max: float,
    guard_mhz: float = GUARD_MHZ,
) -> bool:
    """True when the ranges overlap, or the gap between them is under the guard."""
    return a_min < (b_max + guard_mhz) and b_min < (a_max + guard_mhz)


def times_overlap(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    """True when the half-open windows share any instant."""
    return start_a < end_b and start_b < end_a


def parse_time(value: str | datetime) -> datetime:
    """Parse an ISO-8601 timestamp. A trailing ``Z`` means UTC."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError(f"Timestamp must include a timezone: {value}")
        return value
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError(f"Timestamp must include a timezone: {value}")
    return parsed.astimezone(timezone.utc)


def severity_for_priority(priority: int) -> str:
    """Map emitter priority (1 = most important) to a conflict severity."""
    if priority <= 2:
        return "HIGH"
    if priority == 3:
        return "MEDIUM"
    return "LOW"


def _load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_ships(path: Path | None = None) -> dict[str, dict]:
    """Return notional ships keyed by id."""
    payload = _load_json(path or SHIPS_PATH)
    ships = {}
    for ship in payload["ships"]:
        ships[ship["id"]] = ship
    return ships


def load_plan(path: Path | None = None) -> list[dict]:
    """Return notional spectrum assignments."""
    payload = _load_json(path or PLAN_PATH)
    return list(payload["assignments"])


def _emitter(ships: dict[str, dict], ship_id: str, emitter_name: str) -> dict:
    ship = ships.get(ship_id)
    if ship is None:
        raise KeyError(f"Unknown ship: {ship_id}")
    for emitter in ship["emitters"]:
        if emitter["name"] == emitter_name:
            return emitter
    raise KeyError(f"Unknown emitter {emitter_name} on {ship_id}")


def _freq(value: float) -> int | float:
    number = float(value)
    if number.is_integer():
        return int(number)
    return number


def _assignment_blocks(
    freq_min: float,
    freq_max: float,
    start: datetime,
    end: datetime,
    ship_id: str,
    assignment: dict,
    ships: dict[str, dict],
    *,
    include_same_ship: bool,
) -> tuple[bool, float | None]:
    """Return whether ``assignment`` blocks the range, and the distance in nm.

    Distance is ``None`` when the assignment is on the requesting ship.
    """
    other_id = assignment["ship"]
    if other_id == ship_id and not include_same_ship:
        return False, None
    if not freq_ranges_conflict(
        freq_min,
        freq_max,
        assignment["freq_min_mhz"],
        assignment["freq_max_mhz"],
    ):
        return False, None
    if not times_overlap(
        start,
        end,
        parse_time(assignment["start"]),
        parse_time(assignment["end"]),
    ):
        return False, None
    if other_id == ship_id:
        return True, 0.0

    origin = ships[ship_id]
    other = ships[other_id]
    distance = haversine_nm(origin["lat"], origin["lon"], other["lat"], other["lon"])
    return distance <= CONFLICT_RANGE_NM, distance


def _zulu(moment: datetime) -> str:
    """Format a UTC time as ``HHMMZ``."""
    moment = moment.astimezone(timezone.utc)
    return moment.strftime("%H%M") + "Z"


def _time_shift(conflicts: list[dict]) -> str | None:
    """Recommend waiting out the assignments that block the requested band.

    The delay runs until the latest conflicting window ends. Every ship whose
    assignment ends at that instant is named.
    """
    if not conflicts:
        return None
    latest = max(parse_time(conflict["end"]) for conflict in conflicts)
    ships: list[str] = []
    for conflict in conflicts:
        if parse_time(conflict["end"]) == latest and conflict["ship"] not in ships:
            ships.append(conflict["ship"])
    if len(ships) == 1:
        whose = f"{ships[0]}'s window ends"
    else:
        named = " and ".join(f"{ship}'s" for ship in ships)
        whose = f"{named} windows end"
    return f"delay until {_zulu(latest)} when {whose}"


def _find_alternatives(
    request: dict,
    start: datetime,
    end: datetime,
    ships: dict[str, dict],
    assignments: list[dict],
    emitter: dict,
) -> list[dict]:
    """Up to two free ranges of the same width, inside the emitter's tune limits."""
    freq_min = float(request["freq_min_mhz"])
    freq_max = float(request["freq_max_mhz"])
    width = freq_max - freq_min
    tune_min = float(emitter["tunable_min_mhz"])
    tune_max = float(emitter["tunable_max_mhz"])
    if width <= 0 or tune_min + width > tune_max:
        return []

    ship_id = request["ship"]
    found: list[dict] = []
    cursor = tune_min
    while cursor + width <= tune_max and len(found) < MAX_ALTERNATIVES:
        candidate_max = cursor + width
        same_as_request = math.isclose(cursor, freq_min) and math.isclose(candidate_max, freq_max)
        blocked = False
        if not same_as_request:
            for assignment in assignments:
                blocks, _distance = _assignment_blocks(
                    cursor,
                    candidate_max,
                    start,
                    end,
                    ship_id,
                    assignment,
                    ships,
                    include_same_ship=True,
                )
                if blocks:
                    blocked = True
                    break
        if not same_as_request and not blocked:
            found.append(
                {
                    "freq_min_mhz": _freq(cursor),
                    "freq_max_mhz": _freq(candidate_max),
                }
            )
            cursor = candidate_max + GUARD_MHZ
        else:
            cursor += SEARCH_STEP_MHZ
    return found


def check_request(
    request: dict,
    ships: dict[str, dict] | None = None,
    assignments: list[dict] | None = None,
) -> dict:
    """Check one spectrum request against the notional plan.

    ``request`` keys: ``ship``, ``emitter_name``, ``freq_min_mhz``,
    ``freq_max_mhz``, ``start``, ``end``.

    ``ships`` and ``assignments`` default to ``data/ships.json`` and
    ``data/spectrum_plan.json``. Tests pass their own ships when they need a
    geometry that is outside the demo formation.

    Returns conflicts, alternatives, and ``time_shift``. Conflicts are sorted
    most severe first. Each conflict names the other ship and emitter, that
    emitter's priority, and a severity of HIGH, MEDIUM, or LOW. Alternatives
    are ranges of the same width inside the requesting emitter's tunable
    limits that do not conflict. ``time_shift`` is a delay recommendation when
    conflicts exist and no alternative fits; otherwise it is ``None``.
    """
    if float(request["freq_max_mhz"]) <= float(request["freq_min_mhz"]):
        raise ValueError("freq_max_mhz must be greater than freq_min_mhz")

    ships = ships if ships is not None else load_ships()
    assignments = assignments if assignments is not None else load_plan()

    ship_id = request["ship"]
    emitter = _emitter(ships, ship_id, request["emitter_name"])
    start = parse_time(request["start"])
    end = parse_time(request["end"])
    if start >= end:
        raise ValueError("start must be earlier than end")

    conflicts = []
    for assignment in assignments:
        blocks, distance = _assignment_blocks(
            float(request["freq_min_mhz"]),
            float(request["freq_max_mhz"]),
            start,
            end,
            ship_id,
            assignment,
            ships,
            include_same_ship=False,
        )
        if not blocks:
            continue
        affected = _emitter(ships, assignment["ship"], assignment["emitter_name"])
        priority = int(affected["priority"])
        conflicts.append(
            {
                "ship": assignment["ship"],
                "emitter_name": assignment["emitter_name"],
                "freq_min_mhz": assignment["freq_min_mhz"],
                "freq_max_mhz": assignment["freq_max_mhz"],
                "start": assignment["start"],
                "end": assignment["end"],
                "priority": priority,
                "severity": severity_for_priority(priority),
                "distance_nm": round(distance, 2),
            }
        )

    conflicts.sort(
        key=lambda item: (
            SEVERITY_RANK[item["severity"]],
            item["priority"],
            item["ship"],
            item["emitter_name"],
        )
    )
    alternatives = _find_alternatives(request, start, end, ships, assignments, emitter)
    return {
        "conflicts": conflicts,
        "alternatives": alternatives,
        "time_shift": _time_shift(conflicts) if conflicts and not alternatives else None,
    }
