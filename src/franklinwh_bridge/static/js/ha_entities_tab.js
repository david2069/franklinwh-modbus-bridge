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
    exposedCount: 0,
    page: 1,
    pageSize: 50,
    search: '',
    domain: '',
    instance: '',
    exposedOnly: false,
    loading: false,

    get totalPages() {
      return Math.max(1, Math.ceil(this.total / this.pageSize));
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
    },

    async loadInstances() {
      const data = await fetchJSON('api/ha/instances');
      if (Array.isArray(data)) this.instances = data.map((i) => ({ id: i.id, name: i.name }));
    },

    async loadDomains() {
      const data = await fetchJSON('api/ha/domains');
      if (Array.isArray(data)) this.domains = data;
    },

    async reload() {
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

    prevPage() {
      if (this.page > 1) { this.page--; this.load(); }
    },
    nextPage() {
      if (this.page < this.totalPages) { this.page++; this.load(); }
    },
  };
}
