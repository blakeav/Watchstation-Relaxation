export class MockControls {
  constructor(root, handlers) {
    this.root = root;
    const buttons = [
      ["latency", "Latency spike"],
      ["loss", "Packet loss"],
      ["ooo", "Out-of-order packet"],
      ["disconnect", "Disconnect link"],
      ["mode", "Cycle link mode"],
      ["noise", "Noise jammer"],
      ["deception", "Deception jammer"],
      ["weather", "Squall clutter"],
      ["blind", "Radar blind zone"],
      ["missile", "Missile track"],
      ["density", "Track spike"],
    ];
    this.root.innerHTML = `
      <div class="panel-head"><h2>Training injects</h2></div>
      <div class="injects">
        ${buttons.map(([id, label]) => `<button type="button" data-inject="${id}" aria-pressed="false">${label}</button>`).join("")}
      </div>
    `;
    this.root.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-inject]");
      if (!button) return;
      handlers[button.dataset.inject]?.(button);
    });
  }

  setPressed(id, on) {
    const button = this.root.querySelector(`[data-inject="${id}"]`);
    if (button) button.setAttribute("aria-pressed", String(on));
  }
}
