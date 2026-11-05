/** Local alert tones. No audio files. Critical pulses loop until acknowledge. */

export class TacticalAudio {
  constructor() {
    this.ctx = null;
    this.looping = false;
    this.timer = null;
  }

  get armed() {
    return Boolean(this.ctx && this.ctx.state === "running");
  }

  arm() {
    const Context = window.AudioContext || window.webkitAudioContext;
    if (!Context) return;
    if (!this.ctx) this.ctx = new Context();
    if (this.ctx.state === "suspended") this.ctx.resume();
    if (this.looping) this.startCritical();
  }

  startCritical() {
    this.looping = true;
    if (!this.ctx || this.ctx.state !== "running") return;
    if (this.timer) return;
    this._pulse();
    this.timer = window.setInterval(() => this._pulse(), 720);
  }

  stopCritical() {
    this.looping = false;
    if (this.timer) window.clearInterval(this.timer);
    this.timer = null;
  }

  blip(tier) {
    if (!this.ctx || this.ctx.state !== "running") return;
    if (tier === "WARNING") this._tone(660, 0.22, "sine", 0.18);
    else if (tier === "CAUTION") this._tone(440, 0.16, "triangle", 0.12);
    else if (tier === "INFORMATIONAL") this._tone(990, 0.05, "sine", 0.05);
  }

  _pulse() {
    this._tone(880, 0.09, "square", 0.16);
    window.setTimeout(() => {
      if (this.looping) this._tone(1040, 0.09, "square", 0.14);
    }, 110);
  }

  _tone(freq, duration, type, level) {
    if (!this.ctx) return;
    const t = this.ctx.currentTime;
    const osc = this.ctx.createOscillator();
    const gain = this.ctx.createGain();
    osc.type = type;
    osc.frequency.value = freq;
    gain.gain.setValueAtTime(0.0001, t);
    gain.gain.exponentialRampToValueAtTime(level, t + 0.015);
    gain.gain.exponentialRampToValueAtTime(0.0001, t + duration);
    osc.connect(gain);
    gain.connect(this.ctx.destination);
    osc.start(t);
    osc.stop(t + duration + 0.02);
  }
}
