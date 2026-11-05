const AFFILIATION = ["unknown", "friend", "hostile", "neutral"];
const DOMAIN = ["surface", "air", "missile"];

export class RadarView {
  constructor(root) {
    this.root = root;
    this.scene = null;
    this.rangeNm = 50;
    this.fps = 0;
    this._frames = 0;
    this._fpsStamp = performance.now();
    this._sweep = 0;
    root.innerHTML = `
      <div class="ppi-head">
        <h2>Plan position indicator</h2>
        <div class="scales" role="group" aria-label="Radar range scale">
          ${[10, 25, 50, 100].map((nm) => `<button type="button" data-range="${nm}" class="${nm === 50 ? "is-on" : ""}">${nm} NM</button>`).join("")}
        </div>
      </div>
      <div class="canvas-wrap">
        <canvas></canvas>
        <p class="ppi-readout" id="ppi-readout"></p>
      </div>
      <p class="sr-only" id="ppi-summary" aria-live="polite"></p>
    `;
    this.canvas = root.querySelector("canvas");
    this.ctx = this.canvas.getContext("2d", { alpha: false });
    this.readout = root.querySelector("#ppi-readout");
    this.summary = root.querySelector("#ppi-summary");
    this.scales = root.querySelector(".scales");
    this.scales.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-range]");
      if (!button) return;
      this.onRange?.(Number(button.dataset.range));
    });
    this.observer = new ResizeObserver(() => this._size());
    this.observer.observe(root.querySelector(".canvas-wrap"));
    this._size();
    this._loop = this._loop.bind(this);
    requestAnimationFrame(this._loop);
  }

  setRange(nm) {
    this.rangeNm = nm;
    this.scales.querySelectorAll("button").forEach((button) => {
      button.classList.toggle("is-on", Number(button.dataset.range) === nm);
    });
  }

  setScene(scene) {
    this.scene = scene;
    if (scene?.rangeNm) this.setRange(scene.rangeNm);
    if (this.ctx && this.width) {
      try {
        this._draw();
      } catch (error) {
        if (this.readout) this.readout.textContent = `PPI ${error.message}`;
      }
    }
  }

  _size() {
    const wrap = this.canvas.parentElement;
    const width = Math.max(320, wrap.clientWidth);
    const height = Math.max(320, wrap.clientHeight);
    const ratio = Math.min(2, window.devicePixelRatio || 1);
    this.canvas.width = Math.floor(width * ratio);
    this.canvas.height = Math.floor(height * ratio);
    this.canvas.style.width = `${width}px`;
    this.canvas.style.height = `${height}px`;
    this.ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    this.width = width;
    this.height = height;
  }

  _loop(now) {
    this._frames += 1;
    if (now - this._fpsStamp > 500) {
      this.fps = (this._frames * 1000) / (now - this._fpsStamp);
      this._frames = 0;
      this._fpsStamp = now;
    }
    this._sweep = (now / 4000) % 1;
    try {
      this._draw();
    } catch (error) {
      if (this.readout) this.readout.textContent = `PPI ${error.message}`;
    }
    requestAnimationFrame(this._loop);
  }

  _draw() {
    const ctx = this.ctx;
    const width = this.width || 600;
    const height = this.height || 600;
    ctx.fillStyle = "#020403";
    ctx.fillRect(0, 0, width, height);
    const cx = width / 2;
    const cy = height / 2 + 8;
    const radius = Math.max(40, Math.min(width, height) * 0.42);
    const range = this.rangeNm || 50;
    this._grid(ctx, cx, cy, radius, range);
    const scene = this.scene;
    if (!scene) return;
    this._wedge(ctx, cx, cy, radius, range, scene.radar?.clutter, "rgba(70, 110, 80, 0.28)");
    for (const strobe of scene.radar?.strobes || []) {
      const color = strobe.kind === "deception" ? "rgba(255, 176, 0, 0.28)" : "rgba(255, 50, 40, 0.30)";
      this._wedge(ctx, cx, cy, radius, range, strobe, color);
    }
    if (scene.radar?.degradation?.blind) {
      this._wedge(ctx, cx, cy, radius, range, {
        bearing: scene.radar.degradation.blindBearing,
        half: scene.radar.degradation.blindHalf,
      }, "rgba(120, 0, 0, 0.45)");
    }
    if (scene.coa?.burnThroughNm) this._ring(ctx, cx, cy, radius, range, scene.coa.burnThroughNm, "#ffb000");
    for (const track of scene.tracks || []) this._track(ctx, cx, cy, radius, range, track);
    this._ownship(ctx, cx, cy, radius, range, scene.ownship);
    this._drawSweep(ctx, cx, cy, radius);
    const ship = scene.ownship;
    const hostiles = (scene.tracks || []).filter((track) => track.affiliation === 2).length;
    this.readout.textContent = ship
      ? `HDG ${ship.heading.toFixed(0).padStart(3, "0")}   SPD ${ship.speed.toFixed(0)} KT   SCALE ${range} NM   ${scene.radar?.status || ""}   ${this.fps.toFixed(0)} FPS`
      : "";
    if (this.summary) {
      this.summary.textContent = `Scale ${range} nautical miles. ${scene.tracks?.length || 0} tracks. ${hostiles} hostile. Radar ${scene.radar?.status || "unknown"}.`;
    }
  }

  _grid(ctx, cx, cy, radius, range) {
    ctx.save();
    ctx.strokeStyle = "#1f6b3a";
    ctx.fillStyle = "#8fdf7a";
    ctx.font = "12px Consolas, monospace";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.arc(cx, cy, radius, 0, Math.PI * 2);
    ctx.stroke();
    for (const ring of [10, 25, 50, 100]) {
      if (ring >= range) continue;
      ctx.beginPath();
      ctx.arc(cx, cy, (ring / range) * radius, 0, Math.PI * 2);
      ctx.stroke();
      ctx.fillText(`${ring}`, cx + 6, cy - (ring / range) * radius + 12);
    }
    ctx.fillText(`${range}`, cx + 6, cy - radius + 14);
    ctx.beginPath();
    ctx.moveTo(cx, cy - radius);
    ctx.lineTo(cx, cy + radius);
    ctx.moveTo(cx - radius, cy);
    ctx.lineTo(cx + radius, cy);
    ctx.stroke();
    ctx.font = "12px Consolas, monospace";
    ctx.fillText("N", cx - 4, cy - radius - 6);
    ctx.fillText("E", cx + radius + 6, cy + 4);
    ctx.fillText("S", cx - 4, cy + radius + 14);
    ctx.fillText("W", cx - radius - 16, cy + 4);
    ctx.restore();
  }

  _ring(ctx, cx, cy, radius, range, distance, color) {
    const px = Math.min(radius, (distance / range) * radius);
    ctx.save();
    ctx.strokeStyle = color;
    ctx.setLineDash([5, 5]);
    ctx.beginPath();
    ctx.arc(cx, cy, px, 0, Math.PI * 2);
    ctx.stroke();
    ctx.fillStyle = color;
    ctx.font = "11px Consolas, monospace";
    ctx.fillText(`BT ${distance.toFixed(0)}`, cx + 8, cy - px + 12);
    ctx.restore();
  }

  _wedge(ctx, cx, cy, radius, range, sector, color) {
    if (!sector || !sector.half) return;
    const start = ((sector.bearing - sector.half) * Math.PI) / 180 - Math.PI / 2;
    const end = ((sector.bearing + sector.half) * Math.PI) / 180 - Math.PI / 2;
    ctx.save();
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.arc(cx, cy, radius, start, end);
    ctx.closePath();
    ctx.fill();
    ctx.restore();
  }

  _project(cx, cy, radius, range, east, north) {
    return [cx + (east / range) * radius, cy - (north / range) * radius];
  }

  _ownship(ctx, cx, cy, radius, range, ship) {
    if (!ship) return;
    const rad = (ship.heading * Math.PI) / 180;
    const length = Math.min(radius * 0.35, ((ship.speed / 60) * 12 / range) * radius + 28);
    ctx.save();
    ctx.strokeStyle = "#d7f5cf";
    ctx.fillStyle = "#d7f5cf";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(cx, cy, 7, 0, Math.PI * 2);
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(cx + Math.sin(rad) * length, cy - Math.cos(rad) * length);
    ctx.stroke();
    ctx.fillText("OS", cx + 10, cy - 10);
    ctx.restore();
  }

  _track(ctx, cx, cy, radius, range, track) {
    const [x, y] = this._project(cx, cy, radius, range, track.east, track.north);
    if (Math.hypot(x - cx, y - cy) > radius + 8) return;
    const kind = AFFILIATION[track.affiliation] || "unknown";
    const color = kind === "hostile" ? "#ff4b3e" : kind === "friend" ? "#8fdf7a" : kind === "neutral" ? "#7ee0d6" : "#ffb000";
    ctx.save();
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.globalAlpha = 0.45 + 0.55 * (track.quality || 1);
    ctx.lineWidth = track.domain === 2 ? 2.5 : 1.5;
    ctx.beginPath();
    if (kind === "hostile") {
      ctx.moveTo(x, y - 8);
      ctx.lineTo(x + 8, y);
      ctx.lineTo(x, y + 8);
      ctx.lineTo(x - 8, y);
      ctx.closePath();
    } else if (kind === "friend") {
      ctx.rect(x - 7, y - 5, 14, 10);
    } else if (kind === "neutral") {
      ctx.rect(x - 6, y - 6, 12, 12);
    } else {
      ctx.arc(x, y, 4, 0, Math.PI * 2);
      ctx.moveTo(x - 8, y);
      ctx.lineTo(x + 8, y);
      ctx.moveTo(x, y - 8);
      ctx.lineTo(x, y + 8);
    }
    ctx.stroke();
    if (track.domain === 1) {
      ctx.beginPath();
      ctx.arc(x, y - 10, 3, Math.PI, 0);
      ctx.stroke();
    }
    ctx.font = "11px Consolas, monospace";
    const tag = track.domain === 2 ? "MSL" : `${DOMAIN[track.domain] === "air" ? "A" : "S"}${String(track.id).padStart(3, "0")}`;
    ctx.fillText(tag, x + 10, y - 6);
    ctx.restore();
  }

  _drawSweep(ctx, cx, cy, radius) {
    const angle = this._sweep * Math.PI * 2 - Math.PI / 2;
    ctx.save();
    ctx.strokeStyle = "rgba(143, 223, 122, 0.35)";
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(cx + Math.cos(angle) * radius, cy + Math.sin(angle) * radius);
    ctx.stroke();
    ctx.restore();
  }
}
