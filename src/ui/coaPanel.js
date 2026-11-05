const ACTION = {
  0: "CONTESTED FACE",
  1: "SPATIAL NULL",
  2: "MANEUVER",
  3: "BURN-THROUGH",
};

export class CoaPanel {
  constructor(root, handlers) {
    this.root = root;
    this.root.innerHTML = `
      <div class="panel-head">
        <h2>Course of action</h2>
        <p class="action-code" id="coa-action"></p>
      </div>
      <p class="order" id="coa-order">Calculating</p>
      <p class="speed-line" id="coa-speed"></p>
      <p class="burn" id="coa-burn"></p>
      <p class="rationale" id="coa-why"></p>
      <p class="sensors" id="coa-sensors"></p>
      <p class="coa-status" id="coa-status"></p>
      <div class="actions">
        <button type="button" id="accept-coa">ACCEPT &amp; EXECUTE</button>
        <button type="button" id="reject-coa">REJECT</button>
        <button type="button" id="recalc-coa">RE-CALCULATE</button>
      </div>
    `;
    root.querySelector("#accept-coa").addEventListener("click", () => handlers.accept());
    root.querySelector("#reject-coa").addEventListener("click", () => handlers.reject());
    root.querySelector("#recalc-coa").addEventListener("click", () => handlers.recalculate());
  }

  render(view) {
    const coa = view.coa;
    this.root.querySelector("#coa-action").textContent = coa ? ACTION[coa.action] || "" : "";
    this.root.querySelector("#coa-order").textContent = coa ? coa.order : "Waiting for the engine";
    const speed = this.root.querySelector("#coa-speed");
    if (coa) {
      const delta = coa.speedDelta ? `  (${coa.speedDelta > 0 ? "+" : ""}${coa.speedDelta} KT)` : "";
      speed.textContent = `${coa.speedText}${delta}`;
    } else speed.textContent = "";
    this.root.querySelector("#coa-burn").textContent = coa ? `Notional burn-through ${coa.burnThroughNm.toFixed(0)} NM` : "";
    this.root.querySelector("#coa-why").textContent = coa ? coa.rationale : "";
    this.root.querySelector("#coa-sensors").textContent = coa
      ? coa.sensors.includes(coa.cfar) ? coa.sensors : `${coa.sensors} ${coa.cfar}.`
      : "";
    this.root.querySelector("#coa-status").textContent = view.status || "";
  }
}
