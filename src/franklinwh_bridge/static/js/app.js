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
  // Wall-clock HH:MM of a unix timestamp (for the last-poll indicator).
  clock: (ts) => {
    if (ts == null) return 'never';
    const d = new Date(ts * 1000);
    return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  },
  // Full local timestamp, used as a tooltip on the HH:MM label.
  at: (ts) => (ts == null ? 'never' : new Date(ts * 1000).toLocaleString()),
  // Compact stamp for dense tables: "2 Sep 12:57", or just "12:57" for today.
  // A full toLocaleString() is ~24 characters and dominates a phone-width row;
  // pair this with the full value in a title= so nothing is actually lost.
  short: (ts) => {
    if (ts == null) return '—';
    const d = new Date(ts * 1000);
    const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
    const now = new Date();
    const sameDay = d.getDate() === now.getDate() && d.getMonth() === now.getMonth()
      && d.getFullYear() === now.getFullYear();
    return sameDay ? hm : `${d.getDate()} ${d.toLocaleDateString([], { month: 'short' })} ${hm}`;
  },
};

// ── Mock gateway synthetic history ───────────────────────────
// Shared: the Dashboard chart, the Gateway Comparison modal and the Schedule
// timeline all need a mock gateway's "history". Mock metrics are deliberately
// never persisted, so every one of them would otherwise render blank while the
// gateway itself polls happily. Lived in dashboardTab()'s closure until three
// callers needed it.
const RANGE_SECS = {
  '30m': 1800, '1h': 3600, '2h': 7200, '4h': 14400, '6h': 21600,
  '8h': 28800, '12h': 43200, '18h': 64800, '24h': 86400,
  '3d': 259200, '5d': 432000, '7d': 604800, '30d': 2592000,
};

// Uses actual timestamps so historical bars show a real diurnal arc.
function mockSyntheticSeries(gatewayId, nowTs, rangeSeconds, nPoints) {
  const seed = gatewayId.split('').reduce((s, c) => s + c.charCodeAt(0), 0) || 1;

  // Per-gateway capacity/timing — mirrors Python synthetic_points()
  const solarPeakW = (3.5 + 2.5 * ((seed * 7)  % 100) / 100) * 1000;
  const homeBaseW  =  400 + 400 * ((seed * 17) % 100) / 100;
  const battCapW   = (2.0 + 1.5 * ((seed * 13) % 100) / 100) * 1000;
  const socMorning =  25  +  20 * ((seed * 11) % 100) / 100;

  const step = rangeSeconds / nPoints;
  const points = [];
  for (let i = 0; i < nPoints; i++) {
    const ts   = nowTs - rangeSeconds + i * step;
    const tick = i;

    // Wall-clock fractional hour from unix timestamp (UTC)
    const hour = (ts % 86400) / 3600;

    // Solar: smooth bell curve 6h–20h
    const solarFactor = Math.max(0, Math.sin((hour - 6) * Math.PI / 14));
    const cloudNoise  = 0.88 + 0.12 * Math.sin(tick / 15.0 + seed * 0.7);
    const solar_w     = Math.round(solarPeakW * Math.pow(solarFactor, 1.3) * cloudNoise * 10) / 10;

    // Home load: base + morning peak (8h) + evening peak (19h)
    const morningPeak = 900  * Math.exp(-Math.pow(hour - 8.0,  2) / 1.5);
    const eveningPeak = 1400 * Math.exp(-Math.pow(hour - 19.0, 2) / 3.5);
    const loadJitter  = 1.0  + 0.04 * Math.sin(tick / 4.0 + seed * 1.3);
    const home_w      = Math.round((homeBaseW + morningPeak + eveningPeak) * loadJitter * 10) / 10;

    // Battery: absorbs solar surplus, discharges on deficit
    const rawBatt   = -(solar_w - home_w) * 0.75;
    const battery_w = Math.round(Math.max(-battCapW, Math.min(battCapW, rawBatt)) * 10) / 10;

    // SOC: diurnal arc morning-low → afternoon-high → evening-low
    const socNoonPeak = Math.min(95, socMorning + 55);
    const socCurve = socMorning + (socNoonPeak - socMorning) *
      Math.pow(Math.max(0, Math.sin(Math.max(0, (hour - 6) * Math.PI / 14))), 0.7);
    const soc = Math.round(Math.max(5, Math.min(98, socCurve + 0.3 * Math.sin(tick / 25.0 + seed * 0.5))) * 10) / 10;

    const grid_w = Math.round((home_w - solar_w - battery_w) * 10) / 10;

    const mode_name = (hour >= 22 || hour < 6) ? 'Emergency Backup'
                    : (hour >= 16)             ? 'TOU'
                    :                            'Self-Consumption';
    const grid_mode = mode_name === 'Emergency Backup' ? 'Grid Forming' : 'Grid Following';

    points.push({ ts, battery_w, grid_w, solar_w, home_w, soc, mode_name, grid_mode });
  }
  return points;
}

// ── Operating-mode colours ───────────────────────────────────
// Shared by the Dashboard chart and the Schedule timeline so a mode means the
// same thing in both. BG is a WASH behind the plot — deliberately ~0.07 alpha,
// because mode is context for the data on top, not a value in its own right.
// LEGEND is the saturated swatch used in keys and tooltips.
const MODE_BG_COLORS = {
  'TOU':              'rgba(251,191,36,0.08)',
  'Time of Use':      'rgba(251,191,36,0.08)',
  'Self-Consumption': 'rgba(34,197,94,0.07)',
  'Emergency Backup': 'rgba(239,68,68,0.08)',
};
// Bare RGB triplets for the DOM-drawn schedule timeline, which composes them
// with CSS variables (rgba(R,G,B,var(--tl-mode-alpha))) so the wash strength
// comes from the theme/Appearance cascade instead of being baked in JS. The
// Chart.js dashboard cannot do this — a canvas fill can't read a CSS var — so
// MODE_BG_COLORS above stays as literal rgba for that.
const MODE_RGB = {
  'TOU':              '251,191,36',
  'Time of Use':      '251,191,36',
  'Self-Consumption': '34,197,94',
  'Emergency Backup': '239,68,68',
};
const MODE_RGB_FALLBACK = '148,163,184';

// Mode tint for a timeline track: hue fixed, alpha from the cascade.
function modeWash(mode) {
  return `rgba(${MODE_RGB[mode] || MODE_RGB_FALLBACK}, var(--tl-mode-alpha))`;
}

// In-track mode label. The raw hue is tuned for a dark track and goes muddy on
// a light one, so mix it toward the theme's ink. If color-mix is unsupported
// the declaration is simply dropped and the label inherits a legible colour.
function modeInk(mode) {
  return `color-mix(in srgb, rgb(${MODE_RGB[mode] || MODE_RGB_FALLBACK}) 72%, var(--tl-mode-ink-base))`;
}

const MODE_LEGEND_COLORS = {
  'TOU':              'rgba(251,191,36,0.7)',
  'Time of Use':      'rgba(251,191,36,0.7)',
  'Self-Consumption': 'rgba(34,197,94,0.7)',
  'Emergency Backup': 'rgba(239,68,68,0.7)',
};

// Availability dot colour for a gateway's health state.
function gwDotClass(health) {
  return health === 'connected' ? 'bg-emerald-400'
    : health === 'tcp_only' ? 'bg-amber-400'
    : health === 'unreachable' ? 'bg-red-400'
    : 'bg-slate-500';
}

// Short status label for a gateway row in the selector dropdown.
function gwStatusText(gw) {
  if (!gw) return '';
  if (gw.polling) return 'Polling';
  if (gw.connected) return 'Connected';
  if (gw.health === 'unreachable') return 'Offline';
  if (gw.health === 'disabled' || gw.enabled === false) return 'Disabled';
  return 'Stopped';
}

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
/** Record reachability so the UI can say "I can't see the bridge" instead of
 *  silently showing stale values. Only a TRANSPORT failure counts: an HTTP 401
 *  or 500 means we reached the bridge and it answered, which is a different
 *  problem and must not raise the offline banner. */
function _noteReachable(ok) {
  try {
    const s = window.Alpine && Alpine.store('app');
    if (!s) return;
    if (ok) {
      s.connFailures = 0;
      s.connLastOk = Date.now() / 1000;
    } else {
      s.connFailures = (s.connFailures || 0) + 1;
    }
  } catch (_) { /* store not built yet — nothing to report to */ }
}

/** Bare fetch() has NO timeout. A cleanly-refused connection rejects at once,
 *  but a half-dead path — VPN up and blackholing packets, which is the common
 *  way Tailscale fails — leaves the request hanging for the browser's own
 *  limit (tens of seconds to minutes). During that hang nothing fails, so no
 *  banner appears and no data arrives: the exact silent-stale case we're
 *  trying to kill. 8s is under the 10s poll, so a hung request is abandoned
 *  before the next one starts rather than piling up. */
const FETCH_TIMEOUT_MS = 8000;

async function fetchJSON(url, options = {}, timeoutMs = FETCH_TIMEOUT_MS) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    if (options.body && !options.headers) {
      options.headers = { 'Content-Type': 'application/json' };
    }
    // Respect a caller's own signal if it passed one.
    const r = await fetch(url, { ...options, signal: options.signal || ctl.signal });
    _noteReachable(true);   // it answered, whatever the status
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
    // Transport failure = the request never completed. Two shapes: fetch()
    // rejects with TypeError (refused, DNS, host down), or we aborted it
    // ourselves on timeout. An HTTP error status is neither — it means the
    // bridge answered.
    const timedOut = e.name === 'AbortError';
    if (e instanceof TypeError || timedOut) _noteReachable(false);
    const msg = timedOut ? `no response within ${Math.round(timeoutMs / 1000)}s` : e.message;
    console.warn('[Bridge]', url, msg);
    return { ok: false, error: msg };
  } finally {
    clearTimeout(timer);
  }
}

// ── Point-to-Source Map (SunSpec model.point notation) ────────
const POINT_SOURCES = {
  soc: '713.SoC', soh: '713.SoH',
  battery_power_w: '714.DCW', battery_dc_power_w: '714.DCW', battery_state: '714.DCW',
  battery_current_a: '714.DCA', battery_temp_c: '714.Tmp',
  total_solar: 'ext.15502', pv_proximal: 'ext.15503', pv_remote1: 'ext.15504', pv_remote2: 'ext.15505',
  home_load_ext: 'ext.16000',
  grid_power_w: '701.W', voltage_v: '701.LNV', current_a: '701.A', frequency_hz: '701.Hz',
  power_factor: '701.PF', grid_va: '701.VA', grid_var: '701.Var',
  connection_state: '701.ConnSt', inverter_state: '701.InvSt', grid_mode: '701.DERMode',
  ambient_temp_c: '701.TmpAmb', cabinet_temp_c: '701.TmpCab', mfr_alarm_info: '701.MnAlrmInfo',
  mode_name: 'ext.15507', wset_enabled: '704.WSetEna', wset_pct: '704.WSetPct',
  wset_watts: '704.WSet', wset_mode: '704.WSetMod', wset_mode_name: '704.WSetMod',
  wset_revert_time_s: '704.WSetRvrtTms', wset_revert_remain_s: '704.WSetRvrtRem',
  loc_rem_ctl_name: '715.LocRemCtl', der_heartbeat: '715.DERHb',
  controller_heartbeat: '715.ControllerHb', alarm_reset: '715.AlarmReset', op_ctl: '715.OpCtl',
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

    // Sidebar state. An explicit choice is remembered and always wins; with no
    // saved choice, collapse on a small screen where a 224px rail is a large
    // share of the viewport. Resizing never overrides what the user picked.
    sidebarCollapsed: (() => {
      const saved = localStorage.getItem('fwh-sidebar');
      if (saved !== null) return saved === 'collapsed';
      return window.innerWidth < 768;
    })(),

    toggleSidebar() {
      this.sidebarCollapsed = !this.sidebarCollapsed;
      localStorage.setItem('fwh-sidebar', this.sidebarCollapsed ? 'collapsed' : 'expanded');
    },

    // ── Reachability ──────────────────────────────────────────
    // Two consecutive transport failures before we shout: one can be a single
    // dropped request, and a banner that flickers gets ignored.
    connFailures: 0,
    connLastOk: null,
    connRetrying: false,
    get connLost() { return this.connFailures >= 2; },

    // navigator.onLine is the ONLY network fact a browser exposes, and it only
    // means "an interface is up" — it stays true on wifi with the VPN down, and
    // no API reveals VPN state at all (deliberately: it would leak the user's
    // network posture to any site). But combining it with our own reachability
    // does narrow the cause usefully:
    //   device offline           → it's the device
    //   device online, bridge no → it's the path: VPN, DNS, or the bridge itself
    deviceOffline: !navigator.onLine,
    get connCause() {
      return this.deviceOffline
        ? 'This device has no network connection.'
        : 'This device is online, so it\'s the path to the bridge — check VPN.';
    },
    /** "14:32" of the last successful call — what the screen is actually showing. */
    get connLastOkLabel() { return this.connLastOk ? fmt.clock(this.connLastOk) : 'never'; },
    async retryNow() {
      this.connRetrying = true;
      try {
        await fetchJSON('api/health');          // cheapest possible probe
        if (!this.connLost) await this.refresh();  // back? pull fresh data
      } finally {
        this.connRetrying = false;
      }
    },

    // Timezone confirmation state (see api/system/timezone). Null until the
    // first fetch, so the banner can't flash before we know the answer.
    tz: null,
    async loadTz() {
      this.tz = await fetchJSON('api/system/timezone');
    },
    async confirmTz(name = null) {
      const body = name ? JSON.stringify({ timezone: name }) : JSON.stringify({});
      const res = await fetchJSON('api/system/timezone/confirm', {
        method: 'POST', body,
      });
      if (res) {
        this.tz = res;
        this.toast(`Timezone confirmed: ${res.timezone}`, 'success');
      }
    },

    // Theme
    theme: localStorage.getItem('fwh-theme') || 'dark',

    // Model point source annotations
    showSources: localStorage.getItem('fwh-showSources') === 'true',

    // Topbar preferences — which items are visible and in what order
    topbarPrefs: (() => {
      const DEFAULTS = {
        center: [
          { key: 'grid',    label: 'Grid',    visible: true },
          { key: 'solar',   label: 'Solar',   visible: true },
          { key: 'load',    label: 'Load',    visible: true },
          { key: 'battery', label: 'Battery', visible: true },
        ],
        right: [
          { key: 'release', label: 'Release',   visible: true },
          { key: 'sunspec', label: 'SunSpec',   visible: true },
          { key: 'theme',   label: 'Theme',     visible: true },
          { key: 'refresh', label: 'Refresh',   visible: true },
        ],
      };
      try {
        const saved = localStorage.getItem('fwh-topbar-prefs');
        if (saved) {
          const parsed = JSON.parse(saved);
          // Merge: keep saved order/visibility, add any new defaults
          const merge = (saved, defaults) => {
            const keys = new Set(saved.map(i => i.key));
            const merged = [...saved];
            for (const d of defaults) { if (!keys.has(d.key)) merged.push({ ...d }); }
            return merged;
          };
          return { center: merge(parsed.center || [], DEFAULTS.center), right: merge(parsed.right || [], DEFAULTS.right) };
        }
      } catch (_) {}
      return { center: DEFAULTS.center.map(i => ({...i})), right: DEFAULTS.right.map(i => ({...i})) };
    })(),

    showTopbarPrefs: false,

    saveTopbarPrefs() {
      localStorage.setItem('fwh-topbar-prefs', JSON.stringify(this.topbarPrefs));
    },

    resetTopbarPrefs() {
      localStorage.removeItem('fwh-topbar-prefs');
      this.topbarPrefs = {
        center: [
          { key: 'grid',    label: 'Grid',    visible: true },
          { key: 'solar',   label: 'Solar',   visible: true },
          { key: 'load',    label: 'Load',    visible: true },
          { key: 'battery', label: 'Battery', visible: true },
        ],
        right: [
          { key: 'release', label: 'Release',   visible: true },
          { key: 'sunspec', label: 'SunSpec',   visible: true },
          { key: 'theme',   label: 'Theme',     visible: true },
          { key: 'refresh', label: 'Refresh',   visible: true },
        ],
      };
    },

    moveTopbarItem(section, idx, dir) {
      const arr = this.topbarPrefs[section];
      const newIdx = idx + dir;
      if (newIdx < 0 || newIdx >= arr.length) return;
      const tmp = arr[idx]; arr[idx] = arr[newIdx]; arr[newIdx] = tmp;
      this.saveTopbarPrefs();
    },

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
    batteryLabel: '',

    // Toast notifications
    toasts: [],

    // Styled confirm/prompt dialog (replaces native window.confirm/prompt).
    // `confirm()` resolves true/false; `promptText()` resolves the string or null.
    confirmState: {
      open: false, title: '', message: '', confirmLabel: 'Confirm',
      cancelLabel: 'Cancel', danger: false, input: null, inputValue: '', _resolve: null,
    },

    // Release modal
    showReleaseModal: false,
    releasing: false,

    // Refresh interval handle
    _interval: null,

    // Feature modules: tab id -> {enabled, can_access}. Gates nav + tabs.
    modulesByTab: {},
    // Current user + capabilities (from /api/auth/me).
    me: null,
    capabilities: [],

    init() {
      // Apply saved theme
      document.documentElement.setAttribute('data-theme', this.theme);
      // Track the one network fact the browser will tell us. Fires immediately
      // on a phone losing wifi, well before our 10s poll notices.
      addEventListener('online', () => { this.deviceOffline = false; this.retryNow(); });
      addEventListener('offline', () => { this.deviceOffline = true; });
      this.loadMe();
      this.refresh();
      this.loadModules();
      // This 10s loop IS the retry: it keeps calling fetchJSON regardless of
      // which tab is open, so connFailures clears within one tick of the
      // network returning and the offline banner disappears on its own.
      this._interval = setInterval(() => this.refresh(), 10000);
      // Load battery label from site config (one-shot at startup)
      fetchJSON('api/site').then(d => { if (d && d.battery_label) this.batteryLabel = d.battery_label; });
      this.loadTz();
    },

    async loadModules() {
      const data = await fetchJSON('api/modules');
      if (data && data.modules) {
        const byTab = {};
        for (const m of data.modules) byTab[m.tab] = { enabled: m.enabled, can_access: m.can_access };
        this.modulesByTab = byTab;
        // Honour ?tab= now that we know which tabs this role may actually see.
        this.applyTabFromUrl();
        // If the active tab's module is now hidden, fall back to the dashboard.
        if (!this.canSee(this.activeTab)) this.setTab('dashboard');
      }
    },

    /** Is a tab's module both enabled and accessible? Unknown tabs show (defensive). */
    canSee(tab) {
      const m = this.modulesByTab[tab];
      return m ? (m.enabled && m.can_access) : true;
    },

    async loadMe() {
      const data = await fetchJSON('api/auth/me');
      if (data) { this.me = data.user; this.capabilities = data.capabilities || []; }
    },
    get isAdmin() { return this.me?.role === 'admin'; },
    async logout() {
      await fetchJSON('api/auth/logout', { method: 'POST' });
      const base = document.querySelector('base')?.getAttribute('href')?.replace(/\/$/, '') || '';
      window.location.href = `${base}/login`;
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

    setTab(tab, { push = true } = {}) {
      this.activeTab = tab;
      // Keep ?tab= in sync so the address bar is always shareable/refreshable.
      // replaceState, not pushState: tab switches shouldn't stack up in Back.
      if (push) {
        try {
          const u = new URL(window.location.href);
          u.searchParams.set('tab', tab);
          window.history.replaceState(null, '', u);
        } catch { /* non-browser context — URL sync is cosmetic */ }
      }
      // Notify tab components
      window.dispatchEvent(new CustomEvent('tab:changed', { detail: { tab } }));
    },

    /** Open the tab named in ?tab=, if it's real and this role may see it.
     *  Called after the module/capability list loads, so an unauthorised or
     *  disabled tab falls back to the default rather than rendering empty. */
    applyTabFromUrl() {
      let want = null;
      try { want = new URL(window.location.href).searchParams.get('tab'); } catch { return; }
      if (!want || want === this.activeTab) return;
      if (this.canSee && !this.canSee(want)) return;
      this.setTab(want, { push: false });
    },

    setGateway(gwId) {
      this.activeGateway = gwId;
      this.refresh();
    },

    get multiGateway() {
      return this.gatewayList.length > 1;
    },

    get activeGatewayName() {
      if (this.activeGateway === 'site') return 'Site (All Gateways)';
      const gw = this.gatewayList.find(g => g.id === this.activeGateway);
      return gw ? gw.name : 'Default Gateway';
    },

    get activeGatewayHealth() {
      if (this.activeGateway === 'site') return 'connected';
      const gw = this.gatewayList.find(g => g.id === this.activeGateway);
      return gw ? gw.health : 'connected';
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

    // Styled confirm — returns a Promise<boolean>. Drop-in for window.confirm:
    //   if (!await Alpine.store('app').confirm({ message: '…' })) return;
    confirm(opts = {}) {
      return new Promise((resolve) => {
        this.confirmState = {
          open: true,
          title: opts.title || 'Are you sure?',
          message: opts.message || '',
          confirmLabel: opts.confirmLabel || 'Confirm',
          cancelLabel: opts.cancelLabel || 'Cancel',
          danger: !!opts.danger,
          input: null, inputValue: '',
          _resolve: resolve,
        };
      });
    },

    // Styled prompt — returns a Promise<string|null> (null = cancelled).
    promptText(opts = {}) {
      return new Promise((resolve) => {
        this.confirmState = {
          open: true,
          title: opts.title || 'Enter a value',
          message: opts.message || '',
          confirmLabel: opts.confirmLabel || 'OK',
          cancelLabel: 'Cancel',
          danger: false,
          input: opts.inputType || 'text',   // 'text' | 'password'
          inputValue: opts.value || '',
          _resolve: resolve,
        };
      });
    },

    // Resolve the open dialog. For a prompt, OK yields the typed value.
    _confirmResolve(ok) {
      const st = this.confirmState;
      const done = st._resolve;
      const result = st.input ? (ok ? st.inputValue : null) : ok;
      st.open = false;
      st._resolve = null;
      if (done) done(result);
    },

    // ── Release control helpers ──────────────────────────
    get hasActiveCommand() {
      const state = this.points.battery_command_state;
      return state && state !== 'Not Active' && state !== 'Released';
    },

    get forcedDispatch() {
      // The active user-commanded dispatch label ("Force Charge"/…), or null.
      // Distinct from battery_state, which is the device's *physical*
      // charge/discharge (which also happens during normal operation).
      return this.hasActiveCommand ? this.points.battery_command_state : null;
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
