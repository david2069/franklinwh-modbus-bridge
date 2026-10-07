/**
 * First-run setup wizard (docs/setup-wizard-design.md).
 *
 * Opens by itself for an admin on a fresh install (setup state `pending`),
 * after the legal notice; can be re-run from Settings → Gateways or the
 * Dashboard's "Set up" link (both dispatch `open-setup-wizard`).
 *
 * Every step is a call to /api/setup/* — the wizard holds no logic the API
 * doesn't, so the CLI can drive the same flow.
 */
(function () {
  'use strict';

  const AUTOSHOWN_KEY = 'fwh-setup-autoshown';
  const SCAN_POLL_MS = 700;
  const READING_POLL_MS = 2000;
  const READING_TIMEOUT_MS = 20000;
  const CHECKLIST_LABELS = {
    mqtt_broker: 'MQTT broker connected',
    ha_connection: 'Home Assistant connected to the bridge',
    ha_mqtt_integration: "Home Assistant's MQTT integration set up",
    ha_entities: 'Bridge entities in Home Assistant',
  };
  const SOURCE_LABELS = {
    supervisor: 'Home Assistant host network',
    browser_address: 'the address you opened this page with',
    lan_subnet_env: 'LAN_SUBNET',
    host_network: "this container's network",
    this_machine: "this machine's network",
  };

  // Shared, so the Dashboard and Settings can show state / open the wizard.
  document.addEventListener('alpine:init', () => {
    Alpine.store('setup', {
      state: null,          // pending | done_real | done_demo | skipped
      hasRealGateway: false,
      async load() {
        try {
          const resp = await fetch('api/setup/state');
          if (!resp.ok) return;
          const d = await resp.json();
          this.state = d.state;
          this.hasRealGateway = !!d.has_real_gateway;
        } catch (_) { /* the wizard is optional; never block the UI on it */ }
      },
      open() { window.dispatchEvent(new CustomEvent('open-setup-wizard')); },
    });
  });

  async function api(path, opts = {}) {
    const init = { method: opts.method || 'GET', headers: {} };
    if (opts.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }
    const resp = await fetch(`api/setup/${path}`, init);
    let data = null;
    try { data = await resp.json(); } catch (_) { /* empty body */ }
    if (!resp.ok) {
      const detail = data && data.detail;
      const msg = typeof detail === 'string' ? detail
        : Array.isArray(detail) ? detail.map(d => d.msg).join('; ')
        : `Request failed (${resp.status})`;
      throw new Error(msg);
    }
    return data;
  }

  const sleep = (ms) => new Promise(r => setTimeout(r, ms));

  window.setupWizard = function setupWizard() {
    return {
      open: false,
      step: 'intent',
      busy: false,
      error: '',

      // Real path
      tab: 'search',             // search | address
      subnets: [],               // [{subnet, source}]
      subnetInput: '',
      scan: null,                // GET /scan/{id} body
      manual: { host: '', port: 502, unit_id: 1 },
      probeRow: null,
      picks: [],                 // agate rows chosen: [{row, name, device_type}]
      connected: [],             // [{gateway_id, name}]
      reading: null,             // {points, health, last_error, timedOut}

      // Demo path
      demo: { name: 'Demo aGate', ac_type: 0 },
      demoId: null,

      checklist: [],
      environment: '',

      async init() {
        window.addEventListener('open-setup-wizard', () => this.start());
        await Alpine.store('setup').load();
        if (Alpine.store('setup').state !== 'pending') return;
        // Admins only, and never on top of the legal notice.
        for (let i = 0; i < 50 && !Alpine.store('app').me; i++) await sleep(200);
        if (!Alpine.store('app').isAdmin) return;
        if (!(await this._disclaimerAcknowledged())) {
          window.addEventListener('disclaimer-settled', () => this.autoStart(), { once: true });
          return;
        }
        this.autoStart();
      },

      // Opens by itself once per browser tab: closing it with × leaves setup
      // pending (it returns next visit) without re-opening on every reload.
      autoStart() {
        try {
          if (sessionStorage.getItem(AUTOSHOWN_KEY)) return;
          sessionStorage.setItem(AUTOSHOWN_KEY, '1');
        } catch (_) { /* storage blocked: just open */ }
        this.start();
      },

      async _disclaimerAcknowledged() {
        try {
          const resp = await fetch('api/disclaimer');
          if (!resp.ok) return true;
          return !!(await resp.json()).acknowledged;
        } catch (_) { return true; }
      },

      start() {
        this.reset();
        this.open = true;
      },

      reset() {
        Object.assign(this, {
          step: 'intent', busy: false, error: '', tab: 'search', subnets: [],
          subnetInput: '', scan: null, manual: { host: '', port: 502, unit_id: 1 },
          probeRow: null, picks: [], connected: [], reading: null,
          demo: { name: 'Demo aGate', ac_type: 0 }, demoId: null, checklist: [],
        });
      },

      async close() {
        this.open = false;
        await Alpine.store('setup').load();
        Alpine.store('app').refresh();
      },

      go(step) { this.error = ''; this.step = step; },

      // ── Step 1 ────────────────────────────────────────────────
      async skip() {
        try { await api('state', { method: 'POST', body: { state: 'skipped' } }); } catch (_) { /* closing anyway */ }
        this.close();
      },

      // ── Real path: find ───────────────────────────────────────
      async enterFind() {
        this.go('find');
        this.busy = true;
        try {
          const d = await api('subnets');
          this.environment = d.environment;
          this.subnets = d.subnets || [];
          this.subnetInput = this.subnets.map(s => s.subnet).join(', ');
          if (!this.subnets.length) this.tab = 'address';
        } catch (e) {
          this.error = e.message;
        } finally {
          this.busy = false;
        }
      },

      sourceLabel(src) { return SOURCE_LABELS[src] || src; },

      async startScan() {
        const subnets = this.subnetInput.split(',').map(s => s.trim()).filter(Boolean);
        if (!subnets.length) { this.error = 'Enter a subnet, e.g. 192.168.1.0/24'; return; }
        this.error = '';
        this.picks = [];
        this.busy = true;
        try {
          this.scan = await api('scan', { method: 'POST', body: { subnets } });
          while (this.open && this.scan && this.scan.status === 'running') {
            await sleep(SCAN_POLL_MS);
            this.scan = await api(`scan/${this.scan.scan_id}`);
          }
          if (this.scan && this.scan.status === 'error') this.error = this.scan.error;
        } catch (e) {
          this.error = e.message;
        } finally {
          this.busy = false;
        }
      },

      get scanPct() {
        if (!this.scan || !this.scan.hosts_total) return 0;
        return Math.min(100, Math.round(100 * this.scan.hosts_done / this.scan.hosts_total));
      },

      get scanAgates() { return (this.scan?.results || []).filter(r => r.kind === 'agate'); },
      get scanOthers() { return (this.scan?.results || []).filter(r => r.kind !== 'agate'); },

      async probeManual() {
        if (!this.manual.host.trim()) { this.error = 'Enter the aGate address'; return; }
        this.error = '';
        this.probeRow = null;
        this.busy = true;
        try {
          this.probeRow = await api('probe', {
            method: 'POST',
            body: {
              host: this.manual.host.trim(),
              port: Number(this.manual.port) || 502,
              unit_id: Number(this.manual.unit_id) || 1,
            },
          });
        } catch (e) {
          this.error = e.message;
        } finally {
          this.busy = false;
        }
      },

      rowText(row) {
        if (row.kind === 'agate') {
          return `${row.model || 'aGate'} — serial ${row.serial || '?'}, firmware ${row.version || '?'}`;
        }
        if (row.kind === 'sunspec_other') {
          return `${row.manufacturer || 'SunSpec device'} ${row.model || ''} — not a FranklinWH device`;
        }
        return row.summary || `${row.host}:${row.port}`;
      },

      isPicked(row) { return this.picks.some(p => p.row.host === row.host && p.row.port === row.port); },

      togglePick(row) {
        if (row.kind !== 'agate') return;
        if (this.isPicked(row)) {
          this.picks = this.picks.filter(p => !(p.row.host === row.host && p.row.port === row.port));
        } else {
          this.picks.push({ row, name: row.suggested_name || 'aGate', device_type: row.device_type || 'agate' });
        }
      },

      pickProbe() {
        if (this.probeRow && this.probeRow.kind === 'agate') {
          this.picks = [];
          this.togglePick(this.probeRow);
          this.go('confirm');
        }
      },

      // ── Real path: confirm → connect ──────────────────────────
      async connectPicks() {
        this.error = '';
        this.busy = true;
        this.connected = [];
        try {
          for (const p of this.picks) {
            const d = await api('connect', {
              method: 'POST',
              body: {
                host: p.row.host,
                port: p.row.port || 502,
                unit_id: Number(this.manual.unit_id) || 1,
                name: p.name.trim() || p.row.suggested_name || 'aGate',
                device_type: p.device_type,
              },
            });
            this.connected.push({ gateway_id: d.gateway_id, name: d.gateway?.name || p.name });
          }
          Alpine.store('app').refresh();
          this.go('reading');
          this.watchReading();
        } catch (e) {
          this.error = e.message;
        } finally {
          this.busy = false;
        }
      },

      // ── Real path: first live reading ─────────────────────────
      async watchReading() {
        const first = this.connected[0];
        if (!first) return;
        this.reading = { points: null, health: '', last_error: '', timedOut: false };
        const deadline = Date.now() + READING_TIMEOUT_MS;
        while (this.open && this.step === 'reading') {
          try {
            const gw = await (await fetch(`api/gateways/${first.gateway_id}`)).json();
            this.reading.health = gw.health || '';
            this.reading.last_error = gw.last_error || '';
            const pts = await fetch(`api/gateways/${first.gateway_id}/points`);
            if (pts.ok) {
              const d = await pts.json();
              if (d.ts) { this.reading.points = d.points || {}; return; }
            }
          } catch (_) { /* keep waiting */ }
          if (Date.now() > deadline) { this.reading.timedOut = true; return; }
          await sleep(READING_POLL_MS);
        }
      },

      retryReading() { this.reading = null; this.watchReading(); },

      pt(key, unit = '') {
        const v = this.reading?.points?.[key];
        if (v === undefined || v === null) return '—';
        return `${typeof v === 'number' ? Math.round(v * 10) / 10 : v}${unit}`;
      },

      // ── Demo path ─────────────────────────────────────────────
      async createDemo() {
        this.error = '';
        this.busy = true;
        try {
          const d = await api('demo', { method: 'POST', body: { name: this.demo.name.trim() || 'Demo aGate', ac_type: Number(this.demo.ac_type) } });
          this.demoId = d.gateway_id;
          Alpine.store('app').setGateway(d.gateway_id);
          await this.enterChecklist();
        } catch (e) {
          this.error = e.message;
        } finally {
          this.busy = false;
        }
      },

      // ── Step 6: Home Assistant checklist ──────────────────────
      async enterChecklist() {
        this.go('checklist');
        await this.loadChecklist();
      },

      async loadChecklist() {
        this.busy = true;
        try {
          const d = await api('checklist');
          this.environment = d.environment;
          this.checklist = d.items || [];
        } catch (e) {
          this.error = e.message;
        } finally {
          this.busy = false;
        }
      },

      checkLabel(id) { return CHECKLIST_LABELS[id] || id; },
    };
  };
})();
