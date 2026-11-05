import { Affiliation, Domain } from "../models.js";

const MODES = ["link16", "link22", "fallback"];
const MODE_LABEL = {
  link16: "Link 16",
  link22: "Link 22",
  fallback: "Fallback datalink",
};

function baseTracks() {
  const rows = [
    [201, 35, 16, 40, 14, Affiliation.FRIEND, Domain.SURFACE, 1],
    [202, 125, 20, 190, 12, Affiliation.FRIEND, Domain.SURFACE, 1],
    [204, 250, 12, 70, 18, Affiliation.FRIEND, Domain.SURFACE, 1],
    [310, 58, 34, 235, 16, Affiliation.HOSTILE, Domain.SURFACE, 0.9],
    [318, 300, 42, 15, 260, Affiliation.HOSTILE, Domain.AIR, 0.85],
    [118, 168, 24, 100, 8, Affiliation.UNKNOWN, Domain.SURFACE, 0.7],
    [119, 220, 38, 310, 210, Affiliation.UNKNOWN, Domain.AIR, 0.75],
    [401, 95, 46, 20, 10, Affiliation.NEUTRAL, Domain.SURFACE, 1],
  ];
  return rows.map(([id, bearing, rangeNm, course, speed, affiliation, domain, quality]) => ({
    id,
    bearing,
    rangeNm,
    course,
    speed,
    affiliation,
    domain,
    quality,
    drift: ((id % 7) - 3) * 0.15,
    rangeRate: domain === Domain.AIR ? -0.15 : 0.04,
  }));
}

/**
 * DDIL link stream. Emits binary track payloads and link metrics.
 * Orders queue locally and merge with a vector clock when the link returns.
 */
export class DdilDataLinkService extends EventTarget {
  constructor() {
    super();
    this.node = "CIC-LOCAL";
    this.clock = { "CIC-LOCAL": 0, LINK: 0 };
    this.mode = "link16";
    this.disconnected = false;
    this.latencySpikeUntil = 0;
    this.lossSpikeUntil = 0;
    this.oooUntil = 0;
    this.density = 36;
    this.missile = false;
    this.ownship = {
      heading: 22,
      speed: 16,
      radarRangeNm: 80,
      orderedHeading: null,
      orderedSpeed: null,
    };
    this.tracks = baseTracks();
    this._events = [
      {
        code: "HANDSHAKE",
        tier: "INFORMATIONAL",
        title: "Link handshake",
        detail: "Link 16 handshake complete. Local picture is live.",
      },
    ];
    this._extra = 0;
  }

  modeLabel() {
    return MODE_LABEL[this.mode];
  }

  takeEvents() {
    const events = this._events;
    this._events = [];
    return events;
  }

  bump() {
    this.clock[this.node] = (this.clock[this.node] || 0) + 1;
  }

  order(heading, speed, text) {
    this.bump();
    this.ownship.orderedHeading = heading;
    this.ownship.orderedSpeed = speed;
    const command = {
      id: crypto.randomUUID(),
      time: Date.now(),
      heading,
      speed,
      text,
      clock: { ...this.clock },
      synced: !this.disconnected,
    };
    if (!this.disconnected) this.clock.LINK += 1;
    return command;
  }

  mergeLink() {
    this.clock.LINK = Math.max(this.clock.LINK, this.clock[this.node]);
    this.clock[this.node] += 1;
  }

  setDisconnected(on) {
    const was = this.disconnected;
    this.disconnected = on;
    if (was && !on) {
      this._events.push({
        code: "LINK-BACK",
        tier: "INFORMATIONAL",
        title: "Link restored",
        detail: "Buffered orders merge on the vector clock.",
      });
    }
    if (!was && on) {
      this._events.push({
        code: "LINK-LOSS",
        tier: "INFORMATIONAL",
        title: "Link lost",
        detail: "Local picture is buffering. Orders stay on this console.",
      });
    }
    this.dispatchEvent(new Event("change"));
  }

  spikeLatency() {
    this.latencySpikeUntil = performance.now() + 5000;
    this.bump();
  }

  spikeLoss() {
    this.lossSpikeUntil = performance.now() + 5000;
    this.bump();
  }

  injectOoo() {
    this.oooUntil = performance.now() + 4000;
    this.clock.LINK += 1;
    this.bump();
    this._events.push({
      code: "OOO",
      tier: "CAUTION",
      title: "Out-of-order reconciliation",
      detail: "A late packet arrived. The vector clock advanced and the picture was reconciled.",
    });
  }

  cycleMode() {
    const index = MODES.indexOf(this.mode);
    this.mode = MODES[(index + 1) % MODES.length];
    this.bump();
    this._events.push({
      code: `MODE-${this.mode}`,
      tier: "INFORMATIONAL",
      title: "Link mode change",
      detail: `Active mode is now ${this.modeLabel()}.`,
    });
    this.dispatchEvent(new Event("change"));
  }

  setDensity(count) {
    this.density = count;
  }

  setMissile(on) {
    this.missile = on;
    this._events.push({
      code: on ? "MSL-INJECT" : "MSL-CLEAR",
      tier: "INFORMATIONAL",
      title: on ? "Missile track injected" : "Missile track removed",
      detail: "Training control.",
    });
  }

  sample(now) {
    this._stepOwnship();
    this._stepTracks();
    const latencySpike = now < this.latencySpikeUntil;
    const lossSpike = now < this.lossSpikeUntil;
    const ooo = now < this.oooUntil;
    let latencyMs = 42 + (Math.sin(now / 700) + 1) * 18;
    let bandwidthKbps = 260;
    let dropRate = 0.004;
    let peers = 6;
    if (latencySpike) latencyMs = 860;
    if (lossSpike) {
      bandwidthKbps = 36;
      dropRate = 0.22;
      peers = 2;
    }
    let condition = "CONNECTED";
    if (this.disconnected) {
      condition = "DISCONNECTED / LOCAL BUFFERING";
      latencyMs = 0;
      bandwidthKbps = 0;
      dropRate = 1;
      peers = 0;
    } else if (latencyMs > 250 || dropRate > 0.05 || bandwidthKbps < 80) {
      condition = "DEGRADED";
    }
    const tracks = this._visibleTracks(lossSpike);
    return {
      mode: this.mode,
      modeLabel: this.modeLabel(),
      condition,
      latencyMs: this.disconnected ? null : Math.round(latencyMs),
      bandwidthKbps: Math.round(bandwidthKbps),
      dropRate,
      peers,
      ooo,
      clock: { ...this.clock },
      ownship: { ...this.ownship },
      buffer: packTracks(tracks),
      trackCount: tracks.length,
    };
  }

  _stepOwnship() {
    const ship = this.ownship;
    if (ship.orderedHeading != null) {
      let delta = ((ship.orderedHeading - ship.heading + 540) % 360) - 180;
      const step = Math.max(-4, Math.min(4, delta));
      ship.heading = (ship.heading + step + 360) % 360;
      if (Math.abs(delta) < 0.8) ship.heading = ship.orderedHeading;
    }
    if (ship.orderedSpeed != null) {
      const delta = ship.orderedSpeed - ship.speed;
      ship.speed += Math.max(-1, Math.min(1, delta));
      if (Math.abs(delta) < 0.4) ship.speed = ship.orderedSpeed;
    }
  }

  _stepTracks() {
    for (const track of this.tracks) {
      track.bearing = (track.bearing + track.drift + 360) % 360;
      track.rangeNm = Math.min(96, Math.max(4, track.rangeNm + track.rangeRate));
      if (track.rangeNm > 90) track.rangeRate = -Math.abs(track.rangeRate);
      if (track.rangeNm < 6) track.rangeRate = Math.abs(track.rangeRate);
    }
    const missile = this.tracks.find((track) => track.id === 900);
    if (this.missile && !missile) {
      this.tracks.push({
        id: 900,
        bearing: 18,
        rangeNm: 32,
        course: 200,
        speed: 640,
        affiliation: Affiliation.HOSTILE,
        domain: Domain.MISSILE,
        quality: 1,
        drift: -0.4,
        rangeRate: -1.1,
      });
    }
    if (!this.missile && missile) {
      this.tracks = this.tracks.filter((track) => track.id !== 900);
    }
    if (this.missile && missile) missile.rangeNm = Math.max(3, missile.rangeNm - 1.1);
  }

  _visibleTracks(lossSpike) {
    const needed = Math.max(8, this.density);
    while (this.tracks.filter((track) => track.id !== 900).length < needed) {
      this._extra += 1;
      const id = 500 + this._extra;
      this.tracks.push({
        id,
        bearing: (id * 47) % 360,
        rangeNm: 8 + (id % 50),
        course: (id * 13) % 360,
        speed: 8 + (id % 20),
        affiliation: id % 5 === 0 ? Affiliation.HOSTILE : id % 3 === 0 ? Affiliation.UNKNOWN : Affiliation.FRIEND,
        domain: id % 4 === 0 ? Domain.AIR : Domain.SURFACE,
        quality: 0.8,
        drift: ((id % 5) - 2) * 0.12,
        rangeRate: 0.02,
      });
    }
    let rows = this.tracks.filter((track) => track.id === 900 || track.id < 500 || track.id <= 500 + (needed - 8));
    if (lossSpike) rows = rows.filter((track, index) => track.id === 900 || index % 4 !== 0);
    return rows.slice(0, needed + (this.missile ? 1 : 0));
  }
}

export function packTracks(tracks) {
  const buffer = new ArrayBuffer(4 + tracks.length * 8 * 4);
  const view = new DataView(buffer);
  view.setUint32(0, tracks.length, true);
  const floats = new Float32Array(buffer, 4);
  tracks.forEach((track, index) => {
    const offset = index * 8;
    floats[offset] = track.id;
    floats[offset + 1] = track.bearing;
    floats[offset + 2] = track.rangeNm;
    floats[offset + 3] = track.course;
    floats[offset + 4] = track.speed;
    floats[offset + 5] = track.affiliation;
    floats[offset + 6] = track.domain;
    floats[offset + 7] = track.quality;
  });
  return buffer;
}
