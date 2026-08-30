/**
 * Schedule Tab (SCH2 + v2 Automations) — time/trigger → command-handler dispatch.
 *
 * Window schedules (recurring / one-off) drive the legacy when_spec path; the
 * FWHAI-parity "Automation" triggers (daily/weekly/interval/always) plus ALL/ANY
 * entry & exit condition trees drive the v2 fields on the same /api/schedules
 * surface. Test Verification hits /api/scheduler/evaluate; sensor dropdowns come
 * from /api/sensors. See docs/scheduling-and-orchestration-design.md §6.
 */

// Action → display + timeline colour. Mirrors the command vocabulary.
const SCHEDULE_ACTIONS = [
  { id: 'force_charge',    label: 'Force Charge',    colour: '#10b981', sustained: true,  params: ['power'] },
  { id: 'force_discharge', label: 'Force Discharge', colour: '#f59e0b', sustained: true,  params: ['power'] },
  { id: 'force_standby',   label: 'Force Standby',   colour: '#64748b', sustained: true,  params: [] },
  { id: 'release',         label: 'Release',         colour: '#475569', sustained: true,  params: [] },
  { id: 'reserve_self',    label: 'Self Reserve %',  colour: '#3b82f6', sustained: false, params: ['pct'] },
  { id: 'reserve_tou',     label: 'TOU Reserve %',   colour: '#6366f1', sustained: false, params: ['pct'] },
  { id: 'mode',            label: 'Operating Mode',  colour: '#14b8a6', sustained: false, params: ['mode'] },
  { id: 'none',            label: 'No battery action (HA only)', colour: '#64748b', sustained: false, params: [] },
];

const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

// Trigger-type selector — first two are legacy window schedules (when_spec),
// the rest are v2 fire-based triggers (trigger_kind + trigger_spec).
const TRIGGER_TYPES = [
  { id: 'window',   label: 'Recurring windows',   kind: null },
  { id: 'once',     label: 'One-off date window', kind: null },
  { id: 'daily',    label: 'Daily at time',       kind: 'daily' },
  { id: 'weekly',   label: 'Weekly at time',      kind: 'weekly' },
  { id: 'interval', label: 'Every N minutes',     kind: 'interval' },
  { id: 'monthly',  label: 'Monthly / calendar',  kind: 'monthly' },
  { id: 'cron',     label: 'Custom cron',         kind: 'cron' },
  { id: 'always',   label: 'Always evaluate (sensor-driven)', kind: 'always' },
];

const OPERATORS = ['<', '<=', '==', '!=', '>=', '>', 'between'];

// FWHAI-parity quick presets. Selecting one fills the real fields below, which
// stay editable. Sub-day/daily/weekly map onto interval/daily/weekly; the
// calendar ones map onto the `monthly` engine kind (day + months-set); Custom
// Cron switches to the `cron` kind for a free-form expression.
const TRIGGER_PRESETS = [
  { id: 'min_5',          label: 'Every 5 minutes',            type: 'interval', interval_min: 5 },
  { id: 'min_10',         label: 'Every 10 minutes',           type: 'interval', interval_min: 10 },
  { id: 'min_15',         label: 'Every 15 minutes',           type: 'interval', interval_min: 15 },
  { id: 'min_30',         label: 'Every 30 minutes',           type: 'interval', interval_min: 30 },
  { id: 'min_1',          label: 'Every minute',               type: 'interval', interval_min: 1 },
  { id: 'hourly',         label: 'Every hour',                 type: 'interval', interval_min: 60 },
  { id: 'daily_midnight', label: 'Every day (midnight)',       type: 'daily',    time_of_day: '00:00' },
  { id: 'daily_8am',      label: 'Every day (morning 8am)',    type: 'daily',    time_of_day: '08:00' },
  { id: 'weekly_sun',     label: 'Every week (Sun midnight)',  type: 'weekly',   time_of_day: '00:00', days: [6] },
  { id: 'monthly_1st',    label: 'Every month (1st day)',      type: 'monthly',  month_day: 1, months: [] },
  { id: 'quarterly',      label: 'Quarterly (Jan, Apr, Jul, Oct)', type: 'monthly', month_day: 1, months: [1, 4, 7, 10] },
  { id: 'six_monthly',    label: 'Six monthly (Jan, Jul)',     type: 'monthly',  month_day: 1, months: [1, 7] },
  { id: 'annually',       label: 'Annually (Jan 1st)',         type: 'monthly',  month_day: 1, months: [1] },
  { id: 'custom_cron',    label: 'Custom cron expression…',    type: 'cron' },
];

// Month labels for the monthly-trigger month picker (Jan=1..Dec=12).
const MONTH_LABELS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

function todayISO() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function emptyTree() { return { match: 'ALL', conditions: [] }; }

function scheduleTab() {
  return {
    schedules: [],
    timeline: { segments: [], now_min: 0, weekday: 0 },
    services: [],
    sensors: [],
    haControllable: [],   // exposed HA entities in controllable domains (for actions)

    // Sensors grouped by their `group` label for the condition-picker,
    // preserving first-seen order (native metrics first, then HA · <instance>).
    get sensorGroups() {
      const groups = [];
      const idx = {};
      for (const s of this.sensors) {
        const g = s.group || 'Other';
        if (!(g in idx)) { idx[g] = groups.length; groups.push({ label: g, items: [] }); }
        groups[idx[g]].items.push(s);
      }
      return groups;
    },

    // Display label for a selected sensor id (searchable combobox button text).
    sensorLabel(id) {
      if (!id) return 'Select sensor…';
      const s = this.sensors.find((x) => x.id === id);
      return s ? s.label : id;
    },

    // Groups filtered by a query (matches label, id, or group name); drops empties.
    // Powers the type-to-filter condition picker (FWHAI-style).
    filterGroups(q) {
      const query = (q || '').trim().toLowerCase();
      if (!query) return this.sensorGroups;
      const out = [];
      for (const g of this.sensorGroups) {
        const gm = g.label.toLowerCase().includes(query);
        const items = g.items.filter(
          (s) => gm || s.label.toLowerCase().includes(query) || s.id.toLowerCase().includes(query),
        );
        if (items.length) out.push({ label: g.label, items });
      }
      return out;
    },

    // ── HA actions (one-shot) ──────────────────────────────────
    async loadHaControllable() {
      const CTRL = ['switch', 'input_boolean', 'light', 'select', 'input_select',
                    'number', 'input_number', 'button', 'scene', 'script'];
      const data = await fetchJSON('api/ha/entities?exposed=true&page_size=500');
      const rows = (data && data.entities) || [];
      this.haControllable = rows
        .filter((e) => CTRL.includes(e.domain))
        .map((e) => ({
          instance_id: e.instance, instance_name: e.instance_name,
          entity_id: e.entity_id, domain: e.domain, options: e.options || [],
          label: `${e.friendly_name} (${e.entity_id})`,
        }));
    },

    get haControllableGroups() {
      const groups = [];
      const idx = {};
      for (const e of this.haControllable) {
        const g = e.instance_name || 'HA';
        if (!(g in idx)) { idx[g] = groups.length; groups.push({ label: g, items: [] }); }
        groups[idx[g]].items.push(e);
      }
      return groups;
    },

    haEntityMeta(a) {
      return this.haControllable.find(
        (e) => e.instance_id === a.instance_id && e.entity_id === a.entity_id);
    },

    haControlKind(a) {
      const m = this.haEntityMeta(a);
      if (!m) return '';
      const d = m.domain;
      if (['switch', 'input_boolean', 'light'].includes(d)) return 'toggle';
      if (['select', 'input_select'].includes(d)) return 'select';
      if (['number', 'input_number'].includes(d)) return 'number';
      if (d === 'button') return 'press';
      if (['scene', 'script'].includes(d)) return 'run';
      return 'toggle';
    },

    _defaultServiceFor(domain) {
      if (['select', 'input_select'].includes(domain)) return 'select_option';
      if (['number', 'input_number'].includes(domain)) return 'set_value';
      if (domain === 'button') return 'press';
      if (['scene', 'script'].includes(domain)) return 'turn_on';
      return 'turn_on';  // switch/input_boolean/light
    },

    addHaAction() {
      this.form.ha_actions.push({ instance_id: '', entity_id: '', service: 'turn_on', data: {}, when: 'fire' });
    },
    removeHaAction(i) { this.form.ha_actions.splice(i, 1); },
    addGuard(a) {
      const s = this.sensors[0] ? this.sensors[0].id : 'battery.soc_pct';
      a.guard = { sensor: s, op: '==', value: '' };
    },
    removeGuard(a) { a.guard = null; },

    onHaEntityPick(a, composite) {
      const sep = composite.indexOf('::');
      a.instance_id = composite.slice(0, sep);
      a.entity_id = composite.slice(sep + 2);
      a.data = {};
      const m = this.haEntityMeta(a);
      a.service = this._defaultServiceFor(m ? m.domain : '');
    },

    audit: [],
    auditFilter: '',
    conn: { connected: true, gateways: {}, recent_outages: [] },
    expandedId: null,
    previewDay: new Date().getDay() === 0 ? 6 : new Date().getDay() - 1, // Mon=0
    timelineGateway: 'all',  // 'all' | '<gateway_id>' — paints the bar for one or all

    // ── timeline zoom (time-of-day window) ───────────────────
    // Defaults to the whole of the selected day (today). Narrowing the window
    // spreads the same segments across the full width, so a 90-minute dispatch
    // reads as a block instead of a 6% sliver.
    zoomFromStr: '00:00',
    zoomToStr: '23:59',
    zoomPresets: [
      { label: 'Full day', from: '00:00', to: '23:59' },
      { label: 'Daylight', from: '06:00', to: '20:00' },
      { label: 'Evening peak', from: '15:00', to: '22:00' },
      { label: 'Overnight', from: '20:00', to: '23:59' },
    ],
    _minOf(s) {
      const [h, m] = String(s || '0:0').split(':').map(Number);
      return (h || 0) * 60 + (m || 0);
    },
    _hhmm(m) {
      const t = Math.min(1440, Math.max(0, Math.round(m)));
      return `${String(Math.floor(t / 60)).padStart(2, '0')}:${String(t % 60).padStart(2, '0')}`;
    },
    get zoomFrom() { return Math.max(0, Math.min(1425, this._minOf(this.zoomFromStr))); },
    get zoomTo() {
      const t = this._minOf(this.zoomToStr);
      // 23:59 means "end of day" — treat it as 1440 so a full day is exact.
      return Math.min(1440, Math.max(this.zoomFrom + 15, t === 1439 ? 1440 : t));
    },
    get zoomSpan() { return this.zoomTo - this.zoomFrom; },
    get zoomed() { return this.zoomFrom !== 0 || this.zoomTo !== 1440; },
    setZoom(p) { this.zoomFromStr = p.from; this.zoomToStr = p.to; },
    /** Five evenly spaced axis labels across the current window. */
    get axisTicks() {
      return [0, 1, 2, 3, 4].map((i) => this._hhmm(this.zoomFrom + (this.zoomSpan * i) / 4));
    },
    loading: false,
    _interval: null,

    auditStatuses: ['fired', 'executed', 'ha_action', 'gated', 'waiting', 'missed', 'exit_condition_met'],

    // Edit form. `formOpen` drives modal visibility; `form` holds the data and
    // is NEVER set back to null while the modal could still be evaluating it —
    // nulling it mid-teardown is what threw "form.id is null" floods in Alpine.
    form: null,
    formOpen: false,
    testResult: null,       // entry-conditions Test Verification result
    exitTestResult: null,   // exit-conditions Test Verification result
    traceByCid: {},         // leaf _cid -> {result, live_value} from last test
    _cidSeq: 0,

    actions: SCHEDULE_ACTIONS,
    weekdays: WEEKDAYS,
    triggerTypes: TRIGGER_TYPES,
    triggerPresets: TRIGGER_PRESETS,
    operators: OPERATORS,

    init() {
      this.load();
      window.addEventListener('tab:changed', (e) => {
        if (e.detail.tab === 'schedule') this.load();
      });
      this._interval = setInterval(() => {
        if (Alpine.store('app').activeTab === 'schedule') this.load();
      }, 20000);
    },

    _logUrl() {
      return `api/schedules/log?limit=40${this.auditFilter ? '&status=' + this.auditFilter : ''}`;
    },

    async load() {
      const [sch, tl, svc, sen, log, conn] = await Promise.all([
        fetchJSON('api/schedules'),
        fetchJSON(`api/schedules/timeline?day=${this.previewDay}`),
        fetchJSON('api/services'),
        fetchJSON('api/sensors'),
        fetchJSON(this._logUrl()),
        fetchJSON('api/health/connectivity'),
      ]);
      if (sch && sch.schedules) this.schedules = sch.schedules;
      if (tl && tl.segments) this.timeline = tl;
      if (svc && svc.services) this.services = svc.services;
      if (sen && sen.sensors) this.sensors = sen.sensors;
      if (log && log.events) this.audit = log.events;
      if (conn) this.conn = conn;
      this.loadHaControllable();  // exposed controllable HA entities for actions
    },

    async setAuditFilter(f) {
      this.auditFilter = this.auditFilter === f ? '' : f;
      const log = await fetchJSON(this._logUrl());
      if (log && log.events) this.audit = log.events;
    },

    // ── audit / connectivity display ─────────────────────────
    scheduleName(id) {
      const s = this.schedules.find(x => x.id === id);
      return s ? s.name : (id || '—');
    },

    fmtTs(ts) {
      if (!ts) return '—';
      const d = new Date(ts * 1000);
      const p = (n) => String(n).padStart(2, '0');
      // dd/mm prefix so the audit trail is unambiguous across days
      return `${p(d.getDate())}/${p(d.getMonth() + 1)} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
    },

    auditClass(r) {
      return ({
        fired: 'text-emerald-300', executed: 'text-emerald-300',
        gated: 'text-amber-300', deferred: 'text-amber-300', hold: 'text-amber-300',
        waiting: 'text-sky-300',
        missed: 'text-red-300', failed: 'text-red-300',
        exit_condition_met: 'text-cyan-300', duration_elapsed: 'text-slate-400',
        release: 'text-slate-400', ok: 'text-emerald-300', ha_action: 'text-emerald-300',
        stopped: 'text-red-300',
        // config-change (CRUD) rows
        created: 'text-emerald-300', enabled: 'text-emerald-300',
        updated: 'text-sky-300', disabled: 'text-amber-300', deleted: 'text-red-300',
      })[r] || 'text-slate-300';
    },

    get anyOutage() {
      return this.conn && this.conn.connected === false;
    },
    get recentOutages() {
      return (this.conn && this.conn.recent_outages) || [];
    },
    outageDur(o) {
      if (!o.end_ts) return 'ongoing';
      const s = Math.round(o.end_ts - o.start_ts);
      return s < 60 ? `${s}s` : `${Math.round(s / 60)}m`;
    },

    toggleExpand(id) { this.expandedId = this.expandedId === id ? null : id; },
    get expandedEntry() { return this.schedules.find(x => x.id === this.expandedId) || null; },

    // Readable one-line rendering of a condition tree (nested groups parenthesised).
    treeText(t) {
      if (!t || !t.conditions || !t.conditions.length) return '—';
      const render = (node) => {
        const join = node.match === 'ANY' ? ' OR ' : ' AND ';
        return node.conditions.map(c =>
          c.conditions ? '(' + render(c) + ')' : `${c.sensor} ${c.op} ${c.value}${c.op === 'between' ? '..' + c.value2 : ''}`
        ).join(join);
      };
      return render(t);
    },

    triggerText(e) {
      if (!e.trigger_kind) return this.whenLabel(e);
      const s = e.trigger_spec || {};
      const dur = e.duration_s ? ` for ${Math.round(e.duration_s / 60)} min` : '';
      return this.whenLabel(e) + dur;
    },

    async setPreviewDay(d) {
      this.previewDay = d;
      const tl = await fetchJSON(`api/schedules/timeline?day=${d}`);
      if (tl && tl.segments) this.timeline = tl;
    },

    // ── derived display ──────────────────────────────────────
    get activeCount() {
      return this.schedules.filter(s => s.active_now).length;
    },

    actionMeta(id) {
      return this.actions.find(a => a.id === id) || { label: id, colour: '#64748b' };
    },

    // ── per-gateway colour coding ────────────────────────────
    // Each gateway gets a stable colour (by its position in the gateway list, so
    // the legend matches). Used as a left-edge tint on entry rows + timeline
    // segments so you can see which gateway an entry belongs to under "All
    // gateways". The ACTION colour still fills the block; gateway = the edge.
    gatewayPalette: [
      '#38bdf8', '#f472b6', '#a78bfa', '#fb923c', '#34d399',
      '#facc15', '#22d3ee', '#f87171', '#4ade80', '#e879f9',
    ],
    gatewayColour(id) {
      if (!id || id === 'site') return '#94a3b8';  // "all gateways" → neutral
      const list = Alpine.store('app').gatewayList || [];
      const idx = list.findIndex(g => g.id === id);
      if (idx >= 0) return this.gatewayPalette[idx % this.gatewayPalette.length];
      let h = 0;  // fallback: stable hash for an id not in the current list
      for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) >>> 0;
      return this.gatewayPalette[h % this.gatewayPalette.length];
    },
    // Colour for a schedule entry OR a timeline segment (both carry target_type/id).
    entryTint(e) {
      if (!e) return '#94a3b8';
      if (e.target_type === 'site') return '#94a3b8';
      if (e.target_type === 'service') return this.gatewayColour('svc:' + (e.target_id || ''));
      return this.gatewayColour(e.target_id || 'default');
    },

    sensorMeta(id) {
      return this.sensors.find(s => s.id === id) || { id, label: id, unit: null, kind: 'number' };
    },

    targetLabel(e) {
      if (e.target_type === 'site') return 'Site (all gateways)';
      if (e.target_type === 'service') {
        const s = this.services.find(x => x.id === e.target_id);
        return s ? `Service: ${s.name}` : 'Service';
      }
      const gw = Alpine.store('app').gatewayList.find(g => g.id === (e.target_id || 'default'));
      return gw ? gw.name : (e.target_id || 'Default Gateway');
    },

    // "When" column: trigger entries describe their trigger, legacy show windows.
    whenLabel(e) {
      if (e.trigger_kind) {
        const s = e.trigger_spec || {};
        if (e.trigger_kind === 'daily')    return `Daily ${s.time_of_day || ''}`;
        if (e.trigger_kind === 'weekly')   return `Weekly ${(s.days_of_week || []).map(i => WEEKDAYS[i]).join(' ')} ${s.time_of_day || ''}`;
        if (e.trigger_kind === 'interval') return `Every ${Math.round((s.every_seconds || 0) / 60)} min`;
        if (e.trigger_kind === 'monthly') {
          const mos = (s.months || []);
          const when = mos.length ? mos.map(m => MONTH_LABELS[m - 1]).join(',') : 'monthly';
          return `Day ${s.day || 1} ${when} ${s.time_of_day || ''}`.trim();
        }
        if (e.trigger_kind === 'cron')     return `Cron ${s.expr || ''}`;
        if (e.trigger_kind === 'oneoff')   return `Once ${(s.fire_at || '').replace('T', ' ')}`;
        if (e.trigger_kind === 'always')   return 'Always (conditions)';
      }
      return this.daysLabel(e.when_spec) + ' · ' + this.windowsLabel(e.when_spec);
    },

    condSummary(e) {
      const n = (t) => (t && t.conditions ? t.conditions.length : 0);
      const parts = [];
      if (n(e.entry_conditions)) parts.push(`gate ${n(e.entry_conditions)}`);
      if (n(e.exit_conditions)) parts.push(`exit ${n(e.exit_conditions)}`);
      return parts.join(' · ');
    },

    daysLabel(when) {
      if (when && when.date) {
        return (when.date < todayISO() ? 'Once (passed) ' : 'Once ') + when.date;
      }
      const d = (when && when.days) || [];
      if (!d.length || d.length === 7) return 'Every day';
      return d.slice().sort((a, b) => a - b).map(i => WEEKDAYS[i]).join(' ');
    },

    windowsLabel(when) {
      const w = (when && when.windows) || [];
      return w.map(x => `${x.start}–${x.end}`).join(', ') || '—';
    },

    nextFireLabel(e) {
      if (!e.next_fire) return '—';
      const dt = new Date(e.next_fire);
      const now = new Date();
      const sameDay = dt.toDateString() === now.toDateString();
      const t = `${String(dt.getHours()).padStart(2, '0')}:${String(dt.getMinutes()).padStart(2, '0')}`;
      if (sameDay) return `Today ${t}`;
      if (e.when_spec && e.when_spec.date) return `${e.when_spec.date} ${t}`;
      const daysAway = Math.round((dt - now) / 86400000);
      if (daysAway >= 0 && daysAway < 7) return `${WEEKDAYS[(dt.getDay() + 6) % 7]} ${t}`;
      return `${dt.toISOString().slice(0, 10)} ${t}`;
    },

    // A segment belongs to the selected gateway view? 'site' targets paint on
    // every gateway; a 'gateway' target only on its own; 'service' shows in All.
    _segForGateway(seg) {
      if (this.timelineGateway === 'all') return true;
      if (seg.target_type === 'site') return true;
      if (seg.target_type === 'gateway') return (seg.target_id || 'default') === this.timelineGateway;
      return false;
    },

    // ── timeline geometry ────────────────────────────────────
    /** Clip segments to the zoom window, splitting any that wrap midnight. */
    _clipSegments(segs) {
      const out = [];
      const z0 = this.zoomFrom;
      const z1 = this.zoomTo;
      const push = (seg, left, width) => {
        const s = Math.max(left, z0);
        const e = Math.min(left + width, z1);
        if (e <= s) return;  // entirely outside the window
        out.push({ ...seg, colour: this.actionMeta(seg.action).colour, _l: s, _w: e - s });
      };
      for (const seg of segs) {
        if (seg.end_min <= seg.start_min) {   // wraps past midnight → two pieces
          push(seg, seg.start_min, 1440 - seg.start_min);
          push(seg, 0, seg.end_min);
        } else {
          push(seg, seg.start_min, seg.end_min - seg.start_min);
        }
      }
      return out;
    },

    /** A gateway-targeted segment belongs to this row? Site/service targets fan
     *  out to several gateways, so they get their own row rather than being
     *  duplicated onto every one (which would imply a membership the client
     *  can't confirm). */
    _segOnGateway(seg, gwId) {
      return seg.target_type === 'gateway' && (seg.target_id || 'default') === gwId;
    },

    /** One track per gateway under "All gateways", otherwise a single track.
     *  Splitting by row makes overlap visible — two gateways dispatching at the
     *  same time previously rendered as one block on a shared strip. */
    get timelineRows() {
      const gws = Alpine.store('app').gatewayList || [];
      if (this.timelineGateway !== 'all' || gws.length < 2) {
        const one = gws.find((g) => g.id === this.timelineGateway);
        return [{
          id: this.timelineGateway,
          name: one ? one.name : 'All gateways',
          colour: one ? this.gatewayColour(one.id) : '#94a3b8',
          segments: this._clipSegments(this.timeline.segments.filter((s) => this._segForGateway(s))),
        }];
      }
      const rows = gws.map((g) => ({
        id: g.id,
        name: g.name,
        colour: this.gatewayColour(g.id),
        segments: this._clipSegments(this.timeline.segments.filter((s) => this._segOnGateway(s, g.id))),
      }));
      const fanout = this.timeline.segments.filter(
        (s) => s.target_type === 'site' || s.target_type === 'service',
      );
      if (fanout.length) {
        rows.unshift({
          id: '__fanout', name: 'Site / service', colour: '#94a3b8',
          segments: this._clipSegments(fanout),
        });
      }
      return rows;
    },

    pct(min) { return ((min - this.zoomFrom) / this.zoomSpan) * 100; },
    get nowPct() { return this.pct(this.timeline.now_min); },
    /** The now-marker only means something on today's column, and only when the
     *  current time falls inside the zoom window. */
    get nowVisible() {
      const today = new Date().getDay() === 0 ? 6 : new Date().getDay() - 1;
      const n = this.timeline.now_min;
      return this.previewDay === today && n >= this.zoomFrom && n <= this.zoomTo;
    },

    // ── form ─────────────────────────────────────────────────
    _blankForm(overrides) {
      return Object.assign({
        id: null,
        name: '',
        trigger_type: 'window',
        date: todayISO(),
        days: [],
        windows: [{ start: '14:00', end: '19:00' }],
        time_of_day: '18:00',
        interval_min: 60,
        anchor_time: '',
        month_day: 1,          // monthly: day-of-month (1..31, clamped)
        months: [],            // monthly: month numbers (1..12); empty = every month
        cron_expr: '0 0 1 * *', // cron: free-form expression
        duration_min: 0,
        action: 'force_charge',
        power_w: 1000,
        power_pct: 100,
        power_unit: 'W',   // 'W' (watts) or 'pct' (% of max rate) — one or the other
        pct: 20,
        mode: 'TOU',
        target_type: (Alpine.store('app').activeGateway === 'site') ? 'site' : 'gateway',
        target_id: (Alpine.store('app').activeGateway === 'site') ? '' : Alpine.store('app').activeGateway,
        release: 'release',
        release_policy: 'restore_prior_mode',
        conflict: 'defer',
        missed_policy: 'late_fire_remaining',
        priority: 0,
        enabled: true,
        entry_hold_s: 0,
        ha_actions: [],
        entry_conditions: emptyTree(),
        exit_conditions: emptyTree(),
      }, overrides || {});
    },

    // HH:MM:SS <-> seconds for the dwell/duration field.
    secToHms(s) {
      const n = Math.max(0, parseInt(s, 10) || 0);
      const p = (x) => String(x).padStart(2, '0');
      return `${p(Math.floor(n / 3600))}:${p(Math.floor((n % 3600) / 60))}:${p(n % 60)}`;
    },
    hmsToSec(str) {
      const parts = String(str).split(':').map((x) => parseInt(x, 10) || 0);
      let h = 0, m = 0, s = 0;
      if (parts.length >= 3) [h, m, s] = parts;
      else if (parts.length === 2) [m, s] = parts;
      else [s] = parts;
      return Math.max(0, h * 3600 + m * 60 + s);
    },

    newEntry() {
      this.form = this._blankForm();
      this.formOpen = true;
      this._clearTrace();
    },

    // ── Automation templates ─────────────────────────────────
    // Pre-built automations (opened in the editor for review; start DISABLED so
    // the enable-on-save prompt confirms before they run). Showcase the demand/
    // tariff/const sensors + Lookup (sensor-to-sensor) comparisons.
    automationTemplates: [
      { id: 'peak_shave', label: 'Peak-demand shaving',
        desc: 'Discharge to cap grid import during the peak-demand window' },
      { id: 'export_bonus', label: 'Battery export bonus',
        desc: 'Discharge to grid during the export-bonus window' },
      { id: 'ausgrid_evening', label: 'Ausgrid — Evening discharge (4–9pm)',
        desc: 'Discharge for the peak/export-reward window (network 16:00–21:00 — check your retailer)' },
      { id: 'ausgrid_sponge', label: 'Ausgrid — Solar sponge (10am–3pm)',
        desc: 'Charge from solar to self-consume + avoid the export charge (10:00–15:00)' },
    ],
    _tleaf(sensor, op, value, kind) {
      const leaf = {
        sensor, op, value: 0, value2: 0, value_kind: 'value', value_sensor: '',
        _cid: ++this._cidSeq,
      };
      if (kind === 'sensor') { leaf.value_kind = 'sensor'; leaf.value_sensor = value; }
      else { leaf.value = value; }
      return leaf;
    },
    applyTemplate(id) {
      // Build the form in ONE _blankForm() assignment (like editEntry) so the
      // <select>s render with the template's values — mutating after newEntry()
      // left op/action/trigger selects showing their defaults.
      const common = {
        trigger_type: 'always', action: 'force_discharge',
        power_unit: 'pct', power_pct: 100, release_policy: 'restore_prior_mode',
        enabled: false,  // review → enable-on-save prompt
        // keep discharging until SOC hits the Min-Discharge floor
        exit_conditions: { match: 'ANY', conditions: [
          this._tleaf('battery.soc_pct', '<=', 'const.min_discharge_soc', 'sensor'),
        ] },
      };
      let over;
      if (id === 'peak_shave') {
        over = { name: 'Peak-demand shaving', entry_conditions: { match: 'ALL', conditions: [
          this._tleaf('tariff.demand_window_active', '==', 1),
          // only act when actually importing enough to matter (tune this floor)
          this._tleaf('demand.interval_kw', '>', 1),
          // …and this interval is at/above the period peak → shave to hold it down
          this._tleaf('demand.interval_kw', '>=', 'demand.peak_kw', 'sensor'),
          this._tleaf('battery.soc_pct', '>', 'const.min_discharge_soc', 'sensor'),
        ] } };
      } else if (id === 'export_bonus') {
        over = { name: 'Battery export bonus', entry_conditions: { match: 'ALL', conditions: [
          this._tleaf('tariff.bonus_window_active', '==', 1),
          this._tleaf('battery.soc_pct', '>', 'const.min_discharge_soc', 'sensor'),
        ] } };
      } else if (id === 'ausgrid_evening') {
        // 4–9pm network peak / export-reward window — discharge (covers load,
        // then exports). Uses a recurring window (works without tariff config).
        over = {
          name: 'Ausgrid — Evening discharge (4–9pm)',
          trigger_type: 'window', days: [], windows: [{ start: '16:00', end: '21:00' }],
          entry_conditions: { match: 'ALL', conditions: [
            this._tleaf('battery.soc_pct', '>', 'const.min_discharge_soc', 'sensor'),
          ] },
        };
      } else if (id === 'ausgrid_sponge') {
        // 10am–3pm solar-sponge window — charge from solar to self-consume and
        // avoid the export charge. Charges up to the Max-Charge SoC.
        over = {
          name: 'Ausgrid — Solar sponge (10am–3pm)',
          trigger_type: 'window', days: [], windows: [{ start: '10:00', end: '15:00' }],
          action: 'force_charge',
          entry_conditions: { match: 'ALL', conditions: [
            this._tleaf('battery.soc_pct', '<', 'const.max_charge_soc', 'sensor'),
          ] },
          exit_conditions: { match: 'ANY', conditions: [
            this._tleaf('battery.soc_pct', '>=', 'const.max_charge_soc', 'sensor'),
          ] },
        };
      } else {
        return;
      }
      this._clearTrace();
      this.form = this._blankForm({ ...common, ...over });
      this.formOpen = true;
    },

    editEntry(e) {
      this._clearTrace();
      const p = e.params || {};
      const s = e.trigger_spec || {};
      let trigger_type = 'window';
      if (e.trigger_kind) trigger_type = e.trigger_kind;
      else if (e.when_spec && e.when_spec.date) trigger_type = 'once';
      this.form = this._blankForm({
        id: e.id,
        name: e.name,
        trigger_type,
        date: (e.when_spec && e.when_spec.date) || todayISO(),
        days: e.trigger_kind === 'weekly'
          ? (s.days_of_week || []).slice()
          : ((e.when_spec && e.when_spec.days) || []).slice(),
        windows: ((e.when_spec && e.when_spec.windows) || [{ start: '14:00', end: '19:00' }]).map(w => ({ ...w })),
        time_of_day: s.time_of_day || '18:00',
        interval_min: s.every_seconds ? Math.round(s.every_seconds / 60) : 60,
        anchor_time: s.anchor_time || '',
        month_day: s.day || 1,
        months: (s.months || []).slice(),
        cron_expr: s.expr || '0 0 1 * *',
        duration_min: e.duration_s ? Math.round(e.duration_s / 60) : 0,
        action: e.action,
        power_w: p.power_w ?? 1000,
        power_pct: p.power_pct ?? 100,
        power_unit: ('power_pct' in p) ? 'pct' : 'W',
        pct: p.pct ?? 20,
        mode: p.mode ?? 'TOU',
        target_type: e.target_type,
        target_id: e.target_id || '',
        release: e.release,
        release_policy: e.release_policy || 'restore_prior_mode',
        conflict: e.conflict,
        missed_policy: e.missed_policy || 'late_fire_remaining',
        priority: e.priority,
        enabled: e.enabled,
        entry_hold_s: e.entry_hold_s || 0,
        ha_actions: (e.ha_actions || []).map((a) => ({
          ...a, data: { ...(a.data || {}) }, guard: a.guard ? { ...a.guard } : null,
        })),
        entry_conditions: e.entry_conditions ? this._cloneTree(e.entry_conditions) : emptyTree(),
        exit_conditions: e.exit_conditions ? this._cloneTree(e.exit_conditions) : emptyTree(),
      });
      this.formOpen = true;
    },

    _cloneTree(t) {
      const clone = (node) => ({
        match: node.match || 'ALL',
        conditions: (node.conditions || []).map(c => (c.conditions ? clone(c) : { ...c })),
      });
      return clone(t);
    },

    // Hide via formOpen; keep `form` intact so no `form.X` binding evaluates
    // against null during Alpine's x-if teardown tick.
    closeForm() { this.formOpen = false; this._clearTrace(); },

    // ── which sections are visible for the chosen trigger type ──
    // All guard on `this.form?.` so they never throw during the modal's
    // open/close teardown tick (form is null while the modal is closed).
    get isTrigger() { return !['window', 'once'].includes(this.form?.trigger_type); },
    get showWindows() { return ['window', 'once'].includes(this.form?.trigger_type); },
    get showDays() { return this.form?.trigger_type === 'window' || this.form?.trigger_type === 'weekly'; },
    get showTimeOfDay() { return ['daily', 'weekly'].includes(this.form?.trigger_type); },
    get showInterval() { return this.form?.trigger_type === 'interval'; },
    get showDate() { return this.form?.trigger_type === 'once'; },
    get showMonthly() { return this.form?.trigger_type === 'monthly'; },
    get showCron() { return this.form?.trigger_type === 'cron'; },

    // Apply a quick preset → sets the trigger type + fills its spec fields
    // (which remain editable). No-op for the placeholder option.
    applyPreset(id) {
      const p = this.triggerPresets.find(x => x.id === id);
      if (!p) return;
      this.form.trigger_type = p.type;
      if (p.interval_min != null) this.form.interval_min = p.interval_min;
      if (p.time_of_day) this.form.time_of_day = p.time_of_day;
      this.form.days = p.days ? p.days.slice() : [];
      if (p.month_day != null) this.form.month_day = p.month_day;
      if (p.months != null) this.form.months = p.months.slice();
    },

    toggleDay(i) {
      const idx = this.form.days.indexOf(i);
      if (idx >= 0) this.form.days.splice(idx, 1);
      else this.form.days.push(i);
    },

    monthLabels: MONTH_LABELS,
    toggleMonth(m) {
      const idx = this.form.months.indexOf(m);
      if (idx >= 0) this.form.months.splice(idx, 1);
      else this.form.months.push(m);
    },

    addWindow() { this.form.windows.push({ start: '00:00', end: '06:00' }); },
    removeWindow(i) { this.form.windows.splice(i, 1); },

    // ── condition rows + nested groups ───────────────────────
    _newLeaf() {
      const s = this.sensors[0] ? this.sensors[0].id : 'battery.soc_pct';
      return {
        sensor: s, op: '<', value: 0, value2: 0,
        value_kind: 'value', value_sensor: '',  // RHS: literal Value or Lookup(sensor)
        _cid: ++this._cidSeq,
      };
    },
    isGroup(c) { return !!(c && c.conditions); },
    addCond(which) { this.form[which].conditions.push(this._newLeaf()); this._clearTrace(); },
    addCondGroup(which) { this.form[which].conditions.push({ match: 'ALL', conditions: [] }); this._clearTrace(); },
    removeCond(which, i) { this.form[which].conditions.splice(i, 1); this._clearTrace(); },
    addToGroup(group) { group.conditions.push(this._newLeaf()); this._clearTrace(); },
    removeFromGroup(group, j) { group.conditions.splice(j, 1); this._clearTrace(); },

    // Verification trace helpers (per-condition highlight).
    _clearTrace() { this.traceByCid = {}; this.testResult = null; this.exitTestResult = null; },
    _ensureCids(tree) {
      const walk = (node) => (node.conditions || []).forEach((c) => {
        if (c.conditions) walk(c);
        else if (c._cid == null) c._cid = ++this._cidSeq;
      });
      if (tree) walk(tree);
    },
    condFailed(c) { const t = this.traceByCid[c._cid]; return !!t && t.result === false; },
    condPassed(c) { const t = this.traceByCid[c._cid]; return !!t && t.result === true; },
    condTested(c) { return this.traceByCid[c._cid] !== undefined; },
    condLive(c) { const t = this.traceByCid[c._cid]; return t ? t.live_value : undefined; },
    /** Word, not just a colour — "fails"/"passes" is unambiguous where a red
     *  outline alone reads as an error rather than a test result. A missing
     *  live value is its own case: the condition couldn't be evaluated. */
    condVerdict(c) {
      if (!this.condTested(c)) return '';
      const live = this.condLive(c);
      if (live === undefined || live === null) return '✕ no value';
      return this.condFailed(c) ? '✕ fails now' : '✓ passes now';
    },
    condVerdictClass(c) {
      const live = this.condLive(c);
      if (live === undefined || live === null) return 'text-amber-400';
      return this.condFailed(c) ? 'text-red-400' : 'text-emerald-400';
    },
    /** Ring colour for a tested row: green passes, red fails, amber no value. */
    condRingClass(c) {
      if (!this.condTested(c)) return '';
      const live = this.condLive(c);
      if (live === undefined || live === null) return 'ring-1 ring-amber-500/70 rounded px-1 py-0.5';
      return this.condFailed(c)
        ? 'ring-1 ring-red-500/70 rounded px-1 py-0.5'
        : 'ring-1 ring-emerald-500/60 rounded px-1 py-0.5';
    },

    formActionMeta() { return this.actionMeta(this.form.action); },

    _buildParams() {
      const f = this.form;
      const meta = this.actionMeta(f.action);
      const p = {};
      if (meta.params.includes('power')) {
        if (f.power_unit === 'pct') p.power_pct = Number(f.power_pct);
        else p.power_w = Number(f.power_w);
      }
      if (meta.params.includes('pct')) p.pct = Number(f.pct);
      if (meta.params.includes('mode')) p.mode = f.mode;
      return p;
    },

    // Coerce a value to a number when it looks numeric (so "50" compares numerically).
    _coerceVal(v) {
      if (v === true || v === false) return v;
      if (v === '' || v === null || v === undefined) return v;
      const n = Number(v);
      return Number.isNaN(n) ? v : n;
    },

    // Build a condition tree for the API (recursively), or null when empty.
    // Empty nested groups are pruned so they don't skew ALL/ANY evaluation.
    _buildTree(tree, withCid = false) {
      if (!tree || !tree.conditions.length) return null;
      const build = (node) => {
        const conds = [];
        for (const c of node.conditions) {
          if (c.conditions) {
            if (c.conditions.length) conds.push(build(c));  // skip empty groups
          } else {
            const row = { sensor: c.sensor, op: c.op };
            if (c.value_kind === 'sensor') {
              row.value_kind = 'sensor';
              row.value_sensor = c.value_sensor || '';
            } else {
              row.value = this._coerceVal(c.value);
              if (c.op === 'between') row.value2 = this._coerceVal(c.value2);
            }
            if (withCid && c._cid != null) row.cid = c._cid;  // UI-only, for highlight
            conds.push(row);
          }
        }
        return { match: node.match, conditions: conds };
      };
      const built = build(tree);
      return built.conditions.length ? built : null;
    },

    _buildTriggerSpec() {
      const f = this.form;
      if (f.trigger_type === 'daily') return { time_of_day: f.time_of_day };
      if (f.trigger_type === 'weekly') return { time_of_day: f.time_of_day, days_of_week: f.days.slice() };
      if (f.trigger_type === 'interval') {
        const spec = { every_seconds: Math.max(1, Number(f.interval_min) || 1) * 60 };
        if (f.anchor_time) spec.anchor_time = f.anchor_time;
        return spec;
      }
      if (f.trigger_type === 'monthly') {
        return {
          day: Math.min(31, Math.max(1, Number(f.month_day) || 1)),
          months: (f.months || []).slice().sort((a, b) => a - b),
          time_of_day: f.time_of_day,
        };
      }
      if (f.trigger_type === 'cron') return { expr: (f.cron_expr || '').trim() };
      return {};
    },

    async testVerification(which = 'entry_conditions') {
      const isExit = which === 'exit_conditions';
      this._ensureCids(this.form[which]);
      const tree = this._buildTree(this.form[which], true);
      const setResult = (r) => { if (isExit) this.exitTestResult = r; else this.testResult = r; };
      if (!tree) {
        setResult({ result: null, msg: `No ${isExit ? 'exit' : 'entry'} conditions to test.` });
        return;
      }
      const gw = this.form.target_type === 'gateway' ? (this.form.target_id || 'default') : 'default';
      const res = await fetchJSON(`api/scheduler/evaluate?gateway=${encodeURIComponent(gw)}`, {
        method: 'POST', body: JSON.stringify(tree),
      });
      const out = res && !res.error ? res : { result: null, msg: res?.error || 'Evaluation failed' };
      setResult(out);
      // Map the trace back to rows (by cid) for per-condition highlighting.
      (out.per_condition || []).forEach((t) => {
        if (t.cid != null) this.traceByCid[t.cid] = { result: t.result, live_value: t.live_value };
      });
    },

    async saveEntry() {
      const f = this.form;
      if (!f.name.trim()) { Alpine.store('app').toast('Name is required', 'error'); return; }
      if (this.showWindows && !f.windows.length) { Alpine.store('app').toast('Add at least one time window', 'error'); return; }
      if (f.trigger_type === 'once' && !f.date) { Alpine.store('app').toast('Pick a date', 'error'); return; }

      // Editing an entry that is firing RIGHT NOW → warn before we alter a live
      // battery command. The engine re-applies on save (re-dispatches if the
      // action changed, else the new gates/exits take effect next tick), so make
      // that explicit rather than silently changing a running dispatch.
      const live = f.id ? this.schedules.find((s) => s.id === f.id) : null;
      if (live && live.active_now) {
        const go = await Alpine.store('app').confirm({
          title: 'This automation is running now',
          message: `"${f.name}" is active. Save and apply the new settings to the live run? `
            + `It resumes immediately under the updated rules.`,
          confirmLabel: 'Apply now',
        });
        if (!go) return;
      }

      const body = {
        name: f.name.trim(),
        action: f.action,
        params: this._buildParams(),
        target_type: f.target_type,
        target_id: f.target_type === 'site' ? null : (f.target_id || 'default'),
        release: f.release,
        release_policy: f.release_policy,
        conflict: f.conflict,
        missed_policy: f.missed_policy,
        priority: Number(f.priority) || 0,
        enabled: f.enabled,
        entry_hold_s: Number(f.entry_hold_s) || 0,
        ha_actions: (f.ha_actions || [])
          .filter((a) => a.instance_id && a.entity_id && a.service)
          .map((a) => {
            const o = {
              instance_id: a.instance_id, entity_id: a.entity_id,
              service: a.service, data: a.data || {}, when: a.when || 'fire',
            };
            if (a.guard && a.guard.sensor) {
              o.guard = { sensor: a.guard.sensor, op: a.guard.op, value: this._coerceVal(a.guard.value) };
            }
            return o;
          }),
        entry_conditions: this._buildTree(f.entry_conditions),
        exit_conditions: this._buildTree(f.exit_conditions),
      };

      // Always write BOTH the window spec AND the trigger fields, explicitly
      // clearing whichever the chosen type doesn't use. Otherwise switching an
      // entry from windowed→trigger (or back) leaves the old spec behind — e.g.
      // a daily entry keeping a stale one-time `when_spec.date`, which renders
      // as a broken When/timeline. (Relies on the PATCH honouring explicit nulls.)
      if (f.trigger_type === 'window') {
        body.when_spec = { days: f.days, windows: f.windows };
        body.trigger_kind = null;
        body.trigger_spec = {};
      } else if (f.trigger_type === 'once') {
        body.when_spec = { date: f.date, windows: f.windows };
        body.trigger_kind = null;
        body.trigger_spec = {};
      } else {
        body.trigger_kind = f.trigger_type;
        body.trigger_spec = this._buildTriggerSpec();
        body.when_spec = { days: [], windows: [] };  // drop stale windows/date
        body.duration_s = Number(f.duration_min) > 0 ? Number(f.duration_min) * 60 : null;
      }

      this.loading = true;
      let res;
      if (f.id) {
        res = await fetchJSON(`api/schedules/${f.id}`, { method: 'PATCH', body: JSON.stringify(body) });
      } else {
        res = await fetchJSON('api/schedules', { method: 'POST', body: JSON.stringify(body) });
      }
      this.loading = false;
      if (res && !res.error) {
        Alpine.store('app').toast(f.id ? 'Schedule updated' : 'Schedule created', 'info');
        this.closeForm();
        // Saved but disabled → it won't run. Offer to enable it now rather than
        // let a schedule silently sit inert (a common "why didn't it fire?" trap).
        if (!body.enabled && res.id) {
          const enable = await Alpine.store('app').confirm({
            title: 'Enable now?',
            message: `"${body.name}" is saved but disabled, so it won't run. Enable it?`,
            confirmLabel: 'Enable',
          });
          if (enable) {
            const upd = await fetchJSON(`api/schedules/${res.id}`, {
              method: 'PATCH', body: JSON.stringify({ enabled: true }),
            });
            if (upd && !upd.error) Alpine.store('app').toast('Schedule enabled', 'info');
            else Alpine.store('app').toast('Enable failed', 'error');
          }
        }
        this.load();
      } else {
        Alpine.store('app').toast(`Save failed: ${res?.error || 'unknown'}`, 'error');
      }
    },

    async executeEntry(e) {
      if (!await Alpine.store('app').confirm({
        title: 'Fire now?', message: `Run "${e.name}" immediately?`, confirmLabel: 'Fire',
      })) return;
      const res = await fetchJSON(`api/schedules/${e.id}/execute`, { method: 'POST' });
      if (res && res.status === 'fired') Alpine.store('app').toast(`Fired ${e.name}`, 'info');
      else if (res && res.status === 'gated') Alpine.store('app').toast('Skipped — entry conditions not met', 'error');
      else Alpine.store('app').toast(`Execute: ${res?.status || 'failed'}`, 'error');
      this.load();
    },

    // Gracefully stop the CURRENT run (release now, resume next window). The
    // entry stays enabled; Disable is the permanent stop.
    async stopEntry(e) {
      if (!await Alpine.store('app').confirm({
        title: 'Stop this run?',
        message: `"${e.name}" is running now. Release control and let it resume at the next scheduled time? `
          + `To stop it permanently, disable it instead.`,
        confirmLabel: 'Stop now', danger: true,
      })) return;
      const res = await fetchJSON(`api/schedules/${e.id}/stop`, { method: 'POST' });
      if (res && res.status === 'stopped') Alpine.store('app').toast(`Stopped ${e.name} — released`, 'info');
      else if (res && res.status === 'not_active') Alpine.store('app').toast('Nothing active to stop', 'info');
      else Alpine.store('app').toast(`Stop: ${res?.error || res?.status || 'failed'}`, 'error');
      this.load();
    },

    async toggleEnabled(e) {
      const res = await fetchJSON(`api/schedules/${e.id}`, {
        method: 'PATCH', body: JSON.stringify({ enabled: !e.enabled }),
      });
      if (res && !res.error) this.load();
      else Alpine.store('app').toast('Toggle failed', 'error');
    },

    async deleteEntry(e) {
      if (!await Alpine.store('app').confirm({
        title: 'Delete schedule?', message: `"${e.name}" will be removed.`,
        confirmLabel: 'Delete', danger: true,
      })) return;
      const res = await fetchJSON(`api/schedules/${e.id}`, { method: 'DELETE' });
      if (res && res.deleted) { Alpine.store('app').toast('Schedule deleted', 'info'); this.load(); }
      else Alpine.store('app').toast('Delete failed', 'error');
    },

    // Duplicate an entry: POST a clone with a "(copy)" name. Copies start
    // DISABLED so a clone can't silently start dispatching before it's reviewed.
    async copyEntry(e) {
      const body = {
        name: `${e.name} (copy)`,
        action: e.action,
        params: { ...(e.params || {}) },
        target_type: e.target_type,
        target_id: e.target_type === 'site' ? null : (e.target_id || 'default'),
        enabled: false,
        release: e.release,
        conflict: e.conflict,
        priority: e.priority || 0,
        release_policy: e.release_policy || 'restore_prior_mode',
        missed_policy: e.missed_policy || 'late_fire_remaining',
        entry_hold_s: e.entry_hold_s || 0,
        ha_actions: (e.ha_actions || []).map((a) => ({ ...a, data: { ...(a.data || {}) } })),
        entry_conditions: e.entry_conditions || null,
        exit_conditions: e.exit_conditions || null,
      };
      if (e.trigger_kind) {
        body.trigger_kind = e.trigger_kind;
        body.trigger_spec = { ...(e.trigger_spec || {}) };
        if (e.duration_s) body.duration_s = e.duration_s;
      } else {
        body.when_spec = e.when_spec || { days: [], windows: [] };
      }
      const res = await fetchJSON('api/schedules', { method: 'POST', body: JSON.stringify(body) });
      if (res && !res.error) {
        Alpine.store('app').toast(`Copied "${e.name}" (disabled)`, 'info');
        this.load();
      } else {
        Alpine.store('app').toast(`Copy failed: ${res?.error || 'unknown'}`, 'error');
      }
    },

    // ── Export / Import (share as JSON) ──────────────────────
    _download(filename, obj) {
      const blob = new Blob([JSON.stringify(obj, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = filename; a.click();
      URL.revokeObjectURL(url);
    },
    async exportAll() {
      const d = await fetchJSON('api/schedules/export');
      if (d && !d.error) this._download('franklinwh-automations.json', d);
      else Alpine.store('app').toast('Export failed', 'error');
    },
    async exportEntry(e) {
      const d = await fetchJSON('api/schedules/export?ids=' + e.id);
      if (d && !d.error) {
        const slug = (e.name || 'automation').replace(/[^a-z0-9]+/gi, '-').toLowerCase();
        this._download(`automation-${slug}.json`, d);
      } else Alpine.store('app').toast('Export failed', 'error');
    },

    // import
    importOpen: false, importText: '', importReport: null, importing: false, importFileName: '',
    openImport() {
      this.importOpen = true; this.importText = ''; this.importReport = null; this.importFileName = '';
    },
    closeImport() { this.importOpen = false; },
    async onImportFile(ev) {
      const file = ev.target.files && ev.target.files[0];
      if (!file) return;
      this.importFileName = file.name;
      this.importText = await file.text();
      this.importReport = null;
    },
    _parseImport() {
      try { return JSON.parse(this.importText); }
      catch (e) { Alpine.store('app').toast('Invalid JSON: ' + e.message, 'error'); return null; }
    },
    async validateImport() {
      const bundle = this._parseImport();
      if (!bundle) return;
      const r = await fetchJSON('api/schedules/import?dry_run=true', {
        method: 'POST', body: JSON.stringify(bundle),
      });
      if (r && !r.error) this.importReport = r;
      else Alpine.store('app').toast('Validation failed: ' + (r?.error || 'unknown'), 'error');
    },
    async runImport() {
      const bundle = this._parseImport();
      if (!bundle) return;
      this.importing = true;
      const r = await fetchJSON('api/schedules/import?dry_run=false', {
        method: 'POST', body: JSON.stringify(bundle),
      });
      this.importing = false;
      if (r && !r.error) {
        const skip = r.skipped && r.skipped.length ? `, ${r.skipped.length} skipped` : '';
        Alpine.store('app').toast(`Imported ${r.created.length} (disabled)${skip}`, 'info');
        this.closeImport();
        this.load();
      } else {
        Alpine.store('app').toast('Import failed: ' + (r?.error || 'unknown'), 'error');
      }
    },
  };
}
