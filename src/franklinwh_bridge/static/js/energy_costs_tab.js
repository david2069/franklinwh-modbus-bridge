/**
 * Energy Costs / Tariff Tab (Phase B) — read view over the Phase A tariff engine.
 *
 * Three sections: Setup (resolved config + per-service flags), This period
 * (live tariff / demand / bonus sensor values with end-of-period projection),
 * and Linked automations (schedules referencing tariff/demand sensors).
 */
function energyCostsTab() {
  return {
    overview: null,
    loading: false,
    error: '',
    _timer: null,

    async init() {
      // Only fetch when this tab is actually shown — the module is off by
      // default, so a page-load fetch would needlessly 403. The sidebar hides
      // the tab unless enabled, so activeTab can only be us when reachable.
      if (Alpine.store('app').activeTab === 'energy_costs') this.load();
      // Refresh live values while this tab is open; stop when navigating away.
      window.addEventListener('tab:changed', (ev) => {
        if (!ev.detail) return;
        if (ev.detail.tab === 'energy_costs') {
          this.load();
          this._timer = this._timer || setInterval(() => this.load(), 15000);
        } else if (this._timer) {
          clearInterval(this._timer);
          this._timer = null;
        }
      });
    },

    async load() {
      this.loading = true;
      const data = await fetchJSON('api/tariff/overview');
      this.loading = false;
      if (data && !data.error) {
        this.overview = data;
        this.error = '';
      } else {
        this.error = data?.error || 'Failed to load tariff overview';
      }
    },

    // ── live sensor accessors ──────────────────────────────
    /** Value of a live sensor id, or null. */
    val(id) {
      const s = (this.overview?.live || []).find((x) => x.id === id);
      return s ? s.value : null;
    },
    /** Formatted number with a fixed precision, or an em-dash for null. */
    num(id, digits = 2) {
      const v = this.val(id);
      return v == null || Number.isNaN(v) ? '—' : Number(v).toFixed(digits);
    },
    money(id) {
      const v = this.val(id);
      return v == null ? '—' : '$' + Number(v).toFixed(2);
    },
    bool(id) {
      return this.val(id) === true;
    },

    // ── billing-period projection ──────────────────────────
    get periodPct() {
      const p = this.overview?.period;
      if (!p || !p.days_total || p.days_elapsed == null) return 0;
      return Math.max(0, Math.min(100, (p.days_elapsed / p.days_total) * 100));
    },
    get periodLabel() {
      const p = this.overview?.period;
      if (!p || p.days_elapsed == null || !p.days_total) return 'No billing period configured';
      return `Day ${p.days_elapsed.toFixed(1)} of ${p.days_total.toFixed(0)}`;
    },
    /** Linear end-of-period projection of a live $ sensor, or null. */
    project(id) {
      const p = this.overview?.period;
      const v = this.val(id);
      if (v == null || !p || !p.days_total || !p.days_elapsed) return null;
      return (v / p.days_elapsed) * p.days_total;
    },
    projectMoney(id) {
      const v = this.project(id);
      return v == null ? '—' : '$' + v.toFixed(2);
    },

    // ── config / window formatting ─────────────────────────
    get demandCfg() { return this.overview?.config?.demand || null; },
    get bonusCfg() { return this.overview?.config?.bonus || null; },
    get chargeCfg() { return this.overview?.config?.charge || null; },

    /** "10:00–15:00 · Jan,Feb · Mon–Fri" from a {months,days,start,end} window. */
    fmtWindow(w) {
      if (!w) return '—';
      const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
      const DAY = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
      const parts = [`${w.start || '00:00'}–${w.end || '23:59'}`];
      if (Array.isArray(w.months) && w.months.length && w.months.length < 12) {
        parts.push(w.months.map((m) => MON[m - 1] || m).join(','));
      }
      if (Array.isArray(w.days) && w.days.length && w.days.length < 7) {
        parts.push(w.days.map((d) => DAY[d] || d).join(','));
      } else {
        parts.push('every day');
      }
      return parts.join(' · ');
    },
    cents(rate) {
      if (rate == null) return '—';
      return (Number(rate) * 100).toFixed(2) + '¢/kWh';
    },

    get anyConfig() {
      return !!(this.demandCfg || this.bonusCfg || this.chargeCfg);
    },
    get linked() { return this.overview?.linked || []; },

    // ── navigation ─────────────────────────────────────────
    goSettings() { Alpine.store('app').setTab('settings'); },
    goSchedule() { Alpine.store('app').setTab('schedule'); },
  };
}
