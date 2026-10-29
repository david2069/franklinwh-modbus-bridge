/**
 * Energy Flow card (admin dashboard) — the Sankey plus its day selector.
 *
 * The drawing itself lives in sankey.js, shared verbatim with the standalone
 * /user page; this is only the Alpine wrapper that fetches a day and hands the
 * arcs over. Deliberately thin: two dashboards rendering the same diagram from
 * two different implementations is how they drift.
 */
function energyFlowCard() {
  return {
    day: '',
    today: '',
    period: 'day',   // day | week | month | year
    quality: '',
    spanLabel: '',
    warn: false,
    _ro: null,

    init() {
      this.today = this._localToday();
      this.day = this.today;
      this.load();

      // Redraw on resize: the layout is computed from clientWidth, so a
      // collapsed sidebar or a rotated tablet would otherwise leave the
      // diagram at its old width until the next fetch.
      if (window.ResizeObserver) {
        let last = 0;
        this._ro = new ResizeObserver((entries) => {
          const w = Math.round(entries[0].contentRect.width);
          if (w && Math.abs(w - last) > 12) { last = w; this._draw(); }
        });
        this._ro.observe(this.$refs.sankey);
      }

      // Only the live day moves; a past day is settled. Two minutes, not the
      // 10s power tick — this is a whole-day integral.
      setInterval(() => {
        if (this.day === this._localToday()) this.load();
      }, 120000);

      // Follow the gateway selector, or the card would keep showing the
      // previous gateway's day after a switch.
      this.$watch('$store.app.activeGateway', () => this.load());
    },

    /** The BRIDGE's calendar date (#79): the aGate's daily totals reset at its
     *  midnight, not the viewing device's. (Not toISOString(): that's UTC.) */
    _localToday() {
      return bridgeTime.ymd(Date.now());
    },

    shiftDay(days) {
      const next = bridgeTime.shiftYmd(this.day, days);
      if (next > this.today) return;
      this.day = next;
      this.load();
    },

    goToday() {
      this.today = this._localToday();
      this.day = this.today;
      this.load();
    },

    /** Epoch seconds for the selected period, at the bridge's midnights —
     *  the aGate's own daily totals reset there, so a span in any other zone
     *  disagrees with every other figure by the offset (#79). */
    _span() {
      return bridgeTime.periodSpan(this.day, this.period);
    },

    setPeriod(p) {
      this.period = p;
      this.load();
    },

    /** What the span actually covers — a week anchored on Thursday still
     *  starts on Monday, and showing only the anchor date would mislead. */
    _describeSpan(s, e) {
      if (this.period === 'day') return '';
      const f = (secs) => bridgeTime.dayMon(secs * 1000);
      if (this.period === 'year') return String(bridgeTime.parts(s * 1000).y);
      if (this.period === 'month') return bridgeTime.monthYear(s * 1000);
      return `${f(s)} – ${f(e - 1)}`;
    },

    async load() {
      // Relative to <base href>, like every other tab fetch.
      let url;
      if (this.period === 'day') {
        url = `api/energy/flow?day=${encodeURIComponent(this.day)}`;
        this.spanLabel = '';
      } else {
        const [s, e] = this._span();
        url = `api/energy/flow?start=${s}&end=${e}`;
        this.spanLabel = this._describeSpan(s, e);
      }
      url += `&gateway=${encodeURIComponent(Alpine.store('app').activeGateway)}`;
      try {
        const r = await fetch(url, { headers: { Accept: 'application/json' } });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const data = await r.json();
        this._flows = data.flows;
        this._draw();
        this._setQuality(data.quality);
      } catch (e) {
        this._flows = null;
        if (this.$refs.sankey) {
          this.$refs.sankey.innerHTML =
            '<p class="sankey-empty">Energy flow unavailable.</p>';
        }
        this.quality = '';
        this.warn = false;
      }
    },

    _draw() {
      if (!this._flows || !this.$refs.sankey || !window.FWHSankey) return;
      window.FWHSankey.render(this.$refs.sankey, this._flows, { height: 260 });
    },

    _setQuality(q) {
      if (!q || !q.samples) {
        this.warn = false;
        this.quality = q ? 'No samples recorded for this day.' : '';
        return;
      }
      const pct = Math.round((q.coverage || 0) * 100);
      // Under ~95% the bridge wasn't polling for part of the day, so the
      // totals under-report. Saying so beats showing a short day as a quiet one.
      this.warn = pct < 95;
      const noun = { day: 'day', week: 'week', month: 'month', year: 'year' }[this.period];
      this.quality = this.warn
        ? `Partial data — the bridge recorded ${pct}% of this ${noun}.`
        : 'Derived from power samples; lifetime totals are metered.';
    },
  };
}
