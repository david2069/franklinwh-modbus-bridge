/**
 * Settings Tab — MQTT status, entities, poller status, metrics config
 */
function settingsTab() {
  return {
    mqtt: {},
    poller: {},
    entities: [],
    entityFilter: '',
    metricsRetention: 30,

    async init() {
      await this.loadAll();
      await this.loadMetricsSettings();
      setInterval(() => {
        if (Alpine.store('app').activeTab === 'settings') {
          this.loadAll();
        }
      }, 15000);
    },

    async loadAll() {
      const [mqttStatus, health, topics] = await Promise.all([
        fetchJSON('api/mqtt/status'),
        fetchJSON('api/health'),
        fetchJSON('api/mqtt/topics'),
      ]);

      if (mqttStatus && !mqttStatus.error) this.mqtt = mqttStatus;
      if (health && health.components && health.components.poller) {
        this.poller = health.components.poller;
      }
      if (topics && topics.topics) this.entities = topics.topics;
    },

    async loadMetricsSettings() {
      const data = await fetchJSON('api/settings/metrics');
      if (data && !data.error) {
        this.metricsRetention = data.retention_days;
      }
    },

    async saveMetricsRetention() {
      const data = await fetchJSON('api/settings/metrics', {
        method: 'PUT',
        body: JSON.stringify({ retention_days: this.metricsRetention }),
      });
      if (data && !data.error) {
        Alpine.store('app').toast(`Retention set to ${this.metricsRetention} days`, 'info');
      } else {
        Alpine.store('app').toast('Save failed: ' + (data?.error || 'unknown'), 'error');
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
