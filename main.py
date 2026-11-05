"""FastAPI demo for notional spectrum deconfliction alerts.

Frequencies and ship positions are fictional. They are not a real frequency plan.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, ValidationError

from conflict import SHIPS_PATH, PLAN_PATH, _emitter, check_request, load_ships
from explain import generate_explanations, template_explanations
from llm import AIOffline
from parse_text import parse_free_text

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(
    title="RTSO Engine",
    summary="Notional real-time spectrum deconfliction between ships.",
)


class TransmitRequest(BaseModel):
    ship: str
    emitter_name: str
    freq_min_mhz: float
    freq_max_mhz: float
    start: str
    end: str


class FreeTextRequest(BaseModel):
    text: str


class AlertHub:
    """Tracks browsers and other clients listening on /alerts."""

    def __init__(self) -> None:
        self._sockets: list[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._sockets.append(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self._sockets:
            self._sockets.remove(websocket)

    async def broadcast(self, message: dict) -> None:
        stale: list[WebSocket] = []
        for websocket in list(self._sockets):
            try:
                await websocket.send_json(message)
            except Exception:
                stale.append(websocket)
        for websocket in stale:
            self.disconnect(websocket)


alerts = AlertHub()


def _edge(value: float) -> int | float:
    number = float(value)
    if number.is_integer():
        return int(number)
    return number


def build_alert(request: dict, result: dict) -> dict:
    """Shape a check_request result into the broadcast payload."""
    conflicts = result["conflicts"]
    alternatives = result["alternatives"]
    if conflicts:
        affected: list[str] = []
        for conflict in conflicts:
            if conflict["ship"] not in affected:
                affected.append(conflict["ship"])
        if alternatives:
            options = " or ".join(
                f"{alternative['freq_min_mhz']}–{alternative['freq_max_mhz']} MHz"
                for alternative in alternatives
            )
            recommendation = f"Shift {request['emitter_name']} to {options}."
        elif result.get("time_shift"):
            recommendation = result["time_shift"]
        else:
            recommendation = (
                "No open band of this width was found. Shorten the request or move the time window."
            )
        alert_type = "SPECTRUM_CONFLICT"
        severity = conflicts[0]["severity"]
    else:
        affected = []
        recommendation = "No conflict. Transmit as requested."
        alert_type = "SPECTRUM_CLEAR"
        severity = None

    return {
        "type": alert_type,
        "from_ship": request["ship"],
        "affects": affected,
        "band_mhz": [_edge(request["freq_min_mhz"]), _edge(request["freq_max_mhz"])],
        "window": [request["start"], request["end"]],
        "severity": severity,
        "recommendation": recommendation,
        "conflicts": conflicts,
    }


def _read_json(path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


@app.get("/")
def index() -> FileResponse:
    """Ship consoles. Each panel listens on /alerts."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/ships")
def get_ships() -> dict:
    """Return the notional ship and emitter file."""
    return _read_json(SHIPS_PATH)


@app.get("/plan")
def get_plan() -> dict:
    """Return the notional spectrum assignments."""
    return _read_json(PLAN_PATH)


@app.post("/request")
async def post_request(body: TransmitRequest) -> dict:
    """Check a transmit request and broadcast the alert to /alerts."""
    request = body.model_dump()
    try:
        result = check_request(request)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    alert = build_alert(request, result)
    try:
        explained = generate_explanations(request, result)
    except Exception:
        explained = template_explanations(request, result)
        explained["mode"] = "OFFLINE"
    alert["explanations"] = {
        "apprentice": explained["apprentice"],
        "journeyman": explained["journeyman"],
        "master": explained["master"],
    }
    alert["explanation_mode"] = explained["mode"]
    await alerts.broadcast(alert)
    return alert


@app.post("/parse")
def post_parse(body: FreeTextRequest) -> TransmitRequest:
    """Turn plain language into a transmit request. Offline means use the form."""
    if not body.text.strip():
        raise HTTPException(
            status_code=422,
            detail="Enter a request, or use the manual form.",
        )
    try:
        parsed = parse_free_text(body.text)
        request = TransmitRequest.model_validate(parsed)
        _emitter(load_ships(), request.ship, request.emitter_name)
    except AIOffline:
        raise HTTPException(
            status_code=503,
            detail="The text parser is offline. Use the manual form.",
        ) from None
    except (ValidationError, KeyError, ValueError):
        raise HTTPException(
            status_code=422,
            detail="Could not match that text to a ship and emitter. Use the manual form.",
        ) from None
    return request


@app.websocket("/alerts")
async def alerts_socket(websocket: WebSocket) -> None:
    """Push each SPECTRUM_CONFLICT or SPECTRUM_CLEAR alert as it is decided."""
    await alerts.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        alerts.disconnect(websocket)
