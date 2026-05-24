/**
 * SunSpec Explorer Tab — model tree with live point values
 */
function explorerTab() {
  return {
    models: [],
    filteredModels: [],
    expanded: {},
    filter: '',
    loading: true,

    async init() {
      await this.loadModels();
      // Refresh values periodically
      setInterval(() => {
        if (Alpine.store('app').activeTab === 'explorer') {
          this.$forceUpdate && this.$forceUpdate();
        }
      }, 10000);
    },

    async loadModels() {
      this.loading = true;
      const data = await fetchJSON('api/models');
      if (data && data.models) {
        this.models = data.models;
        this.filteredModels = data.models;
      }
      this.loading = false;
    },

    toggleModel(modelId) {
      this.expanded[modelId] = !this.expanded[modelId];
    },

    applyFilter() {
      const q = this.filter.toLowerCase().trim();
      if (!q) {
        this.filteredModels = this.models;
        return;
      }
      this.filteredModels = this.models.filter(m => {
        const modelMatch = ('m' + m.model_id).includes(q) ||
                          (m.label || '').toLowerCase().includes(q);
        const pointMatch = m.points.some(p =>
          p.name.toLowerCase().includes(q) ||
          (p.unit || '').toLowerCase().includes(q)
        );
        return modelMatch || pointMatch;
      });
      // Auto-expand matching models
      this.filteredModels.forEach(m => {
        this.expanded[m.model_id] = true;
      });
    },

    matchesFilter(model, pt) {
      if (!this.filter) return true;
      const q = this.filter.toLowerCase().trim();
      return pt.name.toLowerCase().includes(q) ||
             (pt.unit || '').toLowerCase().includes(q) ||
             ('m' + model.model_id).includes(q) ||
             (model.label || '').toLowerCase().includes(q);
    },

    getPointValue(pointName) {
      const pts = Alpine.store('app').points;
      // Try exact match first
      if (pts[pointName] !== undefined) return pts[pointName];
      // Try snake_case conversion
      const snake = pointName.replace(/([A-Z])/g, '_$1').toLowerCase().replace(/^_/, '');
      if (pts[snake] !== undefined) return pts[snake];
      return null;
    },
  };
}
