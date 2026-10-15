/**
 * HA Entities Tab — browse Home Assistant entities across instances and pick
 * which to expose as ha:<instance>:<entity> Automation condition sensors.
 */
function haEntitiesTab() {
  return {
    instances: [],
    domains: [],
    entities: [],
    total: 0,
    grandTotal: 0,
    exposedCount: 0,
    page: 1,
    pageSize: 50,
    search: '',
    domain: '',
    instance: '',
    exposedOnly: false,
    loading: false,
    dash: {
      open: false, layout: 'grouped', diagnostics: false, instance: '',
      yaml: '', count: 0, published: 0, source: '', error: '', loading: false, copyMsg: '',
    },

    /** Topic presets — each is a synonym set, because the same concept is named
     *  differently by every integration (pv / solar / enphase / envoy all mean
     *  the same array). The API ORs comma-separated terms. */
    presets: [
      { label: 'Solar / PV', terms: 'pv,solar,enphase,envoy,inverter' },
      { label: 'Battery', terms: 'battery,soc,franklin,powerwall,charge' },
      { label: 'Grid / Export', terms: 'grid,export,feed_in,feed-in,import,curtail' },
      { label: 'Price / Tariff', terms: 'price,amber,tariff,cost,rate' },
      { label: 'Automations', terms: 'automation.' },
    ],

    get totalPages() {
      return Math.max(1, Math.ceil(this.total / this.pageSize));
    },

    /** Enabled automations in the current view — the ones that can fight a
     *  Bridge rule for control of the same entity. */
    get activeAutomations() {
      return this.entities.filter(
        (e) => e.domain === 'automation' && String(e.state).toLowerCase() === 'on',
      ).length;
    },

    /** Toggle: clicking the active preset clears it. */
    applyPreset(p) {
      this.search = this.search === p.terms ? '' : p.terms;
      this.page = 1;
      this.load();
    },

    clearFilters() {
      this.resetFilters();
      this.load();
    },

    async init() {
      await this.loadInstances();
      if (this.instances.length) {
        await this.loadDomains();
        await this.load();
      }
      // Refresh when the user switches back to this tab (new instances/entities).
      window.addEventListener('tab:changed', (ev) => {
        if (ev.detail && ev.detail.tab === 'ha_entities') this.reload();
      });
      // Settings → MQTT has a shortcut that lands here with the card open.
      window.addEventListener('ha:dashboard-card', () => this.openDashboard());
    },

    async loadInstances() {
      const data = await fetchJSON('api/ha/instances');
      if (Array.isArray(data)) this.instances = data.map((i) => ({ id: i.id, name: i.name }));
    },

    async loadDomains() {
      const data = await fetchJSON('api/ha/domains');
      if (Array.isArray(data)) this.domains = data;
    },

    resetFilters() {
      this.search = '';
      this.domain = '';
      this.instance = '';
      this.exposedOnly = false;
      this.page = 1;
    },

    // Refresh + re-entering the tab both give a clean slate (no stale filters).
    async reload() {
      this.resetFilters();
      await this.loadInstances();
      if (this.instances.length) await this.loadDomains();
      await this.load();
    },

    async load() {
      this.loading = true;
      try {
        const p = new URLSearchParams({ page: this.page, page_size: this.pageSize });
        if (this.search) p.set('search', this.search);
        if (this.domain) p.set('domain', this.domain);
        if (this.instance) p.set('instance', this.instance);
        if (this.exposedOnly) p.set('exposed', 'true');
        const data = await fetchJSON(`api/ha/entities?${p.toString()}`);
        if (data && !data.error) {
          this.entities = data.entities || [];
          this.total = data.total || 0;
          this.grandTotal = data.grand_total ?? data.total ?? 0;
          this.exposedCount = data.exposed_count || 0;
        }
      } finally {
        this.loading = false;
      }
    },

    async toggleExpose(e) {
      const next = !e.exposed;
      const data = await fetchJSON('api/ha/entities/expose', {
        method: 'POST',
        body: JSON.stringify({ instance_id: e.instance, entity_id: e.entity_id, exposed: next }),
      });
      if (data && !data.error) {
        e.exposed = next;
        this.exposedCount += next ? 1 : -1;
        Alpine.store('app').toast(
          `${next ? 'Exposed' : 'Hidden'}: ${e.friendly_name}`, 'info');
        // If viewing exposed-only, a hidden row should drop out.
        if (this.exposedOnly && !next) this.load();
      } else {
        Alpine.store('app').toast('Failed: ' + (data?.error || 'unknown'), 'error');
      }
    },

    // ── Dashboard card YAML (#86) ──────────────────────────────
    async openDashboard() {
      this.dash.open = true;
      await this.loadDashboard();
    },

    async loadDashboard() {
      const d = this.dash;
      d.loading = true;
      d.error = '';
      d.copyMsg = '';
      d.yaml = '';
      d.count = 0;
      try {
        const p = new URLSearchParams({ layout: d.layout, diagnostics: d.diagnostics ? 'true' : 'false' });
        if (d.instance) p.set('instance', d.instance);
        const data = await fetchJSON(`api/ha/dashboard-yaml?${p.toString()}`);
        if (data && data.yaml) {
          d.yaml = data.yaml;
          d.count = data.entity_count || 0;
          d.published = data.published_count || 0;
          d.source = data.instance ? data.instance.name : '';
        } else {
          d.error = (data && data.error) || 'No YAML returned.';
        }
      } finally {
        d.loading = false;
      }
    },

    /** navigator.clipboard only exists in a secure context, and HA ingress is
     *  often plain http — so fall back to execCommand on the readonly textarea,
     *  and failing that leave the text selected for Ctrl/Cmd+C. */
    async copyDashboard() {
      const d = this.dash;
      const ta = this.$refs.dashYaml;
      if (navigator.clipboard && window.isSecureContext) {
        try {
          await navigator.clipboard.writeText(d.yaml);
          d.copyMsg = 'Copied.';
          return;
        } catch (_) { /* fall through */ }
      }
      let ok = false;
      if (ta) {
        ta.focus();
        ta.select();
        try { ok = document.execCommand('copy'); } catch (_) { ok = false; }
      }
      d.copyMsg = ok ? 'Copied.' : 'Press Ctrl/Cmd+C to copy the selected text.';
    },

    prevPage() {
      if (this.page > 1) { this.page--; this.load(); }
    },
    nextPage() {
      if (this.page < this.totalPages) { this.page++; this.load(); }
    },
  };
}
