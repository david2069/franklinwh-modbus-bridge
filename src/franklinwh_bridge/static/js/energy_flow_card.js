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

    /** Local calendar date. toISOString() would convert to UTC first, which
     *  east of Greenwich hands back yesterday for most of the working day. */
    _localToday() {
      const d = new Date();
      const p = (n) => String(n).padStart(2, '0');
      return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
    },

    shiftDay(days) {
      const [y, m, d] = this.day.split('-').map(Number);
      const dt = new Date(y, m - 1, d + days);
      const p = (n) => String(n).padStart(2, '0');
      const next = `${dt.getFullYear()}-${p(dt.getMonth() + 1)}-${p(dt.getDate())}`;
      if (next > this.today) return;
      this.day = next;
      this.load();
    },

    goToday() {
      this.today = this._localToday();
      this.day = this.today;
      this.load();
    },

    /** Local-midnight epoch seconds for the selected period.
     *  Local, not UTC — the aGate's own daily totals reset at local midnight,
     *  so a UTC span disagrees with every other figure by the offset. */
    _span() {
      const [y, m, d] = this.day.split('-').map(Number);
      const start = new Date(y, m - 1, d);
      const end = new Date(y, m - 1, d);
      if (this.period === 'week') {
        // Monday-start, matching energy_totals.period_start().
        start.setDate(start.getDate() - ((start.getDay() + 6) % 7));
        end.setTime(start.getTime());
        end.setDate(end.getDate() + 7);
      } else if (this.period === 'month') {
        start.setDate(1);
        end.setTime(start.getTime());
        end.setMonth(end.getMonth() + 1);
      } else if (this.period === 'year') {
        start.setMonth(0, 1);
        end.setTime(start.getTime());
        end.setFullYear(end.getFullYear() + 1);
      } else {
        end.setDate(end.getDate() + 1);
      }
      return [Math.floor(start.getTime() / 1000), Math.floor(end.getTime() / 1000)];
    },

    setPeriod(p) {
      this.period = p;
      this.load();
    },

    /** What the span actually covers — a week anchored on Thursday still
     *  starts on Monday, and showing only the anchor date would mislead. */
    _describeSpan(s, e) {
      if (this.period === 'day') return '';
      const f = (secs) => new Date(secs * 1000).toLocaleDateString(undefined,
        { day: '2-digit', month: 'short' });
      if (this.period === 'year') return String(new Date(s * 1000).getFullYear());
      if (this.period === 'month') {
        return new Date(s * 1000).toLocaleDateString(undefined,
          { month: 'long', year: 'numeric' });
      }
      return `${f(s)} – ${f(e - 1)}`;
    },

    async load() {
      // Relative to <base href>, like every other tab fetch. 'site' is the
      // aggregate pseudo-gateway and has no per-gateway series, so fall back
      // to the default rather than send it as an id the API can't resolve.
      let url;
      if (this.period === 'day') {
        url = `api/energy/flow?day=${encodeURIComponent(this.day)}`;
        this.spanLabel = '';
      } else {
        const [s, e] = this._span();
        url = `api/energy/flow?start=${s}&end=${e}`;
        this.spanLabel = this._describeSpan(s, e);
      }
      const activeGw = Alpine.store('app')?.activeGateway;
      if (activeGw && activeGw !== 'site') {
        url += `&gateway=${encodeURIComponent(activeGw)}`;
      }
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
