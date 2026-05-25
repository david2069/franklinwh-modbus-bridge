/**
 * Logs Tab — filterable log viewer with pagination and level summaries
 */
function logsTab() {
  return {
    logs: [],
    loading: false,
    autoRefresh: true,
    _refreshInterval: null,

    // Filters
    searchQuery: '',
    levelFilter: '',
    sourceFilter: '',

    // Pagination
    page: 0,
    pageSize: 100,

    // Summary counts
    summary: { DEBUG: 0, INFO: 0, WARNING: 0, ERROR: 0, CRITICAL: 0 },

    // Level config
    levels: [
      { name: 'DEBUG',    color: 'text-slate-400' },
      { name: 'INFO',     color: 'text-emerald-400' },
      { name: 'WARNING',  color: 'text-amber-400' },
      { name: 'ERROR',    color: 'text-red-400' },
      { name: 'CRITICAL', color: 'text-red-500' },
    ],

    async init() {
      await this.loadLogs();
      this.startAutoRefresh();
    },

    destroy() {
      this.stopAutoRefresh();
    },

    startAutoRefresh() {
      this.stopAutoRefresh();
      this._refreshInterval = setInterval(() => {
        if (this.autoRefresh && Alpine.store('app').activeTab === 'logs') {
          this.loadLogs();
        }
      }, 5000);
    },

    stopAutoRefresh() {
      if (this._refreshInterval) {
        clearInterval(this._refreshInterval);
        this._refreshInterval = null;
      }
    },

    async loadLogs() {
      this.loading = this.logs.length === 0; // only show spinner on first load
      const data = await fetchJSON('api/logs?limit=2000');
      if (data && data.logs) {
        this.logs = data.logs;
        this.computeSummary();
      }
      this.loading = false;
    },

    computeSummary() {
      const s = { DEBUG: 0, INFO: 0, WARNING: 0, ERROR: 0, CRITICAL: 0 };
      for (const entry of this.logs) {
        const lvl = entry.level || 'INFO';
        if (lvl in s) s[lvl]++;
      }
      this.summary = s;
    },

    toggleLevelFilter(level) {
      this.levelFilter = this.levelFilter === level ? '' : level;
      this.page = 0;
    },

    // ── Computed ────────────────────────────────────────────

    get filteredLogs() {
      let result = this.logs;

      if (this.levelFilter) {
        result = result.filter(e => e.level === this.levelFilter);
      }

      if (this.searchQuery) {
        const q = this.searchQuery.toLowerCase();
        result = result.filter(e =>
          (e.message || '').toLowerCase().includes(q)
        );
      }

      if (this.sourceFilter) {
        const sf = this.sourceFilter.toLowerCase();
        result = result.filter(e =>
          (e.name || '').toLowerCase().includes(sf)
        );
      }

      // Most recent first
      return result.slice().reverse();
    },

    get totalPages() {
      return Math.max(1, Math.ceil(this.filteredLogs.length / this.pageSize));
    },

    get paginatedLogs() {
      const start = this.page * this.pageSize;
      return this.filteredLogs.slice(start, start + this.pageSize);
    },

    prevPage() {
      if (this.page > 0) this.page--;
    },

    nextPage() {
      if (this.page < this.totalPages - 1) this.page++;
    },

    // ── Formatters ─────────────────────────────────────────

    formatTs(ts) {
      if (!ts) return '--';
      const d = new Date(ts * 1000);
      const h = String(d.getHours()).padStart(2, '0');
      const m = String(d.getMinutes()).padStart(2, '0');
      const s = String(d.getSeconds()).padStart(2, '0');
      return `${h}:${m}:${s}`;
    },

    levelChipClass(level) {
      switch (level) {
        case 'DEBUG':    return 'muted';
        case 'INFO':     return 'ok';
        case 'WARNING':  return 'warn';
        case 'ERROR':
        case 'CRITICAL': return 'error';
        default:         return '';
      }
    },
  };
}
