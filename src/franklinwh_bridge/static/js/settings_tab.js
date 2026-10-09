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
    mqttGateways: [],
    mqttGatewayPoints: null,
    selectedMqttGateway: '',
    entityFilter: '',
    metricsRetention: 30,
    metricsRawAge: 7,
    // Point history (per-point electrical readings). Off by default: it can
    // add hundreds of MB to a ~20MB database, so an upgrade must not start
    // consuming that on an add-on running from an SD card.
    ph: null,
    phPoints: [],
    phStorage: null,
    phSaving: false,
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
    newGw: { gateway_id: '', name: '', host: '', port: 502, unit_id: 1, poll_interval: 10, timeout: 10, description: '', mock: false, home_load_source: 'standard' },
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
      await this.loadPointHistory();
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
      // Multi-gateway: `gateways` carries every publishing gateway. The flat
      // `topics` remains the selected one for back-compat. Without this the
      // panel only ever showed the default gateway, so a second gateway
      // publishing dozens of entities looked identical to one publishing none.
      if (topics && Array.isArray(topics.gateways)) {
        this.mqttGateways = topics.gateways;
        const keep = this.mqttGateways.find(g => g.gateway_id === this.selectedMqttGateway);
        if (!keep) {
          this.selectedMqttGateway = topics.gateway_id
            || (this.mqttGateways[0] || {}).gateway_id || '';
        }
        this.applyMqttGateway();
      } else if (topics && topics.topics) {
        this.entities = topics.topics;
      }
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
        // plan description + permissions (migration 36); permissive defaults so
        // an older service edits exactly as it behaves.
        plan_type: svc.plan_type || 'unknown',
        export_allowed: svc.export_allowed !== false,
        solar_export_allowed: svc.solar_export_allowed !== false && svc.solar_export_allowed !== 0,
        battery_export_allowed: svc.battery_export_allowed !== false && svc.battery_export_allowed !== 0,
        export_restriction_note: svc.export_restriction_note || '',
        export_limit_kw: Number(svc.export_limit_kw) || 0,
        charging_allowed: svc.charging_allowed !== false,
        discharging_allowed: svc.discharging_allowed !== false,
        // where the service is billed (migration 38). Blank = not stated; the
        // form never pre-fills a guess, so an empty field means empty on file.
        // Connection-level (network's grant) vs retail — kept distinct so a
        // plan switch can preserve the first.
        network: svc.network || '',
        retailer: svc.retailer || '',
        pto_status: svc.pto_status || 'unknown',
        pto_reference: svc.pto_reference || '',
        country: svc.country || '',
        timezone: svc.timezone || '',
        // calculation method + rates (stored in the pricing JSON)
        pricing: {
          // Spread FIRST so any pricing key this form doesn't model survives the
          // round-trip. Rebuilding from an allow-list silently dropped whatever
          // wasn't listed — `seasons` (the TOU rate model) was written by the
          // API and would have been wiped by the next Save from this editor.
          // The server replaces `pricing` wholesale, so whatever the form omits
          // is gone.
          ...(svc.pricing || {}),
          billing_cycle_day: (svc.pricing && svc.pricing.billing_cycle_day) || 1,
          demand_rate: (svc.pricing && svc.pricing.demand_rate) || 0,
          demand_interval_min: (svc.pricing && svc.pricing.demand_interval_min) || 30,
          demand_charge_basis: (svc.pricing && svc.pricing.demand_charge_basis) || 'per_kw_day',
          export_bonus_rate: (svc.pricing && svc.pricing.export_bonus_rate) || 0,
          // TOU import rates. Recorded (the AGL preset writes them) but not yet
          // priced — no import-rate engine exists. Kept in the payload so a
          // save doesn't silently drop what the preset captured.
          import_rates: (svc.pricing && svc.pricing.import_rates) || {
            peak: 0, off_peak: 0, gst_inclusive: true,
            peak_window: { start: '15:00', end: '21:00', months: [] },
          },
          export_rates: (svc.pricing && svc.pricing.export_rates) || null,
          // Seasonal rate model — deep-copied so edits don't mutate the loaded
          // row before Save.
          seasons: JSON.parse(JSON.stringify((svc.pricing && svc.pricing.seasons) || [])),
          default_rate: JSON.parse(JSON.stringify(
            (svc.pricing && svc.pricing.default_rate) || { buy: 0, sell: 0 })),
          export_charge: (svc.pricing && svc.pricing.export_charge) || {
            window: { months: [], days: [], start: '10:00', end: '15:00' },
            rate: 0.0123, free_kwh_per_day: 6.84,
          },
          // Standing charges — a list, copied so edits don't mutate the row.
          fixed_charges: ((svc.pricing && svc.pricing.fixed_charges) || []).map((c) => ({
            type: c.type || 'supply',
            description: c.description || '',
            levied_by: c.levied_by || 'utility',
            frequency: c.frequency || 'daily',
            rate: Number(c.rate) || 0,
            tax_rate: Number(c.tax_rate) || 0,
          })),
        },
      };
      // Land on a section that is actually in use rather than an empty pane.
      this.tariffTab = svc.has_peak_demand ? 'demand'
        : svc.has_export_bonus ? 'bonus'
        : (svc.pricing && svc.pricing.export_charge) ? 'charge'
        : 'fixed';
      this.seasonIdx = 0;
      this.rateProblems = [];
      this.serviceEditId = svc.id;
    },

    // ── Fixed / standing charges ──────────────────────────────
    addFixedCharge() {
      this.serviceEdit.pricing.fixed_charges.push({
        type: 'supply', description: '', levied_by: 'utility',
        frequency: 'daily', rate: 0, tax_rate: 0,
      });
    },
    removeFixedCharge(i) { this.serviceEdit.pricing.fixed_charges.splice(i, 1); },

    /** Per-day equivalent of one charge, tax-inclusive. Mirrors the server's
     *  _daily_equiv() against a nominal 30-day period — the live sensors use
     *  the real billing-period length. */
    _fixedPerDay(fc) {
      const rate = Number(fc.rate) || 0;
      const per = { daily: 1, weekly: 7, monthly: 30, quarterly: 90, annual: 360 }[fc.frequency] || 1;
      return (rate / per) * (1 + (Number(fc.tax_rate) || 0));
    },
    fixedChargePerDay(fc) { return '$' + this._fixedPerDay(fc).toFixed(4); },
    fixedChargesPerDay() {
      const t = (this.serviceEdit.pricing.fixed_charges || [])
        .reduce((s, fc) => s + this._fixedPerDay(fc), 0);
      return '$' + t.toFixed(4);
    },
    fixedChargesPerMonth() {
      const t = (this.serviceEdit.pricing.fixed_charges || [])
        .reduce((s, fc) => s + this._fixedPerDay(fc), 0) * 30;
      return '$' + t.toFixed(2);
    },

    cancelServiceEdit() {
      // Hide via serviceEditId (the form's x-if); keep serviceEdit intact so the
      // billing window x-for bindings don't read .months/.days off an emptied
      // object during Alpine's teardown tick (edit/add overwrites it next time).
      this.serviceEditId = null;
    },

    // Fill the billing form with Ausgrid's mandatory two-way export tariff EA029
    // (FY27 rates, per Amber). REWARD 4-9pm @ 3.8551c for all exports; export
    // CHARGE 10am-3pm @ 1.3552c above the free "Basic Export Level" of ~6.83
    // kWh/day (≈212 kWh over a 31-day period). Applies every day, all year.
    // (Demand tariff is seasonal + needs the network demand-window times, so it
    //  is configured separately — see the Peak-demand section.)
    loadAusgridPreset() {
      const e = this.serviceEdit;
      e.has_export_bonus = true;
      e.bonus_window = { months: [], days: [], start: '16:00', end: '21:00' };
      e.pricing.export_bonus_rate = 0.038551;
      e.has_export_charge = true;
      e.pricing.export_charge = {
        window: { months: [], days: [], start: '10:00', end: '15:00' },
        rate: 0.013552, free_kwh_per_day: 6.83,
      };
      if (!e.pricing.billing_cycle_day) e.pricing.billing_cycle_day = 1;
      Alpine.store('app').toast('Ausgrid EA029 two-way tariff (FY27) loaded — review & save', 'info');
    },

    // Fill the billing form with AGL's TOU retail plan (rates from the user's
    // "your new electricity plan is all set" statement, 2026-09-12).
    //
    // What the engine ACTUALLY prices from this: the 28c evening feed-in
    // (bonus window) and the daily supply charge. The plan has NO demand
    // charges, so peak-demand stays off.
    //
    // The import rates (peak 54.175c / off-peak 21.626c) are recorded under
    // pricing.import_rates but are NOT priced — the billing engine has no
    // import-rate model yet, so storing them keeps the plan on file without
    // pretending they feed net_total.
    //
    // Deliberately does NOT touch has_export_charge: a network two-way export
    // charge (Ausgrid EA029) can sit underneath a retail plan, and silently
    // clearing it would understate costs. Review that toggle separately.
    loadAglPreset() {
      const e = this.serviceEdit;
      e.plan_type = 'tou';
      e.has_tou = true;
      e.has_peak_demand = false;

      // Evening peak FiT — 28 c/kWh, 5pm-9pm every day, all year. This is the
      // full export rate in the window (same convention as the Ausgrid preset),
      // not a bonus added on top.
      e.has_export_bonus = true;
      e.bonus_window = { months: [], days: [], start: '17:00', end: '21:00' };
      e.pricing.export_bonus_rate = 0.28;

      // Supply charge 158.631 c/day, already GST-inclusive → tax_rate 0.
      // Replace any existing supply row so re-loading the preset can't stack it.
      const others = (e.pricing.fixed_charges || []).filter((fc) => fc.type !== 'supply');
      e.pricing.fixed_charges = [...others, {
        type: 'supply', description: 'AGL daily supply charge', levied_by: 'utility',
        frequency: 'daily', rate: 1.58631, tax_rate: 0,
      }];

      // On file, not priced (see note above).
      e.pricing.import_rates = {
        peak: 0.54175, off_peak: 0.21626, gst_inclusive: true,
        // Peak 3pm-9pm daily, Nov-Mar and Jun-Aug. Off-peak covers all other
        // hours, and all day during Apr-May and Sep-Oct.
        peak_window: { start: '15:00', end: '21:00', months: [11, 12, 1, 2, 3, 6, 7, 8] },
      };
      e.pricing.export_rates = {
        off_peak: 0.03, morning_peak: 0.03, evening_peak: 0.28, gst_inclusive: false,
        morning_peak_window: { start: '07:00', end: '08:00' },
      };
      if (!e.pricing.billing_cycle_day) e.pricing.billing_cycle_day = 1;
      Alpine.store('app').toast(
        'AGL TOU plan loaded — 28c evening FiT + supply charge priced; import rates on file only',
        'info',
      );
    },

    // ── Share a tariff profile ─────────────────────────────
    async exportService(id) {
      const data = await fetchJSON(`api/services/${id}/export`);
      if (!data || data.error) {
        Alpine.store('app').toast('Export failed', 'error');
        return;
      }
      const name = (data.profile?.name || 'tariff').replace(/[^\w.-]+/g, '_');
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `${name}-tariff.json`;
      a.click();
      URL.revokeObjectURL(a.href);
      Alpine.store('app').toast('Tariff profile exported', 'info');
    },

    importReport: null,
    importBundle: null,
    async pickImportFile(ev, serviceId) {
      const file = ev.target.files?.[0];
      ev.target.value = '';          // allow re-picking the same file
      if (!file) return;
      let bundle;
      try {
        bundle = JSON.parse(await file.text());
      } catch (_) {
        Alpine.store('app').toast('That file is not valid JSON', 'error');
        return;
      }
      this.importBundle = { bundle, serviceId };
      // Dry run FIRST, always: importing overwrites rates that price real
      // money, so it must never be one unconfirmed click.
      this.importReport = await fetchJSON('api/services/import', {
        method: 'POST', body: JSON.stringify(bundle),
      });
    },
    async confirmImport() {
      const { bundle, serviceId } = this.importBundle || {};
      const q = serviceId ? `?dry_run=false&service_id=${encodeURIComponent(serviceId)}`
                          : '?dry_run=false';
      const res = await fetchJSON(`api/services/import${q}`, {
        method: 'POST', body: JSON.stringify(bundle),
      });
      if (res && res.applied_to) {
        Alpine.store('app').toast('Tariff profile imported', 'info');
        this.importReport = null; this.importBundle = null;
        await this.loadServices();
        if (this.serviceEditId) this.cancelServiceEdit();
      } else {
        Alpine.store('app').toast('Import failed: ' + (res?.detail || 'unknown'), 'error');
      }
    },
    cancelImport() { this.importReport = null; this.importBundle = null; },

    async saveService() {
      const isNew = this.serviceEditId === 'new';
      const url = isNew ? 'api/services' : `api/services/${this.serviceEditId}`;
      // The server reads the PRESENCE of pricing.export_charge as "enabled",
      // so turning the toggle off has to clear it — but clearing it outright
      // destroyed the rate and free allowance, and re-ticking the box came
      // back empty. Worse, a save made while it was off wiped a config the
      // user never touched. Park the values instead so the toggle is
      // reversible.
      if (this.serviceEdit.pricing) {
        const pr = this.serviceEdit.pricing;
        if (!this.serviceEdit.has_export_charge) {
          if (pr.export_charge) pr._export_charge_parked = pr.export_charge;
          pr.export_charge = null;
        } else if (!pr.export_charge && pr._export_charge_parked) {
          pr.export_charge = pr._export_charge_parked;
        }
      }
      // has_export_charge is a UI-only flag — the server derives it from
      // pricing.export_charge. The API now rejects unknown fields rather than
      // dropping them silently, so it must not be sent.
      // Stamp the derived plan type so service.plan_type reflects the actual
      // rates rather than whatever was last picked from a dropdown.
      this.serviceEdit.plan_type = this.derivedPlanType;
      const { has_export_charge: _uiOnly, ...payload } = this.serviceEdit;
      const data = await fetchJSON(url, {
        method: isNew ? 'POST' : 'PATCH',
        body: JSON.stringify(payload),
      });
      if (data && !data.error) {
        await this.loadServices();
        // Closing on save dropped you back to the services list and lost the
        // sub-tab you were editing — punishing for a form you tweak in several
        // places. A new service still closes, because the next step is to edit
        // the one you just made.
        if (isNew) this.cancelServiceEdit();
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
        managed: h.managed,
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
        is_default: !!this.haEdit.is_default,
        enabled: !!this.haEdit.enabled,
      };
      // The Supervisor-managed instance's URL and token aren't editable.
      if (!this.haEdit.managed) body.base_url = this.haEdit.base_url;
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
          this.newGw = { gateway_id: '', name: '', host: '', port: 502, unit_id: 1, poll_interval: 10, timeout: 10, description: '', mock: false, home_load_source: 'standard' };
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
        // Default true for a gateway saved before migration 39.
        publish_to_ha: gw.publish_to_ha !== false && gw.publish_to_ha !== 0,
        ac_type: gw.ac_type ?? 0,
        home_load_source: gw.home_load_source || 'standard',
        mock: !!gw.mock,
      };
      this.phaseDetect = null;
    },

    // Detection result for the gateway being edited: {detected, matches_declared, ...}
    // ── Seasonal TOU rates (seasons -> blocks -> time periods) ─
    // Mirrors gateway/rate_model.py. The season owns months, a block owns
    // hours (and optionally weekdays), a TIME PERIOD owns the {buy, sell} pair —
    // because a plan's import and export boundaries need not align.
    PERIOD_IDS: ['super_off_peak', 'off_peak', 'mid_peak', 'on_peak'],
    PERIOD_LABELS: {
      super_off_peak: 'Super Off-Peak', off_peak: 'Off-Peak',
      mid_peak: 'Mid-Peak', on_peak: 'On-Peak',
    },
    PERIOD_DOTS: {
      super_off_peak: '#3b82f6', off_peak: '#64748b',
      mid_peak: '#f59e0b', on_peak: '#ef4444',
    },
    seasonIdx: 0,
    rateProblems: [],

    get seasons() { return (this.serviceEdit?.pricing?.seasons) || []; },

    // ── Tariff-wide rate ───────────────────────────────────
    // What a kWh costs when no time period overrides it. A flat or tiered
    // tariff is complete with just this — seasons and time periods exist to
    // OVERRIDE it, not as a prerequisite. Tiers here count consumption at the
    // METER over the billing period, which is what a tiered plan actually
    // bills on.
    get defaultRate() {
      const pr = this.serviceEdit?.pricing;
      if (!pr) return null;
      if (!pr.default_rate) pr.default_rate = { buy: 0, sell: 0 };
      return pr.default_rate;
    },
    isDefaultTiered(side) { return Array.isArray(this.defaultRate?.[side]); },
    makeDefaultTiered(side) {
      const flat = Number(this.defaultRate[side]) || 0;
      this.defaultRate[side] = [{ up_to_kwh: 1000, rate: flat }, { rate: flat }];
      this.checkRates();
    },
    makeDefaultFlat(side) {
      const l = this.defaultRate[side];
      this.defaultRate[side] = Number(l?.[0]?.rate) || 0;
      this.checkRates();
    },
    addDefaultTier(side) {
      const l = this.defaultRate[side];
      const bounded = l.filter((t) => typeof t.up_to_kwh === 'number');
      const next = bounded.length ? Math.max(...bounded.map((t) => t.up_to_kwh)) * 2 : 1000;
      l.splice(l.length - 1, 0, { up_to_kwh: next, rate: l[l.length - 1]?.rate ?? 0 });
      this.checkRates();
    },
    removeDefaultTier(side, i) {
      const l = this.defaultRate[side];
      if (l.length <= 2) { this.makeDefaultFlat(side); return; }
      l.splice(i, 1);
      this.checkRates();
    },
    // Changing the tariff-wide rate does NOT silently rewrite the per-period
    // overrides — that would throw away deliberate work. Offered, never
    // assumed.
    applyDefaultToAllPeriods() {
      const src = JSON.parse(JSON.stringify(this.defaultRate));
      let n = 0;
      for (const s of this.seasons) {
        for (const k of Object.keys(s.time_periods || {})) {
          s.time_periods[k] = JSON.parse(JSON.stringify(src));
          n += 1;
        }
      }
      this.checkRates();
      Alpine.store('app').toast(`Reset ${n} time period(s) to the tariff rate`, 'info');
    },

    // Plan type DERIVED from the rates, not typed by hand. It was a dropdown
    // the user set and nothing read — it could say "Flat rate" over a
    // four-season TOU plan and nothing would notice. Deriving it means the
    // label cannot disagree with the configuration.
    get derivedPlanType() {
      const ss = this.seasons;
      const dr = this.serviceEdit?.pricing?.default_rate;
      const drTiered = Array.isArray(dr?.buy) || Array.isArray(dr?.sell);
      const drSet = dr && (dr.buy || dr.sell || drTiered);
      if (!ss.length) return drTiered ? 'tiered' : (drSet ? 'fixed' : 'unknown');
      const tiered = ss.some((s) => Object.values(s.time_periods || {}).some(
        (w) => Array.isArray(w.buy) || Array.isArray(w.sell)));
      const timed = ss.length > 1 || ss.some((s) => (s.blocks || []).length > 1);
      if ((tiered || drTiered) && timed) return 'hybrid';
      if (tiered || drTiered) return 'tiered';
      if (timed) return 'tou';
      return 'fixed';
    },
    get planTypeLabel() {
      return {
        unknown: 'No rates configured', fixed: 'Flat rate',
        tiered: 'Tiered (by kWh used)', tou: 'Time of use',
        hybrid: 'Hybrid — time of use with tiers',
      }[this.derivedPlanType];
    },
    get season() { return this.seasons[this.seasonIdx] || null; },

    _blankSeason(name) {
      const periods = {};
      for (const w of this.PERIOD_IDS) periods[w] = { buy: 0, sell: 0 };
      return {
        id: 'season_' + Date.now().toString(36),
        name: name || `Season ${this.seasons.length + 1}`,
        months: [],
        time_periods: periods,
        // One all-day block so a new season is valid immediately rather than
        // failing validation before it has been touched.
        blocks: [{ start: '00:00', end: '24:00', time_period: 'off_peak', days: [] }],
      };
    },
    // A flat plan is expressible in the same model — one catch-all season,
    // one all-day block, one time period — so it needs a shortcut, not a second
    // structure. Anything else would give two ways to mean the same thing.
    addFlatSeason() {
      const season = this._blankSeason('Flat rate');
      season.months = [];                       // catch-all: every month
      season.blocks = [{ start: '00:00', end: '24:00', time_period: 'off_peak', days: [] }];
      if (!this.serviceEdit.pricing.seasons) this.serviceEdit.pricing.seasons = [];
      this.serviceEdit.pricing.seasons.push(season);
      this.seasonIdx = this.seasons.length - 1;
      this.checkRates();
      Alpine.store('app').toast('Flat rate season added — set buy and sell on Off-Peak', 'info');
    },

    addSeason() {
      if (!this.serviceEdit.pricing.seasons) this.serviceEdit.pricing.seasons = [];
      this.serviceEdit.pricing.seasons.push(this._blankSeason());
      this.seasonIdx = this.seasons.length - 1;
      this.checkRates();
    },
    removeSeason(i) {
      this.serviceEdit.pricing.seasons.splice(i, 1);
      this.seasonIdx = Math.max(0, Math.min(this.seasonIdx, this.seasons.length - 1));
      this.checkRates();
    },
    monthOwner(m) {
      const s = this.seasons.find((x) => (x.months || []).includes(m));
      return s ? s.name : null;
    },
    // Months are EXCLUSIVE across seasons: clicking one that belongs elsewhere
    // moves it, rather than silently creating an overlap the resolver would
    // have to arbitrate.
    toggleMonth(m) {
      if (!this.season) return;
      const mine = (this.season.months || []).includes(m);
      for (const s of this.seasons) {
        s.months = (s.months || []).filter((x) => x !== m);
      }
      if (!mine) this.season.months.push(m);
      this.season.months.sort((a, b) => a - b);
      this.checkRates();
    },
    addBlock() {
      this.season.blocks.push({ start: '00:00', end: '06:00', time_period: 'off_peak', days: [] });
      this.checkRates();
    },
    removeBlock(i) { this.season.blocks.splice(i, 1); this.checkRates(); },
    // Presets rather than seven toggles — every plan we've seen uses one of
    // these three shapes.
    setBlockDays(block, preset) {
      block.days = preset === 'weekdays' ? [0, 1, 2, 3, 4]
        : preset === 'weekend' ? [5, 6] : [];
      this.checkRates();
    },
    blockDayPreset(block) {
      const d = (block.days || []).join(',');
      if (d === '0,1,2,3,4') return 'weekdays';
      if (d === '5,6') return 'weekend';
      return 'all';
    },
    // A time period nothing references is dead weight in the grid — say so rather
    // than implying its price applies.
    periodUsed(w) {
      return (this.season?.blocks || []).some((b) => b.time_period === w);
    },
    // ── Tier ladders on a time period's price ─────────────
    // A price is either a number or a list of tiers. Tiers live on the period, so
    // a TOU band with a tiered price IS a hybrid plan — no separate concept.
    isTiered(period, side) { return Array.isArray(this.season?.time_periods?.[period]?.[side]); },
    makeTiered(period, side) {
      const flat = Number(this.season.time_periods[period][side]) || 0;
      // Seed with the existing rate so switching modes never silently changes
      // today's price — only the shape it's expressed in.
      this.season.time_periods[period][side] = [
        { up_to_kwh: 1000, rate: flat },
        { rate: flat },
      ];
      this.checkRates();
    },
    makeFlat(period, side) {
      const ladder = this.season.time_periods[period][side];
      this.season.time_periods[period][side] = Number(ladder?.[0]?.rate) || 0;
      this.checkRates();
    },
    addTier(period, side) {
      const ladder = this.season.time_periods[period][side];
      const last = ladder[ladder.length - 1];
      // Insert BEFORE the unbounded tier — that one must stay last or the
      // ladder stops covering everything above the final threshold.
      const bounded = ladder.filter((t) => typeof t.up_to_kwh === 'number');
      const nextLimit = bounded.length ? Math.max(...bounded.map((t) => t.up_to_kwh)) * 2 : 1000;
      ladder.splice(ladder.length - 1, 0, { up_to_kwh: nextLimit, rate: last?.rate ?? 0 });
      this.checkRates();
    },
    removeTier(period, side, i) {
      const ladder = this.season.time_periods[period][side];
      if (ladder.length <= 2) { this.makeFlat(period, side); return; }
      ladder.splice(i, 1);
      this.checkRates();
    },

    copyRatesToAllSeasons() {
      if (!this.season) return;
      const src = JSON.parse(JSON.stringify(this.season.time_periods));
      for (const s of this.seasons) if (s !== this.season) s.time_periods = JSON.parse(JSON.stringify(src));
      Alpine.store('app').toast('Rates copied to every season', 'info');
    },
    async checkRates() {
      const data = await fetchJSON('api/tariff/validate-rates', {
        method: 'POST',
        body: JSON.stringify({
          seasons: this.seasons,
          default_rate: this.serviceEdit?.pricing?.default_rate || null,
        }),
      });
      this.rateProblems = (data && data.problems) || [];
    },

    // ── Tariff sub-tabs ────────────────────────────────────
    // The four tariff components are long forms; stacked, the service editor
    // scrolled for pages. Only one is edited at a time.
    tariffTab: 'demand',
    get tariffTabs() {
      const e = this.serviceEdit || {};
      const pricing = e.pricing || {};
      return [
        { id: 'demand', label: 'Peak demand', on: () => !!e.has_peak_demand },
        { id: 'bonus', label: 'Export bonus', on: () => !!e.has_export_bonus },
        { id: 'charge', label: 'Export charge', on: () => !!e.has_export_charge },
        { id: 'fixed', label: 'Standing charges',
          on: () => (pricing.fixed_charges || []).length > 0 },
        { id: 'import', label: 'Energy rates',
          on: () => (pricing.seasons || []).length > 0 },
      ];
    },

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
            // Hand-built body: anything omitted here is silently not saved,
            // which is how the publish toggle appeared to work and didn't.
            publish_to_ha: g.publish_to_ha,
            ac_type: g.ac_type,
            home_load_source: g.home_load_source,
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

    async loadPointHistory() {
      const [cfg, storage] = await Promise.all([
        fetchJSON('api/point-history/config'),
        fetchJSON('api/point-history/storage'),
      ]);
      if (cfg && !cfg.error) {
        this.ph = cfg.config;
        this.phPoints = cfg.available_points || [];
      }
      if (storage && !storage.error) this.phStorage = storage;
    },

    /** Projected size of the CURRENT form values, so the cost of a choice is
     *  visible while it is still a choice rather than discovered afterwards.
     *  Computed client-side from the row size the API reports — asking the
     *  server on every keystroke would be a request per digit typed. */
    get phProjectedBytes() {
      if (!this.ph || !this.phStorage) return 0;
      const rowBytes = this.phStorage.bytes_per_row || 0;
      const interval = Number(this.ph.interval_s) || 0;
      if (interval <= 0) return 0;
      const rows = (86400 / interval) * (this.ph.points?.length || 0)
        * (Number(this.ph.retention_days) || 0);
      return Math.round(rows * rowBytes);
    },

    /** Bytes as something readable. The projection reaches GB at the upper end
     *  of the settings (every point, 5s, a year), so a fixed MB unit would show
     *  "512000 MB" exactly where the number matters most. */
    phBytesHuman(n) {
      if (!(n > 0)) return '0 MB';
      const mb = n / (1024 * 1024);
      if (mb < 1) return `${Math.round(n / 1024)} KB`;
      if (mb < 1024) return `${mb.toFixed(mb < 10 ? 1 : 0)} MB`;
      return `${(mb / 1024).toFixed(2)} GB`;
    },

    phTogglePoint(key) {
      if (!this.ph) return;
      const at = this.ph.points.indexOf(key);
      if (at >= 0) this.ph.points.splice(at, 1);
      else this.ph.points.push(key);
    },

    async savePointHistory() {
      if (!this.ph) return;
      this.phSaving = true;
      try {
        const data = await fetchJSON('api/point-history/config', {
          method: 'PUT',
          body: JSON.stringify({
            enabled: !!this.ph.enabled,
            points: this.ph.points,
            interval_s: Number(this.ph.interval_s),
            retention_days: Number(this.ph.retention_days),
          }),
        });
        if (data && !data.error) {
          // Re-read rather than trusting the echo: enabling it changes what the
          // actual stored figure will become, and the panel should not keep
          // showing the stale one beside the new projection.
          await this.loadPointHistory();
          Alpine.store('app').toast(
            this.ph.enabled
              ? `Recording ${this.ph.points.length} points every ${this.ph.interval_s}s`
              : 'Point history off', 'info');
        } else {
          Alpine.store('app').toast(
            'Save failed: ' + (data?.detail || data?.error || 'unknown'), 'error');
        }
      } finally {
        this.phSaving = false;
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

    /** Point the entity table at one gateway's entities AND its values. */
    async applyMqttGateway() {
      const g = (this.mqttGateways || []).find(x => x.gateway_id === this.selectedMqttGateway);
      this.entities = g ? g.topics : [];

      // Values must come from the gateway whose entities are on screen, not
      // from whatever the topbar happens to be showing. Without this the mock's
      // rows rendered the REAL gateway's readings — verified against the broker:
      // mock SoC 30.3 / SoH 96.0 displayed as 37 / 94.9. Rows that silently
      // belong to a different device are worse than no rows at all.
      this.mqttGatewayPoints = null;
      if (!g) return;
      const data = await fetchJSON(`api/gateways/${encodeURIComponent(g.gateway_id)}/points`);
      if (data && data.points) this.mqttGatewayPoints = data.points;
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
      // Prefer the selected MQTT gateway's own points; fall back to the store
      // only before that fetch lands.
      const pts = this.mqttGatewayPoints || Alpine.store('app').points;
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
