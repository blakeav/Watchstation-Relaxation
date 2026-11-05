/** Radar interference stream. Same shape as analyze_radar.jammer_at plus weather and face flags. */

export class RadarInterferenceService extends EventTarget {
  constructor() {
    super();
    this.noise = false;
    this.deception = false;
    this.weather = false;
    this.blind = false;
    this._events = [];
  }

  snapshot() {
    const strobes = [];
    if (this.noise) {
      strobes.push({
        bearing: 48,
        half: 32,
        kind: "noise",
        power: 0.86,
        factor: 0.42,
        primaryFace: true,
        sjrDb: -14,
      });
    }
    if (this.deception) {
      strobes.push({
        bearing: 312,
        half: 16,
        kind: "deception",
        power: 0.5,
        factor: 0.72,
        primaryFace: !this.noise,
        sjrDb: -6,
      });
    }
    const clutter = this.weather
      ? { phenomenon: "squall", rateMmH: 18, bearing: 150, half: 40, factor: 0.62, lossDb: 9 }
      : { phenomenon: "clear", rateMmH: 0, bearing: 0, half: 0, factor: 1, lossDb: 0 };
    const degradation = {
      blind: this.blind,
      blindBearing: 205,
      blindHalf: 55,
      primaryFace: strobes.some((strobe) => strobe.primaryFace && strobe.kind === "noise" && strobe.power >= 0.65),
      capacity: this.blind ? 0 : this.noise ? 35 : 96,
      faceDamagePct: this.blind ? 60 : 0,
      modulesOffline: this.blind ? 6 : 0,
    };
    let status = "RADIATE";
    if (this.blind) status = "BLIND";
    else if (this.noise || this.deception || this.weather) status = "DEGRADED";
    return { status, strobes, clutter, degradation };
  }

  takeEvents() {
    const events = this._events;
    this._events = [];
    return events;
  }

  _note(code, tier, title, detail) {
    this._events.push({ code, tier, title, detail });
  }

  setNoise(on) {
    this.noise = on;
    this._note(
      on ? "JAM-ON" : "JAM-OFF",
      "INFORMATIONAL",
      on ? "Noise strobe injected" : "Noise strobe cleared",
      on ? "Training control. Noise jammer is on the primary face." : "Training control. Noise strobe removed."
    );
    this.dispatchEvent(new Event("change"));
  }

  setDeception(on) {
    this.deception = on;
    this._note(
      on ? "DEC-ON" : "DEC-OFF",
      "INFORMATIONAL",
      on ? "Deception strobe injected" : "Deception strobe cleared",
      "Training control."
    );
    this.dispatchEvent(new Event("change"));
  }

  setWeather(on) {
    this.weather = on;
    this._note(
      on ? "WX-ON" : "WX-OFF",
      "INFORMATIONAL",
      on ? "Squall clutter injected" : "Squall clutter cleared",
      on ? "Rain rate is above the clutter threshold." : "Weather sector cleared."
    );
    this.dispatchEvent(new Event("change"));
  }

  setBlind(on) {
    this.blind = on;
    this._note(
      on ? "BLIND-ON" : "BLIND-OFF",
      "INFORMATIONAL",
      on ? "Blind zone injected" : "Blind zone cleared",
      "Training control for a failed radar face."
    );
    this.dispatchEvent(new Event("change"));
  }
}
