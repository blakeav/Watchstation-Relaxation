/* Training aid. Live nets follow the current OPTASK LINK and ship procedures. */
const LinkCoa = (() => {
  const LINK = {
    link16: "Link 16",
    link11: "Link 11",
    link22: "Link 22",
    jreap: "JREAP",
  };

  const ALT = {
    unspecified: "the alternate path named in the current OPTASK LINK",
    link16: "Link 16",
    link11: "Link 11",
    link22: "Link 22",
    jreap: "JREAP on the backup bearer",
    satellite: "the satellite path",
    "backup-sat": "the backup satellite",
    voice: "a voice picture",
  };

  function unitOf(input) {
    const name = (input.unit || "").trim();
    return name || "the reporting unit";
  }

  function callsign(input) {
    const name = (input.unit || "").trim();
    return name || "the reporting unit";
  }

  function whenOf(input) {
    return input.when ? input.when : "the time of this report";
  }

  function alternateOf(input) {
    if (!input.alternate || input.alternate === input.link) return ALT.unspecified;
    return ALT[input.alternate] || ALT.unspecified;
  }

  function dutyLabel(input) {
    const labels = {
      pu: input.link === "link16" ? "a participating JU" : "a participating unit",
      ntr: "the network time reference",
      fju: input.link === "jreap" ? "a forwarding node" : "a forwarding unit",
      ncs: input.link === "link11" ? "Link 11 net control" : "network control",
      jico: "the JICO cell",
      jre: "a JRE node",
    };
    return labels[input.duty] || "a participating unit";
  }

  function seesLine(input) {
    if (input.sees === "yes") return "I still see other units on this link.";
    if (input.sees === "no") return "I see no other units on this link.";
    return "I cannot tell yet whether I still see other units.";
  }

  function timeLine(input) {
    if (input.timeQuality === "good") return "Time quality is good.";
    if (input.timeQuality === "suspect") return "Time quality is suspect.";
    if (input.timeQuality === "lost") return "Time quality is lost.";
    return "Time quality is not yet known.";
  }

  function diagnose(input) {
    if (input.symptom === "jammed") return "jam";
    if (input.symptom === "display") return "display";
    if (input.symptom === "conflict") return "conflict";
    if (input.symptom === "via-sat" && input.link !== "jreap") return "via-satellite";
    if (input.link === "jreap" || input.symptom === "extension") return "extension";
    if (input.symptom === "time-ref") return "time";
    if (input.timeQuality === "lost" && (input.duty === "ntr" || input.scope === "group")) return "time";
    if (
      input.duty === "ntr" &&
      input.scope !== "group" &&
      (input.symptom === "dropped" || input.symptom === "no-entry" || input.timeQuality === "suspect" || input.timeQuality === "lost")
    ) {
      return "time";
    }
    if (
      input.duty === "ncs" &&
      (input.link === "link11" || input.link === "link22") &&
      (input.symptom === "dropped" || input.symptom === "no-entry")
    ) {
      return "ncs";
    }
    if (input.symptom === "relay") return "relay";
    if (input.scope === "group") return "group";
    if (input.symptom === "stale" && input.scope === "one") return "stale";
    if (input.timeQuality === "suspect" && input.scope === "one" && input.duty !== "ntr") return "local-time";
    if (input.scope === "several" || input.symptom === "partial") return "partial";
    return "single";
  }

  function entry(input, unit) {
    switch (input.link) {
      case "link11":
        return [
          "Net control transmits net sync, then net test, on the existing net.",
          `${unit} answers the net test and reports ready on the tactical voice net.`,
          "Net control returns the net to roll call and polls this unit.",
          "Confirm the unit answers only the address assigned in the link plan.",
        ];
      case "link22":
        return [
          "Confirm the active network control station and the super network on voice.",
          `${unit} late-enters that same super network.`,
          "Network control confirms the unit is in the network.",
          `${unit} exchanges one test track before it resumes full participation.`,
        ];
      case "jreap":
        return [
          "Leave the radio tactical link alone if units on that link still see each other.",
          "Name the failed piece: the JRE processor, the bearer, or the forwarding filters.",
          "Restore the bearer in the order already written for this extension.",
          "Exchange one test track with the far end.",
        ];
      default:
        return [
          `${unit} receives initial entry from the active network time reference and reaches coarse synchronization.`,
          `${unit} exchanges round-trip timing until fine synchronization is complete.`,
          `${unit} transmits only after fine synchronization. The first transmission is a short status, not the track file.`,
        ];
    }
  }

  function ddilRestore(input, unit, alt) {
    const link = LINK[input.link];
    switch (input.ddil) {
      case "denied":
        return [
          `Treat ${link} as denied after one failed check. Further calls on that path only add congestion.`,
          `Stop transmissions on the denied path and shift together to ${alt}.`,
          "Set the emissions condition the current plan requires for that shift.",
        ];
      case "disrupted":
        return [
          "Change one thing, then wait long enough to see if the link settles. A second change too soon hides the fault.",
          "Prefer a relay, a clearer line of sight, or the planned robust mode before you change nets or control stations.",
        ];
      case "intermittent":
        return [
          `Log each dropout and each reentry for ${unit}. Two dropouts on the same unit point to line of sight or relay, not a new architecture.`,
          "Keep the same net and the same control station across the dropouts. Reenter. Do not rebuild the group each time the path blinks.",
        ];
      case "limited":
        return [
          "Cut the exchange to surveillance and command tracks. Hold imagery and bulk updates off this path.",
          "Prove the path with one test track. Add load only after that track correlates.",
        ];
      default:
        return [];
    }
  }

  function prove(unit) {
    return [
      `${unit} and one designated unit each see the other.`,
      "One live contact correlates to a single track. Duplicate tracks mean the path is not restored.",
      "Synchronization or net control stays steady long enough for a short voice confirmation.",
      "CIC tells the officer of the deck the picture is current again, and from what time.",
    ];
  }

  function fallback(input, alt) {
    const lines = [
      "Stop the failed attempt. Report terminal status, who you still see, and time quality to JICO.",
      `JICO moves the affected units together onto ${alt}. Units shift on the mark, not one by one.`,
      "On a voice picture, the detecting ship passes position, course, and speed. One station consolidates.",
      "Call the link restored only after the prove-it checks pass on the new path.",
    ];
    if (input.ddil === "denied") {
      lines[0] = "The path is denied. Skip further reentry attempts on it.";
    }
    return lines;
  }

  function report(input, symptomText) {
    const who = callsign(input);
    const link = LINK[input.link];
    const width =
      input.scope === "group" ? "whole strike group" : input.scope === "several" ? "several units" : "one unit";
    const tried = (input.notes || "").trim();
    const lines = [
      `JICO, this is ${who}.`,
      `${link} casualty at ${whenOf(input)}. Width: ${width}.`,
      `Symptom: ${symptomText}.`,
      `I am ${dutyLabel(input)}. ${timeLine(input)} ${seesLine(input)}`,
      "Picture from this link is stale. Request course of action.",
    ];
    if (tried) lines.push(`Already done: ${tried}`);
    return lines.join(" ");
  }

  function bridge(unit, link) {
    return `CIC tells the officer of the deck that ${link} tracks for ${unit} are stale. The bridge keeps station on radar and visual and takes fresh contacts by voice.`;
  }

  function pack(input, spec) {
    const unit = unitOf(input);
    const alt = alternateOf(input);
    const extra = spec.applyDdil === false ? [] : ddilRestore(input, unit, alt);
    const restore = spec.restore.slice();
    if (input.ddil === "denied" && spec.replaceOnDenied) {
      restore.length = 0;
      restore.push(...extra);
    } else if (extra.length) {
      restore.push(...extra);
    }
    return {
      tag: spec.tag,
      headline: spec.headline,
      assessment: spec.assessment,
      reasons: spec.reasons,
      now: spec.now,
      direct: spec.direct,
      restore,
      prove: spec.prove || prove(unit),
      fallback: spec.fallback || fallback(input, alt),
      report: spec.report,
      order: spec.order,
    };
  }

  function displayCase(input) {
    const unit = unitOf(input);
    const link = LINK[input.link];
    const wide = input.scope !== "one";
    return pack(input, {
      tag: "Display casualty",
      headline: `Keep ${link} up until the terminal itself is proven down.`,
      assessment: wide
        ? `Dark screens on more than one ship are still display casualties until each CIC reads terminal status. The group should not reenter because the repeaters failed.`
        : `A dark screen on ${unit} is a display casualty until CIC reads the terminal status. The strike group should not reenter or shift because one console failed.`,
      reasons: [
        "The report is a display or repeater symptom, not yet a lost interface.",
        "Reentry and a group shift both disturb a net that may still be good.",
      ],
      now: [
        bridge(unit, link).replace("are stale", "may be stale on the bridge display only"),
        "Read the terminal status in CIC. Record whether the terminal still shows participation, sync, and time quality.",
        "If the terminal is still in the net, restore the display or use another console. Leave the link settings as they are.",
        "Tell the bridge the link is up and the failed piece is the display.",
      ],
      direct: [
        "JICO logs it as a display casualty.",
        "JICO asks one other ship whether it still sees this unit. If yes, the interface stays as assigned.",
        "JICO directs a group reentry only if the terminal status shows the unit is actually out of the net.",
      ],
      restore: [
        "Terminal still participating: repair the display path. Stop there.",
        "Terminal out of the net: send a corrected report and use the single-unit reentry course of action.",
      ],
      applyDdil: false,
      report: report(input, "display or repeater casualty, link status not yet confirmed"),
      order: `All stations, this is JICO. ${unit} has a ${link} display casualty. Hold the net and the control station. ${unit}, report terminal status. Nearest unit, say if you still see ${unit}.`,
    });
  }

  function conflictCase(input) {
    const unit = unitOf(input);
    const link = LINK[input.link];
    return pack(input, {
      tag: "Stop the duplicate path",
      headline: "Leave one forwarding path up and stop the path that is duplicating tracks.",
      assessment: `Conflicting tracks on ${link} are a correlation failure. Another reentry, with both paths still forwarding, copies the same contact again.`,
      reasons: [
        "Two paths are producing two tracks for one contact.",
        "The group needs one named source before anyone reenters a gateway.",
      ],
      now: [
        bridge(unit, link),
        "Stop extra forwarding at the gateway that is producing the duplicates. Keep the local tactical picture.",
        "Report which path you stopped and which tracks doubled.",
      ],
      direct: [
        "JICO names the single forwarding path the group will use.",
        "Units drop the duplicate and keep the track from the named source.",
        "The stopped gateway rejoins only after a test track correlates to one track.",
      ],
      restore: [
        "JICO states the surviving path on voice.",
        "The forwarding unit sends one test track.",
        "A second unit confirms that test track landed once.",
        "Resume normal forwarding on the named path only.",
      ],
      report: report(input, "duplicate or conflicting tracks"),
      order: `All stations, this is JICO. ${link} tracks are duplicating. Forward on the path I name only. Drop the duplicate tracks. ${unit}, stop the extra forward and stand by for a test track.`,
    });
  }

  function satelliteShift(unit, alt) {
    return [
      "Leave the radio tactical link alone where units still see each other.",
      `${unit} moves the forwarding session to ${alt}.`,
      "The far end meets on that path.",
      "Exchange one test track. Hold imagery and bulk tracks off the satellite until that track correlates.",
    ];
  }

  function viaSatelliteCase(input) {
    const unit = unitOf(input);
    const link = LINK[input.link];
    const alt = alternateOf(input);
    return pack(input, {
      tag: "Use the satellite",
      headline: `Keep ${link} for ships that still see each other. Put ${unit} on ${alt}.`,
      assessment: `${unit} has lost the radio path. The satellite is the secondary connection for that ship. Ships still inside line of sight stay on ${link}.`,
      reasons: [
        "A satellite path carries the picture past the horizon. It does not replace the radio net for ships that can still see each other.",
        "The satellite stays lightly loaded. One test track crosses before surveillance tracks.",
      ],
      now: [
        bridge(unit, link),
        `Confirm ${unit} has no line of sight, and name who still holds ${link}.`,
        "Stop any attempt to push the full track file over the satellite.",
      ],
      direct: [
        `JICO holds every unit that still sees ${link} on that net.`,
        `JICO assigns the forwarding unit and sends ${unit} to ${alt}.`,
        "The forwarding unit and the far end exchange one test track, then surveillance and command tracks only.",
      ],
      restore: satelliteShift(unit, alt),
      report: report(input, `no line of sight on ${link}, shifting to the satellite`),
      order: `All stations, this is JICO. Units that still see ${link}, hold that net. ${unit}, you are moving to ${alt}. Forwarding unit, call when one test track is across. Hold imagery off the satellite.`,
    });
  }

  function extensionCase(input) {
    const unit = unitOf(input);
    const link = LINK[input.link];
    const alt = alternateOf(input);
    const satellite = input.alternate === "satellite" || input.alternate === "backup-sat";
    const radioNote =
      input.link === "jreap"
        ? "Units that still see each other on the radio link stay on that link."
        : "If the radio link is still up, do not reenter it. Fix the extension.";
    return pack(input, {
      tag: satellite ? "Satellite bearer" : "Restore range extension",
      headline:
        input.ddil === "denied"
          ? `Shift off the denied path onto ${alt}. Leave the radio link in place.`
          : satellite
            ? `Move the forwarding session to ${alt}. Leave the radio link in place.`
            : "Restore the extension path. Leave a working radio link in place.",
      assessment: `${unit} lost the beyond-line-of-sight path. ${radioNote}`,
      reasons: [
        "A range-extension failure can look like a dead tactical link from the far end.",
        "Rebuilding the radio net does not repair a satellite, shore, or processor path.",
      ],
      now: [
        bridge(unit, link),
        "Separate the fault on voice: radio participants who still see each other, versus units that only had the extension.",
        "Stop forwarding stale far-end tracks into the radio net.",
      ],
      direct:
        input.ddil === "denied"
          ? [
              "JICO confirms which units still see each other on the radio link. Those units hold.",
              `JICO moves the extension users onto ${alternateOf(input)}.`,
              "The forwarding unit stops the denied bearer before the group shifts.",
            ]
          : [
              "JICO confirms which end still holds the radio picture.",
              satellite
                ? `JICO sends the forwarding unit to ${alt}. The failed satellite stays down.`
                : "JICO assigns the backup bearer from the link plan if the primary bearer is down.",
              "Both ends exchange one test track before full forwarding.",
            ],
      restore: satellite ? satelliteShift(unit, alt) : entry(input.link === "jreap" ? input : { ...input, link: "jreap" }, unit),
      replaceOnDenied: true,
      report: report(input, "range extension down, radio link to be confirmed"),
      order:
        input.ddil === "denied"
          ? `All stations, this is JICO. The range extension is denied. Units on the radio link hold that link. ${unit}, stop the denied bearer and stop forwarding stale tracks. Extension users, shift to ${alternateOf(input)} on my mark.`
          : `All stations, this is JICO. The beyond-line-of-sight path is the casualty. Units on the radio link hold that link. ${unit}, stop forwarding stale far-end tracks. Both ends, move to ${alt} and exchange one test track.`,
    });
  }

  function timeCase(input) {
    const unit = unitOf(input);
    const link = LINK[input.link];
    const isNtr = input.duty === "ntr";
    return pack(input, {
      tag: "Name one time reference",
      headline: "Put one time reference back in charge before any unit reenters.",
      assessment: isNtr
        ? `${unit} is the time reference and time is not reliable. The group needs the preplanned alternate reference on voice before this unit drops offline.`
        : `Time reference for ${link} is down or suspect. Units that reenter against different clocks will not share a net.`,
      reasons: [
        "A tactical link with more than one time reference, or with none, will not synchronize.",
        "Naming the alternate comes before reentry.",
      ],
      now: [
        bridge(unit, link),
        isNtr
          ? "Stay up until JICO names the alternate and that unit reports it is the reference. Then leave the reference role."
          : "Do not appoint yourself the time reference. Report time quality and wait for JICO to name the reference.",
        "Mark every track from this link stale. Pass fresh contacts by voice.",
      ],
      direct: [
        "JICO polls one unit that still hears the net and confirms whether anyone still holds system time.",
        "JICO orders the preplanned alternate time reference to assume the role.",
        "The alternate reports on voice when it is the reference. Only then does JICO release units to reenter.",
        "The old reference secures that role after the alternate's report.",
      ],
      restore: [
        "Alternate reference reports: I am the network time reference.",
        ...entry(input, "Each unit that is out"),
        "JICO confirms at least two units see each other under the new reference.",
      ],
      replaceOnDenied: true,
      report: report(input, "time reference down or time quality lost"),
      order: `All stations, this is JICO. ${link} time reference is down. Do not reenter yet. Preplanned alternate, assume network time reference and report when you have it. All others, hold voice picture and mark link tracks stale.`,
    });
  }

  function ncsCase(input) {
    const unit = unitOf(input);
    const link = LINK[input.link];
    return pack(input, {
      tag: "Shift net control",
      headline: `Move ${link} control to the preplanned alternate, then reenter the units that dropped.`,
      assessment: `${unit} was ${dutyLabel(input)}. A net without its control station does not pick up dropped units. The group shifts control on voice, then units enter against that one station.`,
      reasons: [
        "Participating units cannot complete entry if net control is the station that failed.",
        "Two stations controlling the same net will split the group.",
      ],
      now: [
        bridge(unit, link),
        "If you are the failed control station, say so in the report and stop polling.",
        "Other units hold what they still have and wait for the alternate control station to be named.",
      ],
      direct: [
        "JICO confirms the control station is down with one other unit.",
        "JICO directs the preplanned alternate to assume net control.",
        "The alternate reports on voice that it has control.",
        "Only the units that are out then reenter. Units that stayed in the net hold.",
      ],
      restore: [
        "Alternate control station reports it has the net.",
        ...entry(input, "Each dropped unit"),
      ],
      report: report(input, `${link} control station down`),
      order: `All stations, this is JICO. ${link} net control is down. Preplanned alternate, assume control and report when you have the net. Dropped units reenter only after that report. Everyone else, hold the net.`,
    });
  }

  function relayCase(input) {
    const unit = unitOf(input);
    const link = LINK[input.link];
    return pack(input, {
      tag: "Restore the relay",
      headline: "Fill the relay gap. Keep the units that still see each other on the current net.",
      assessment: `${link} relay or forwarding through ${unit} is the break. Ships on the near side still have a net. Pulling everyone off to rebuild removes the half that is working.`,
      reasons: [
        "A relay casualty splits the group into two pockets that cannot hear each other.",
        "The fix is a planned relay or the extension path, then a check across the gap.",
      ],
      now: [
        bridge(unit, link),
        "If you are the relay, stop forwarding and say which side you still hear.",
        "Each pocket reports who it still sees. Stay on the current net while you do.",
      ],
      direct: [
        "JICO picks the preplanned relay unit, or the extension path if no radio relay can see both sides.",
        "That unit reports when it hears both sides.",
        "JICO then has one unit on each side confirm a single test track across the relay.",
      ],
      restore: [
        "Named relay reports it hears both sides.",
        "One test track crosses the relay and correlates once.",
        "Units that dropped during the gap reenter on the existing net.",
        ...entry(input, "A unit that is still out"),
      ],
      replaceOnDenied: true,
      report: report(input, "relay or forwarding path lost"),
      order: `All stations, this is JICO. ${link} relay is down. Stay on the current net. Report who you still see. I will name the relay. Do not change nets until a test track crosses the gap.`,
    });
  }

  function groupCase(input) {
    const unit = unitOf(input);
    const link = LINK[input.link];
    const stale = input.symptom === "stale";
    return pack(input, {
      tag: stale ? "Group picture frozen" : "Group link is down",
      headline: stale
        ? `Find out whether ${link} is up with updates stopped, or the group is actually out of the net.`
        : `Stop group transmissions on ${link}, name the station that still owns the net, then reenter together.`,
      assessment: stale
        ? `The whole strike group is reporting a frozen ${link} picture. Confirm participation and time before you tear the net down.`
        : `${link} is down across the strike group. Independent reentry attempts will create several clocks and several nets.`,
      reasons: [
        "Width is the whole group, so this is an architecture casualty.",
        "One order from JICO has to cover every CIC.",
      ],
      now: [
        bridge(unit, link),
        "Each CIC reports three facts on voice: in the net or out, time quality, and whether any other unit is still seen.",
        stale
          ? "If the terminal still shows participation, do not reenter. Wait for JICO to check filters and the time reference."
          : "Cease track transmit on this link until JICO names the reference and releases reentry.",
      ],
      direct: stale
        ? [
            "JICO asks two ships for terminal status, not just the display.",
            "If they are still in the net, JICO checks the time reference and the forwarding filters.",
            "If they are out, JICO runs the group reentry below.",
          ]
        : [
            "JICO freezes local changes. No ship changes net, reference, or control on its own.",
            "JICO names the surviving time reference or the preplanned alternate.",
            "JICO releases reentry. Ships enter, then call fine sync or net-test complete on voice.",
          ],
      restore: stale
        ? [
            "Two ships confirm they are still participating.",
            "Time reference confirms time quality, or the alternate assumes reference and reports.",
            "Clear the filter or forwarding fault that stopped updates.",
            "One new track updates on a second ship. That is the proof.",
          ]
        : [
            "JICO names the single time reference or control station.",
            ...entry(input, "All units"),
            "Two units confirm they see each other. Then the rest call in.",
          ],
      replaceOnDenied: true,
      report: report(input, stale ? "group picture frozen" : "group link down"),
      order: stale
        ? `All stations, this is JICO. ${link} picture is frozen for the group. Report if your terminal is still in the net and your time quality. Do not reenter until I say so.`
        : `All stations, this is JICO. ${link} is down for the group. Stop track transmit. I will name one time reference. Reenter only after that station reports, then call your sync on voice.`,
    });
  }

  function staleCase(input) {
    const unit = unitOf(input);
    const link = LINK[input.link];
    return pack(input, {
      tag: "Updates stopped",
      headline: `Restore track updates on the existing ${link} net. Reenter only if the terminal is actually out.`,
      assessment: `${unit} still has a console up, but tracks are not updating. A filter, a forwarder, or a stuck terminal causes this. A full reentry is the step after those checks fail.`,
      reasons: [
        "A frozen picture and a lost link are different faults.",
        "One ship with stale tracks is not a reason to move the group.",
      ],
      now: [
        bridge(unit, link),
        "Read terminal status. If it shows you out of the net, switch to the single-ship reentry actions and tell JICO.",
        "If it shows you in the net, check receive filters and whether the forwarding unit is still forwarding.",
      ],
      direct: [
        "JICO asks one other ship whether its picture is still updating.",
        "If the other ship is current, the fault is local to the reporting unit or to one forwarder.",
        "If several pictures are frozen, JICO treats it as a group picture failure.",
      ],
      restore: [
        "Confirm the terminal is still participating.",
        "If it is, reset the local filter or select the forwarding path JICO names.",
        "Confirm a track that just updated on another ship now updates here. Stop if it does.",
        "If the terminal is out of the net, reenter on the same net:",
        ...entry(input, unit),
      ],
      report: report(input, "tracks frozen or not updating"),
      order: `All stations, this is JICO. ${unit} reports a frozen ${link} picture. Hold the net. ${unit}, report whether the terminal is still in the net. Nearest unit, say if your picture is still updating.`,
    });
  }

  function localTimeCase(input) {
    const unit = unitOf(input);
    const link = LINK[input.link];
    return pack(input, {
      tag: "Fix own time, same net",
      headline: `Restore time on ${unit}, then reenter the existing ${link} net.`,
      assessment: "This ship's time is suspect and the ship is not the group time reference. Fixing the local time source and reentering keeps the group on one clock.",
      reasons: [
        "A participating unit with bad time will fail entry or disturb the net if it transmits early.",
        "The group time reference stays put.",
      ],
      now: [
        bridge(unit, link),
        "Stop transmitting on the link while time quality is suspect.",
        "Restore the ship's time source. Report to JICO when time quality is good.",
        "Do not assume the time-reference role.",
      ],
      direct: [
        "JICO confirms another unit still holds the net and the current reference.",
        "JICO releases this ship to reenter after it reports good time.",
        "One designated ship confirms it sees the reentering unit.",
      ],
      restore: [
        "Report good time to JICO and wait for release to reenter.",
        ...entry(input, unit),
      ],
      report: report(input, "own time quality suspect, still using the group time reference"),
      order: `All stations, this is JICO. ${unit} has suspect time and is staying off the link until time is good. The current time reference remains. ${unit} will reenter the same net after it reports good time.`,
    });
  }

  function partialCase(input) {
    const unit = unitOf(input);
    const link = LINK[input.link];
    const seesNobody = input.sees === "no";
    return pack(input, {
      tag: "Part of the group",
      headline: seesNobody
        ? "Find the pocket that is still up, then bring the dropped units back onto that net."
        : "Keep the units that still see each other on this net, and reenter only the units that are out.",
      assessment: `The ${link} casualty covers several ships, not the whole strike group. JICO maps who still sees whom before any net change.`,
      reasons: [
        "A partial outage is usually line of sight, a relay, or a few terminals.",
        "Ships that still see the net should stay on it.",
      ],
      now: [
        bridge(unit, link),
        "Report the units you still see and the units you have lost. Names, not just 'partial.'",
        seesNobody
          ? "You see nobody from here. Ask JICO what the rest of the group sees before you change nets."
          : "You still see part of the net. Stay on it.",
      ],
      direct: [
        "JICO builds the split on voice: which units see the net, which see only a neighbor, which see nobody.",
        "If the gap is line of sight, JICO assigns the planned relay or the extension. The net number stays.",
        "JICO sends reentry orders only to the units that are out.",
      ],
      restore: [
        "JICO states which units stay and which units reenter.",
        ...entry(input, "Each unit that is out"),
        "A unit that stayed in the net confirms it sees each reentering unit.",
      ],
      replaceOnDenied: true,
      report: report(input, "several units lost, group not fully down"),
      order: `All stations, this is JICO. ${link} is down for part of the group. Units that still see the net, stay there and report who you see. Units that are out, stand by to reenter the same net. Do not change the time reference.`,
    });
  }


  function jamCase(input) {
    const unit = unitOf(input);
    const path = LINK[input.link] || "the reported path";
    const alt = alternateOf(input);
    const width =
      input.scope === "group" ? "the whole group" : input.scope === "several" ? "more than one ship" : unit;
    return pack(input, {
      tag: "Spectrum jamming",
      headline: `Stop using the jammed ${path}. Shift ${width} together to ${alt}.`,
      assessment: `Jamming on ${path} is a spectrum casualty, not a single ship that needs another reentry. One check confirms the path. After that, the affected units leave it together.`,
      reasons: [
        "A jammed path answers more calls with more noise.",
        "Units that still hold a clean path should not follow the jammed units off it.",
      ],
      now: [
        `CIC tells the officer of the deck that the ${path} picture may be false or stale. The bridge keeps station on radar and visual if those are clean.`,
        "Make one check that the terminal is still up. Then stop. Do not keep calling on the jammed path.",
        "Report the path, the width, and a bearing only if a console already displays it. Do not search for the source.",
        "Stand by to shift on JICO's mark. Do not change to a frequency that is not already in the plan.",
      ],
      direct: [
        "JICO treats the path as denied after that one check.",
        `JICO shifts the affected units together, on the mark, to ${alt}.`,
        "Units that still hold a clean path stay there unless JICO orders the whole group to move.",
        "JICO sets only the emissions condition the current plan already requires.",
      ],
      restore: [
        `Stay on ${alt} until JICO says the original path is clear.`,
        "Exchange one test track on the new path. Hold imagery and bulk updates.",
        "A return to the original path is a group action on the mark, after one unit confirms that path is usable.",
      ],
      prove: [
        "The affected units see each other on the path they shifted to.",
        "One live contact correlates to a single track.",
        "Units that stayed on the clean path still hold it.",
        "CIC tells the officer of the deck which picture is current, and from what time.",
      ],
      fallback: [
        "If the alternate is also jammed, stop it after one check and go to a voice picture.",
        "On voice, the detecting ship passes position, course, and speed. One station consolidates.",
        "Call the path restored only after the prove-it checks pass.",
      ],
      applyDdil: false,
      report: `JICO, this is ${callsign(input)}. ${path} jamming at ${whenOf(input)}. Width: ${width}. I made one check and I am staying off that path. I am not searching for the source. Request the shift.`,
      order: `All stations, this is JICO. ${path} is jammed for ${width}. Affected units, stop transmitting on that path and stand by to shift to ${alt} on my mark. Units that still hold a clean path, stay there. Do not search for the source.`,
    });
  }

  function singleCase(input) {
    const unit = unitOf(input);
    const link = LINK[input.link];
    const noEntry = input.symptom === "no-entry";
    const sees = input.sees === "yes";
    return pack(input, {
      tag: "Single ship reentry",
      headline: sees
        ? `Reenter ${unit} on the existing ${link} net. The group holds.`
        : `Confirm with another ship that ${link} is still up, then reenter ${unit} on that net.`,
      assessment: noEntry
        ? `${unit} never completed entry. The rest of the group should already be the net ${unit} is trying to join.`
        : `${unit} dropped from ${link}. One ship out is a local reentry until another ship says the net itself is down.`,
      reasons: [
        "Width is one ship.",
        sees
          ? "This ship still sees other units, so the net is up."
          : "This ship sees nobody. Another ship has to confirm the net is up before anyone changes it.",
      ],
      now: [
        bridge(unit, link),
        "Send the voice report. Include sync state if the terminal gives it.",
        "Check the local path that does not change the architecture: terminal power and status, antenna and line of sight, and time quality.",
        "Use the ship's spare terminal or antenna path only if the combat-systems procedure already allows it, and tell JICO before you reenter.",
      ],
      direct: [
        sees
          ? "JICO directs every other ship to hold the current net, reference, and control station."
          : "JICO asks two other ships whether they still hold the net and whether they see this unit.",
        `JICO names one ship to watch for ${unit} and to confirm it on voice.`,
        "If those ships are also down, JICO stops this single-ship plan and runs the group plan.",
      ],
      restore: [
        ...(sees
          ? []
          : ["Wait until JICO confirms two other ships still hold the net. Then reenter."]),
        ...entry(input, unit),
      ],
      replaceOnDenied: true,
      report: report(input, noEntry ? "cannot enter the net" : "was in the net and dropped"),
      order: sees
        ? `All stations, this is JICO. ${unit} is reentering ${link}. Everyone else hold the net and the time reference. ${unit}, call when synchronization is complete. Nearest unit, confirm you see ${unit}.`
        : `All stations, this is JICO. ${unit} is down on ${link} and sees nobody. Two stations report whether you still hold the net. Hold your settings until I say otherwise.`,
    });
  }

  function perspective(input, plan) {
    if (input.perspective === "jico") {
      return {
        nowTitle: "JICO, direct this now",
        now: plan.direct,
        directTitle: "Order the reporting CIC to do this",
        direct: plan.now,
      };
    }
    return {
      nowTitle: "CIC, do this now",
      now: plan.now,
      directTitle: "What you need from JICO",
      direct: plan.direct,
    };
  }

  function detect(text, current) {
    const raw = (text || "").trim();
    const t = raw.toLowerCase();
    const gaps = [];
    const input = {
      perspective: (current && current.perspective) || "cic",
      unit: "",
      when: (current && current.when) || "",
      link: "link16",
      scope: "one",
      duty: "pu",
      symptom: "dropped",
      ddil: "unknown",
      sees: "unknown",
      timeQuality: "unknown",
      alternate: "unspecified",
      notes: "",
    };

    if (!raw) {
      return { input, findings: [], gaps: ["Describe the problem first."], empty: true };
    }

    const linkHits = [];
    if (/\blink\s*16\b|\bl16\b|\bjtids\b|\bmids\b/.test(t)) linkHits.push("link16");
    if (/\blink\s*11\b|\bl11\b/.test(t)) linkHits.push("link11");
    if (/\blink\s*22\b|\bl22\b/.test(t)) linkHits.push("link22");
    if (/\bjreap\b|\brange extension\b|\bextension path\b|\bbearer\b/.test(t)) linkHits.push("jreap");
    const satWord = "satellite|satcom|sat com";
    const satDown =
      new RegExp(`\\b(${satWord})\\b.{0,24}\\b(down|denied|lost|failed|out)\\b`).test(t) ||
      new RegExp(`\\b(down|denied|lost|failed)\\b.{0,24}\\b(${satWord})\\b`).test(t);
    const backupSat = /\b(backup|second|secondary|alternate) (satellite|satcom)\b/.test(t);
    const useSatellite =
      !satDown &&
      (/\b(shift|move|put|sending|send).{0,24}\b(satellite|satcom)\b/.test(t) ||
        /\bon the satellite\b/.test(t) ||
        /\bno line of sight\b/.test(t) ||
        /\bover the horizon\b/.test(t) ||
        /\bbeyond line of sight\b/.test(t));

    const jammed = /\bjamm?(?:ed|ing)\b|\binterference\b/.test(t);

    if (linkHits.length === 0 && !satDown) {
      gaps.push(jammed ? "No path named. Treating this as Link 16 jamming until you name the path." : "No link named. Using Link 16 until you change it.");
    } else if (
      linkHits.includes("jreap") &&
      /\b(extension|jreap|bearer)\b/.test(t) &&
      /\b(down|denied|lost|failed)\b/.test(t)
    ) {
      input.link = "jreap";
    } else if (linkHits.length) {
      input.link = linkHits[linkHits.length - 1];
    }

    if (/\bi am the jico\b|\bthis is jico\b|\bjico cell\b/.test(t)) input.perspective = "jico";
    else if (/\bcic report\b|\bthis is uss\b|\bship cic\b/.test(t)) input.perspective = "cic";

    const uss = raw.match(/\bUSS\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){0,2})/);
    const hull = raw.match(/\b((?:DDG|CG|CVN|LHD|LHA|LPD|LSD|FFG|LCS)[\s-]?\d{1,4})\b/i);
    if (uss) input.unit = `USS ${uss[1]}`;
    else if (hull) input.unit = hull[1].toUpperCase().replace(/\s+/, "-");
    else gaps.push("No ship name. The report will say “the reporting unit.”");

    const clock = t.match(/\bat\s+(\d{1,2}):(\d{2})\b/);
    const zulu = t.match(/\b(?:at|time)\s+([0-2]\d[0-5]\d)\b/);
    if (clock) input.when = `${clock[1].padStart(2, "0")}:${clock[2]}`;
    else if (zulu) input.when = `${zulu[1].slice(0, 2)}:${zulu[1].slice(2)}`;

    let special = "";
    if (jammed) {
      input.symptom = "jammed";
      input.ddil = "denied";
      special = "jam";
    }

    if (
      /\b(whole|entire)\b.{0,24}\b(group|force|strike)\b|\ball (ships|units|stations)\b|\bevery ship\b|\bgroup is down\b/.test(
        t
      )
    ) {
      input.scope = "group";
    } else if (
      /\b(several|multiple|two|three|few)\b.{0,20}\b(ships|units|stations)\b|\bpart of the (group|net)\b|\bsome units\b/.test(
        t
      )
    ) {
      input.scope = "several";
    } else if (/\bone ship\b|\bsingle ship\b|\bonly this (ship|unit)\b|\bjust (us|this ship)\b/.test(t)) {
      input.scope = "one";
    } else if (special === "jam") {
      input.scope = "several";
      gaps.push("How many ships is not stated. Treating the jamming as more than one ship until JICO confirms the width.");
    } else {
      input.scope = "one";
      gaps.push("How many ships is not stated. Using one ship.");
    }

    const saysNcs = /\bnet control\b|\bncs\b|\bdncs\b|\bcontrol station\b/.test(t);
    const saysNtr = /\bnetwork time reference\b|\btime reference\b|\bntr\b/.test(t);
    const saysForward = /\bforwarding unit\b|\bforwarder\b|\bgateway\b|\bjre node\b/.test(t);
    if (saysNcs && (input.link === "link11" || input.link === "link22")) input.duty = "ncs";
    else if (saysNtr && input.link === "link16") input.duty = "ntr";
    else if (saysNtr && (input.link === "link11" || input.link === "link22")) input.duty = "ncs";
    else if (saysForward) input.duty = input.link === "jreap" && /\bjre node\b/.test(t) ? "jre" : "fju";
    else if (input.link === "jreap") input.duty = "jre";

    if (
      /\b(see|sees|seeing) (no|nobody|no one|none)\b|\bno other units\b|\bsee nobody\b|\bdon't see (anyone|any other)\b|\bcannot see (any|other)\b/.test(
        t
      )
    ) {
      input.sees = "no";
    } else if (/\bstill see\b|\bcan see other\b|\bsee other units\b|\bsees other\b|\bradio link is up\b|\bstill on the net\b/.test(t)) {
      input.sees = "yes";
    }

    if (/\btime (quality )?(is )?lost\b|\blost time\b|\bno (system )?time\b/.test(t)) input.timeQuality = "lost";
    else if (/\btime (quality )?(is )?suspect\b|\bsuspect time\b|\bbad time\b|\bclock is off\b/.test(t)) input.timeQuality = "suspect";
    else if (/\btime (quality )?(is )?(good|ok|okay)\b/.test(t)) input.timeQuality = "good";

    if (/\bdenied\b|\bjammed\b|\bno path\b/.test(t)) input.ddil = "denied";
    else if (/\bintermittent\b|\bin and out\b|\bcomes and goes\b|\bdropping in and out\b/.test(t)) input.ddil = "intermittent";
    else if (/\blimited\b|\bbandwidth\b|\bcongested\b/.test(t)) input.ddil = "limited";
    else if (/\bdisrupted\b|\berror rate\b|\bgarbled\b|\bhigh error\b/.test(t)) input.ddil = "disrupted";

    if (satDown) {
      input.link = "jreap";
      input.alternate = backupSat ? "backup-sat" : /\bvoice picture\b|\bto voice\b|\bvoice net\b/.test(t) ? "voice" : "backup-sat";
      if (!backupSat && input.alternate === "backup-sat") {
        gaps.push("Backup satellite was not named. The order uses the second satellite path in the link plan.");
      }
    } else if (useSatellite && input.link !== "jreap") {
      input.alternate = backupSat ? "backup-sat" : "satellite";
    } else if (/\bvoice picture\b|\bto voice\b|\bvoice net\b/.test(t)) input.alternate = "voice";
    else if (/\b(shift to|onto) link\s*11\b/.test(t) && input.link !== "link11") input.alternate = "link11";
    else if (/\b(shift to|onto) link\s*16\b/.test(t) && input.link !== "link16") input.alternate = "link16";
    else if (/\b(shift to|onto) link\s*22\b/.test(t) && input.link !== "link22") input.alternate = "link22";
    else if (/\bjreap backup\b|\bbackup bearer\b/.test(t) && input.link !== "jreap") input.alternate = "jreap";

    const ntrDown =
      /\b(time reference|ntr)\b.{0,40}\b(down|lost|dropped|out)\b/.test(t) ||
      /\b(down|lost|dropped)\b.{0,30}\b(time reference|ntr)\b/.test(t);
    if (special) {
      /* Radar and jamming keep the reading set above. */
    } else if (!/\bnot (the |a )?(display|console|screen)\b/.test(t) && /\bdisplay\b|\bconsole\b|\brepeater\b|\bscreen is dark\b|\bblank screen\b|\bdark screen\b/.test(t)) {
      input.symptom = "display";
    } else if (/\bduplicate\b|\bconflicting tracks\b|\bdouble tracks\b|\btwo tracks\b/.test(t)) {
      input.symptom = "conflict";
    } else if (useSatellite && input.link !== "jreap") {
      input.symptom = "via-sat";
    } else if (satDown || input.link === "jreap" || /\brange extension\b|\bextension (path|is|down)\b|\bbearer\b/.test(t)) {
      input.symptom = "extension";
    } else if (ntrDown) {
      input.symptom = "time-ref";
    } else if (/\brelay\b/.test(t) && /\b(down|lost|failed|out)\b/.test(t)) {
      input.symptom = "relay";
    } else if (/\bfrozen\b|\bnot updating\b|\bstale\b|\bpicture stuck\b|\btracks stuck\b/.test(t)) {
      input.symptom = "stale";
    } else if (/\b(cannot|can't|unable to) (enter|sync|get in)\b|\bno entry\b|\bout of sync\b|\bfine sync\b/.test(t)) {
      input.symptom = "no-entry";
    } else if (/\bmissing\b|\bpartial\b|\bcan't see one\b/.test(t)) {
      input.symptom = "partial";
    } else if (/\bdropped\b|\blink down\b|\blost (the )?link\b|\bout of the net\b|\bfell out\b/.test(t)) {
      input.symptom = "dropped";
    } else {
      gaps.push("The failure itself is vague. Using a drop from the net.");
    }

    const tried = raw.match(/already (?:tried|done)[:,]?\s*(.+)/i);
    if (tried) input.notes = tried[1].trim();

    return present(input, gaps, "rules");
  }

  function present(input, gaps, source) {
    const scopeLabel = { one: "One ship", several: "Several ships", group: "Whole strike group" };
    const symptomLabel = {
      dropped: "Dropped from the net",
      "no-entry": "Cannot enter the net",
      partial: "Up, but missing units",
      stale: "Picture frozen",
      extension: "Extension path down",
      "via-sat": "Shift the far unit to satellite",
      "time-ref": "Time reference down",
      relay: "Relay lost",
      display: "Display casualty",
      conflict: "Duplicate tracks",
      jammed: "Spectrum jamming",
    };
    const dutyLabelText = {
      pu: "Participating unit",
      ntr: "Network time reference",
      fju: "Forwarding unit",
      ncs: "Net control",
      jre: "JRE node",
      jico: "JICO cell",
    };
    const findings = [
      LINK[input.link],
      scopeLabel[input.scope],
      dutyLabelText[input.duty] || "Participating unit",
      symptomLabel[input.symptom] || input.symptom,
    ];
    if (input.unit) findings.unshift(input.unit);
    if (input.sees === "yes") findings.push("Still sees other units");
    if (input.sees === "no") findings.push("Sees no other units");
    if (input.timeQuality !== "unknown") findings.push(`Time ${input.timeQuality}`);
    if (input.ddil !== "unknown") findings.push(input.ddil[0].toUpperCase() + input.ddil.slice(1));
    if (input.alternate !== "unspecified") findings.push(`Shift to ${ALT[input.alternate] || input.alternate}`);
    return { input, findings, gaps: gaps || [], empty: false, source: source || "rules" };
  }

  function build(input) {
    const key = diagnose(input);
    const builders = {
      display: displayCase,
      conflict: conflictCase,
      "via-satellite": viaSatelliteCase,
      extension: extensionCase,
      time: timeCase,
      ncs: ncsCase,
      relay: relayCase,
      group: groupCase,
      stale: staleCase,
      "local-time": localTimeCase,
      partial: partialCase,
      jam: jamCase,
      single: singleCase,
    };
    const plan = builders[key](input);
    const sides = perspective(input, plan);
    return {
      tag: plan.tag,
      headline: plan.headline,
      assessment: plan.assessment,
      reasons: plan.reasons,
      nowTitle: sides.nowTitle,
      now: sides.now,
      directTitle: sides.directTitle,
      direct: sides.direct,
      restore: plan.restore,
      prove: plan.prove,
      fallback: plan.fallback,
      report: plan.report,
      order: plan.order,
      linkName: LINK[input.link],
      key,
    };
  }

  return { build, detect, present, LINK, ALT };
})();
