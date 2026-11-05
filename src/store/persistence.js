const DB_NAME = "cic-watchstander";
const DB_VERSION = 1;
const ALERT_CAP = 400;

function request(req) {
  return new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function transactionDone(tx) {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error);
  });
}

export class WatchStore {
  constructor() {
    this.db = null;
    this.memoryAlerts = [];
    this.memoryCommands = [];
    this.ready = this._open();
  }

  async _open() {
    if (!("indexedDB" in window)) return;
    const db = await new Promise((resolve, reject) => {
      const open = indexedDB.open(DB_NAME, DB_VERSION);
      open.onupgradeneeded = () => {
        const database = open.result;
        if (!database.objectStoreNames.contains("alerts")) {
          const alerts = database.createObjectStore("alerts", { keyPath: "id" });
          alerts.createIndex("byTime", "time");
        }
        if (!database.objectStoreNames.contains("commands")) {
          database.createObjectStore("commands", { keyPath: "id" });
        }
      };
      open.onsuccess = () => resolve(open.result);
      open.onerror = () => reject(open.error);
    });
    this.db = db;
  }

  async appendAlert(alert) {
    await this.ready;
    if (!this.db) {
      this.memoryAlerts.push(alert);
      if (this.memoryAlerts.length > ALERT_CAP) this.memoryAlerts.shift();
      return;
    }
    const tx = this.db.transaction("alerts", "readwrite");
    const store = tx.objectStore("alerts");
    store.put(alert);
    await transactionDone(tx);
    await this._trim();
  }

  async updateAlert(alert) {
    await this.ready;
    if (!this.db) {
      const index = this.memoryAlerts.findIndex((item) => item.id === alert.id);
      if (index >= 0) this.memoryAlerts[index] = alert;
      return;
    }
    const tx = this.db.transaction("alerts", "readwrite");
    tx.objectStore("alerts").put(alert);
    await transactionDone(tx);
  }

  async search(query) {
    await this.ready;
    const needle = query.trim().toLowerCase();
    const rows = await this._allAlerts();
    return rows
      .filter((alert) => {
        if (!needle) return true;
        return `${alert.tier} ${alert.code} ${alert.title} ${alert.detail}`.toLowerCase().includes(needle);
      })
      .sort((a, b) => b.time - a.time)
      .slice(0, 80);
  }

  async enqueue(command) {
    await this.ready;
    if (!this.db) {
      this.memoryCommands.push(command);
      return;
    }
    const tx = this.db.transaction("commands", "readwrite");
    tx.objectStore("commands").put(command);
    await transactionDone(tx);
  }

  async pending() {
    await this.ready;
    const rows = await this._allCommands();
    return rows.filter((command) => !command.synced).sort((a, b) => a.time - b.time);
  }

  async markSynced(ids, linkClock) {
    await this.ready;
    const rows = await this._allCommands();
    const pendingIds = new Set(ids);
    for (const command of rows) {
      if (!pendingIds.has(command.id)) continue;
      command.synced = true;
      command.syncedAt = Date.now();
      command.clock = { ...command.clock, LINK: linkClock };
      if (!this.db) continue;
    }
    if (!this.db) return;
    const tx = this.db.transaction("commands", "readwrite");
    const store = tx.objectStore("commands");
    rows.filter((command) => pendingIds.has(command.id)).forEach((command) => store.put(command));
    await transactionDone(tx);
  }

  async _allAlerts() {
    if (!this.db) return this.memoryAlerts.slice();
    const tx = this.db.transaction("alerts", "readonly");
    return request(tx.objectStore("alerts").getAll());
  }

  async _allCommands() {
    if (!this.db) return this.memoryCommands.slice();
    const tx = this.db.transaction("commands", "readonly");
    return request(tx.objectStore("commands").getAll());
  }

  async _trim() {
    const countTx = this.db.transaction("alerts", "readonly");
    const count = await request(countTx.objectStore("alerts").count());
    if (count <= ALERT_CAP) return;
    const tx = this.db.transaction("alerts", "readwrite");
    const index = tx.objectStore("alerts").index("byTime");
    let extra = count - ALERT_CAP;
    await new Promise((resolve, reject) => {
      const cursor = index.openCursor();
      cursor.onerror = () => reject(cursor.error);
      cursor.onsuccess = () => {
        const step = cursor.result;
        if (!step || extra <= 0) {
          resolve();
          return;
        }
        step.delete();
        extra -= 1;
        step.continue();
      };
    });
    await transactionDone(tx);
  }
}
