/**
 * FranklinWH Modbus Bridge — Frontend App State
 * Alpine.js global store + utilities
 */

// ── Utility ─────────────────────────────────────────────────
const fmt = {
  kw:  (v) => v != null ? `${(v / 1000).toFixed(2)}` : '--',
  w:   (v) => v != null ? `${Math.round(v)}` : '--',
  soc: (v) => v != null ? `${Math.round(v)}` : '--',
  pct: (v) => v != null ? `${Math.round(v)}%` : '--',
  kwh: (v) => v != null ? `${(v / 1000).toFixed(1)}` : '--',
  ago: (ts) => {
    if (ts == null) return 'never';
    const s = Math.floor(Date.now() / 1000 - ts);
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s/60)}m ago`;
    return `${Math.floor(s/3600)}h ago`;
  },
};

function socRingOffset(soc) {
  // stroke-dasharray = 251.3 (2*pi*40)
  if (soc == null) return 251.3;
  const pct = Math.min(100, Math.max(0, soc)) / 100;
  return 251.3 * (1 - pct);
}

function socColour(soc) {
  if (soc == null) return 'var(--text-muted)';
  if (soc >= 80) return 'var(--ok)';
  if (soc >= 30) return 'var(--accent)';
  if (soc >= 15) return 'var(--warn)';
  return 'var(--danger)';
}

function powerColour(watts, type) {
  if (watts == null) return 'var(--text-muted)';
  // Battery: positive = discharging (amber/warn), negative = charging (green/ok)
  if (type === 'battery') return watts > 0 ? 'var(--warn)' : watts < 0 ? 'var(--ok)' : 'var(--text-muted)';
  // Grid: positive = importing (red/danger), negative = exporting (green/ok)
  if (type === 'grid') return watts > 0 ? 'var(--danger)' : watts < 0 ? 'var(--ok)' : 'var(--text-muted)';
  if (type === 'solar') return watts > 0 ? 'var(--solar)' : 'var(--text-muted)';
  return 'var(--text-primary)';
}

// ── Polling ──────────────────────────────────────────────────
async function fetchJSON(url, options = {}) {
  try {
    if (options.body && !options.headers) {
      options.headers = { 'Content-Type': 'application/json' };
    }
    const r = await fetch(url, options);
    if (!r.ok) {
      let detail = `HTTP ${r.status}`;
      try {
        const j = await r.json();
        if (j && j.detail) detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail);
      } catch (_) {}
      throw new Error(detail);
    }
    return await r.json();
  } catch (e) {
    console.warn('[Bridge]', url, e.message);
    return { ok: false, error: e.message };
  }
}

// ── Main Alpine App ──────────────────────────────────────────
document.addEventListener('alpine:init', () => {

  Alpine.store('app', {
    // Tab state
    activeTab: 'dashboard',

    // Sidebar state
    sidebarCollapsed: false,

    // Connection state
    connected: false,
    lastPollTs: null,
    version: '--',
    env: '',

    // Data cache
    points: {},
    quality: null,

    // Toast notifications
    toasts: [],

    // Refresh interval handle
    _interval: null,

    init() {
      this.refresh();
      this._interval = setInterval(() => this.refresh(), 10000);
    },

    setTab(tab) {
      this.activeTab = tab;
      // Notify tab components
      window.dispatchEvent(new CustomEvent('tab:changed', { detail: { tab } }));
    },

    async refresh() {
      const data = await fetchJSON('api/points');
      if (data && !data.error) {
        this.points = data.points || {};
        this.quality = data.quality;
        this.lastPollTs = data.ts;
        this.connected = true;
      } else {
        this.connected = false;
      }

      const health = await fetchJSON('api/health');
      if (health && !health.error) {
        this.version = health.version || '--';
        this.env = health.environment || '';
      }
    },

    toast(message, level = 'info') {
      const id = Date.now();
      this.toasts.push({ id, message, level });
      setTimeout(() => {
        this.toasts = this.toasts.filter(t => t.id !== id);
      }, 4000);
    },

    // Convenience getters for templates
    // Keys must match what GET /api/points actually returns
    get batteryPowerW() { return this.points.battery_power_w ?? null; },
    get gridPowerW() { return this.points.grid_power_w ?? null; },
    get solarPowerW() { return this.points.total_solar ?? null; },
    get homePowerW() { return this.points.home_load_ext ?? null; },
    get batterySoc() { return this.points.soc ?? null; },
  });

});
