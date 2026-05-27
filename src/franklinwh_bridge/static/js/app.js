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

    // Theme
    theme: localStorage.getItem('fwh-theme') || 'dark',

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

    // Release modal
    showReleaseModal: false,
    releasing: false,

    // Refresh interval handle
    _interval: null,

    init() {
      // Apply saved theme
      document.documentElement.setAttribute('data-theme', this.theme);
      this.refresh();
      this._interval = setInterval(() => this.refresh(), 10000);
    },

    toggleTheme() {
      this.theme = this.theme === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', this.theme);
      localStorage.setItem('fwh-theme', this.theme);
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

    // ── Release control helpers ──────────────────────────
    get hasActiveCommand() {
      const state = this.points.battery_command_state;
      return state && state !== 'Not Active' && state !== 'Released';
    },

    get wsetEnabled() {
      return this.points.wset_enabled ?? null;
    },

    get isReleaseable() {
      // Releaseable if WSetEna is active OR a software command is running
      const wsetEna = this.wsetEnabled;
      const hwActive = wsetEna != null && wsetEna !== 0 && wsetEna !== '0';
      return hwActive || this.hasActiveCommand;
    },

    get releaseControlState() {
      const pts = this.points;
      return {
        wsetEna:    pts.wset_enabled ?? null,
        wsetPct:    pts.wset_pct ?? null,
        wsetPctRaw: pts.wset_pct_raw ?? null,
        wsetW:      pts.wset_watts ?? null,
        wsetMod:    pts.wset_mode ?? null,
        locRemCtl:  pts.loc_rem_ctl_name ?? null,
        cmdState:   pts.battery_command_state ?? 'Unknown',
        cmdPower:   pts.battery_command_power_w ?? null,
        watchdog:   pts.sw_watchdog_remain_s ?? null,
        elapsed:    pts.command_elapsed_s ?? null,
      };
    },

    async forceRelease() {
      if (this.releasing) return;
      this.releasing = true;

      try {
        // Send the release command through the command handler
        const data = await fetchJSON('api/command', {
          method: 'POST',
          body: JSON.stringify({ slug: 'battery_command', value: 'Release' }),
        });

        if (data && data.ok) {
          this.toast('Control released — battery returned to native mode', 'info');
        } else {
          this.toast(`Release failed: ${data?.result || data?.error || 'Unknown error'}`, 'error');
        }

        // Refresh state after short delay for registers to settle
        setTimeout(() => this.refresh(), 800);
      } catch (e) {
        this.toast(`Release error: ${e.message}`, 'error');
      } finally {
        this.releasing = false;
      }
    },

    // Convenience getters for templates
    // Keys must match what GET /api/points actually returns
    get batteryPowerW() { return this.points.battery_power_w ?? null; },
    get gridPowerW() { return this.points.grid_power_w ?? null; },
    get solarPowerW() { return this.points.total_solar ?? null; },
    get homePowerW() { return this.points.home_load_ext ?? null; },
    get batterySoc() { return this.points.soc ?? null; },
    get acTypeCode() { return this.points.ac_type_code ?? 0; },
    get acTypeName() {
      const names = { 0: 'Single Phase', 1: 'Split Phase', 2: 'Three Phase' };
      return names[this.acTypeCode] ?? 'Unknown';
    },
  });

});
