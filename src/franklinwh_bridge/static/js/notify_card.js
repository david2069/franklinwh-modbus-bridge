/**
 * Settings → Home Assistant → Notifications (#296 item 48, phase 1).
 *
 * The Push notifications switch, Status notices under it, the Notifications check,
 * Companion Devices (add, discover, test, test with buttons, broadcast) and Recent
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
    // Per-device row results: id -> {ok, msg}
    rowResult: {},
    // Test with buttons in flight: id -> request_id
    waiting: {},
    // Add / Edit dialog
    dlg: false,
    form: { id: null, alias: '', instance_id: '', service: '', enabled: true },
    found: [],
    filter: '',
    discovering: false,
    dlgResult: null,
    // Broadcast dialog
    bc: false,
    bcMessage: '',
    bcResult: null,

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
      } catch (e) {
        this.error = 'Could not load notifications: ' + e.message;
      }
      this.loadChecks();
      this.loadLog();
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

    kindIcon(kind) {
      return { phone: '📱', tablet: '📱', tv: '📺', speaker: '🔊' }[kind] || '🔔';
    },

    // ── devices ──
    async toggleDevice(d) {
      try {
        await fetchJSON(`api/notify/devices/${d.id}`, {
          method: 'PATCH', body: JSON.stringify({ enabled: !d.enabled }) });
        await this.loadAll();
      } catch (e) {
        this.rowResult = { ...this.rowResult, [d.id]: { ok: false, msg: e.message } };
      }
    },

    async removeDevice(d) {
      if (!await Alpine.store('app').confirm({
        title: 'Remove device?', message: `${d.alias} stops receiving notifications.`,
        confirmLabel: 'Remove', danger: true,
      })) return;
      try {
        await fetchJSON(`api/notify/devices/${d.id}`, { method: 'DELETE' });
        await this.loadAll();
        Alpine.store('app').toast(`${d.alias} removed`, 'info');
      } catch (e) {
        this.rowResult = { ...this.rowResult, [d.id]: { ok: false, msg: e.message } };
      }
    },

    async testDevice(d) {
      this.rowResult = { ...this.rowResult, [d.id]: { ok: true, msg: 'Sending…' } };
      try {
        const r = await fetchJSON(`api/notify/devices/${d.id}/test`, { method: 'POST' });
        this.rowResult = { ...this.rowResult, [d.id]: r.delivered
          ? { ok: true, msg: 'Test sent' }
          : { ok: false, msg: 'Not sent: ' + (r.errors[0] || r.summary) } };
      } catch (e) {
        this.rowResult = { ...this.rowResult, [d.id]: { ok: false, msg: e.message } };
      }
      this.loadLog();
    },

    async testButtons(d) {
      this.rowResult = { ...this.rowResult, [d.id]: { ok: true, msg: 'Sent: tap Yes or No on the phone (2 min)' } };
      try {
        const r = await fetchJSON(`api/notify/devices/${d.id}/test-buttons`, { method: 'POST' });
        this.waiting = { ...this.waiting, [d.id]: r.request_id };
        this.pollTest(d, r.request_id);
      } catch (e) {
        this.rowResult = { ...this.rowResult, [d.id]: { ok: false, msg: e.message } };
      }
    },

    async pollTest(d, rid) {
      for (let i = 0; i < 70; i++) {
        await new Promise(res => setTimeout(res, 2000));
        let r;
        try { r = await fetchJSON(`api/notify/test-buttons/${rid}`, { background: true }); }
        catch (_) { continue; }
        if (r.result === 'waiting') continue;
        const msg = r.result === 'no reply' ? 'No reply within 120 s'
          : `Round trip works: you tapped ${r.result === 'yes' ? 'Yes' : 'No'}`;
        this.rowResult = { ...this.rowResult, [d.id]: { ok: r.result !== 'no reply', msg } };
        break;
      }
      const w = { ...this.waiting }; delete w[d.id]; this.waiting = w;
      this.loadChecks();
      this.loadLog();
    },

    // ── add / edit dialog ──
    openAdd() {
      const inst = this.instances.find(i => i.enabled) || this.instances[0];
      this.form = { id: null, alias: '', instance_id: inst ? inst.id : '', service: '', enabled: true };
      this.found = []; this.filter = ''; this.dlgResult = null; this.dlg = true;
    },

    openEdit(d) {
      this.form = { id: d.id, alias: d.alias, instance_id: d.instance_id, service: d.service,
                    enabled: d.enabled };
      this.found = []; this.filter = ''; this.dlgResult = null; this.dlg = true;
    },

    async discover() {
      this.discovering = true; this.dlgResult = null;
      try {
        this.found = (await fetchJSON(
          `api/notify/discover?instance_id=${encodeURIComponent(this.form.instance_id)}`)).services;
        if (!this.found.length) this.dlgResult = { ok: false, msg: 'This Home Assistant has no notify services' };
      } catch (e) {
        this.found = [];
        this.dlgResult = { ok: false, msg: 'Discover failed: ' + e.message };
      } finally { this.discovering = false; }
    },

    get filteredFound() {
      const f = this.filter.trim().toLowerCase();
      return f ? this.found.filter(s => s.service.includes(f) || s.label.toLowerCase().includes(f))
               : this.found;
    },

    pick(s) {
      this.form.service = s.service;
      if (!this.form.alias) this.form.alias = s.label.startsWith('Send a notification')
        ? s.service.replace(/^mobile_app_/, '').replace(/_/g, ' ') : s.label;
    },

    async save() {
      this.dlgResult = null;
      const f = this.form;
      try {
        if (f.id) {
          await fetchJSON(`api/notify/devices/${f.id}`, { method: 'PATCH',
            body: JSON.stringify({ alias: f.alias, service: f.service, enabled: f.enabled }) });
        } else {
          await fetchJSON('api/notify/devices', { method: 'POST', body: JSON.stringify(f) });
        }
        this.dlg = false;
        Alpine.store('app').toast(`${f.alias} saved`, 'info');
        await this.loadAll();
      } catch (e) {
        this.dlgResult = { ok: false, msg: 'Not saved: ' + e.message };
      }
    },

    // ── broadcast ──
    openBroadcast() { this.bcMessage = ''; this.bcResult = null; this.bc = true; },

    async sendBroadcast() {
      this.bcResult = { ok: true, msg: 'Sending…' };
      try {
        const r = await fetchJSON('api/notify/broadcast', { method: 'POST',
          body: JSON.stringify({ message: this.bcMessage }) });
        this.bcResult = { ok: r.delivered > 0, msg: `Sent to ${r.delivered} of ${r.total} device(s)` };
      } catch (e) {
        this.bcResult = { ok: false, msg: 'Not sent: ' + e.message };
      }
      this.loadLog();
    },

    fmtTime(ts) { return bridgeTime.full(ts * 1000); },
  };
}
