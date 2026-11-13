/**
 * Settings → Home Assistant → Notifications (#296 item 48, phase 1).
 *
 * The Push notifications switch, Status notices under it, the Notifications check,
 * Auto-notify triggers (api/notify/triggers, as the Direct Connect bridge's), Companion Devices (add, discover, test, test with buttons, broadcast) and Recent
 * notifications. Data from api/notify/*; the switch is the critical-alert channel's
 * setting, Status notices its own (api/alerts/settings).
 *
 * Results of an action inside the open device or broadcast dialog stay in that dialog;
 * a row's Test results stay on the row (no toasts from dialogs).
 */
function notifyCard() {
  return {
    push: true,
    statusNotices: true,
    busy: false,
    checks: [],
    checking: false,
    devices: [],
    instances: [],
    log: [],
    error: '',
    loaded: false,
    // Per-device row results (Test with buttons): id -> {kind, text}
    rowResult: {},
    // Test with buttons in flight: id -> request_id
    waiting: {},
    // Add / Edit dialog, as the Direct Connect bridge's
    dlg: false,
    form: { id: null, alias: '', instance_id: '', service: '', kind: 'other' },
    found: [],
    filter: '',
    pickerOpen: false,
    dlgMsg: null,
    // Broadcast dialog
    bc: false,
    bcTitle: 'FranklinWH',
    bcMessage: '',
    bcResult: null,
    // Auto-notify triggers: config.triggers, per-trigger stats, a failed save's message,
    // "Chosen devices" picked before any device is ticked, and the Send test dialog.
    trg: {},
    trgStats: {},
    trgErr: '',
    trgPick: {},
    tt: null,

    init() { this.loadAll(); },

    async loadAll() {
      this.error = '';
      try {
        const [s, a, d] = await Promise.all([
          fetchJSON('api/notify/settings'), fetchJSON('api/alerts/settings'),
          fetchJSON('api/notify/devices'),
        ]);
        this.push = s.push;
        this.statusNotices = a.status_notices;
        this.devices = d.devices;
        this.instances = d.instances;
        this.loaded = true;
      } catch (e) {
        this.error = 'Could not load notifications: ' + e.message;
      }
      this.loadChecks();
      this.loadTriggers();
      this.loadLog();
    },

    async loadTriggers() {
      try {
        const d = await fetchJSON('api/notify/triggers');
        this.trg = d.config.triggers;
        this.trgStats = d.stats || {};
      } catch (_) { /* leave the last known */ }
    },

    async saveTriggers(patch) {
      try {
        this.trg = (await fetchJSON('api/notify/triggers', {
          method: 'PUT', body: JSON.stringify({ triggers: patch }) })).config.triggers;
        this.trgErr = '';
      } catch (e) { this.trgErr = 'Save failed: ' + e.message; }
    },

    toggleTrigger(key) { this.saveTriggers({ [key]: { enabled: !this.trg[key].enabled } }); },

    setThreshold(key, val) {
      const n = Math.max(0, Math.min(100, parseInt(val, 10) || 0));
      this.saveTriggers({ [key]: { threshold: n } });
    },

    // The group heading (Scheduler) above the first row of its group.
    triggerGroupStart(key) {
      const all = Object.entries(this.trg || {});
      const i = all.findIndex(([k]) => k === key);
      const g = (all[i] || [])[1]?.group;
      return g && (i === 0 || all[i - 1][1].group !== g) ? g : '';
    },

    triggerStat(key) {
      const s = this.trgStats[key];
      return s && s.count ? `${s.count}× · last ${this.fmtTime(s.last_ts)}` : '';
    },

    triggerTargetValue(key, t) {
      return (t.target && t.target.type === 'device') || this.trgPick[key] ? 'device' : 'all';
    },

    triggerTargetIds(t) { return (t.target && t.target.type === 'device' && t.target.ids) || []; },

    // Chosen devices saves when the first device is ticked; All devices saves at once.
    setTriggerTarget(key, value) {
      this.trgPick[key] = value === 'device';
      if (value === 'all') this.saveTriggers({ [key]: { target: { type: 'all' } } });
    },

    // The last device ticked stays ticked (choose All devices instead).
    toggleTriggerDevice(key, t, id, on) {
      const ids = this.triggerTargetIds(t).filter(x => x !== id);
      if (on) ids.push(id);
      if (ids.length) this.saveTriggers({ [key]: { target: { type: 'device', ids } } });
    },

    openTriggerTest() { this.tt = { to: 'all', ids: [], sending: false, msg: null }; },

    async sendTriggerTest() {
      const t = this.tt; t.msg = null;
      if (t.to === 'device' && !t.ids.length) { t.msg = { kind: 'warn', text: 'Choose at least one device' }; return; }
      t.sending = true;
      try {
        const r = await fetchJSON('api/notify/triggers/test', { method: 'POST',
          body: JSON.stringify(t.to === 'all' ? {} : { device_ids: t.ids }) });
        t.msg = r.delivered ? { kind: 'ok', text: `Test sent to ${r.delivered} of ${r.total} device(s)` }
                            : { kind: 'warn', text: `Not delivered to any of ${r.total} device(s): ${r.summary}` };
        this.loadLog();
      } catch (e) { t.msg = { kind: 'danger', text: 'Test failed: ' + e.message }; }
      finally { t.sending = false; }
    },

    async loadChecks() {
      this.checking = true;
      try { this.checks = (await fetchJSON('api/notify/checks')).lines; }
      catch (e) { this.checks = []; this.error = 'Notifications check failed: ' + e.message; }
      finally { this.checking = false; }
    },

    async loadLog() {
      try { this.log = (await fetchJSON('api/notify/log?limit=20')).entries; }
      catch (_) { this.log = []; }
    },

    async togglePush() {
      this.busy = true;
      try {
        this.push = (await fetchJSON('api/notify/settings', {
          method: 'PUT', body: JSON.stringify({ push: !this.push }) })).push;
        Alpine.store('app').toast(`Push notifications ${this.push ? 'on' : 'off'}`, 'info');
        this.loadChecks();
      } catch (e) {
        Alpine.store('app').toast('Failed: ' + e.message, 'error');
      } finally { this.busy = false; }
    },

    async toggleStatusNotices() {
      this.busy = true;
      try {
        this.statusNotices = (await fetchJSON('api/alerts/settings', {
          method: 'PUT', body: JSON.stringify({ status_notices: !this.statusNotices }) })
        ).status_notices;
        Alpine.store('app').toast(`Status notices ${this.statusNotices ? 'on' : 'off'}`, 'info');
      } catch (e) {
        Alpine.store('app').toast('Failed: ' + e.message, 'error');
      } finally { this.busy = false; }
    },

    statusLabel(s) {
      return { pass: 'Pass', fail: 'Fail', not_set_up: 'Not set up' }[s] || s;
    },

    docsUrl(path) { return DOCS_URL + path; },

    // ── devices ──
    async toggleDevice(d) {
      try {
        await fetchJSON(`api/notify/devices/${d.id}`, {
          method: 'PATCH', body: JSON.stringify({ enabled: !d.enabled }) });
        await this.loadAll();
      } catch (e) { Alpine.store('app').toast('Failed: ' + e.message, 'error'); }
    },

    async removeDevice(d) {
      if (!await Alpine.store('app').confirm({
        title: 'Remove this device?', message: `Remove "${d.alias}"? It will stop receiving notifications.`,
        confirmLabel: 'Remove', danger: true,
      })) return;
      try {
        await fetchJSON(`api/notify/devices/${d.id}`, { method: 'DELETE' });
        await this.loadAll();
      } catch (e) { Alpine.store('app').toast('Failed: ' + e.message, 'error'); }
    },

    async testDevice(d) {
      this.busy = true;
      try {
        const r = await fetchJSON(`api/notify/devices/${d.id}/test`, { method: 'POST' });
        Alpine.store('app').toast(r.delivered ? 'Sent to ' + d.alias : 'Failed: ' + (r.errors[0] || r.summary),
                                  r.delivered ? 'success' : 'error');
      } catch (e) { Alpine.store('app').toast('Failed: ' + e.message, 'error'); }
      finally { this.busy = false; this.loadLog(); }
    },

    // Yes / No test: the tapped answer shows on the device's row and stays there.
    async testButtons(d) {
      const say = (kind, text) => { this.rowResult = { ...this.rowResult, [d.id]: { kind, text } }; };
      say('info', 'Sending…');
      let rid;
      try {
        rid = (await fetchJSON(`api/notify/devices/${d.id}/test-buttons`, { method: 'POST' })).request_id;
      } catch (e) { say('danger', 'Not sent: ' + e.message); this.loadChecks(); return; }
      say('info', `Sent — tap Yes or No on ${d.alias}…`);
      this.waiting = { ...this.waiting, [d.id]: rid };
      for (let i = 0; i < 65; i++) {
        await new Promise(res => setTimeout(res, 2000));
        let r;
        try { r = await fetchJSON(`api/notify/test-buttons/${rid}`, { background: true }); }
        catch (_) { continue; }
        if (r.result === 'waiting') continue;
        if (r.result === 'no reply') say('warn', 'No reply within 120 s');
        else say('ok', 'Round trip works: you tapped ' + (r.result === 'yes' ? 'Yes' : 'No'));
        break;
      }
      const w = { ...this.waiting }; delete w[d.id]; this.waiting = w;
      this.loadChecks();
      this.loadLog();
    },

    // ── add / edit dialog ──
    openAdd() {
      const inst = this.instances.find(i => i.enabled);
      this.form = { id: null, alias: '', instance_id: inst ? inst.id : '', service: '', kind: 'other' };
      this.found = []; this.filter = ''; this.pickerOpen = false; this.dlgMsg = null; this.dlg = true;
    },

    openEdit(d) {   // rename / re-target an existing device
      this.form = { id: d.id, alias: d.alias, instance_id: d.instance_id,
                    service: 'notify.' + d.service, kind: d.kind || 'other' };
      this.found = []; this.filter = ''; this.pickerOpen = false; this.dlgMsg = null; this.dlg = true;
    },

    say(kind, text) { this.dlgMsg = { kind, text }; },

    // Ask the CHOSEN instance only: a notification goes to exactly one Home Assistant.
    async discover() {
      this.busy = true; this.dlgMsg = null;
      try {
        this.found = (await fetchJSON(
          `api/notify/discover?instance_id=${encodeURIComponent(this.form.instance_id)}`)).services;
        if (!this.found.length) { this.say('warn', 'No notify services found on that instance'); return; }
        this.filter = ''; this.pickerOpen = true;
      } catch (e) {
        this.found = [];
        this.say('danger', 'Discover failed: ' + e.message);
      } finally { this.busy = false; }
    },

    get picker() {
      const q = this.filter.trim().toLowerCase();
      return this.found
        .map(t => ({ service: t.service, label: t.friendly_name, kind: t.kind || 'other',
                     seen: this.fmtSeen(t.last_updated), shadows: t.duplicate_of || '',
                     saved: t.saved }))
        .filter(x => !q || x.service.toLowerCase().includes(q) || x.label.toLowerCase().includes(q));
    },

    //: "seen 2 h ago" / "seen just now" / '' when Home Assistant does not report it.
    fmtSeen(iso) {
      if (!iso) return '';
      const then = Date.parse(iso);
      if (!then) return '';
      const mins = Math.max(0, Math.round((Date.now() - then) / 60000));
      if (mins < 2) return 'seen just now';
      if (mins < 60) return `seen ${mins} min ago`;
      const hrs = Math.round(mins / 60);
      if (hrs < 48) return `seen ${hrs} h ago`;
      return `seen ${Math.round(hrs / 24)} d ago`;
    },

    // Take Home Assistant's own friendly name across as the alias, and its type.
    pick(opt) {
      this.form.service = 'notify.' + opt.service;
      this.form.kind = opt.kind;
      if (!this.form.alias) this.form.alias = opt.label;
      this.pickerOpen = false;
    },

    async testNew() {
      const f = this.form;
      if (!f.instance_id || !f.service) { this.say('warn', 'Pick an instance and a notify service first'); return; }
      this.busy = true; this.dlgMsg = null;
      try {
        const r = await fetchJSON('api/notify/test', { method: 'POST',
          body: JSON.stringify({ instance_id: f.instance_id, service: f.service }) });
        if (r.delivered) this.say('ok', 'Test sent to ' + f.service + ' — check your device');
        else this.say('danger', 'Test failed: ' + (r.errors[0] || r.summary));
      } catch (e) { this.say('danger', 'Test failed: ' + e.message); }
      finally { this.busy = false; this.loadLog(); }
    },

    async save() {
      const f = this.form;
      if (!f.alias.trim() || !f.instance_id || !f.service.trim()) {
        this.say('warn', 'Alias, HA instance and notify service are all required'); return;
      }
      this.dlgMsg = null;
      const body = { alias: f.alias, instance_id: f.instance_id, service: f.service, kind: f.kind || 'other' };
      try {
        if (f.id) await fetchJSON(`api/notify/devices/${f.id}`, { method: 'PATCH', body: JSON.stringify(body) });
        else await fetchJSON('api/notify/devices', { method: 'POST', body: JSON.stringify(body) });
        this.dlg = false;
        Alpine.store('app').toast(f.id ? 'Device updated' : 'Device added', 'success');
        await this.loadAll();
      } catch (e) { this.say('danger', 'Not saved: ' + e.message); }
    },

    // ── broadcast ──
    openBroadcast() { this.bcTitle = 'FranklinWH'; this.bcMessage = ''; this.bcResult = null; this.bc = true; },

    async sendBroadcast() {
      this.bcResult = { ok: true, msg: 'Sending…' };
      try {
        const r = await fetchJSON('api/notify/broadcast', { method: 'POST',
          body: JSON.stringify({ title: this.bcTitle, message: this.bcMessage }) });
        this.bcResult = { ok: r.delivered > 0, msg: `Sent to ${r.delivered} of ${r.total} device(s)` };
      } catch (e) {
        this.bcResult = { ok: false, msg: 'Not sent: ' + e.message };
      }
      this.loadLog();
    },

    fmtTime(ts) { return bridgeTime.full(ts * 1000); },
  };
}
