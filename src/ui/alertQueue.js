const TIER_ORDER = { CRITICAL: 0, WARNING: 1, CAUTION: 2, INFORMATIONAL: 3 };

export class AlertQueue {
  constructor(root, handlers) {
    this.root = root;
    this.handlers = handlers;
    this.root.innerHTML = `
      <div class="panel-head">
        <h2>Alert triage</h2>
        <button type="button" id="ack-alert">ACKNOWLEDGE</button>
      </div>
      <div class="alert-list" role="log" aria-live="assertive" aria-relevant="additions"></div>
    `;
    this.list = root.querySelector(".alert-list");
    root.querySelector("#ack-alert").addEventListener("click", () => handlers.ack());
  }

  render(alerts) {
    const rows = [...alerts].sort((a, b) => TIER_ORDER[a.tier] - TIER_ORDER[b.tier] || b.time - a.time);
    this.list.replaceChildren();
    if (!rows.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "No active alerts.";
      this.list.append(empty);
      return;
    }
    for (const alert of rows) {
      const article = document.createElement("article");
      article.className = `alert ${alert.tier.toLowerCase()}${alert.acked ? " acked" : ""}`;
      article.dataset.code = alert.code;
      const title = document.createElement("h3");
      title.textContent = `${alert.tier}  ${alert.title}`;
      const detail = document.createElement("p");
      detail.textContent = alert.detail;
      const time = document.createElement("time");
      time.dateTime = new Date(alert.time).toISOString();
      time.textContent = new Date(alert.time).toISOString().slice(11, 19) + "Z";
      article.append(title, detail, time);
      this.list.append(article);
    }
  }
}

export class AlertHistory {
  constructor(root) {
    this.root = root;
    this.root.innerHTML = `
      <div class="panel-head">
        <h2>Alert history</h2>
        <label>Search
          <input id="alert-search" type="search" placeholder="Missile, link, clutter" autocomplete="off">
        </label>
      </div>
      <div class="history-list"></div>
    `;
    this.list = root.querySelector(".history-list");
    this.input = root.querySelector("#alert-search");
    this.input.addEventListener("input", () => this.onSearch?.(this.input.value));
  }

  render(rows) {
    this.list.replaceChildren();
    if (!rows.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "No matching alerts in the local log.";
      this.list.append(empty);
      return;
    }
    for (const alert of rows) {
      const row = document.createElement("p");
      row.textContent = `${new Date(alert.time).toISOString().slice(11, 19)}Z  ${alert.tier}  ${alert.title}  ${alert.acked ? "ACK" : "OPEN"}${alert.cleared ? "  CLEARED" : ""}`;
      this.list.append(row);
    }
  }
}
