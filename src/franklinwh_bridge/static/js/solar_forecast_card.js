/**
 * Dashboard Solar Forecast card (#88) — Open-Meteo weather + PV forecast.
 *
 * Today/Tomorrow totals + peak, an hourly kW bar chart with a NOW marker, the
 * current weather, and actual PV today against the forecast. Data from
 * GET api/solar/forecast (cached server-side 15 min). Modelled on the Local
 * Bridge's card; times render in the bridge's zone, labelled (time_fmt.js),
 * from each hour's epoch `ts` rather than Open-Meteo's bare local strings.
 *
 * Two optional extras (#116), both refreshed every 2 min on the dashboard:
 *   - where today's solar went (→home/→batt/→grid): GET api/energy/flow, the
 *     Energy Flow card's reconstructed arcs — an estimate, labelled as such;
 *   - the battery band under the bars: GET api/energy/battery-hours, today's
 *     sampled battery power integrated per hour and split by sign.
 * Either failing leaves the forecast itself untouched.
 */
function solarForecastCard() {
  return {
    data: null,
    loading: false,
    error: '',
    nowMs: Date.now(),
    solarSplit: null,     // today's {home, battery, grid} kWh, reconstructed
    batteryHours: [],     // today's [{start, end, charge_kwh, discharge_kwh, state}]

    init() {
      this.load();
      this.loadExtras();
      // Re-read every 15 min (the server cache period); move the NOW marker
      // every minute without refetching.
      setInterval(() => {
        if (this.$store.app.activeTab === 'dashboard') this.load();
      }, 15 * 60 * 1000);
      setInterval(() => {
        if (this.$store.app.activeTab === 'dashboard') this.loadExtras();
      }, 2 * 60 * 1000);
      setInterval(() => { this.nowMs = Date.now(); }, 60 * 1000);
      this.$watch('$store.app.activeGateway', () => { this.load(); this.loadExtras(); });
    },

    /** The solar split and the battery band. Both optional: a failure blanks
     *  that row, never the forecast. */
    async loadExtras() {
      const gw = encodeURIComponent(this._gateway());
      const [flow, hours] = await Promise.allSettled([
        fetchJSON('api/energy/flow?gateway=' + gw, { background: true }),
        fetchJSON('api/energy/battery-hours?gateway=' + gw, { background: true }),
      ]);
      if (flow.status === 'fulfilled' && flow.value && flow.value.flows) {
        const f = flow.value.flows;
        this.solarSplit = {
          home: f.solar_to_home || 0, battery: f.solar_to_battery || 0, grid: f.solar_to_grid || 0,
        };
      } else {
        this.solarSplit = null;
      }
      this.batteryHours = (hours.status === 'fulfilled' && hours.value && hours.value.hours) || [];
    },

    _gateway() {
      return this.$store.app.activeGateway;
    },

    async load(force) {
      this.loading = true;
      this.error = '';
      try {
        const q = new URLSearchParams({ gateway: this._gateway() });
        if (force) q.set('force', 'true');
        this.data = await fetchJSON('api/solar/forecast?' + q.toString(), { background: true });
        this.nowMs = Date.now();
      } catch (e) {
        this.error = e.message;
      } finally {
        this.loading = false;
      }
    },

    get configured() { return !!(this.data && this.data.configured); },
    get current() { return (this.data && this.data.current) || {}; },
    get sensors() { return (this.data && this.data.sensors) || {}; },

    /** Site-local calendar date "YYYY-MM-DD" (Open-Meteo's day key). */
    _siteDate(ms, addDays = 0) {
      const off = ((this.data && this.data.utc_offset_seconds) || 0) * 1000;
      return new Date(ms + off + addDays * 86400000).toISOString().slice(0, 10);
    },
    _day(date) {
      return ((this.data && this.data.days) || []).find((d) => d.date === date) || null;
    },
    get today() { return this._day(this._siteDate(this.nowMs)); },
    get tomorrow() { return this._day(this._siteDate(this.nowMs, 1)); },

    /** "THU, 15 OCT" from the forecast's day key (a calendar date, so no
     *  zone arithmetic: it is already the site's day). */
    dayHeader(d, fallback) {
      if (!d || !d.date) return fallback;
      const [y, m, day] = d.date.split('-').map(Number);
      const t = new Date(Date.UTC(y, m - 1, day));
      const WD = ['SUN', 'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT'];
      const MO = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'];
      return `${WD[t.getUTCDay()]}, ${day} ${MO[m - 1]}`;
    },
    peakAt(d) {
      return (d && d.peak_ts != null) ? bridgeTime.time(d.peak_ts * 1000) : '–';
    },

    /** Hourly bars: height as % of the peak, tagged today / now. */
    get bars() {
      const h = (this.data && this.data.hourly) || [];
      if (!h.length) return [];
      const max = Math.max(0.05, ...h.map((x) => x.kw || 0));
      const todayDate = this._siteDate(this.nowMs);
      const now = this.nowMs;
      return h.map((x) => {
        const ms = x.ts != null ? x.ts * 1000 : null;
        const hr = ms != null ? bridgeTime.parts(ms).h : -1;
        return {
          ms,
          kw: x.kw,
          hm: ms != null ? bridgeTime.time(ms) : (x.time || '').slice(11, 16),
          tick: ({ 0: '00', 6: '06', 12: '12', 18: '18' })[hr] || '',
          h: Math.max(1, ((x.kw || 0) / max) * 100),
          isToday: (x.time || '').slice(0, 10) === todayDate,
          isNow: ms != null && ms <= now && now < ms + 3600000,
          past: ms != null && ms + 3600000 <= now,
        };
      });
    },

    /** One band cell per bar: the battery hour that contains the bar's start.
     *  Hours with no samples (tomorrow, the rest of today, an outage) stay
     *  blank rather than reading as "idle". */
    get band() {
      const hrs = this.batteryHours || [];
      return this.bars.map((bar) => {
        const h = bar.ms == null ? null
          : hrs.find((x) => x.start * 1000 <= bar.ms && bar.ms < x.end * 1000);
        if (!h || !h.state) return { state: null, title: '' };
        return {
          state: h.state,
          title: `${bar.hm} battery ${h.state === 'charge' ? 'charging' : h.state === 'discharge' ? 'discharging' : 'idle'}`
            + ` · in ${this.num(h.charge_kwh, 2)} / out ${this.num(h.discharge_kwh, 2)} kWh (est.)`,
        };
      });
    },
    get bandShown() { return this.band.some((b) => b.state); },

    get soc() {
      const v = this.$store.app.batterySoc;
      return (v == null || isNaN(v)) ? null : Number(v);
    },

    get actualKwh() {
      const a = this.data && this.data.actual_today_kwh;
      return a == null ? null : Number(a);
    },
    get dayPct() { return this.sensors['solar_forecast.day_pct']; },
    get vsExpected() {
      const pct = this.sensors['solar_forecast.vs_expected_pct'];
      if (pct == null) return null;
      const diff = Math.round(pct - 100);
      return {
        ahead: diff >= 0,
        label: Math.abs(diff) < 8 ? 'on track'
          : (diff > 0 ? `${diff}% ahead` : `${Math.abs(diff)}% behind`),
      };
    },

    get generatedAt() {
      const g = this.data && this.data.generated_at;
      return g ? bridgeTime.time(g * 1000) : '';
    },
    num(v, dp = 1) { return (v == null || isNaN(v)) ? '–' : Number(v).toFixed(dp); },
  };
}
