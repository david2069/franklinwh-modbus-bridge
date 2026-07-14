/**
 * Schedule Tab (SCH2) — time → command-handler dispatch editor.
 *
 * Modelled on the FWHAI TOU editor: a 24h visual timeline coloured by ACTION
 * (not tariff), plus an entry table. Drives the bridge's own REST schedule API.
 * See docs/scheduling-and-orchestration-design.md §6.
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

function todayISO() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function scheduleTab() {
  return {
    schedules: [],
    timeline: { segments: [], now_min: 0, weekday: 0 },
    services: [],
    previewDay: new Date().getDay() === 0 ? 6 : new Date().getDay() - 1, // Mon=0
    loading: false,
    _interval: null,

    // edit form ('null' = closed)
    form: null,

    actions: SCHEDULE_ACTIONS,
    weekdays: WEEKDAYS,

    init() {
      this.load();
      window.addEventListener('tab:changed', (e) => {
        if (e.detail.tab === 'schedule') this.load();
      });
      this._interval = setInterval(() => {
        if (Alpine.store('app').activeTab === 'schedule') this.load();
      }, 20000);
    },

    async load() {
      const [sch, tl, svc] = await Promise.all([
        fetchJSON('api/schedules'),
        fetchJSON(`api/schedules/timeline?day=${this.previewDay}`),
        fetchJSON('api/services'),
      ]);
      if (sch && sch.schedules) this.schedules = sch.schedules;
      if (tl && tl.segments) this.timeline = tl;
      if (svc && svc.services) this.services = svc.services;
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

    targetLabel(e) {
      if (e.target_type === 'site') return 'Site (all gateways)';
      if (e.target_type === 'service') {
        const s = this.services.find(x => x.id === e.target_id);
        return s ? `Service: ${s.name}` : 'Service';
      }
      const gw = Alpine.store('app').gatewayList.find(g => g.id === (e.target_id || 'default'));
      return gw ? gw.name : (e.target_id || 'Default Gateway');
    },

    daysLabel(when) {
      if (when && when.date) {
        return (when.date < todayISO() ? 'Once (passed) ' : 'Once ') + when.date;
      }
      const d = (when && when.days) || [];
      if (!d.length) return 'Every day';
      if (d.length === 7) return 'Every day';
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
      // A one-time (dated) entry always spells out the date -- "Mon" would be
      // ambiguous (this Monday vs. a recurring one), which is the exact
      // confusion this feature exists to remove.
      if (e.when_spec && e.when_spec.date) return `${e.when_spec.date} ${t}`;
      const daysAway = Math.round((dt - now) / 86400000);
      if (daysAway >= 0 && daysAway < 7) return `${WEEKDAYS[(dt.getDay() + 6) % 7]} ${t}`;
      return `${dt.toISOString().slice(0, 10)} ${t}`;
    },

    // ── timeline geometry ────────────────────────────────────
    // Returns flat visual segments (wrapping windows split at midnight).
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
    newEntry() {
      this.form = {
        id: null,
        name: '',
        repeats: true,
        date: todayISO(),
        days: [],
        windows: [{ start: '14:00', end: '19:00' }],
        action: 'force_charge',
        power_w: 1000,
        power_pct: 0,
        pct: 20,
        mode: 'TOU',
        target_type: (Alpine.store('app').activeGateway === 'site') ? 'site' : 'gateway',
        target_id: (Alpine.store('app').activeGateway === 'site') ? '' : Alpine.store('app').activeGateway,
        release: 'release',
        conflict: 'defer',
        priority: 0,
        enabled: true,
      };
    },

    editEntry(e) {
      const p = e.params || {};
      this.form = {
        id: e.id,
        name: e.name,
        repeats: !(e.when_spec && e.when_spec.date),
        date: (e.when_spec && e.when_spec.date) || todayISO(),
        days: ((e.when_spec && e.when_spec.days) || []).slice(),
        windows: ((e.when_spec && e.when_spec.windows) || [{ start: '14:00', end: '19:00' }]).map(w => ({ ...w })),
        action: e.action,
        power_w: p.power_w ?? 1000,
        power_pct: p.power_pct ?? 0,
        pct: p.pct ?? 20,
        mode: p.mode ?? 'TOU',
        target_type: e.target_type,
        target_id: e.target_id || '',
        release: e.release,
        conflict: e.conflict,
        priority: e.priority,
        enabled: e.enabled,
      };
    },

    closeForm() { this.form = null; },

    toggleDay(i) {
      const idx = this.form.days.indexOf(i);
      if (idx >= 0) this.form.days.splice(idx, 1);
      else this.form.days.push(i);
    },

    addWindow() { this.form.windows.push({ start: '00:00', end: '06:00' }); },
    removeWindow(i) { this.form.windows.splice(i, 1); },

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

    async saveEntry() {
      const f = this.form;
      if (!f.name.trim()) { Alpine.store('app').toast('Name is required', 'error'); return; }
      if (!f.windows.length) { Alpine.store('app').toast('Add at least one time window', 'error'); return; }
      if (!f.repeats && !f.date) { Alpine.store('app').toast('Pick a date for a one-time entry', 'error'); return; }
      const body = {
        name: f.name.trim(),
        when_spec: f.repeats
          ? { days: f.days, windows: f.windows }
          : { date: f.date, windows: f.windows },
        action: f.action,
        params: this._buildParams(),
        target_type: f.target_type,
        target_id: f.target_type === 'site' ? null : (f.target_id || 'default'),
        release: f.release,
        conflict: f.conflict,
        priority: Number(f.priority) || 0,
        enabled: f.enabled,
      };
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
