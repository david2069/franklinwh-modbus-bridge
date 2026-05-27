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
    republishing: false,
    unpublishing: false,
    showTopics: false,
    storage: null,
    archiving: false,
    backups: [],
    backupBusy: false,
    exportRange: '24h',

    async init() {
      await this.loadAll();
      await this.loadMetricsSettings();
      await this.loadStorage();
      await this.loadBackups();
      setInterval(() => {
        if (Alpine.store('app').activeTab === 'settings') {
          this.loadAll();
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

    async loadMetricsSettings() {
      const data = await fetchJSON('api/settings/metrics');
      if (data && !data.error) {
        this.metricsRetention = data.retention_days;
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

    getEntityValue(slug) {
      const pts = Alpine.store('app').points;
      return pts[slug] !== undefined ? String(pts[slug]) : '--';
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
