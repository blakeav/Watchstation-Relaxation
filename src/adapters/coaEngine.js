/** Tactical course-of-action math. Pure: safe to run inside the worker. */

export function normalize(deg) {
  return ((deg % 360) + 360) % 360;
}

export function shortest(from, to) {
  let delta = normalize(to) - normalize(from);
  if (delta > 180) delta -= 360;
  if (delta < -180) delta += 360;
  return delta;
}

function pad(deg) {
  return String(Math.round(normalize(deg))).padStart(3, "0");
}

/**
 * @param {import("../models.js").ShipState} ship
 * @param {object} interference
 * @param {"primary"|"alternate"} prefer
 */
export function recommend(ship, interference, prefer = "primary") {
  const strobes = interference?.strobes || [];
  const clutter = interference?.clutter || null;
  const degradation = interference?.degradation || {};
  const noise = strobes.find((strobe) => strobe.kind === "noise" && strobe.primaryFace);
  const deception = strobes.find((strobe) => strobe.kind === "deception");
  const strobe = noise || strobes[0] || null;
  const blind = Boolean(degradation.blind);
  const rain = clutter && clutter.rateMmH >= 8;

  let action = 3;
  let target = ship.heading;
  let speed = ship.speed;
  let sensors = "No handoff. Continue the primary radar search.";
  let cfar = "CFAR nominal";
  let rationale = "No electronic-attack strobe and no blind zone. Maintain heading and speed.";

  if (blind) {
    action = 2;
    rationale = "Radar face is blind on the stored sector. Turn that sector off the threat axis and onto the beam.";
    sensors = "Cross-cue EO/IR, ESM, and passive sonar. Treat the blind sector as uncovered.";
    cfar = "Hold radar transmit on the failed face";
  } else if (noise && noise.power >= 0.65) {
    action = 2;
    rationale = "Severe noise jamming is on the primary tracking face. Put that strobe on the beam so the forward sector clears.";
    sensors = "Cross-cue ESM and EO/IR. Raise CFAR on the jammed face and keep a passive sonar watch.";
    cfar = "Raise CFAR on the jammed face";
    speed = Math.max(ship.speed, 21);
  } else if (deception) {
    action = 1;
    rationale = "Deception strobe is painting a tracking face. A short turn opens a spatial null. Do not chase the false range rate.";
    sensors = "Cross-cue EO/IR and ESM before accepting a range or a course from the jammed tracker.";
    cfar = "Hold CFAR. Confirm the track on a passive sensor";
  } else if (strobe) {
    action = 3;
    rationale = "Strobe is present and the notional burn-through range still covers the forward sector. Maintain heading.";
    sensors = "Keep the primary face. Cross-cue ESM if the strobe strength rises.";
  }

  if (action === 2 || action === 1) {
    const bearing = (blind ? degradation.blindBearing : strobe?.bearing) ?? ship.heading;
    const beamOffset = action === 1 ? 20 : 90;
    const starboard = normalize(bearing - beamOffset);
    const port = normalize(bearing + beamOffset);
    const toStarboard = shortest(ship.heading, starboard);
    const toPort = shortest(ship.heading, port);
    const wantPort = prefer === "alternate" ? Math.abs(toPort) > Math.abs(toStarboard) : Math.abs(toPort) < Math.abs(toStarboard);
    target = wantPort ? port : starboard;
    if (action === 2) {
      const relative = wantPort ? 270 : 90;
      rationale = `Placing the mainbeam jamming strobe on the relative beam (${pad(relative)}°) to clear the forward sector.`;
    }
  }

  const delta = shortest(ship.heading, target);
  const factor = blind ? 0.15 : strobe ? strobe.factor : 1;
  const burnThroughNm = Math.max(2, ship.radarRangeNm * factor);
  const abs = Math.abs(Math.round(delta));
  const direction = abs < 1 ? "STEADY" : delta > 0 ? "STARBOARD" : "PORT";
  const order =
    abs < 1
      ? `STEADY ON HDG ${pad(ship.heading)}°`
      : `TURN ${direction} ${pad(abs)}° TO HDG ${pad(target)}°`;

  if (rain) {
    sensors = `${sensors} Rain clutter is above the watch threshold. Raise CFAR on the weather sector.`;
    cfar = "Raise CFAR on the weather sector";
  }

  return {
    action,
    heading: normalize(target),
    speed,
    delta,
    direction,
    order,
    speedText: `SPD ${Math.round(speed)} KT`,
    speedDelta: Math.round(speed - ship.speed),
    rationale,
    sensors,
    cfar,
    burnThroughNm,
  };
}
