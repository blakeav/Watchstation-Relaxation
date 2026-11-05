import { Tier } from "./models.js";
import { DdilDataLinkService } from "./adapters/ddilDataLink.js";
import { RadarInterferenceService } from "./adapters/radarInterference.js";
import { TacticalAudio } from "./audio/tacticalAudio.js";
import { WatchStore } from "./store/persistence.js";
import { RadarView } from "./ui/radarView.js";
import { AlertHistory, AlertQueue } from "./ui/alertQueue.js";
import { CoaPanel } from "./ui/coaPanel.js";
import { LinkTelemetry } from "./ui/linkTelemetry.js";
import { MockControls } from "./ui/mockControls.js";
import { WeatherDisplay } from "./ui/weatherDisplay.js";

const RANGES = [10, 25, 50, 100];
const TIER_RANK = { CRITICAL: 0, WARNING: 1, CAUTION: 2, INFORMATIONAL: 3 };

const linkService = new DdilDataLinkService();
const radarService = new RadarInterferenceService();
const audio = new TacticalAudio();
const store = new WatchStore();
const worker = new Worker(new URL("./workers/tactical.worker.js", import.meta.url), { type: "module" });

const radarView = new RadarView(document.querySelector("#ppi"));
const weatherView = new WeatherDisplay(document.querySelector("#weather"));
const alerts = new AlertQueue(document.querySelector("#alerts"), { ack });
const coaPanel = new CoaPanel(document.querySelector("#coa"), {
  accept,
  reject,
  recalculate,
});
const telemetry = new LinkTelemetry(document.querySelector("#link"));
const history = new AlertHistory(document.querySelector("#history"));
const mocks = new MockControls(document.querySelector("#mocks"), {
  latency: () => linkService.spikeLatency(),
  loss: () => linkService.spikeLoss(),
  ooo: () => linkService.injectOoo(),
  disconnect: (button) => {
    linkService.setDisconnected(!linkService.disconnected);
    mocks.setPressed("disconnect", linkService.disconnected);
    button.setAttribute("aria-pressed", String(linkService.disconnected));
    if (!linkService.disconnected) syncQueue();
  },
  mode: () => linkService.cycleMode(),
  noise: (button) => toggleRadar("noise", !radarService.noise, button),
  deception: (button) => toggleRadar("deception", !radarService.deception, button),
  weather: (button) => toggleRadar("weather", !radarService.weather, button),
  blind: (button) => toggleRadar("blind", !radarService.blind, button),
  missile: (button) => {
    linkService.setMissile(!linkService.missile);
    mocks.setPressed("missile", linkService.missile);
    button.setAttribute("aria-pressed", String(linkService.missile));
  },
  density: (button) => {
    const spiked = linkService.density < 100;
    linkService.setDensity(spiked ? 280 : 36);
    mocks.setPressed("density", spiked);
    button.setAttribute("aria-pressed", String(spiked));
  },
});

const active = new Map();
let seq = 0;
let latest = null;
let rangeNm = 50;
let prefer = "primary";
let coaStatus = "Recommendation is live.";
let queueCount = 0;
let searchQuery = "";
let lastHistory = 0;

radarView.onRange = setRange;
history.onSearch = (query) => {
  searchQuery = query;
  refreshHistory();
};

worker.onmessage = (event) => {
  if (event.data.seq !== seq) return;
  latest = { ...latest, ...event.data };
  publish();
};
worker.onerror = (error) => {
  coaStatus = `Engine error: ${error.message || "the tactical worker did not start"}`;
  publish();
};

document.querySelector("#arm-audio").addEventListener("click", arm);
window.addEventListener("pointerdown", arm, { once: true });
window.addEventListener("keydown", onKey);

store.ready.then(async () => {
  queueCount = (await store.pending()).length;
  await refreshHistory();
  publish();
});

setInterval(tick, 200);
tick();

function arm() {
  audio.arm();
  const button = document.querySelector("#arm-audio");
  button.textContent = audio.armed ? "Tones armed" : "Arm alert tones";
  if (unackedCritical()) audio.startCritical();
}

function toggleRadar(name, on, button) {
  if (name === "noise") radarService.setNoise(on);
  if (name === "deception") radarService.setDeception(on);
  if (name === "weather") radarService.setWeather(on);
  if (name === "blind") radarService.setBlind(on);
  mocks.setPressed(name, on);
  button.setAttribute("aria-pressed", String(on));
}

function setRange(nm) {
  rangeNm = nm;
  radarView.setRange(nm);
}

function tick() {
  const now = performance.now();
  const link = linkService.sample(now);
  const radar = radarService.snapshot();
  for (const event of [...linkService.takeEvents(), ...radarService.takeEvents()]) {
    openAlert({ ...event, sticky: false });
  }
  reconcile(link, radar, latest?.summary);
  seq += 1;
  worker.postMessage(
    {
      type: "process",
      seq,
      buffer: link.buffer,
      ownship: link.ownship,
      radar,
      prefer,
    },
    [link.buffer]
  );
  latest = { ...(latest || {}), link, radar, ownship: link.ownship };
  publish();
}

function reconcile(link, radar, summary) {
  const wanted = new Map();
  const add = (code, tier, title, detail) => wanted.set(code, { code, tier, title, detail, sticky: true });
  if (summary?.missile || linkService.missile) {
    add("MISSILE", Tier.CRITICAL, "Missile track", "Inbound missile-class track inside 45 NM. Acknowledge to silence the pulse.");
  }
  if (radar.degradation.blind) {
    add("BLIND", Tier.CRITICAL, "Radar blind zone", "A radar face is blind. The dark sector is drawn on the PPI.");
  }
  if (radar.degradation.primaryFace) {
    add("JAM-PRIMARY", Tier.CRITICAL, "Primary face jammed", "Severe noise jamming is on the primary tracking face.");
  }
  if (link.condition === "DEGRADED") {
    add("LINK-DEGRADED", Tier.WARNING, "Link degraded", "Latency, loss, or bandwidth crossed the watch limit.");
  }
  if (link.condition.startsWith("DISCONNECTED")) {
    add("LINK-DOWN", Tier.WARNING, "Link down, local buffering", "The picture is local. New orders queue until the link returns.");
  }
  if (radar.clutter.rateMmH >= 8) {
    add("CLUTTER", Tier.WARNING, "Weather clutter", `${radar.clutter.phenomenon} at ${radar.clutter.rateMmH.toFixed(0)} mm/h on ${radar.clutter.bearing.toFixed(0)}°.`);
  }
  if (radar.strobes.some((strobe) => strobe.kind === "deception")) {
    add("SENSOR-FALLBACK", Tier.WARNING, "Passive sensor fallback", "Deception jamming is up. Cross-cue EO/IR and ESM before accepting the track.");
  }
  if (!link.condition.startsWith("DISCONNECTED") && link.bandwidthKbps < 80) {
    add("LOW-BW", Tier.CAUTION, "Low bandwidth", `${link.bandwidthKbps} kbps remaining on ${link.modeLabel}.`);
  }
  if (link.ooo) {
    add("OOO-LIVE", Tier.CAUTION, "Packet reconciliation", "An out-of-order packet is in the current window.");
  }

  for (const [code, spec] of wanted) {
    if (!active.has(code)) openAlert(spec);
  }
  for (const [code, alert] of active) {
    if (!alert.sticky) continue;
    if (wanted.has(code)) continue;
    alert.cleared = true;
    active.delete(code);
    store.updateAlert(alert);
  }
  if (unackedCritical()) audio.startCritical();
  else audio.stopCritical();
}

function openAlert(spec) {
  const alert = {
    id: crypto.randomUUID(),
    time: Date.now(),
    tier: spec.tier,
    code: spec.code,
    title: spec.title,
    detail: spec.detail,
    acked: false,
    cleared: false,
    sticky: Boolean(spec.sticky),
  };
  if (alert.sticky) active.set(alert.code, alert);
  else {
    active.set(alert.code, alert);
    window.setTimeout(() => {
      const current = active.get(alert.code);
      if (current && current.id === alert.id && !current.sticky) {
        current.cleared = true;
        active.delete(alert.code);
        store.updateAlert(current);
        publish();
      }
    }, 9000);
  }
  store.appendAlert(alert);
  if (alert.tier === Tier.CRITICAL) audio.startCritical();
  else audio.blip(alert.tier);
  refreshHistory();
}

function ack() {
  const next = [...active.values()]
    .filter((alert) => !alert.acked)
    .sort((a, b) => TIER_RANK[a.tier] - TIER_RANK[b.tier] || b.time - a.time)[0];
  if (!next) return;
  next.acked = true;
  store.updateAlert(next);
  if (!unackedCritical()) audio.stopCritical();
  publish();
  refreshHistory();
}

function unackedCritical() {
  return [...active.values()].some((alert) => alert.tier === Tier.CRITICAL && !alert.acked);
}

async function accept() {
  const coa = latest?.coa;
  if (!coa) return;
  const command = linkService.order(coa.heading, coa.speed, coa.order);
  await store.enqueue(command);
  queueCount = (await store.pending()).length;
  coaStatus = command.synced
    ? `Order sent. ${coa.order}.`
    : `Queued locally. ${coa.order}. It will sync when the link returns.`;
  publish();
}

function reject() {
  linkService.ownship.orderedHeading = null;
  linkService.ownship.orderedSpeed = null;
  coaStatus = "Recommendation rejected. Holding the current heading.";
  publish();
}

function recalculate() {
  prefer = prefer === "primary" ? "alternate" : "primary";
  coaStatus = prefer === "alternate" ? "Recalculated on the alternate beam." : "Recalculated on the primary beam.";
  tick();
}

async function syncQueue() {
  const pending = await store.pending();
  if (!pending.length) {
    queueCount = 0;
    publish();
    return;
  }
  linkService.mergeLink();
  await store.markSynced(pending.map((command) => command.id), linkService.clock.LINK);
  const last = pending[pending.length - 1];
  linkService.ownship.orderedHeading = last.heading;
  linkService.ownship.orderedSpeed = last.speed;
  queueCount = 0;
  coaStatus = `Queue synced at LINK ${linkService.clock.LINK}. ${pending.length} order${pending.length === 1 ? "" : "s"} merged.`;
  publish();
}

function publish() {
  const link = latest?.link || null;
  const radar = latest?.radar || radarService.snapshot();
  const scene = {
    rangeNm,
    ownship: latest?.ownship || linkService.ownship,
    tracks: latest?.tracks || [],
    radar,
    coa: latest?.coa || null,
  };
  radarView.setScene(scene);
  weatherView.setLive(radar);
  alerts.render([...active.values()]);
  coaPanel.render({ coa: latest?.coa || null, status: coaStatus });
  telemetry.render(link, queueCount);
  const clock = document.querySelector("#zulu");
  if (clock) clock.textContent = new Date().toISOString().slice(11, 19) + "Z";
  if (Date.now() - lastHistory > 2000) refreshHistory();
}

async function refreshHistory() {
  lastHistory = Date.now();
  history.render(await store.search(searchQuery));
}

function onKey(event) {
  const tag = document.activeElement?.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
  if (event.code === "Space") {
    event.preventDefault();
    arm();
    ack();
  } else if (event.key === "Enter") {
    if (tag === "BUTTON") return;
    event.preventDefault();
    arm();
    accept();
  } else if (event.key >= "1" && event.key <= "4") {
    setRange(RANGES[Number(event.key) - 1]);
  }
}
