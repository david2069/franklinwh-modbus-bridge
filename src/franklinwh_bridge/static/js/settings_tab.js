/**
 * Settings Tab — MQTT status, gateway connectivity, entities, poller status, metrics config
 */
function settingsTab() {
  return {
    mqtt: {},
    mqttConfig: {},
    mqttEditing: false,
    mqttEdit: {},
    mqttTesting: false,
    mqttTestResult: null,
    mqttSaving: false,
    confirmRepublish: false,
    confirmUnpublish: false,
    poller: {},
    gateway: {},
    gwTesting: false,
    gwTestResult: null,
    entities: [],
    entityFilter: '',
    metricsRetention: 30,
    metricsRawAge: 7,
    republishing: false,
    unpublishing: false,
    showTopics: false,
    storage: null,
    archiving: false,
    backups: [],
    backupBusy: false,
    exportRange: '24h',
    confirmRestoreName: null,
    restoring: false,
    // Site config
    siteConfig: {},
    siteEditing: false,
    siteEdit: {},
    // System setup
    setupEditing: false,
    setupEdit: {},

    // Electricity Utility Services (Layer 1)
    services: [],
    serviceEditId: null,   // service id being edited, or 'new', or null
    serviceEdit: {},
    // Feature modules (admin enable/disable)
    modules: [],
    // Users (admin)
    users: [],
    newUser: { username: '', password: '', role: 'user' },
    showAddUser: false,
    // Home Assistant instances (inbound entity access)
    haInstances: [],
    haEditId: null,        // instance id being edited, or 'new', or null
    haEdit: {},
    haTesting: false,
    haTestResult: null,
    haSaving: false,
    // Automation constants (user-defined SoC parameters)
    constants: {},
    constantsSpec: {},
    constantsSaving: false,
    // Gateway management
    gwList: [],
    gwBusy: false,
    showAddGateway: false,
    newGw: { gateway_id: '', name: '', host: '', port: 502, unit_id: 1, poll_interval: 10, timeout: 10, description: '', mock: false },
    gwTestResults: {},     // per-gateway TCP test result, keyed by gateway id
    gwDiagResults: {},     // per-gateway diagnose result, keyed by gateway id
    gwRestartResults: {},  // per-gateway restart result, keyed by gateway id
    gwHealthResults: {},   // per-gateway force-healthcheck result, keyed by gateway id
    deleteGwTarget: null,  // gateway pending delete confirmation (styled modal)
    editGw: null,          // gateway being edited (PATCH form fields) — shown in a modal,
                            // which also hosts Test/Diagnose/Healthcheck/Restart
    // Publishing groups
    groups: [],
    groupsBusy: false,
    expandedGroup: null,
    groupDetail: null,
    entityCatalog: [],
    memberFilter: '',
    newGroup: { slug: '', name: '', description: '' },
    showCreateGroup: false,

    async init() {
      await this.loadAll();
      await this.loadMqttConfig();
      await this.loadMetricsSettings();
      await this.loadStorage();
      await this.loadBackups();
      await this.loadGroups();
      await this.loadGateway();
      await this.loadSiteConfig();
      await this.loadServices();
      await this.loadGateways();
      await this.loadHaInstances();
      await this.loadConstants();
      await this.loadModules();
      if (Alpine.store('app').isAdmin) await this.loadUsers();
      setInterval(() => {
        if (Alpine.store('app').activeTab === 'settings') {
          this.loadAll();
          // Refresh HA connection status/entity counts, but not while editing
          // (would clobber the in-progress form).
          if (this.haEditId === null) this.loadHaInstances();
        }
      }, 15000);
    },

    async loadAll() {
      const [mqttStatus, appStatus, topics] = await Promise.all([
        fetchJSON('api/mqtt/status'),
        fetchJSON('api/status'),
        fetchJSON('api/mqtt/topics'),
      ]);

      if (mqttStatus && !mqttStatus.error) this.mqtt = mqttStatus;
      if (appStatus && appStatus.components && appStatus.components.poller) {
        this.poller = appStatus.components.poller;
      }
      if (topics && topics.topics) this.entities = topics.topics;
    },

    async loadMqttConfig() {
      const data = await fetchJSON('api/mqtt/config');
      if (data && !data.error && data.host) {
        this.mqttConfig = data;
      }
    },

    async loadGateway() {
      const data = await fetchJSON('api/gateway');
      if (data && !data.error) {
        this.gateway = data;
      }
    },

    async testGateway() {
      this.gwTesting = true;
      this.gwTestResult = null;
      try {
        const data = await fetchJSON('api/gateway/test');
        if (data && data.ok) {
          this.gwTestResult = {
            ok: true,
            msg: `TCP connection OK to ${data.host}:${data.port}`,
            latency: data.latency_ms,
          };
        } else {
          this.gwTestResult = {
            ok: false,
            msg: data?.error || 'Connection failed',
            latency: null,
          };
        }
      } catch (e) {
        this.gwTestResult = { ok: false, msg: String(e), latency: null };
      } finally {
        this.gwTesting = false;
      }
    },

    // ── Site config ──────────────────────────────────────────

    async loadSiteConfig() {
      const data = await fetchJSON('api/site');
      if (data && !data.error) this.siteConfig = data;
    },

    startSiteEdit() {
      this.siteEdit = { ...this.siteConfig };
      this.siteEditing = true;
    },

    async saveSiteConfig() {
      const data = await fetchJSON('api/site', {
        method: 'PATCH',
        body: JSON.stringify(this.siteEdit),
      });
      if (data && !data.error) {
        this.siteConfig = data;
        this.siteEditing = false;
        Alpine.store('app').toast('Site config saved', 'info');
      } else {
        Alpine.store('app').toast('Save failed: ' + (data?.error || 'unknown'), 'error');
      }
    },

    // ── System Setup ──────────────────────────────────────────

    startSetupEdit() {
      this.setupEdit = {
        full_backup:     !!this.siteConfig.full_backup,
        grid_forming:    !!this.siteConfig.grid_forming,
        generator_input: !!this.siteConfig.generator_input,
        load_shedding:   !!this.siteConfig.load_shedding,
        nonbackup_loads: !!this.siteConfig.nonbackup_loads,
        solar_type:      this.siteConfig.solar_type || 'none',
        solar_kwp:       this.siteConfig.solar_kwp || 0,
        battery_label:   this.siteConfig.battery_label || '',
      };
      this.setupEditing = true;
    },

    async saveSetupConfig() {
      const data = await fetchJSON('api/site', {
        method: 'PATCH',
        body: JSON.stringify(this.setupEdit),
      });
      if (data && !data.error) {
        this.siteConfig = data;
        this.setupEditing = false;
        Alpine.store('app').toast('System setup saved', 'info');
      } else {
        Alpine.store('app').toast('Save failed: ' + (data?.error || 'unknown'), 'error');
      }
    },

    // ── Electricity Utility Services ───────────────────────

    AC_SERVICE_LABELS: { 1: 'Single Phase', 2: 'Split Phase', 3: 'Three Phase' },

    acServiceLabel(v) {
      return this.AC_SERVICE_LABELS[v] || 'Single Phase';
    },

    async loadServices() {
      const data = await fetchJSON('api/services');
      if (data && data.services) this.services = data.services;
    },

    addService() {
      this.serviceEdit = {
        name: 'Service ' + (this.services.length + 1),
        meter_number: '', account: '', ac_service: 1, rated_amps: 0,
      };
      this.serviceEditId = 'new';
    },

    monthLabels: ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'],
    dayLabels: ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'],
    _win(w, dStart, dEnd) {
      return w
        ? { months: [...(w.months || [])], days: [...(w.days || [])], start: w.start || dStart, end: w.end || dEnd }
        : { months: [], days: [], start: dStart, end: dEnd };
    },
    toggleWinMonth(win, m) { const i = win.months.indexOf(m); if (i >= 0) win.months.splice(i, 1); else win.months.push(m); },
    toggleWinDay(win, d) { const i = win.days.indexOf(d); if (i >= 0) win.days.splice(i, 1); else win.days.push(d); },

    editService(svc) {
      this.serviceEdit = {
        name: svc.name, meter_number: svc.meter_number, account: svc.account,
        ac_service: svc.ac_service, rated_amps: svc.rated_amps,
        // billing / tariff
        has_tou: !!svc.has_tou, has_peak_demand: !!svc.has_peak_demand,
        has_export_bonus: !!svc.has_export_bonus,
        min_monthly_bill: svc.min_monthly_bill || 0,
        pricing_api: svc.pricing_api || 'none',
        demand_window: this._win(svc.demand_window, '14:00', '20:00'),
        bonus_window: this._win(svc.bonus_window, '00:00', '06:00'),
        has_export_charge: !!(svc.pricing && svc.pricing.export_charge),
        // calculation method + rates (stored in the pricing JSON)
        pricing: {
          billing_cycle_day: (svc.pricing && svc.pricing.billing_cycle_day) || 1,
          demand_rate: (svc.pricing && svc.pricing.demand_rate) || 0,
          demand_interval_min: (svc.pricing && svc.pricing.demand_interval_min) || 30,
          demand_charge_basis: (svc.pricing && svc.pricing.demand_charge_basis) || 'per_kw_day',
          export_bonus_rate: (svc.pricing && svc.pricing.export_bonus_rate) || 0,
          export_charge: (svc.pricing && svc.pricing.export_charge) || {
            window: { months: [], days: [], start: '10:00', end: '15:00' },
            rate: 0.0123, free_kwh_per_day: 6.84,
          },
        },
      };
      this.serviceEditId = svc.id;
    },

    cancelServiceEdit() {
      // Hide via serviceEditId (the form's x-if); keep serviceEdit intact so the
      // billing window x-for bindings don't read .months/.days off an emptied
      // object during Alpine's teardown tick (edit/add overwrites it next time).
      this.serviceEditId = null;
    },

    // Fill the billing form with the Ausgrid two-way (solar-sponge) tariff so the
    // user only has to confirm/save. Reward 4-9pm @ 3.85c; export CHARGE 10am-3pm
    // @ 1.23c with ~6.84 kWh/day free (≈212 kWh over a 31-day period).
    loadAusgridPreset() {
      const e = this.serviceEdit;
      e.has_export_bonus = true;
      e.bonus_window = { months: [], days: [], start: '16:00', end: '21:00' };
      e.pricing.export_bonus_rate = 0.0385;
      e.has_export_charge = true;
      e.pricing.export_charge = {
        window: { months: [], days: [], start: '10:00', end: '15:00' },
        rate: 0.0123, free_kwh_per_day: 6.84,
      };
      if (!e.pricing.billing_cycle_day) e.pricing.billing_cycle_day = 1;
      Alpine.store('app').toast('Ausgrid two-way tariff loaded — review & save', 'info');
    },

    async saveService() {
      const isNew = this.serviceEditId === 'new';
      const url = isNew ? 'api/services' : `api/services/${this.serviceEditId}`;
      // export_charge only persists when the toggle is on
      if (this.serviceEdit.pricing && !this.serviceEdit.has_export_charge) {
        this.serviceEdit.pricing = { ...this.serviceEdit.pricing, export_charge: null };
      }
      const data = await fetchJSON(url, {
        method: isNew ? 'POST' : 'PATCH',
        body: JSON.stringify(this.serviceEdit),
      });
      if (data && !data.error) {
        await this.loadServices();
        this.cancelServiceEdit();
        Alpine.store('app').toast(isNew ? 'Service added' : 'Service saved', 'info');
      } else {
        Alpine.store('app').toast('Save failed: ' + (data?.error || 'unknown'), 'error');
      }
    },

    async deleteService(svc) {
      if (!await Alpine.store('app').confirm({
        title: 'Delete service?', message: `"${svc.name}" will be removed.`,
        confirmLabel: 'Delete', danger: true,
      })) return;
      const data = await fetchJSON(`api/services/${svc.id}`, { method: 'DELETE' });
      if (data && !data.error) {
        await this.loadServices();
        Alpine.store('app').toast('Service deleted', 'info');
      } else {
        Alpine.store('app').toast('Delete failed: ' + (data?.error || 'unknown'), 'error');
      }
    },

    // ── Feature modules ───────────────────────────────────────
    async loadModules() {
      const data = await fetchJSON('api/modules');
      if (data && data.modules) this.modules = data.modules;
    },
    async toggleModule(m) {
      if (m.core) return;  // core modules can't be disabled
      const next = !m.enabled;
      const data = await fetchJSON(`api/modules/${m.id}`, {
        method: 'PATCH', body: JSON.stringify({ enabled: next }),
      });
      if (data && !data.error) {
        m.enabled = next;
        Alpine.store('app').loadModules();  // update the sidebar/nav immediately
        Alpine.store('app').toast(`${m.label} ${next ? 'enabled' : 'disabled'}`, 'info');
      } else {
        Alpine.store('app').toast('Failed: ' + (data?.error || 'unknown'), 'error');
      }
    },

    // ── Users (admin) ─────────────────────────────────────────
    async loadUsers() {
      const data = await fetchJSON('api/users');
      if (Array.isArray(data)) this.users = data;
    },
    async createUser() {
      if (!this.newUser.username.trim() || !this.newUser.password) {
        Alpine.store('app').toast('Username and password required', 'error'); return;
      }
      const data = await fetchJSON('api/users', { method: 'POST', body: JSON.stringify(this.newUser) });
      if (data && !data.error) {
        await this.loadUsers();
        this.newUser = { username: '', password: '', role: 'user' };
        this.showAddUser = false;
        Alpine.store('app').toast('User created', 'info');
      } else {
        Alpine.store('app').toast('Failed: ' + (data?.error || 'unknown'), 'error');
      }
    },
    async patchUser(u, body, ok) {
      const data = await fetchJSON(`api/users/${u.id}`, { method: 'PATCH', body: JSON.stringify(body) });
      if (data && !data.error) { await this.loadUsers(); if (ok) Alpine.store('app').toast(ok, 'info'); }
      else { await this.loadUsers(); Alpine.store('app').toast('Failed: ' + (data?.error || 'unknown'), 'error'); }
    },
    setUserRole(u, role) { this.patchUser(u, { role }, `${u.username} → ${role}`); },
    toggleUserEnabled(u) { this.patchUser(u, { enabled: !u.enabled }, `${u.username} ${u.enabled ? 'disabled' : 'enabled'}`); },
    async resetUserPassword(u) {
      const pw = await Alpine.store('app').promptText({
        title: `Reset password`, message: `New password for ${u.username}:`,
        inputType: 'password', confirmLabel: 'Set password',
      });
      if (!pw) return;
      this.patchUser(u, { password: pw }, `Password reset for ${u.username}`);
    },
    async deleteUser(u) {
      if (!await Alpine.store('app').confirm({
        title: 'Delete user?', message: `"${u.username}" will be removed.`,
        confirmLabel: 'Delete', danger: true,
      })) return;
      const data = await fetchJSON(`api/users/${u.id}`, { method: 'DELETE' });
      if (data && !data.error) { await this.loadUsers(); Alpine.store('app').toast('User deleted', 'info'); }
      else { Alpine.store('app').toast('Failed: ' + (data?.error || 'unknown'), 'error'); }
    },

    // ── Home Assistant instances ──────────────────────────────
    async loadHaInstances() {
      const data = await fetchJSON('api/ha/instances');
      if (Array.isArray(data)) this.haInstances = data;
    },

    addHa() {
      this.haEdit = { name: 'Home', base_url: '', token: '', is_default: this.haInstances.length === 0, enabled: true };
      this.haTestResult = null;
      this.haEditId = 'new';
    },

    editHa(h) {
      this.haEdit = {
        name: h.name, base_url: h.base_url, token: '',
        has_token: h.has_token, is_default: h.is_default, enabled: h.enabled,
      };
      this.haTestResult = null;
      this.haEditId = h.id;
    },

    cancelHaEdit() {
      this.haEditId = null;
      this.haEdit = {};
      this.haTestResult = null;
    },

    async saveHa() {
      const isNew = this.haEditId === 'new';
      // Build payload: on edit, omit token when left blank so it's preserved.
      const body = {
        name: this.haEdit.name,
        base_url: this.haEdit.base_url,
        is_default: !!this.haEdit.is_default,
        enabled: !!this.haEdit.enabled,
      };
      if (this.haEdit.token) body.token = this.haEdit.token;
      else if (isNew) body.token = null;

      this.haSaving = true;
      try {
        const url = isNew ? 'api/ha/instances' : `api/ha/instances/${this.haEditId}`;
        const data = await fetchJSON(url, {
          method: isNew ? 'POST' : 'PATCH',
          body: JSON.stringify(body),
        });
        if (data && !data.error) {
          await this.loadHaInstances();
          this.cancelHaEdit();
          Alpine.store('app').toast(isNew ? 'HA instance added' : 'HA instance saved', 'info');
        } else {
          Alpine.store('app').toast('Save failed: ' + (data?.error || 'unknown'), 'error');
        }
      } finally {
        this.haSaving = false;
      }
    },

    async deleteHa(h) {
      if (!await Alpine.store('app').confirm({
        title: 'Delete HA instance?', message: `"${h.name}" will be removed.`,
        confirmLabel: 'Delete', danger: true,
      })) return;
      const data = await fetchJSON(`api/ha/instances/${h.id}`, { method: 'DELETE' });
      if (data && !data.error) {
        await this.loadHaInstances();
        Alpine.store('app').toast('HA instance deleted', 'info');
      } else {
        Alpine.store('app').toast('Delete failed: ' + (data?.error || 'unknown'), 'error');
      }
    },

    // ── Automation constants (user-defined SoC parameters) ──────
    async loadConstants() {
      const data = await fetchJSON('api/automation/constants');
      if (data && data.values && !data.error) {
        this.constants = data.values;
        this.constantsSpec = data.spec || {};
      }
    },
    async saveConstants() {
      this.constantsSaving = true;
      try {
        const data = await fetchJSON('api/automation/constants', {
          method: 'PUT', body: JSON.stringify(this.constants),
        });
        if (data && data.values && !data.error) {
          this.constants = data.values;
          Alpine.store('app').toast('Constants saved', 'info');
        } else {
          Alpine.store('app').toast('Save failed: ' + (data?.error || 'unknown'), 'error');
        }
      } finally {
        this.constantsSaving = false;
      }
    },

    async testHa() {
      this.haTesting = true;
      this.haTestResult = null;
      try {
        const data = await fetchJSON('api/ha/test', {
          method: 'POST',
          body: JSON.stringify({
            base_url: this.haEdit.base_url,
            token: this.haEdit.token || null,
            // On edit, let the probe use the STORED token when the field is blank.
            ha_id: this.haEditId !== 'new' ? this.haEditId : null,
          }),
        });
        if (data && data.connected) {
          this.haTestResult = { ok: true, msg: `Connected — ${data.entity_count} entities` };
        } else {
          this.haTestResult = { ok: false, msg: data?.last_error || 'Connection failed' };
        }
      } catch (e) {
        this.haTestResult = { ok: false, msg: String(e) };
      } finally {
        this.haTesting = false;
      }
    },

    // ── Gateway management ─────────────────────────────────

    async loadGateways() {
      const data = await fetchJSON('api/gateways');
      if (data && data.gateways) this.gwList = data.gateways;
    },

    async addGateway() {
      const g = this.newGw;
      if (!g.gateway_id || !g.name || (!g.mock && !g.host)) {
        Alpine.store('app').toast('ID and Name are required (Host required for real gateways)', 'error');
        return;
      }
      this.gwBusy = true;
      try {
        const data = await fetchJSON('api/gateways', {
          method: 'POST',
          body: JSON.stringify(g),
        });
        if (data && !data.error && data.id) {
          Alpine.store('app').toast(`Gateway "${data.name}" added`, 'info');
          this.newGw = { gateway_id: '', name: '', host: '', port: 502, unit_id: 1, poll_interval: 10, timeout: 10, description: '', mock: false };
          this.showAddGateway = false;
          await this.loadGateways();
        } else {
          Alpine.store('app').toast('Add failed: ' + (data?.detail || data?.error || 'unknown'), 'error');
        }
      } finally {
        this.gwBusy = false;
      }
    },

    async testGw(gwId) {
      this.gwBusy = true;
      // Mark this row as testing (new object ref so Alpine re-renders).
      this.gwTestResults = { ...this.gwTestResults, [gwId]: { testing: true } };
      try {
        const data = await fetchJSON(`api/gateways/${gwId}/test`, { method: 'POST' });
        const result = (data && data.ok)
          ? { ok: true, msg: `TCP OK to ${data.host}:${data.port}`, latency: data.latency_ms }
          : { ok: false, msg: data?.error || 'Connection failed', latency: null };
        this.gwTestResults = { ...this.gwTestResults, [gwId]: result };
      } finally {
        this.gwBusy = false;
      }
    },

    async diagnoseGw(gwId) {
      this.gwBusy = true;
      this.gwDiagResults = { ...this.gwDiagResults, [gwId]: { testing: true } };
      try {
        const data = await fetchJSON(`api/gateways/${gwId}/diagnose`, { method: 'POST' });
        if (!data.checks && !data.mock) {
          this.gwDiagResults = { ...this.gwDiagResults, [gwId]: { ok: false, summary: 'Diagnose failed', verdict: data.error || data.detail || 'No response from bridge' } };
          return;
        }
        const c = data.checks || {};
        const healthy = data.mock || !!(c.tcp_502_modbus?.ok && c.tcp_9000_local_api?.ok && c.modbus_protocol?.ok);
        const summary = data.mock ? 'mock — no live connection' : [
          `502:${c.tcp_502_modbus?.ok ? '✓' : '✗'}`,
          `9000:${c.tcp_9000_local_api?.ok ? '✓' : '✗'}`,
          `Modbus:${c.modbus_protocol?.ok ? '✓' : '✗'}`,
        ].join(' ');
        this.gwDiagResults = {
          ...this.gwDiagResults,
          [gwId]: { ok: healthy, summary, verdict: data.verdict || '', checks: c, mock: !!data.mock },
        };
      } finally {
        this.gwBusy = false;
      }
    },

    async restartGw(gwId) {
      this.gwBusy = true;
      this.gwRestartResults = { ...this.gwRestartResults, [gwId]: { testing: true } };
      try {
        const data = await fetchJSON(`api/gateways/${gwId}/restart`, { method: 'POST' });
        if (data && data.restarted) {
          this.gwRestartResults = { ...this.gwRestartResults, [gwId]: { ok: true, msg: 'Restarted — reconnecting…' } };
          Alpine.store('app').toast(`Gateway ${gwId} restarted`, 'info');
        } else {
          const msg = data?.error || data?.detail || 'unknown';
          this.gwRestartResults = { ...this.gwRestartResults, [gwId]: { ok: false, msg } };
          Alpine.store('app').toast('Restart failed: ' + msg, 'error');
        }
      } finally {
        this.gwBusy = false;
        await this.loadGateways();
      }
    },

    async forceHealthcheckGw(gwId) {
      this.gwBusy = true;
      this.gwHealthResults = { ...this.gwHealthResults, [gwId]: { testing: true } };
      try {
        const data = await fetchJSON(`api/gateways/${gwId}/healthcheck`, { method: 'POST' });
        if (data && data.health) {
          this.gwHealthResults = { ...this.gwHealthResults, [gwId]: { ok: true, health: data.health } };
          Alpine.store('app').toast(`Gateway ${gwId} health: ${data.health}`, 'info');
        } else {
          const msg = data?.error || data?.detail || 'unknown';
          this.gwHealthResults = { ...this.gwHealthResults, [gwId]: { ok: false, health: msg } };
          Alpine.store('app').toast('Health check failed: ' + msg, 'error');
        }
      } finally {
        this.gwBusy = false;
        await this.loadGateways();
      }
    },

    async startGw(gwId) {
      this.gwBusy = true;
      try {
        const data = await fetchJSON(`api/gateways/${gwId}/start`, { method: 'POST' });
        if (data && data.started) {
          Alpine.store('app').toast(`Gateway ${gwId} started`, 'info');
        } else {
          Alpine.store('app').toast('Start failed: ' + (data?.error || data?.detail || 'unknown'), 'error');
        }
      } finally {
        this.gwBusy = false;
        // Always resync — the row may have been stale (e.g. already running/stopped).
        await this.loadGateways();
      }
    },

    async stopGw(gwId) {
      this.gwBusy = true;
      try {
        const data = await fetchJSON(`api/gateways/${gwId}/stop`, { method: 'POST' });
        if (data && data.stopped) {
          Alpine.store('app').toast(`Gateway ${gwId} stopped`, 'info');
        } else {
          Alpine.store('app').toast('Stop failed: ' + (data?.error || data?.detail || 'unknown'), 'error');
        }
      } finally {
        this.gwBusy = false;
        // Always resync — the row may have been stale (e.g. already stopped).
        await this.loadGateways();
      }
    },

    // Open the styled delete-confirmation modal for a gateway.
    confirmRemoveGw(gw) {
      this.deleteGwTarget = gw;
    },

    async doRemoveGw() {
      const gw = this.deleteGwTarget;
      if (!gw) return;
      this.gwBusy = true;
      try {
        const data = await fetchJSON(`api/gateways/${gw.id}`, { method: 'DELETE' });
        if (data && data.deleted) {
          Alpine.store('app').toast(`Gateway "${gw.name}" removed`, 'info');
          this.deleteGwTarget = null;
          if (this.editGw?.id === gw.id) this.editGw = null;  // close the edit modal it was opened from
          await this.loadGateways();
        } else {
          Alpine.store('app').toast('Remove failed: ' + (data?.detail || data?.error || 'unknown'), 'error');
        }
      } finally {
        this.gwBusy = false;
      }
    },

    // ── Gateway edit (PATCH) ──────────────────────────────
    startEditGw(gw) {
      this.editGw = {
        id: gw.id,
        name: gw.name,
        host: gw.host,
        port: gw.port,
        unit_id: gw.unit_id ?? 1,
        poll_interval: gw.poll_interval ?? 10,
        timeout: gw.timeout ?? 10,
        description: gw.description || '',
        service_id: gw.service_id || '',
        phase: gw.phase || 'all',
        phase_view: gw.phase_view || 'both',
        ac_type: gw.ac_type ?? 0,
      };
      this.phaseDetect = null;
    },

    // Detection result for the gateway being edited: {detected, matches_declared, ...}
    phaseDetect: null,

    async detectPhases() {
      const g = this.editGw;
      if (!g) return;
      const data = await fetchJSON(`api/gateways/${g.id}/detect-phases`);
      if (data && !data.error) {
        this.phaseDetect = data;
        if (data.detected) {
          g.phase = data.detected;   // pre-fill the selector with what was detected
          Alpine.store('app').toast(`Detected phase: ${data.detected}`, 'info');
        } else {
          Alpine.store('app').toast('No phase data yet — let the gateway poll first', 'error');
        }
      } else {
        Alpine.store('app').toast('Detect failed: ' + (data?.error || 'unknown'), 'error');
      }
    },

    async saveEditGw() {
      const g = this.editGw;
      if (!g || !g.name || !g.host) {
        Alpine.store('app').toast('Name and Host are required', 'error');
        return;
      }
      this.gwBusy = true;
      try {
        const data = await fetchJSON(`api/gateways/${g.id}`, {
          method: 'PATCH',
          body: JSON.stringify({
            name: g.name, host: g.host, port: g.port,
            unit_id: g.unit_id, poll_interval: g.poll_interval, timeout: g.timeout,
            description: g.description,
            service_id: g.service_id, phase: g.phase, phase_view: g.phase_view,
          }),
        });
        if (data && !data.error) {
          Alpine.store('app').toast('Gateway updated', 'info');
          this.editGw = null;
          await this.loadGateways();
        } else {
          Alpine.store('app').toast('Update failed: ' + (data?.detail || data?.error || 'unknown'), 'error');
        }
      } finally {
        this.gwBusy = false;
      }
    },

    async loadMetricsSettings() {
      const data = await fetchJSON('api/settings/metrics');
      if (data && !data.error) {
        this.metricsRetention = data.retention_days;
        if (data.raw_age_days != null) this.metricsRawAge = data.raw_age_days;
      }
    },

    async loadStorage() {
      const data = await fetchJSON('api/stats/storage');
      if (data && !data.error) {
        this.storage = data;
      }
    },

    async runArchive() {
      this.archiving = true;
      try {
        const data = await fetchJSON('api/metrics/archive', {
          method: 'POST',
        });
        if (data && !data.error) {
          const msg = data.archived_rows > 0
            ? `Archived ${data.archived_rows} rows`
            : 'No rows to archive';
          Alpine.store('app').toast(msg, 'info');
          await this.loadStorage();
        } else {
          Alpine.store('app').toast(
            'Archive failed: ' + (data?.error || 'unknown'),
            'error',
          );
        }
      } finally {
        this.archiving = false;
      }
    },

    async saveMetricsSettings() {
      if (this.metricsRawAge > this.metricsRetention) {
        Alpine.store('app').toast('Raw window must be ≤ retention', 'error');
        return;
      }
      const data = await fetchJSON('api/settings/metrics', {
        method: 'PUT',
        body: JSON.stringify({
          retention_days: this.metricsRetention,
          raw_age_days: this.metricsRawAge,
        }),
      });
      if (data && !data.error) {
        this.metricsRetention = data.retention_days;
        this.metricsRawAge = data.raw_age_days;
        Alpine.store('app').toast(
          `Raw ${this.metricsRawAge}d · retention ${this.metricsRetention}d`, 'info');
      } else {
        Alpine.store('app').toast('Save failed: ' + (data?.detail || data?.error || 'unknown'), 'error');
      }
    },

    async loadBackups() {
      const data = await fetchJSON('api/backup/list');
      if (data && !data.error) {
        this.backups = data.backups || [];
      }
    },

    async createBackup() {
      this.backupBusy = true;
      try {
        const data = await fetchJSON('api/backup/create', {
          method: 'POST',
          body: JSON.stringify({}),
        });
        if (data && !data.error) {
          Alpine.store('app').toast(`Backup created: ${data.name}`, 'info');
          await this.loadBackups();
          await this.loadStorage();
        } else {
          Alpine.store('app').toast(
            'Backup failed: ' + (data?.error || 'unknown'),
            'error',
          );
        }
      } finally {
        this.backupBusy = false;
      }
    },

    async restoreBackup(name) {
      if (!name) return;
      this.restoring = true;
      try {
        const data = await fetchJSON('api/backup/restore', {
          method: 'POST',
          body: JSON.stringify({ name }),
        });
        if (data && data.ok) {
          Alpine.store('app').toast(
            `Restored from "${name}". Restart the bridge to reload state.`,
            'info',
          );
          await this.loadBackups();
        } else {
          Alpine.store('app').toast(
            'Restore failed: ' + (data?.error || data?.detail || 'unknown'),
            'error',
          );
        }
      } catch (e) {
        Alpine.store('app').toast('Restore failed: ' + String(e), 'error');
      } finally {
        this.restoring = false;
      }
    },

    async loadGroups() {
      const data = await fetchJSON('api/groups');
      if (data && !data.error && data.groups) {
        this.groups = data.groups;
      }
    },

    async toggleGroup(slug, enabled) {
      this.groupsBusy = true;
      try {
        const data = await fetchJSON(`api/groups/${slug}`, {
          method: 'PATCH',
          body: JSON.stringify({ enabled }),
        });
        if (data && !data.error) {
          await this.loadGroups();
          await this.loadAll();
          // Refresh detail if this group is expanded
          if (this.expandedGroup === slug) this.groupDetail = data;
          Alpine.store('app').toast(
            `Group "${data.name}" ${enabled ? 'enabled' : 'disabled'}`,
            'info',
          );
        } else {
          Alpine.store('app').toast(
            'Toggle failed: ' + (data?.error || 'unknown'),
            'error',
          );
        }
      } finally {
        this.groupsBusy = false;
      }
    },

    async expandGroup(slug) {
      if (this.expandedGroup === slug) {
        this.expandedGroup = null;
        this.groupDetail = null;
        this.memberFilter = '';
        return;
      }
      // Load entity catalog on first expand
      if (this.entityCatalog.length === 0) {
        const cat = await fetchJSON('api/groups/entities');
        if (cat && cat.entities) this.entityCatalog = cat.entities;
      }
      const data = await fetchJSON(`api/groups/${slug}`);
      if (data && !data.error) {
        this.groupDetail = data;
        this.expandedGroup = slug;
        this.memberFilter = '';
      }
    },

    isMember(entitySlug) {
      return this.groupDetail?.members?.includes(entitySlug) ?? false;
    },

    get filteredCatalog() {
      if (!this.memberFilter) return this.entityCatalog;
      const q = this.memberFilter.toLowerCase();
      return this.entityCatalog.filter(e =>
        e.slug.includes(q) || e.name.toLowerCase().includes(q) ||
        e.state_group.includes(q) || e.ha_type.includes(q)
      );
    },

    async toggleMember(entitySlug) {
      if (!this.expandedGroup) return;
      const slug = this.expandedGroup;
      if (this.isMember(entitySlug)) {
        await fetchJSON(`api/groups/${slug}/members/${entitySlug}`, {
          method: 'DELETE',
        });
      } else {
        await fetchJSON(`api/groups/${slug}/members`, {
          method: 'POST',
          body: JSON.stringify({ entity_slug: entitySlug }),
        });
      }
      // Refresh group detail and list
      const data = await fetchJSON(`api/groups/${slug}`);
      if (data && !data.error) this.groupDetail = data;
      await this.loadGroups();
    },

    async createGroup() {
      const s = this.newGroup;
      if (!s.slug || !s.name) {
        Alpine.store('app').toast('Slug and name are required', 'error');
        return;
      }
      this.groupsBusy = true;
      try {
        const data = await fetchJSON('api/groups', {
          method: 'POST',
          body: JSON.stringify({
            slug: s.slug,
            name: s.name,
            description: s.description,
          }),
        });
        if (data && !data.error && data.slug) {
          Alpine.store('app').toast(`Group "${data.name}" created`, 'info');
          this.newGroup = { slug: '', name: '', description: '' };
          this.showCreateGroup = false;
          await this.loadGroups();
        } else {
          Alpine.store('app').toast(
            'Create failed: ' + (data?.detail || data?.error || 'unknown'),
            'error',
          );
        }
      } finally {
        this.groupsBusy = false;
      }
    },

    async deleteGroup(slug) {
      this.groupsBusy = true;
      try {
        const data = await fetchJSON(`api/groups/${slug}`, {
          method: 'DELETE',
        });
        if (data && data.deleted) {
          Alpine.store('app').toast('Group deleted', 'info');
          if (this.expandedGroup === slug) {
            this.expandedGroup = null;
            this.groupDetail = null;
          }
          await this.loadGroups();
          await this.loadAll();
        } else {
          Alpine.store('app').toast(
            'Delete failed: ' + (data?.detail || data?.error || 'unknown'),
            'error',
          );
        }
      } finally {
        this.groupsBusy = false;
      }
    },

    get filteredEntities() {
      if (!this.entityFilter) return this.entities;
      const q = this.entityFilter.toLowerCase();
      return this.entities.filter(e =>
        e.slug.toLowerCase().includes(q) ||
        e.name.toLowerCase().includes(q) ||
        e.ha_type.toLowerCase().includes(q) ||
        (e.source || '').toLowerCase().includes(q)
      );
    },

    getEntityValue(entity) {
      const pts = Alpine.store('app').points;
      const key = entity.stat_key || entity.slug;
      const raw = pts[key];
      if (raw === undefined || raw === null) return '--';
      if (typeof raw === 'string') return raw;
      // Binary sensors: show On/Off instead of 1/0
      if (entity.ha_type === 'binary_sensor') return raw ? 'On' : 'Off';
      const scale = entity.value_scale ?? 1;
      const prec = entity.value_precision;
      const val = raw * scale;
      return prec != null ? val.toFixed(prec) : String(val);
    },

    /** Colour-code the source badge by model family. */
    sourceClass(source) {
      if (!source) return 'bg-slate-700/50 text-slate-500';
      if (source === 'virtual') return 'bg-purple-900/50 text-purple-300';
      if (source.startsWith('ext.')) return 'bg-amber-900/50 text-amber-300';
      if (source.startsWith('701.')) return 'bg-blue-900/50 text-blue-300';
      if (source.startsWith('713.') || source.startsWith('714.')) return 'bg-emerald-900/50 text-emerald-300';
      if (source.startsWith('704.') || source.startsWith('715.')) return 'bg-orange-900/50 text-orange-300';
      if (source.startsWith('702.')) return 'bg-teal-900/50 text-teal-300';
      if (source.startsWith('502.')) return 'bg-yellow-900/50 text-yellow-300';
      return 'bg-slate-700/50 text-slate-400';
    },

    startMqttEdit() {
      this.mqttEdit = {
        host: this.mqttConfig.host || 'localhost',
        port: this.mqttConfig.port || 1883,
        username: this.mqttConfig.username || '',
        password: '',
        tls_mode: this.mqttConfig.tls_mode || 'off',
        topic_prefix: this.mqttConfig.topic_prefix || 'franklinwh',
        discovery_prefix: this.mqttConfig.discovery_prefix || 'homeassistant',
      };
      this.mqttEditing = true;
      this.mqttTestResult = null;
    },

    cancelMqttEdit() {
      this.mqttEditing = false;
      this.mqttTestResult = null;
    },

    async testMqttConnection() {
      this.mqttTesting = true;
      this.mqttTestResult = null;
      try {
        const data = await fetchJSON('api/mqtt/test', {
          method: 'POST',
          body: JSON.stringify({
            host: this.mqttEdit.host,
            port: Number(this.mqttEdit.port),
          }),
        });
        if (data && data.ok) {
          this.mqttTestResult = { ok: true, msg: 'Connection OK' };
        } else {
          this.mqttTestResult = {
            ok: false,
            msg: data?.error || data?.detail || 'Failed',
          };
        }
      } catch (e) {
        this.mqttTestResult = { ok: false, msg: String(e) };
      } finally {
        this.mqttTesting = false;
      }
    },

    async saveMqttConfig() {
      this.mqttSaving = true;
      try {
        const payload = {
          host: this.mqttEdit.host,
          port: Number(this.mqttEdit.port),
          username: this.mqttEdit.username || null,
          tls_mode: this.mqttEdit.tls_mode,
          topic_prefix: this.mqttEdit.topic_prefix,
          discovery_prefix: this.mqttEdit.discovery_prefix,
        };
        if (this.mqttEdit.password) {
          payload.password = this.mqttEdit.password;
        }
        const data = await fetchJSON('api/mqtt/config', {
          method: 'PATCH',
          body: JSON.stringify(payload),
        });
        if (data && !data.error) {
          Alpine.store('app').toast('MQTT config saved', 'info');
          this.mqttEditing = false;
          await this.loadMqttConfig();
          // Reconnect with new settings
          await fetchJSON('api/mqtt/reconnect', { method: 'POST' });
          Alpine.store('app').toast('MQTT reconnecting…', 'info');
          await this.loadAll();
        } else {
          Alpine.store('app').toast(
            'Save failed: ' + (data?.error || 'unknown'),
            'error',
          );
        }
      } finally {
        this.mqttSaving = false;
      }
    },

    async republishDiscovery() {
      this.republishing = true;
      try {
        const data = await fetchJSON('api/mqtt/publish', { method: 'POST' });
        if (data && !data.error) {
          Alpine.store('app').toast('Discovery republish queued', 'info');
          // Poll for confirmation — discovery_published should flip to true
          await this._pollDiscoveryStatus(true);
        } else {
          Alpine.store('app').toast('Republish failed: ' + (data?.error || 'unknown'), 'error');
        }
      } finally {
        this.republishing = false;
      }
    },

    async unpublishDiscovery() {
      this.unpublishing = true;
      try {
        const data = await fetchJSON('api/mqtt/unpublish', { method: 'POST' });
        if (data && !data.error) {
          const count = data.topics_cleared ?? 0;
          Alpine.store('app').toast(
            `Discovery unpublished — ${count} topics tombstoned`,
            'info',
          );
          await this.loadAll();
        } else {
          Alpine.store('app').toast('Unpublish failed: ' + (data?.error || 'unknown'), 'error');
        }
      } finally {
        this.unpublishing = false;
      }
    },

    /** Poll MQTT status until discovery_published matches expected, max 5s. */
    async _pollDiscoveryStatus(expected) {
      for (let i = 0; i < 5; i++) {
        await new Promise(r => setTimeout(r, 1000));
        await this.loadAll();
        if (this.mqtt.discovery_published === expected) {
          Alpine.store('app').toast(
            `Discovery published — ${this.mqtt.entity_count ?? '?'} entities`,
            'info',
          );
          return;
        }
      }
      // Timed out — still refresh
      await this.loadAll();
    },
  };
}
