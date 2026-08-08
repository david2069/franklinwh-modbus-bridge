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
  { id: 'always',   label: 'Always evaluate (sensor-driven)', kind: 'always' },
];

const OPERATORS = ['<', '<=', '==', '!=', '>=', '>', 'between'];

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
    audit: [],
    auditFilter: '',
    conn: { connected: true, gateways: {}, recent_outages: [] },
    expandedId: null,
    previewDay: new Date().getDay() === 0 ? 6 : new Date().getDay() - 1, // Mon=0
    loading: false,
    _interval: null,

    auditStatuses: ['fired', 'executed', 'gated', 'missed', 'exit_condition_met'],

    // edit form ('null' = closed)
    form: null,
    testResult: null,

    actions: SCHEDULE_ACTIONS,
    weekdays: WEEKDAYS,
    triggerTypes: TRIGGER_TYPES,
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
      return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`;
    },

    auditClass(r) {
      return ({
        fired: 'text-emerald-300', executed: 'text-emerald-300',
        gated: 'text-amber-300', deferred: 'text-amber-300', hold: 'text-amber-300',
        missed: 'text-red-300', failed: 'text-red-300',
        exit_condition_met: 'text-cyan-300', duration_elapsed: 'text-slate-400',
        release: 'text-slate-400', ok: 'text-emerald-300',
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

    // Readable one-line rendering of a condition tree (one level of nesting shown as "(…)").
    treeText(t) {
      if (!t || !t.conditions || !t.conditions.length) return '—';
      const join = t.match === 'ANY' ? ' OR ' : ' AND ';
      return t.conditions.map(c =>
        c.conditions ? '(…)' : `${c.sensor} ${c.op} ${c.value}${c.op === 'between' ? '..' + c.value2 : ''}`
      ).join(join);
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

    // ── timeline geometry ────────────────────────────────────
    get visualSegments() {
      const out = [];
      for (const seg of this.timeline.segments) {
        const colour = this.actionMeta(seg.action).colour;
        if (seg.end_min <= seg.start_min) {
          out.push({ ...seg, colour, _l: seg.start_min, _w: 1440 - seg.start_min });
          out.push({ ...seg, colour, _l: 0, _w: seg.end_min });
        } else {
          out.push({ ...seg, colour, _l: seg.start_min, _w: seg.end_min - seg.start_min });
        }
      }
      return out;
    },

    pct(min) { return (min / 1440) * 100; },
    get nowPct() { return this.pct(this.timeline.now_min); },

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
        duration_min: 0,
        action: 'force_charge',
        power_w: 1000,
        power_pct: 0,
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
        entry_conditions: emptyTree(),
        exit_conditions: emptyTree(),
      }, overrides || {});
    },

    newEntry() {
      this.testResult = null;
      this.form = this._blankForm();
    },

    editEntry(e) {
      this.testResult = null;
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
        duration_min: e.duration_s ? Math.round(e.duration_s / 60) : 0,
        action: e.action,
        power_w: p.power_w ?? 1000,
        power_pct: p.power_pct ?? 0,
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
        entry_conditions: e.entry_conditions ? this._cloneTree(e.entry_conditions) : emptyTree(),
        exit_conditions: e.exit_conditions ? this._cloneTree(e.exit_conditions) : emptyTree(),
      });
    },

    _cloneTree(t) {
      return { match: t.match || 'ALL', conditions: (t.conditions || []).map(c => ({ ...c })) };
    },

    closeForm() { this.form = null; this.testResult = null; },

    // ── which sections are visible for the chosen trigger type ──
    get isTrigger() { return !['window', 'once'].includes(this.form.trigger_type); },
    get showWindows() { return ['window', 'once'].includes(this.form.trigger_type); },
    get showDays() { return this.form.trigger_type === 'window' || this.form.trigger_type === 'weekly'; },
    get showTimeOfDay() { return ['daily', 'weekly'].includes(this.form.trigger_type); },
    get showInterval() { return this.form.trigger_type === 'interval'; },
    get showDate() { return this.form.trigger_type === 'once'; },

    toggleDay(i) {
      const idx = this.form.days.indexOf(i);
      if (idx >= 0) this.form.days.splice(idx, 1);
      else this.form.days.push(i);
    },

    addWindow() { this.form.windows.push({ start: '00:00', end: '06:00' }); },
    removeWindow(i) { this.form.windows.splice(i, 1); },

    // ── condition rows ───────────────────────────────────────
    addCond(which) {
      const firstSensor = this.sensors[0] ? this.sensors[0].id : 'battery.soc_pct';
      this.form[which].conditions.push({ sensor: firstSensor, op: '<', value: 0, value2: 0 });
    },
    removeCond(which, i) { this.form[which].conditions.splice(i, 1); },

    formActionMeta() { return this.actionMeta(this.form.action); },

    _buildParams() {
      const f = this.form;
      const meta = this.actionMeta(f.action);
      const p = {};
      if (meta.params.includes('power')) {
        if (Number(f.power_pct) > 0) p.power_pct = Number(f.power_pct);
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

    // Build a condition tree for the API, or null when there are no rows.
    _buildTree(tree) {
      if (!tree || !tree.conditions.length) return null;
      return {
        match: tree.match,
        conditions: tree.conditions.map(c => {
          const row = { sensor: c.sensor, op: c.op, value: this._coerceVal(c.value) };
          if (c.op === 'between') row.value2 = this._coerceVal(c.value2);
          return row;
        }),
      };
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
      return {};
    },

    async testVerification() {
      const tree = this._buildTree(this.form.entry_conditions);
      if (!tree) { this.testResult = { result: null, msg: 'No entry conditions to test.' }; return; }
      const gw = this.form.target_type === 'gateway' ? (this.form.target_id || 'default') : 'default';
      const res = await fetchJSON(`api/scheduler/evaluate?gateway=${encodeURIComponent(gw)}`, {
        method: 'POST', body: JSON.stringify(tree),
      });
      this.testResult = res && !res.error ? res : { result: null, msg: res?.error || 'Evaluation failed' };
    },

    async saveEntry() {
      const f = this.form;
      if (!f.name.trim()) { Alpine.store('app').toast('Name is required', 'error'); return; }
      if (this.showWindows && !f.windows.length) { Alpine.store('app').toast('Add at least one time window', 'error'); return; }
      if (f.trigger_type === 'once' && !f.date) { Alpine.store('app').toast('Pick a date', 'error'); return; }

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
        entry_conditions: this._buildTree(f.entry_conditions),
        exit_conditions: this._buildTree(f.exit_conditions),
      };

      if (f.trigger_type === 'window') {
        body.when_spec = { days: f.days, windows: f.windows };
      } else if (f.trigger_type === 'once') {
        body.when_spec = { date: f.date, windows: f.windows };
      } else {
        body.trigger_kind = f.trigger_type;
        body.trigger_spec = this._buildTriggerSpec();
        if (Number(f.duration_min) > 0) body.duration_s = Number(f.duration_min) * 60;
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
        this.load();
      } else {
        Alpine.store('app').toast(`Save failed: ${res?.error || 'unknown'}`, 'error');
      }
    },

    async executeEntry(e) {
      if (!confirm(`Fire "${e.name}" now?`)) return;
      const res = await fetchJSON(`api/schedules/${e.id}/execute`, { method: 'POST' });
      if (res && res.status === 'fired') Alpine.store('app').toast(`Fired ${e.name}`, 'info');
      else if (res && res.status === 'gated') Alpine.store('app').toast('Skipped — entry conditions not met', 'error');
      else Alpine.store('app').toast(`Execute: ${res?.status || 'failed'}`, 'error');
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
      if (!confirm(`Delete schedule "${e.name}"?`)) return;
      const res = await fetchJSON(`api/schedules/${e.id}`, { method: 'DELETE' });
      if (res && res.deleted) { Alpine.store('app').toast('Schedule deleted', 'info'); this.load(); }
      else Alpine.store('app').toast('Delete failed', 'error');
    },
  };
}
