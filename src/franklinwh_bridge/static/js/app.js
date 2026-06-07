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

// ── Point-to-Source Map (SunSpec model.point notation) ────────
const POINT_SOURCES = {
  soc: '713.SoC', soh: '713.SoH',
  battery_power_w: '714.DCW', battery_dc_power_w: '714.DCW', battery_state: '714.DCW',
  battery_current_a: '714.DCA',
  total_solar: 'ext.15502', pv_proximal: 'ext.15503', pv_remote1: 'ext.15504', pv_remote2: 'ext.15505',
  home_load_ext: 'ext.16000',
  grid_power_w: '701.W', voltage_v: '701.LNV', current_a: '701.A', frequency_hz: '701.Hz',
  power_factor: '701.PF', grid_va: '701.VA', grid_var: '701.Var',
  connection_state: '701.ConnSt', inverter_state: '701.InvSt', grid_mode: '701.ConnSt',
  ambient_temp_c: '701.TmpAmb', cabinet_temp_c: '701.TmpCab',
  mode_name: 'ext.15507', wset_enabled: '704.WSetEna', wset_pct: '704.WSetPct',
  wset_watts: '704.WSet', wset_mode: '704.WSetMod',
  wset_revert_time_s: '704.WSetRvrtTms', wset_revert_remain_s: '704.WSetRvrtRem',
  loc_rem_ctl_name: '715.LocRemCtl',
  sw_watchdog_remain_s: 'virtual', command_elapsed_s: 'virtual', last_command_result: 'virtual',
  self_reserve_pct: 'ext.15508', tou_reserve_pct: 'ext.15509',
  wh_available: '713.WHAvail', wh_rating: '713.WHRtg',
  max_charge_rate_w: '702.WChaRteMaxRtg', max_discharge_rate_w: '702.WDisChaRteMaxRtg',
  pv_energy_total_wh: 'ext.15510', dc_energy_discharged_wh: '714.DCWhInj', dc_energy_charged_wh: '714.DCWhAbs',
  grid_export_wh: '701.TotWhInj', grid_import_wh: '701.TotWhAbs',
  ac_type_code: '701.ACType',
  voltage_l1_v: '701.VL1', current_l1_a: '701.AL1', power_l1_w: '701.WL1',
  pf_l1: '701.PFL1', va_l1: '701.VAL1', var_l1: '701.VarL1',
  voltage_l2_v: '701.VL2', current_l2_a: '701.AL2', power_l2_w: '701.WL2',
  pf_l2: '701.PFL2', va_l2: '701.VAL2', var_l2: '701.VarL2',
  voltage_l1l2_v: '701.VL1L2', voltage_l2l3_v: '701.VL2L3', voltage_l3l1_v: '701.VL3L1',
  voltage_l3_v: '701.VL3', current_l3_a: '701.AL3', power_l3_w: '701.WL3',
  pf_l3: '701.PFL3', va_l3: '701.VAL3', var_l3: '701.VarL3',
  battery_command_state: 'virtual', battery_command_power_w: 'virtual',
  battery_command_target_soc: 'virtual',
};

// ── Main Alpine App ──────────────────────────────────────────
document.addEventListener('alpine:init', () => {

  Alpine.store('app', {
    // Tab state
    activeTab: 'dashboard',

    // Sidebar state
    sidebarCollapsed: false,

    // Theme
    theme: localStorage.getItem('fwh-theme') || 'dark',

    // Model point source annotations
    showSources: localStorage.getItem('fwh-showSources') === 'true',

    // Connection state
    connected: false,
    lastPollTs: null,
    version: '--',
    env: '',

    // Multi-gateway
    activeGateway: 'default',  // 'default', gateway_id, or 'site'
    gatewayList: [],            // [{id, name, health, polling, ...}]

    // Data cache
    points: {},
    quality: null,
    bridgeStats: null,

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

    toggleSources() {
      this.showSources = !this.showSources;
      localStorage.setItem('fwh-showSources', this.showSources);
    },

    /** Return SunSpec source string for a point key, or '' if hidden */
    src(key) {
      return this.showSources ? (POINT_SOURCES[key] || '') : '';
    },

    setTab(tab) {
      this.activeTab = tab;
      // Notify tab components
      window.dispatchEvent(new CustomEvent('tab:changed', { detail: { tab } }));
    },

    setGateway(gwId) {
      this.activeGateway = gwId;
      this.refresh();
    },

    get multiGateway() {
      return this.gatewayList.length > 1;
    },

    async refresh() {
      // Fetch points from the active gateway (or site aggregator)
      const gw = this.activeGateway;
      let pointsUrl;
      if (gw === 'site') {
        pointsUrl = 'api/site/status';
      } else if (gw && gw !== 'default') {
        pointsUrl = `api/gateways/${gw}/points`;
      } else {
        pointsUrl = 'api/points';
      }

      const data = await fetchJSON(pointsUrl);
      if (data && !data.error) {
        this.points = data.points || {};
        this.quality = data.quality ?? null;
        this.lastPollTs = data.ts ?? null;
        this.connected = true;
      } else {
        this.connected = false;
      }

      const health = await fetchJSON('api/health');
      if (health && !health.error) {
        this.version = health.version || '--';
        this.env = health.environment || '';
      }

      const stats = await fetchJSON('api/stats');
      if (stats && !stats.error) {
        this.bridgeStats = stats;
      }

      // Refresh gateway list periodically
      const gwData = await fetchJSON('api/gateways');
      if (gwData && gwData.gateways) {
        this.gatewayList = gwData.gateways;
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
      // Releaseable if WSetEna is active, revert timer running, OR a software command is running
      const wsetEna = this.wsetEnabled;
      const hwActive = wsetEna != null && wsetEna !== 0 && wsetEna !== '0';
      const rvrtActive = (this.points.wset_revert_remain_s ?? 0) > 0;
      return hwActive || rvrtActive || this.hasActiveCommand;
    },

    get releaseControlState() {
      const pts = this.points;
      return {
        wsetEna:    pts.wset_enabled ?? null,
        wsetPct:    pts.wset_pct ?? null,
        wsetPctRaw: pts.wset_pct_raw ?? null,
        wsetW:      pts.wset_watts ?? null,
        wsetMod:    pts.wset_mode ?? null,
        rvrtTms:    pts.wset_revert_time_s ?? null,
        rvrtRem:    pts.wset_revert_remain_s ?? null,
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
