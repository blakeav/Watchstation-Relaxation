/** Weather scope. Forecast samples come from the radar weather check. Local clutter draws immediately. */

const RINGS = [35, 70];

export class WeatherDisplay {
  constructor(root) {
    this.root = root;
    this.live = null;
    this.forecast = null;
    this.root.innerHTML = `
      <div class="ppi-head">
        <h2>Weather</h2>
        <button type="button" id="weather-load">Read forecast</button>
      </div>
      <div class="weather-fields">
        <label>Lat <input id="weather-lat" type="number" step="0.01" min="-90" max="90" value="36.85"></label>
        <label>Lon <input id="weather-lon" type="number" step="0.01" min="-180" max="180" value="-76.29"></label>
        <label>Range <input id="weather-range" type="number" step="1" min="1" max="70" value="24"> nmi</label>
      </div>
      <p id="weather-status" class="weather-status">Local picture. Read the forecast for the government sample.</p>
      <p id="weather-headline" class="weather-headline"></p>
      <div class="weather-body">
        <div class="canvas-wrap weather-map-wrap">
          <canvas aria-label="Weather map"></canvas>
        </div>
        <dl class="weather-readout" id="weather-readout"></dl>
      </div>
    `;
    this.canvas = root.querySelector("canvas");
    this.ctx = this.canvas.getContext("2d");
    this.status = root.querySelector("#weather-status");
    this.headline = root.querySelector("#weather-headline");
    this.readout = root.querySelector("#weather-readout");
    root.querySelector("#weather-load").addEventListener("click", () => this.loadForecast());
    this.observer = new ResizeObserver(() => this._size());
    this.observer.observe(root.querySelector(".weather-map-wrap"));
    this._size();
    this._paintLive();
  }

  setLive(radar) {
    this.live = radar;
    if (!this.forecast) this._paintLive();
    this._draw();
  }

  async loadForecast() {
    const lat = Number(this.root.querySelector("#weather-lat").value);
    const lon = Number(this.root.querySelector("#weather-lon").value);
    const rangeNm = Number(this.root.querySelector("#weather-range").value);
    this.status.textContent = "Reading government forecasts within 70 nautical miles…";
    try {
      const response = await fetch("/api/weather", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lat, lon, rangeNm }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || payload.error) {
        this.status.textContent = payload.error || "The weather check did not complete.";
        return;
      }
      this.forecast = payload;
      this.status.textContent = "Forecast loaded. Local clutter still overlays the map.";
      this.headline.textContent = payload.headline || "";
      this.headline.className = `weather-headline ${payload.weatherFactor ? "factor" : "clear"}`;
      this._fillForecast(payload);
      this._draw();
    } catch {
      this.status.textContent = "Forecast server is not reachable. The local clutter picture is still shown.";
    }
  }

  _paintLive() {
    const clutter = this.live?.clutter;
    const raining = clutter && clutter.rateMmH > 0;
    this.headline.textContent = raining ? "Local squall is a radar factor." : "No local weather clutter.";
    this.headline.className = `weather-headline ${raining ? "factor" : "clear"}`;
    const rows = raining
      ? [
          ["Phenomenon", clutter.phenomenon],
          ["Rain rate", `${clutter.rateMmH.toFixed(1)} mm/h`],
          ["Bearing", `${clutter.bearing.toFixed(0)}°`],
          ["Two-way loss", `${clutter.lossDb.toFixed(1)} dB`],
          ["Radar factor", "Yes"],
        ]
      : [
          ["Phenomenon", "clear"],
          ["Rain rate", "0.0 mm/h"],
          ["Bearing", "—"],
          ["Two-way loss", "0.0 dB"],
          ["Radar factor", "No"],
        ];
    this._rows(rows);
  }

  _fillForecast(payload) {
    this._rows([
      ["Position", `${Number(payload.lat).toFixed(2)}, ${Number(payload.lon).toFixed(2)}`],
      ["Phenomenon", payload.phenomenon],
      ["Rain rate", `${Number(payload.rateMmH).toFixed(1)} mm/h`],
      ["Cloud cover", `${Math.round(payload.cloudPercent)}%`],
      ["Two-way loss", `${Number(payload.lossDb).toFixed(1)} dB over ${Number(payload.rangeNm).toFixed(0)} nmi`],
      ["Tolerance", `${Number(payload.toleranceDb).toFixed(1)} dB`],
      ["Radar factor", payload.weatherFactor ? "Yes" : "No"],
    ]);
  }

  _rows(rows) {
    this.readout.replaceChildren();
    for (const [name, value] of rows) {
      const term = document.createElement("dt");
      term.textContent = name;
      const detail = document.createElement("dd");
      detail.textContent = value;
      this.readout.append(term, detail);
    }
  }

  _size() {
    const wrap = this.canvas.parentElement;
    const width = Math.max(280, wrap.clientWidth);
    const height = Math.max(280, wrap.clientHeight);
    const ratio = Math.min(2, window.devicePixelRatio || 1);
    this.canvas.width = Math.floor(width * ratio);
    this.canvas.height = Math.floor(height * ratio);
    this.canvas.style.width = `${width}px`;
    this.canvas.style.height = `${height}px`;
    this.ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    this.width = width;
    this.height = height;
    this._draw();
  }

  _draw() {
    const ctx = this.ctx;
    if (!ctx || !this.width) return;
    const width = this.width;
    const height = this.height;
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = "#071018";
    ctx.fillRect(0, 0, width, height);
    const cx = width / 2;
    const cy = height / 2;
    const reach = 70;
    const radius = Math.max(40, Math.min(width, height) / 2 - 28);
    const scale = radius / reach;

    ctx.strokeStyle = "#3d5c73";
    ctx.fillStyle = "#9ec9e0";
    ctx.lineWidth = 1;
    ctx.font = "12px Consolas, monospace";
    for (const ring of RINGS) {
      ctx.beginPath();
      ctx.arc(cx, cy, ring * scale, 0, Math.PI * 2);
      ctx.stroke();
      ctx.fillText(`${ring} nmi`, cx + 6, cy - ring * scale + 14);
    }
    for (let bearing = 0; bearing < 360; bearing += 45) {
      const tip = polar(cx, cy, bearing, radius);
      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.lineTo(tip.x, tip.y);
      ctx.stroke();
    }
    ctx.fillText("N", cx - 4, cy - radius - 6);
    ctx.fillText("E", cx + radius + 6, cy + 4);
    ctx.fillText("S", cx - 4, cy + radius + 14);
    ctx.fillText("W", cx - radius - 14, cy + 4);

    const clutter = this.live?.clutter;
    if (clutter && clutter.half) {
      const start = ((clutter.bearing - clutter.half) * Math.PI) / 180 - Math.PI / 2;
      const end = ((clutter.bearing + clutter.half) * Math.PI) / 180 - Math.PI / 2;
      ctx.fillStyle = "rgba(76, 120, 168, 0.45)";
      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.arc(cx, cy, radius, start, end);
      ctx.closePath();
      ctx.fill();
    }

    for (const sample of this.forecast?.samples || []) {
      if (sample.bearingDeg == null) continue;
      const at = polar(cx, cy, sample.bearingDeg, sample.rangeNm * scale);
      ctx.beginPath();
      ctx.arc(at.x, at.y, sample.affectsRadar ? 8 : 6, 0, Math.PI * 2);
      ctx.fillStyle = sample.affectsRadar ? "#ff5a4a" : colorFor(sample.phenomenon);
      ctx.fill();
      ctx.strokeStyle = "#071018";
      ctx.stroke();
    }

    const factor = this.forecast
      ? Boolean(this.forecast.weatherFactor)
      : Boolean(clutter && clutter.rateMmH >= 8);
    ctx.beginPath();
    ctx.arc(cx, cy, 7, 0, Math.PI * 2);
    ctx.fillStyle = factor ? "#ff5a4a" : "#7ec8a3";
    ctx.fill();
    ctx.fillStyle = "#d7eef8";
    ctx.fillText("POS", cx + 10, cy - 8);
  }
}

function polar(originX, originY, bearing, distance) {
  const radians = (Number(bearing) * Math.PI) / 180;
  return {
    x: originX + Math.sin(radians) * distance,
    y: originY - Math.cos(radians) * distance,
  };
}

function colorFor(phenomenon) {
  const name = String(phenomenon || "").toLowerCase();
  if (name.includes("thunder")) return "#b794f6";
  if (name.includes("snow")) return "#d6e8f5";
  if (name.includes("rain") || name.includes("squall")) return "#7eb0e0";
  if (name.includes("fog")) return "#a8b3bd";
  if (name.includes("cloud")) return "#8aa0b2";
  return "#7ec8a3";
}
