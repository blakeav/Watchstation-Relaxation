export class LinkTelemetry {
  constructor(root) {
    this.root = root;
    this.root.innerHTML = `
      <div class="panel-head">
        <h2>DDIL link</h2>
        <p class="condition" id="link-condition">CONNECTING</p>
      </div>
      <p class="mode" id="link-mode"></p>
      <dl class="metrics">
        <div><dt>Bandwidth</dt><dd id="link-bw">—</dd></div>
        <div><dt>Packet loss</dt><dd id="link-loss">—</dd></div>
        <div><dt>Latency</dt><dd id="link-lat">—</dd></div>
        <div><dt>Peers</dt><dd id="link-peers">—</dd></div>
      </dl>
      <p class="clock" id="link-clock"></p>
      <p class="queue" id="link-queue"></p>
    `;
  }

  render(link, queueCount) {
    if (!link) return;
    const condition = this.root.querySelector("#link-condition");
    condition.textContent = link.condition;
    condition.dataset.state = link.condition.startsWith("DISCONNECTED")
      ? "down"
      : link.condition === "DEGRADED"
        ? "degraded"
        : "up";
    this.root.querySelector("#link-mode").textContent = link.modeLabel;
    this.root.querySelector("#link-bw").textContent = `${link.bandwidthKbps} kbps`;
    this.root.querySelector("#link-loss").textContent = `${Math.round(link.dropRate * 100)}%`;
    this.root.querySelector("#link-lat").textContent = link.latencyMs == null ? "—" : `${link.latencyMs} ms`;
    this.root.querySelector("#link-peers").textContent = String(link.peers);
    const clock = link.clock || {};
    this.root.querySelector("#link-clock").textContent = `Vector clock  CIC-LOCAL ${clock["CIC-LOCAL"] || 0}   LINK ${clock.LINK || 0}`;
    this.root.querySelector("#link-queue").textContent =
      queueCount > 0 ? `${queueCount} order${queueCount === 1 ? "" : "s"} queued locally` : "No queued orders";
  }
}
