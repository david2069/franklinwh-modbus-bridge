/**
 * SunSpec Explorer Tab — two-panel model tree + point detail
 */
function explorerTab() {
  return {
    models: [],
    selectedModelId: null,
    filter: '',
    loading: true,
    refreshing: false,
    addrMode: 'sunspec', // 'sunspec' or 'base0'
    gateway: { host: '--', port: 502, unit_id: 1 },

    async init() {
      await Promise.all([this.loadGateway(), this.loadModels()]);
    },

    async loadGateway() {
      const data = await fetchJSON('api/gateway');
      if (data && !data.error) {
        this.gateway = data;
      }
    },

    async loadModels() {
      this.loading = true;
      const data = await fetchJSON('api/models');
      if (data && data.models) {
        this.models = data.models;
        // Auto-select first model if none selected
        if (!this.selectedModelId && this.models.length > 0) {
          this.selectedModelId = this.models[0].model_id;
        }
      }
      this.loading = false;
    },

    async refreshCatalog() {
      this.refreshing = true;
      const data = await fetchJSON('api/models/refresh', { method: 'POST' });
      if (data && !data.error) {
        const msg = data.changes
          ? `Catalog updated: +${data.added_models?.length || 0} models`
          : 'Catalog unchanged';
        Alpine.store('app').toast(msg, 'info');
        await this.loadModels();
      } else {
        Alpine.store('app').toast(
          'Refresh failed: ' + (data?.error || 'unknown'),
          'error'
        );
      }
      this.refreshing = false;
    },

    selectModel(modelId) {
      this.selectedModelId = modelId;
    },

    // ── Computed properties ────────────────────────────────

    get modelCount() {
      return this.models.length;
    },

    get pointCount() {
      return this.models.reduce((n, m) => n + m.points.length, 0);
    },

    get filteredModels() {
      const q = this.filter.toLowerCase().trim();
      if (!q) return this.models;

      return this.models.filter(m => {
        const modelMatch =
          ('m' + m.model_id).includes(q) ||
          String(m.model_id).includes(q) ||
          (m.label || '').toLowerCase().includes(q);
        const pointMatch = m.points.some(
          p =>
            p.name.toLowerCase().includes(q) ||
            (p.label || '').toLowerCase().includes(q) ||
            (p.unit || '').toLowerCase().includes(q)
        );
        return modelMatch || pointMatch;
      });
    },

    get selectedModel() {
      if (!this.selectedModelId) return null;
      return this.models.find(m => m.model_id === this.selectedModelId) || null;
    },

    get selectedModelPoints() {
      if (!this.selectedModel) return [];
      const q = this.filter.toLowerCase().trim();
      if (!q) return this.selectedModel.points;

      // If filter is active and matches model-level, show all points
      const m = this.selectedModel;
      const modelMatch =
        ('m' + m.model_id).includes(q) || String(m.model_id).includes(q) ||
        (m.label || '').toLowerCase().includes(q);
      if (modelMatch) return m.points;

      // Otherwise filter points
      return m.points.filter(
        p =>
          p.name.toLowerCase().includes(q) ||
          (p.label || '').toLowerCase().includes(q) ||
          (p.unit || '').toLowerCase().includes(q)
      );
    },

    // ── Value helpers ──────────────────────────────────────

    getPointValue(pointName) {
      const pts = Alpine.store('app').points;
      if (pts[pointName] !== undefined) return pts[pointName];
      // Try snake_case conversion (SunSpec CamelCase → bridge snake_case)
      const snake = pointName
        .replace(/([A-Z])/g, '_$1')
        .toLowerCase()
        .replace(/^_/, '');
      if (pts[snake] !== undefined) return pts[snake];
      return null;
    },

    formatPointValue(pt) {
      const val = this.getPointValue(pt.name);
      if (val == null) return '--';

      // Resolve enum symbol if available
      if (pt.symbols && pt.symbols[String(val)]) {
        return val + ' (' + pt.symbols[String(val)] + ')';
      }
      // Format numbers reasonably
      if (typeof val === 'number') {
        return Number.isInteger(val) ? String(val) : val.toFixed(2);
      }
      return String(val);
    },

    formatAddress(addr) {
      if (addr == null) return '';
      // SunSpec addresses are 1-based (40001+), Base-0 are the raw register number
      if (this.addrMode === 'base0') return String(addr);
      return String(40001 + addr);
    },
  };
}
