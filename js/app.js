(() => {
  const form = document.getElementById("coa-form");
  const output = document.getElementById("output");
  const duty = document.getElementById("duty");
  const link = document.getElementById("link");
  const alternate = document.getElementById("alternate");

  const dutyOptions = {
    link16: [
      ["pu", "Participating unit"],
      ["ntr", "Network time reference"],
      ["fju", "Forwarding unit"],
      ["jico", "JICO cell"],
    ],
    link11: [
      ["pu", "Participating unit"],
      ["ncs", "Net control station"],
      ["fju", "Forwarding unit"],
      ["jico", "JICO cell"],
    ],
    link22: [
      ["pu", "Participating unit"],
      ["ncs", "Network control station"],
      ["fju", "Forwarding unit"],
      ["jico", "JICO cell"],
    ],
    jreap: [
      ["jre", "JRE node"],
      ["fju", "Forwarding unit"],
      ["pu", "Far-end unit"],
      ["jico", "JICO cell"],
    ],
  };

  const alternateOptions = [
    ["unspecified", "Use the OPTASK LINK alternate"],
    ["link16", "Link 16"],
    ["link11", "Link 11"],
    ["link22", "Link 22"],
    ["jreap", "JREAP backup bearer"],
    ["satellite", "Satellite"],
    ["backup-sat", "Backup satellite"],
    ["voice", "Voice picture"],
  ];

  function fillSelect(select, options, current) {
    const keep = options.some(([value]) => value === current) ? current : options[0][0];
    select.replaceChildren();
    options.forEach(([value, label]) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      select.append(option);
    });
    select.value = keep;
  }

  function syncSelects() {
    fillSelect(duty, dutyOptions[link.value], duty.value);
    fillSelect(
      alternate,
      alternateOptions.filter(([value]) => value === "unspecified" || value === "voice" || value !== link.value),
      alternate.value
    );
  }

  function read() {
    const data = new FormData(form);
    return {
      perspective: data.get("perspective"),
      unit: data.get("unit"),
      when: data.get("when"),
      link: data.get("link"),
      scope: data.get("scope"),
      duty: data.get("duty"),
      symptom: data.get("symptom"),
      ddil: data.get("ddil"),
      sees: data.get("sees"),
      timeQuality: data.get("timeQuality"),
      alternate: data.get("alternate"),
      notes: data.get("notes"),
    };
  }

  const problemPresets = {
    single: "CIC report. USS Sample dropped out of Link 16 at 16:40. One ship. We see nobody. Time quality is good.",
    ntr: "I am the JICO. USS Sample is the network time reference and dropped off Link 16. Time quality is lost. They see no other units.",
    ncs: "I am the JICO. USS Sample is Link 11 net control and the control station dropped. One ship. Time quality is good.",
    jreap: "I am the JICO. The JREAP range extension is denied. Several ships. USS Sample is the forwarding unit. The radio link is up and they still see other units. Shift to a voice picture.",
    group: "I am the JICO. The whole strike group Link 16 picture is frozen. Tracks are not updating.",
    display: "CIC report. USS Sample, one ship. The Link 16 display is dark. It may be only the console.",
    satellite: "I am the JICO. The primary satellite is down. Link 16 is still up and ships still see each other. USS Sample is the forwarding unit. Move the extension to the backup satellite.",
    horizon: "USS Sample has no line of sight on Link 16 and sees nobody. Put them on the satellite. One ship.",
    jam: "I am the JICO. Link 16 is jammed. Several ships dropped at the same time. Interference on the net. Shift to a voice picture.",
  };

  let detection = null;
  let detectionStale = false;

  function list(items, key, checked) {
    const ol = document.createElement("ol");
    ol.className = "steps";
    items.forEach((text, index) => {
      const li = document.createElement("li");
      const check = document.createElement("input");
      check.type = "checkbox";
      check.id = `${key}-${index}`;
      check.checked = checked.has(text);
      const span = document.createElement("label");
      span.htmlFor = check.id;
      span.textContent = text;
      li.append(check, span);
      ol.append(li);
    });
    return ol;
  }

  function section(title, node) {
    const wrap = document.createElement("section");
    wrap.className = "block";
    const h = document.createElement("h3");
    h.textContent = title;
    wrap.append(h, node);
    return wrap;
  }

  function scriptBlock(title, text, id) {
    const wrap = document.createElement("section");
    wrap.className = "block script";
    const head = document.createElement("div");
    head.className = "script-head";
    const h = document.createElement("h3");
    h.textContent = title;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "copy";
    button.textContent = "Copy";
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(text);
        button.textContent = "Copied";
      } catch {
        button.textContent = "Select the text";
      }
      setTimeout(() => {
        button.textContent = "Copy";
      }, 1600);
    });
    head.append(h, button);
    const p = document.createElement("p");
    p.id = id;
    p.textContent = text;
    wrap.append(head, p);
    return wrap;
  }

  function applyDetected(result) {
    detection = result;
    detectionStale = false;
    if (result.empty) {
      render();
      return;
    }
    const input = result.input;
    ["perspective", "unit", "when", "link", "scope", "symptom", "ddil", "sees", "timeQuality", "alternate", "notes"].forEach(
      (key) => {
        const field = form.elements[key];
        if (field && input[key] !== undefined) field.value = input[key];
      }
    );
    syncSelects();
    if (input.duty && duty.querySelector(`option[value="${input.duty}"]`)) duty.value = input.duty;
    render();
  }

  const SOFT = new Set(["ddil", "sees", "timeQuality", "alternate"]);
  let appModel = "";

  function mergeModel(rules, modelInput) {
    const input = { ...rules.input };
    const locked = rules.input.symptom === "jammed";
    Object.entries(modelInput || {}).forEach(([key, value]) => {
      if (value === undefined || value === "") return;
      if (SOFT.has(key) && (value === "unknown" || value === "unspecified")) return;
      if (locked && (key === "link" || key === "symptom")) return;
      if (locked && key === "sees" && rules.input.sees === "unknown") return;
      input[key] = value;
    });
    return LinkCoa.present(input, [], "model");
  }

  async function runDetect() {
    await statusReady.catch(() => {});
    const text = form.elements.problem.value;
    const rules = LinkCoa.detect(text, read());
    if (rules.empty || !appModel) {
      if (!rules.empty && !appModel) {
        rules.gaps.unshift("The app model is not running. The onboard reader was used.");
      }
      applyDetected(rules);
      return;
    }
    const button = document.getElementById("detect");
    button.disabled = true;
    const previous = button.textContent;
    button.textContent = "Reading the report…";
    try {
      const response = await fetch("/api/interpret", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.error || "The language model did not answer.");
      applyDetected(mergeModel(rules, body.input));
    } catch (error) {
      const message = error && error.message ? error.message : "The language model did not answer.";
      rules.gaps.unshift(`${message} The onboard reader was used.`);
      applyDetected(rules);
    } finally {
      button.disabled = false;
      button.textContent = previous;
    }
  }

  function applyPreset(name) {
    const problem = problemPresets[name];
    if (!problem) return;
    form.elements.problem.value = problem;
    document.querySelectorAll(".presets button").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.preset === name));
    });
    runDetect();
  }

  function render() {
    const checked = new Set(
      [...output.querySelectorAll(".steps input:checked")].map((box) => box.nextElementSibling.textContent)
    );
    const plan = LinkCoa.build(read());
    output.replaceChildren();

    const head = document.createElement("header");
    head.className = "result-head";
    const tag = document.createElement("p");
    tag.className = "tag";
    tag.textContent = plan.tag;
    const h2 = document.createElement("h2");
    h2.textContent = plan.headline;
    const assessment = document.createElement("p");
    assessment.className = "assessment";
    assessment.textContent = plan.assessment;
    const why = document.createElement("ul");
    why.className = "reasons";
    plan.reasons.forEach((reason) => {
      const li = document.createElement("li");
      li.textContent = reason;
      why.append(li);
    });
    if (detection && !detection.empty) {
      const reading = document.createElement("p");
      reading.className = "reading";
      const label = document.createElement("strong");
      const who = detection.source === "model" ? "App model read this as. " : "Onboard reader. ";
      label.textContent = detectionStale ? "Adjusted reading. " : who;
      reading.append(label, document.createTextNode(detection.findings.join(" · ")));
      if (detection.gaps.length) {
        const gaps = document.createElement("ul");
        gaps.className = "gaps";
        detection.gaps.forEach((gap) => {
          const li = document.createElement("li");
          li.textContent = gap;
          gaps.append(li);
        });
        reading.append(gaps);
      }
      head.append(reading);
    }
    head.append(tag, h2, assessment, why);

    output.append(
      head,
      section(plan.nowTitle, list(plan.now, "now", checked)),
      section(plan.directTitle, list(plan.direct, "direct", checked)),
      section("Reestablish", list(plan.restore, "restore", checked)),
      scriptBlock("CIC report to JICO", plan.report, "report-text"),
      scriptBlock("JICO order to the group", plan.order, "order-text"),
      section("Prove the link is back", list(plan.prove, "prove", checked)),
      section("If it is still down", list(plan.fallback, "fallback", checked))
    );
  }

  link.addEventListener("change", () => {
    syncSelects();
    render();
  });
  form.addEventListener("input", (event) => {
    if (event.target.name === "problem") return;
    if (detection) detectionStale = true;
    render();
  });
  form.addEventListener("change", (event) => {
    if (event.target.name === "problem") return;
    if (detection) detectionStale = true;
    render();
  });
  document.getElementById("detect").addEventListener("click", runDetect);
  form.elements.problem.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      runDetect();
    }
  });
  document.querySelector(".presets").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-preset]");
    if (!button) return;
    applyPreset(button.dataset.preset);
  });

  const when = form.elements.when;
  if (!when.value) {
    const now = new Date();
    when.value = `${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}`;
  }
  const status = document.getElementById("model-status");
  const statusReady = fetch("/api/status")
    .then((response) => response.json())
    .then((body) => {
      appModel = body.model || "";
      status.textContent = appModel
        ? `App model ready: ${appModel}`
        : "App model is not running. Start Ollama with llama3.2. Until then the onboard reader is used.";
    })
    .catch(() => {
      appModel = "";
      status.textContent = "Open this page from the Link COA server so the app model can be reached. The onboard reader still works.";
    });
  syncSelects();
  render();
})();
