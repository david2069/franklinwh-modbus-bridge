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
  // Wall-clock HH:MM of a unix timestamp (for the last-poll indicator), in
  // the BRIDGE's zone, not the viewing device's (#79; time_fmt.js).
  clock: (ts) => (ts == null ? 'never' : bridgeTime.time(ts * 1000)),
  // Full timestamp with its zone label, used as a tooltip on the HH:MM label.
  // When the viewing device is in another zone, its own time follows,
  // labelled, so both readings are on hand without doubling every row.
  at: (ts) => {
    if (ts == null) return 'never';
    const ms = ts * 1000, note = bridgeTime.viewerNote(ms);
    return `${bridgeTime.full(ms)} ${bridgeTime.label(ms)}${note ? ` (${note})` : ''}`;
  },
  // Compact stamp for dense tables: "2 Sep 12:57", or just "12:57" for today.
  // A full stamp is ~24 characters and dominates a phone-width row; pair this
  // with fmt.at in a title= so nothing is actually lost. The zone label goes
  // once in the column header (fmt.tz), not on every row.
  short: (ts) => {
    if (ts == null) return '—';
    const ms = ts * 1000;
    const hm = bridgeTime.time(ms);
    return bridgeTime.sameDay(ms, Date.now()) ? hm : `${bridgeTime.dayMon(ms)} ${hm}`;
  },
  // The zone every time on the page is shown in, for headers and legends.
  tz: () => bridgeTime.label(),
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

// Settings sub-tabs, in display order. Every Settings card belongs to one.
/** Navigation tabs, in bottom-bar priority order (#262). One list drives the
 *  phone bar and its More sheet, like the Direct Connect bridge's registry;
 *  the desktop sidebar keeps its own markup. Icons are the sidebar's. */
const NAV_TABS = [
  { key: 'dashboard', short: 'Home', icon: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>' },
  { key: 'schedule', short: 'Schedule', icon: '<rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/><circle cx="12" cy="16" r="3"/>' },
  { key: 'energy_costs', short: 'Costs', icon: '<line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>' },
  { key: 'ha_entities', short: 'HA', icon: '<path d="M3 9.5L12 3l9 6.5"/><path d="M5 10v10h14V10"/><rect x="9" y="13" width="6" height="7"/>' },
  { key: 'logs', short: 'Logs', icon: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>' },
  { key: 'explorer', short: 'SunSpec', icon: '<path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/>' },
  { key: 'sequencer', short: 'Sequencer', icon: '<polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>' },
  { key: 'settings', short: 'Settings', icon: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>' },
];
/** At this width and below the bottom bar replaces the sidebar (as Direct Connect). */
const PHONE_NAV_QUERY = '(max-width: 699px)';
/** The published user guide, and the page for each tab (#261: links, not bundled). */
const DOCS_URL = 'https://david2069.github.io/franklinwh-modbus-bridge/';
const GUIDE_PAGES = {
  dashboard: 'user/ui/dashboard/', explorer: 'user/ui/sunspec-explorer/', logs: 'user/ui/logs/',
  sequencer: 'user/ui/sequencer/', schedule: 'user/scheduler/', ha_entities: 'user/ui/home-assistant-page/',
  energy_costs: 'user/ui/energy-costs/', settings: 'user/ui/settings-admin/',
};

const SETTINGS_SECTIONS = [
  { id: 'gateways',       label: 'Site & Gateways' },
  { id: 'home-assistant', label: 'Home Assistant' },
  { id: 'energy',         label: 'Energy & Automation' },
  { id: 'data',           label: 'Data & Backup' },
  { id: 'admin',          label: 'Admin' },
];

// Availability dot colour for a gateway's health state.
function gwDotClass(health) {
  return health === 'connected' ? 'bg-emerald-400'
    : health === 'tcp_only' ? 'bg-amber-400'
    : health === 'unreachable' ? 'bg-red-400'
    : 'bg-slate-500';
}

// A gateway's name as the UI shows it. "Default Gateway" is only the label the
// first row was created with; an unset-up placeholder says what it is instead.
function gwLabel(gw) {
  if (!gw) return '';
  if (gw.health === 'unconfigured') return 'aGate — not set up';
  return gw.name || gw.id;
}

// Short status label for a gateway row in the selector dropdown.
function gwStatusText(gw) {
  if (!gw) return '';
  if (gw.polling) return 'Polling';
  if (gw.connected) return 'Connected';
  if (gw.health === 'unreachable') return 'Offline';
  if (gw.health === 'unconfigured') return 'Not set up';
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
/** Send a finished outage to the bridge's log. Plain fetch, not fetchJSON:
 *  a failure here must not count as another outage. Best effort only. */
function _reportOutage(startedAt, failures, lastError) {
  try {
    fetch('api/ui/connection-outage', {
      method: 'POST',
      keepalive: true,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        started_at: startedAt,
        ended_at: Date.now() / 1000,
        failures,
        message: String(lastError?.message || '').slice(0, 300),
        url: String(lastError?.url || '').slice(0, 300),
        device_offline: !!lastError?.deviceOffline,
        page: String(new URLSearchParams(location.search).get('tab') || 'dashboard').slice(0, 40),
      }),
    }).catch(() => {});
  } catch (_) { /* never let reporting break the UI */ }
}

/** A 401 means the bridge answered but no longer accepts this page's login.
 *  That is not "Disconnected" (which the page used to show, with frozen
 *  values and buttons that silently did nothing). Ask the bridge why the
 *  login ended and show it in the logged-out modal. Once per page: every
 *  poll after this would 401 too. */
let _loggedOutHandled = false;
async function _noteLoggedOut(detail) {
  if (_loggedOutHandled) return;
  _loggedOutHandled = true;
  let info = {
    reason: detail?.reason || 'unknown',
    reason_text: detail?.reason_text || 'The bridge no longer accepts this page\'s login.',
    ended_at: detail?.ended_at || null,
    ended_by: detail?.ended_by || null,
    username: null,
  };
  // The cookie may simply be gone (no reason in the 401). The login id saved
  // at sign-in lets the bridge say what happened to that login.
  let sid = null;
  try { sid = localStorage.getItem('fwh-login-sid'); } catch (_) {}
  if (sid) {
    try {
      const r = await fetch(`api/auth/session-status?sid=${encodeURIComponent(sid)}`);
      const st = await r.json();
      if (st && st.state === 'ended') info = { ...info, ...st };
    } catch (_) { /* keep what the 401 said */ }
  }
  info.noticed_at = Date.now() / 1000;
  try {
    const s = window.Alpine && Alpine.store('app');
    if (s) s.loggedOut = info;
  } catch (_) {}
}

/** Served through Home Assistant: the add-on's panel (#247). HA signed the
 *  user in and the bridge has no login of its own there, so the logged-out
 *  modal and its "Log in" (a bridge login page that doesn't exist under
 *  ingress: "page not found") must never show. A 401 here means HA's session
 *  for this panel lapsed (a sleeping laptop, a background tab); HA renews it
 *  once the page is in use, so keep polling and only say so if it persists. */
const HA_INGRESS = document.documentElement.dataset.haIngress === '1';
const HA_SESSION_GRACE_S = 90;

function _noteIngress401() {
  try {
    const s = window.Alpine && Alpine.store('app');
    if (!s) return;
    const now = Date.now() / 1000;
    if (!s.haSessionLostSince) s.haSessionLostSince = now;
    s.haSessionLost = now - s.haSessionLostSince >= HA_SESSION_GRACE_S;
  } catch (_) {}
}

function _noteIngressOk() {
  try {
    const s = window.Alpine && Alpine.store('app');
    if (s && s.haSessionLostSince) { s.haSessionLostSince = null; s.haSessionLost = false; }
  } catch (_) {}
}

/** Reload the Home Assistant page around the panel, which renews HA's session.
 *  Same origin under ingress, so the top window is reachable. */
function _reloadHaPage() {
  try { window.top.location.reload(); } catch (_) { location.reload(); }
}

function _noteReachable(ok, info = {}) {
  try {
    const s = window.Alpine && Alpine.store('app');
    if (!s) return;
    if (ok) {
      // Back in contact after the banner was up: tell the bridge, so the
      // outage is in its log (it never saw the requests that didn't arrive).
      if (s.connFailures >= 2 && s.connOutageStart) {
        _reportOutage(s.connOutageStart, s.connFailures, s.connLastError);
      }
      s.connOutageStart = null;
      s.connFailures = 0;
      s.connLastOk = Date.now() / 1000;
      s.connLastError = null;
    } else {
      if (!s.connOutageStart) s.connOutageStart = Date.now() / 1000;
      // A failure the USER triggered counts double, so one failed Save raises
      // the banner immediately. The 2-failure threshold exists to stop the
      // background poll flapping on a single blip — but a person who just
      // pressed Save and got nothing is entitled to know at once, not after
      // the next poll happens to fail too.
      s.connFailures = (s.connFailures || 0) + (info.background ? 1 : 2);
      s.connLastError = {
        message: info.message || 'no response',
        url: info.url || '',
        ts: Date.now() / 1000,
        // Distinguishes "your device is offline" from "the bridge is
        // unreachable but your wifi is fine" — different fixes.
        deviceOffline: typeof navigator !== 'undefined' && navigator.onLine === false,
      };
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

// GitHub's new-Gist page, for "Save as Gist" (#228). Nothing is sent from the
// bridge: the user pastes and chooses secret (unlisted, not private) or public.
const GIST_NEW_URL = 'https://gist.github.com/';

/** How long a Sequencer run can legitimately take, from its own steps.
 *  POST /api/sequence/execute answers only when the whole run has finished,
 *  and runs sleep, verify and wait on purpose — far past FETCH_TIMEOUT_MS. A
 *  request abandoned at 8s reported "couldn't reach the bridge" while the
 *  bridge carried on writing registers. Mirrors franklinwh_modbus.sequencer:
 *  sleep_ms | post_sleep_ms, verify_timeout_ms (2s default) when verifying,
 *  wait_for.timeout_ms (30s default); plus Modbus time per step. */
const SEQUENCE_UNKNOWN_TIMEOUT_MS = 10 * 60 * 1000;
function sequenceTimeoutMs(sequence, gatewayCount = 1) {
  const steps = Array.isArray(sequence) ? sequence
    : (sequence && Array.isArray(sequence.steps)) ? sequence.steps : null;
  if (!steps) return SEQUENCE_UNKNOWN_TIMEOUT_MS;
  let ms = 0;
  for (const s of steps) {
    if (!s || typeof s !== 'object') continue;
    ms += Number(s.sleep_ms ?? s.post_sleep_ms ?? 0) || 0;
    if (s.verify) ms += Number(s.verify_timeout_ms ?? 2000) || 0;
    if (s.wait_for) ms += Number(s.wait_for.timeout_ms ?? 30000) || 0;
    ms += 5000;
  }
  return Math.max(FETCH_TIMEOUT_MS, ms * Math.max(1, gatewayCount) + 15000);
}

/** What to say when a long run outlives even its own timeout: the request was
 *  abandoned, not refused — the run may well still be going. */
function sequenceNoReplyMessage(data) {
  return (data && /^no response within/.test(data.technical || ''))
    ? `No reply after ${data.technical.replace('no response within ', '')} — the sequence may still be running on the bridge. Check Logs before running it again.`
    : null;
}

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
      let raw = null;
      try {
        const j = await r.json();
        raw = j && j.detail;
        if (raw) detail = typeof raw === 'string' ? raw : (raw.message || JSON.stringify(raw));
      } catch (_) {}
      if (r.status === 401 && !String(url).includes('api/auth/login')) {
        if (HA_INGRESS) _noteIngress401();
        else _noteLoggedOut(raw && typeof raw === 'object' ? raw : null);
      }
      throw new Error(detail);
    }
    if (HA_INGRESS) _noteIngressOk();
    return await r.json();
  } catch (e) {
    // Transport failure = the request never completed. Two shapes: fetch()
    // rejects with TypeError (refused, DNS, host down), or we aborted it
    // ourselves on timeout. An HTTP error status is neither — it means the
    // bridge answered.
    const timedOut = e.name === 'AbortError';
    if (e instanceof TypeError || timedOut) {
      _noteReachable(false, {
        background: !!options.background,
        message: timedOut ? `no response within ${Math.round(timeoutMs / 1000)}s` : e.message,
        url,
      });
    }
    // What the USER sees. "Failed to fetch" and "Load failed" are the
    // browser's words for "the request never left", which tells a person
    // nothing and reads as though their work is gone. Callers prefix this
    // with "Save failed: " etc., so it has to complete that sentence.
    const transport = (e instanceof TypeError) || timedOut;
    const technical = timedOut
      ? `no response within ${Math.round(timeoutMs / 1000)}s`
      : e.message;
    const msg = transport
      ? "couldn't reach the bridge — your changes are still here, see the banner above"
      : technical;
    console.warn('[Bridge]', url, technical);
    return { ok: false, error: msg, technical };
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
  sw_watchdog_remain_s: 'virtual', sw_watchdog_limit_s: 'virtual', command_elapsed_s: 'virtual', last_command_result: 'virtual',
  active_reserve_pct: 'ext.15508', self_reserve_pct: 'ext.15508', tou_reserve_pct: 'ext.15509',
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

    // Phone navigation (#262).
    phoneNav: window.matchMedia(PHONE_NAV_QUERY).matches,
    viewW: window.innerWidth,
    moreOpen: false,
    get navTabs() { return NAV_TABS.filter(t => this.canSee(t.key)); },
    /** Slots as in Direct Connect: max(4, width / 64). The last is always More,
     *  which also holds Guide and Support info. */
    get barTabs() { return this.navTabs.slice(0, Math.max(4, Math.floor(this.viewW / 64)) - 1); },
    get moreTabs() { return this.navTabs.slice(this.barTabs.length); },
    get moreActive() { return this.moreTabs.some(t => t.key === this.activeTab); },
    get guideUrl() { return DOCS_URL + (GUIDE_PAGES[this.activeTab] || ''); },
    goTab(key) { this.moreOpen = false; this.setTab(key); },
    openSupportInfo() {
      this.moreOpen = false;
      this.setTab('settings', { section: 'admin' });
      setTimeout(() => document.getElementById('support-info')?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 50);
    },

    toggleSidebar() {
      this.sidebarCollapsed = !this.sidebarCollapsed;
      localStorage.setItem('fwh-sidebar', this.sidebarCollapsed ? 'collapsed' : 'expanded');
    },

    // ── Reachability ──────────────────────────────────────────
    // Two consecutive transport failures before we shout: one can be a single
    // dropped request, and a banner that flickers gets ignored.
    connFailures: 0,
    connOutageStart: null,   // when the current run of failures began
    connLastOk: null,
    connLastError: null,
    connDetailsOpen: false,
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
    //: Where the page is actually talking to — the first thing to check when a
    //: phone on a VPN can reach the page but not the API.
    get connOrigin() { return location.origin; },
    get connErrorLabel() {
      const e = this.connLastError;
      if (!e) return '';
      const path = (e.url || '').replace(/^.*\/api\//, 'api/');
      return path ? `${e.message} — ${path}` : e.message;
    },
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
    loggedOutDismissed: false,  // "Stay on this page" in the logged-out modal
    loggedOut: null,    // set on a 401: {reason, reason_text, ended_at, ...} — see _noteLoggedOut
    haIngress: HA_INGRESS,
    haSessionLostSince: null,  // add-on only: first 401 of a run (#247)
    haSessionLost: false,      // ...and it has lasted past HA_SESSION_GRACE_S
    reloadHaPage() { _reloadHaPage(); },
    lastPollTs: null,
    version: '--',
    env: '',
    // {source, waiting_for_broker} from /api/health — add-on with no broker yet.
    mqttBroker: null,

    // Multi-gateway
    settingsSection: 'gateways',  // Settings sub-tab (see SETTINGS_SECTIONS)
    activeGateway: 'default',  // 'default', gateway_id, or 'site'
    gatewayChosen: false,      // set once the user picks; stops auto-selection
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
      const phoneMq = window.matchMedia(PHONE_NAV_QUERY);
      phoneMq.addEventListener('change', (e) => { this.phoneNav = e.matches; this.moreOpen = false; });
      addEventListener('resize', () => { this.viewW = window.innerWidth; });
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
      this.goToLogin();
    },
    goToLogin() {
      if (HA_INGRESS) { _reloadHaPage(); return; }  // HA owns the login (#247)
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
      if (!this.showSources) return '';
      // Home load's register is a per-gateway choice (#35): show the one in use.
      if (key === 'home_load_ext' && this.points?.home_load_source) {
        return 'ext.' + this.points.home_load_source;
      }
      return POINT_SOURCES[key] || '';
    },

    setTab(tab, { push = true, section = null } = {}) {
      this.activeTab = tab;
      if (tab === 'settings' && section && SETTINGS_SECTIONS.some(s => s.id === section)) {
        this.settingsSection = section;
      }
      // Keep ?tab= (and ?section= on Settings) in sync so the address bar is
      // always shareable/refreshable — and so docs can link to a section.
      // replaceState, not pushState: tab switches shouldn't stack up in Back.
      if (push) {
        try {
          const u = new URL(window.location.href);
          u.searchParams.set('tab', tab);
          if (tab === 'settings') u.searchParams.set('section', this.settingsSection);
          else u.searchParams.delete('section');
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
      let section = null;
      try {
        const params = new URL(window.location.href).searchParams;
        want = params.get('tab');
        section = params.get('section');
      } catch { return; }
      if (!want || want === this.activeTab) return;
      if (this.canSee && !this.canSee(want)) return;
      this.setTab(want, { push: false, section });
    },

    get settingsSections() { return SETTINGS_SECTIONS; },

    // Settings sub-tab. Pushes ?section= so a section can be linked to.
    setSettingsSection(section) {
      this.setTab('settings', { section });
    },

    setGateway(gwId) {
      this.activeGateway = gwId;
      this.gatewayChosen = true;  // the user's pick — stop auto-selecting
      this.refresh();
    },

    // Until the user picks one, view a gateway that is actually producing
    // data: with the primary gateway unconfigured (or offline) and a mock or
    // second aGate polling, showing an empty placeholder with empty cards is the
    // least useful thing we could do.
    autoSelectGateway() {
      if (this.gatewayChosen || this.activeGateway === 'site') return;
      const current = this.gatewayList.find(g => g.id === this.activeGateway);
      if (current && current.polling) return;
      const polling = this.gatewayList.find(g => g.polling);
      if (polling && polling.id !== this.activeGateway) {
        this.activeGateway = polling.id;
        this.refresh();
      }
    },

    // A real aGate is configured (not a mock, has an address). The SunSpec
    // Explorer and Sequencer read/write real Modbus registers, which a mock
    // doesn't have — they explain that instead of erroring.
    get hasRealGateway() {
      return this.gatewayList.some(g => !g.mock && g.enabled && g.health !== 'unconfigured');
    },

    // Any gateway that can produce data: an aGate with an address, or a demo.
    get hasAnyGateway() {
      return this.gatewayList.some(g => g.health !== 'unconfigured');
    },

    get multiGateway() {
      return this.gatewayList.length > 1;
    },

    get activeGatewayName() {
      if (this.activeGateway === 'site') return 'Site (All Gateways)';
      const gw = this.gatewayList.find(g => g.id === this.activeGateway);
      return gw ? gwLabel(gw) : 'aGate';
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

      const data = await fetchJSON(pointsUrl, { background: true });
      if (data && !data.error) {
        this.points = data.points || {};
        this.quality = data.quality ?? null;
        this.lastPollTs = data.ts ?? null;
        this.connected = true;
      } else {
        this.connected = false;
      }

      const health = await fetchJSON('api/health', { background: true });
      if (health && !health.error) {
        this.version = health.version || '--';
        this.env = health.environment || '';
        this.mqttBroker = health.mqtt_broker || null;
      }

      const stats = await fetchJSON('api/stats', { background: true });
      if (stats && !stats.error) {
        this.bridgeStats = stats;
      }

      // Refresh gateway list periodically
      const gwData = await fetchJSON('api/gateways', { background: true });
      if (gwData && gwData.gateways) {
        this.gatewayList = gwData.gateways;
        this.autoSelectGateway();
      }
    },

    toast(message, level = 'info') {
      const id = Date.now() + Math.random();
      this.toasts.push({ id, message, level });
      // Errors stay until dismissed: a failed command that vanishes after 4s
      // is easy to miss, and is exactly the one that matters.
      if (level !== 'error') {
        setTimeout(() => this.dismissToast(id), 4000);
      }
    },

    dismissToast(id) {
      this.toasts = this.toasts.filter(t => t.id !== id);
    },

    // Commands go to the gateway being viewed. /api/command always meant the
    // DEFAULT gateway: viewing a mock (or a second aGate) showed its state but
    // sent Release/Force to a different gateway entirely.
    get commandUrl() {
      const gw = this.activeGateway;
      return gw && gw !== 'site'
        ? `api/gateways/${encodeURIComponent(gw)}/command`
        : 'api/command';
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
        // Active with a limit of 0 = no time limit (not "expired").
        watchdogNoLimit: this.hasActiveCommand && (pts.sw_watchdog_limit_s ?? null) === 0,
        targetSoc:  pts.battery_command_target_soc ?? 0,
        elapsed:    pts.command_elapsed_s ?? null,
      };
    },

    async forceRelease() {
      if (this.releasing) return;
      this.releasing = true;

      try {
        // Send the release command through the command handler
        const data = await fetchJSON(this.commandUrl, {
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
