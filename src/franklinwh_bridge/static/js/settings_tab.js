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
    groups: [],
    groupsBusy: false,

    async init() {
      await this.loadAll();
      await this.loadMqttConfig();
      await this.loadMetricsSettings();
      await this.loadStorage();
      await this.loadBackups();
      await this.loadGroups();
      await this.loadGateway();
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
