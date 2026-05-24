/**
 * Settings & Logs Tab — MQTT status, entities, log viewer, poller status
 */
function settingsTab() {
  return {
    mqtt: {},
    poller: {},
    entities: [],
    logs: [],
    entityFilter: '',

    async init() {
      await this.loadAll();
      // Auto-refresh when tab is active
      setInterval(() => {
        if (Alpine.store('app').activeTab === 'settings') {
          this.loadAll();
        }
      }, 15000);
    },

    async loadAll() {
      const [mqttStatus, health, topics, logs] = await Promise.all([
        fetchJSON('api/mqtt/status'),
        fetchJSON('api/health'),
        fetchJSON('api/mqtt/topics'),
        fetchJSON('api/logs?limit=200'),
      ]);

      if (mqttStatus && !mqttStatus.error) this.mqtt = mqttStatus;
      if (health && health.components && health.components.poller) {
        this.poller = health.components.poller;
      }
      if (topics && topics.topics) this.entities = topics.topics;
      if (logs && logs.logs) this.logs = logs.logs.reverse(); // newest first
    },

    async loadLogs() {
      const data = await fetchJSON('api/logs?limit=200');
      if (data && data.logs) {
        this.logs = data.logs.reverse();
      }
    },

    get filteredEntities() {
      if (!this.entityFilter) return this.entities;
      const q = this.entityFilter.toLowerCase();
      return this.entities.filter(e =>
        e.slug.toLowerCase().includes(q) ||
        e.name.toLowerCase().includes(q) ||
        e.ha_type.toLowerCase().includes(q)
      );
    },

    getEntityValue(slug) {
      const pts = Alpine.store('app').points;
      return pts[slug] !== undefined ? String(pts[slug]) : '--';
    },

    async republishDiscovery() {
      const data = await fetchJSON('api/mqtt/publish', { method: 'POST' });
      if (data && !data.error) {
        Alpine.store('app').toast('Discovery republish queued', 'info');
      } else {
        Alpine.store('app').toast('Republish failed: ' + (data?.error || 'unknown'), 'error');
      }
      setTimeout(() => this.loadAll(), 1000);
    },

    async unpublishDiscovery() {
      const data = await fetchJSON('api/mqtt/unpublish', { method: 'POST' });
      if (data && !data.error) {
        Alpine.store('app').toast(`Discovery unpublished (${data.topics_cleared} topics)`, 'info');
      } else {
        Alpine.store('app').toast('Unpublish failed: ' + (data?.error || 'unknown'), 'error');
      }
      setTimeout(() => this.loadAll(), 1000);
    },
  };
}
