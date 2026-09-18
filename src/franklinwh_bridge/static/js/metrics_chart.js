/**
 * Live metrics chart — AC and DC/inverter, user picks the series.
 *
 * These points have no stored history: `metrics` keeps only the four power
 * channels plus SoC and temperatures, and `metric_samples` is empty. So this
 * buffers in the browser from the store's live points rather than pretending
 * it can backfill — the chart starts when you open it.
 *
 * The hard part is the axes. Volts (~246), amps (~2), hertz (~50), power
 * factor (0–1) and VA (~570) share no scale: on one axis everything except VA
 * is a flat line at the bottom. Each series therefore declares a unit, and
 * axes are created per *unit actually selected*, alternating left/right. The
 * alternative — normalising every series to 0–1 — makes the lines comparable
 * but the numbers meaningless, which is the wrong trade for diagnosing a
 * voltage sag.
 *
 * Chart.js instances are kept in a module-level map, NOT on the Alpine
 * component: Alpine deep-proxies reactive properties and Chart.js's internal
 * object graph sends that into infinite recursion. Same reason dashboard_tab.js
 * holds its chart in a closure.
 */
(function () {
  'use strict';

  const CATALOG = {
    ac: {
      title: 'AC Power — live',
      series: [
        { key: 'voltage_v',     label: 'Voltage',        unit: 'V',  colour: '#38bdf8', on: true },
        { key: 'current_a',     label: 'Current',        unit: 'A',  colour: '#fbbf24', on: true },
        { key: 'frequency_hz',  label: 'Frequency',      unit: 'Hz', colour: '#34d399', on: true },
        { key: 'power_factor',  label: 'Power factor',   unit: 'PF', colour: '#a78bfa' },
        { key: 'grid_va',       label: 'Apparent',       unit: 'VA', colour: '#f472b6' },
        { key: 'grid_var',      label: 'Reactive',       unit: 'var', colour: '#fb923c' },
        { key: 'grid_power_w',  label: 'Grid power',     unit: 'W',  colour: '#ef4444' },
        { key: 'voltage_l1_v',  label: 'Voltage L1',     unit: 'V',  colour: '#60a5fa' },
        { key: 'voltage_l2_v',  label: 'Voltage L2',     unit: 'V',  colour: '#818cf8' },
        { key: 'voltage_l3_v',  label: 'Voltage L3',     unit: 'V',  colour: '#c084fc' },
        { key: 'current_l1_a',  label: 'Current L1',     unit: 'A',  colour: '#fcd34d' },
        { key: 'current_l2_a',  label: 'Current L2',     unit: 'A',  colour: '#fdba74' },
        { key: 'current_l3_a',  label: 'Current L3',     unit: 'A',  colour: '#fca5a5' },
        { key: 'pf_l1',         label: 'PF L1',          unit: 'PF', colour: '#d8b4fe' },
      ],
    },
    dc: {
      title: 'DC / Inverter — live',
      series: [
        { key: 'dc_power_w',          label: 'DC power',      unit: 'W',  colour: '#22d3ee', on: true },
        { key: 'battery_dc_power_w',  label: 'Battery DC',    unit: 'W',  colour: '#34d399', on: true },
        { key: 'soc',                 label: 'SoC',           unit: '%',  colour: '#f59e0b', on: true },
        { key: 'battery_1_voltage_v', label: 'Battery volts', unit: 'V',  colour: '#60a5fa' },
        { key: 'battery_current_a',   label: 'Battery amps',  unit: 'A',  colour: '#fbbf24' },
        { key: 'soh',                 label: 'State of health', unit: '%', colour: '#a78bfa' },
        { key: 'cabinet_temp_c',      label: 'Cabinet temp',  unit: '°C', colour: '#f472b6' },
        { key: 'ambient_temp_c',      label: 'Ambient temp',  unit: '°C', colour: '#fb923c' },
      ],
    },
  };

  // kind -> Chart instance. Outside Alpine's reactive scope, deliberately.
  const _charts = {};
  // kind -> {byKey: {key: [{x: epoch_ms, y}]}} ring buffers.
  const _buf = {};
  // Safety cap only. The visible span is the WINDOW, and the buffer is
  // trimmed by age rather than by count — 6h at 1s would be 21,600 points,
  // far more than is useful or drawable, so the cap bounds memory while the
  // window bounds meaning.
  const MAX_POINTS = 2000;

  const WINDOWS = [
    { ms: 5 * 60000,   label: '5m' },
    { ms: 15 * 60000,  label: '15m' },
    { ms: 60 * 60000,  label: '1h' },
    { ms: 360 * 60000, label: '6h' },
  ];

  const INTERVALS = [
    { ms: 1000,  label: '1s' },
    { ms: 2000,  label: '2s' },
    { ms: 5000,  label: '5s' },
    { ms: 10000, label: '10s' },
    { ms: 30000, label: '30s' },
  ];
  const PREFS_KEY = 'fwh-metrics-chart';

  function loadPrefs() {
    try {
      return JSON.parse(localStorage.getItem(PREFS_KEY)) || {};
    } catch (_) {
      return {};
    }
  }

  function savePrefs(patch) {
    try {
      localStorage.setItem(PREFS_KEY, JSON.stringify({ ...loadPrefs(), ...patch }));
    } catch (_) { /* private mode — the chart still works, it just won't persist */ }
  }

  function bufFor(kind) {
    if (!_buf[kind]) _buf[kind] = { byKey: {} };
    return _buf[kind];
  }

  function clockLabel(ms) {
    return new Date(ms).toLocaleTimeString([], { hour12: false });
  }

  // Never zoom tighter than this, however little has been collected —
  // three points across a full-width axis is its own kind of misleading.
  const MIN_VISIBLE_MS = 60000;

  /** Human span for the header, so "600 samples" means something. */
  function spanLabel(ms) {
    if (!(ms > 0)) return '';
    const m = ms / 60000;
    if (m < 1) return `${Math.round(ms / 1000)}s`;
    if (m < 90) return `${Math.round(m)} min`;
    return `${(m / 60).toFixed(1)} h`;
  }

  /**
   * Stable axis id per unit, built from the unit's position in the selection.
   *
   * Sanitising the unit string does not work: '%' has no alphanumerics, so it
   * collapses to an empty suffix, and any second symbol-only unit would map to
   * the same id and be silently plotted on someone else's scale. Indexing
   * cannot collide.
   */
  function axisIds(units) {
    const map = {};
    units.forEach((u, i) => { map[u] = `y${i}`; });
    return map;
  }

  window.metricsChart = function metricsChart(kind) {
    const cfg = CATALOG[kind];
    return {
      open: false,
      kind,
      title: cfg.title,
      series: cfg.series.map((s) => ({ ...s, on: !!s.on })),
      sampleCount: 0,
      intervals: INTERVALS,
      windows: WINDOWS,
      intervalMs: loadPrefs().intervalMs || 2000,
      // 5 min by default: the axis is pinned, so a longer default means
      // opening onto a mostly-empty plot. Wider spans are one click away
      // and the choice persists.
      windowMs: loadPrefs().windowMs || 5 * 60000,
      expanded: !!loadPrefs().expanded,
      windowLabel: '',
      filling: true,
      _timer: null,

      get selected() {
        return this.series.filter((s) => s.on);
      },

      show() {
        this.open = true;
        // Build after the modal is in the DOM, or the canvas has no size and
        // Chart.js locks in a 0-height layout.
        this.$nextTick(() => {
          this._build();
          this._sample();
          this._restartTimer();
        });
      },

      _restartTimer() {
        if (this._timer) clearInterval(this._timer);
        this._timer = setInterval(() => this._sample(), this.intervalMs);
      },

      setInterval_(ms) {
        this.intervalMs = ms;
        savePrefs({ intervalMs: ms });
        this._restartTimer();
        // The buffer is NOT cleared: points carry their own timestamp and the
        // x axis is real time, so a stretch sampled at 2s and one at 30s are
        // drawn at their true spacing rather than being flattened together.
        this._refreshMeta();
      },

      setWindow(ms) {
        this.windowMs = ms;
        savePrefs({ windowMs: ms });
        // Redraw immediately rather than waiting for the span to fill. The
        // axis is pinned to [now - window, now], so picking 6h shows a 6h
        // axis at once with whatever has been collected sitting at its right
        // edge — the alternative (an axis that floats to the data) means the
        // choice appears to do nothing for hours.
        this._trim();
        this._applyWindow();
        const ch = _charts[this.kind];
        if (ch) ch.update('none');
        this._refreshMeta();
      },

      /** Milliseconds actually collected. */
      _heldMs() {
        const any = Object.values(bufFor(this.kind).byKey)[0] || [];
        return any.length > 1 ? any[any.length - 1].x - any[0].x : 0;
      },

      /**
       * Pin the x axis to the trailing window — but treat the span as a
       * CEILING, not a promise.
       *
       * Pinning the full span regardless meant picking 6h with 32s collected
       * drew the data as a 0.15%-wide sliver: technically a correct 6h axis,
       * visually an empty chart, and clicking the button looked like it did
       * nothing. So the axis shows what exists and grows into the chosen span
       * as the data arrives, then scrolls. Picking a SMALLER span still takes
       * effect immediately, which is the direction that has data to cut.
       */
      _applyWindow() {
        const ch = _charts[this.kind];
        if (!ch) return;
        const now = Date.now();
        const visible = Math.min(
          this.windowMs, Math.max(this._heldMs(), MIN_VISIBLE_MS),
        );
        ch.options.scales.x.min = now - visible;
        ch.options.scales.x.max = now;
      },

      /** Drop points older than the window (plus one, so the line still
       *  enters from the left edge instead of starting inside the plot). */
      _trim() {
        const cutoff = Date.now() - this.windowMs;
        const buf = bufFor(this.kind);
        for (const key of Object.keys(buf.byKey)) {
          const arr = buf.byKey[key];
          let drop = 0;
          while (drop + 1 < arr.length && arr[drop + 1].x < cutoff) drop++;
          if (drop) arr.splice(0, drop);
          if (arr.length > MAX_POINTS) arr.splice(0, arr.length - MAX_POINTS);
        }
      },

      toggleExpand() {
        this.expanded = !this.expanded;
        savePrefs({ expanded: this.expanded });
        // Let the modal resize first, then let Chart.js re-measure it.
        this.$nextTick(() => {
          const ch = _charts[this.kind];
          if (ch) ch.resize();
        });
      },

      hide() {
        this.open = false;
        if (this._timer) { clearInterval(this._timer); this._timer = null; }
        if (_charts[this.kind]) { _charts[this.kind].destroy(); delete _charts[this.kind]; }
      },

      toggle(key) {
        const s = this.series.find((x) => x.key === key);
        if (!s) return;
        s.on = !s.on;
        // Rebuild rather than patch: adding a series can introduce a whole new
        // unit axis, which Chart.js will not create on a dataset update.
        this._build();
      },

      clear() {
        _buf[this.kind] = { byKey: {} };
        this.sampleCount = 0;
        this.windowLabel = '';
        this._build();
      },

      /** Sample count, and how much of the chosen window is actually filled.
       *  Saying "15m" when 40s has been collected would misdescribe an axis
       *  that is mostly empty by design. */
      _refreshMeta() {
        const any = Object.values(bufFor(this.kind).byKey)[0] || [];
        this.sampleCount = any.length;
        const held = this._heldMs();
        this.filling = held < this.windowMs * 0.98;
        this.windowLabel = held > 0
          ? `${spanLabel(held)} of ${spanLabel(this.windowMs)}`
          : `0s of ${spanLabel(this.windowMs)}`;
      },

      _build() {
        const canvas = this.$refs.canvas;
        if (!canvas || !window.Chart) return;
        if (_charts[this.kind]) { _charts[this.kind].destroy(); delete _charts[this.kind]; }

        const buf = bufFor(this.kind);
        const chosen = this.series.filter((s) => s.on);

        // One axis per distinct unit in the current selection, alternating
        // sides so two units stay readable and more than two still resolve.
        const units = [...new Set(chosen.map((s) => s.unit))];
        const axisOf = axisIds(units);
        // Linear on epoch-ms rather than a category axis of time strings.
        // Categories are spaced evenly whatever the real gap, so a stretch
        // sampled at 2s and one at 30s would be drawn identically, and a gap
        // from the modal being closed would vanish. (Chart.js's `time` scale
        // would need the date-fns adapter — another bundle to vendor for no
        // gain over formatting the ticks here.)
        const scales = {
          x: {
            type: 'linear',
            // Pinned to the trailing window rather than floating to the data,
            // so the span is what the user chose from the moment they choose
            // it. Refreshed on every sample so it scrolls.
            min: Date.now() - this.windowMs,
            max: Date.now(),
            ticks: {
              maxTicksLimit: 7, color: '#94a3b8', font: { size: 10 },
              callback: (v) => clockLabel(v),
            },
            grid: { color: 'rgba(148,163,184,.12)' },
          },
        };
        units.forEach((u, i) => {
          scales[axisOf[u]] = {
            type: 'linear',
            position: i % 2 === 0 ? 'left' : 'right',
            title: { display: true, text: u, color: '#94a3b8', font: { size: 10 } },
            ticks: { color: '#94a3b8', font: { size: 10 }, maxTicksLimit: 6 },
            // Only the first axis draws gridlines, or several overlapping
            // grids at different intervals turn the plot into moiré.
            grid: { drawOnChartArea: i === 0, color: 'rgba(148,163,184,.12)' },
          };
        });

        _charts[this.kind] = new window.Chart(canvas.getContext('2d'), {
          type: 'line',
          data: {
            datasets: chosen.map((s) => ({
              label: `${s.label} (${s.unit})`,
              data: [...(buf.byKey[s.key] || [])],
              borderColor: s.colour,
              backgroundColor: s.colour,
              yAxisID: axisOf[s.unit],
              borderWidth: 1.6,
              pointRadius: 0,
              // Straight segments, not a spline. Chart.js's bezier smoothing
              // overshoots between points, so a curve can peak above any
              // sample actually taken — on a chart used to spot voltage and
              // frequency excursions, that invents the excursion.
              tension: 0,
              spanGaps: true,
            })),
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            interaction: { mode: 'index', intersect: false },
            plugins: {
              legend: { labels: { color: '#cbd5e1', boxWidth: 10, font: { size: 11 } } },
              tooltip: {
                enabled: true,
                callbacks: { title: (items) => clockLabel(items[0].parsed.x) },
              },
            },
            scales,
          },
        });
      },

      _sample() {
        const pts = (window.Alpine && Alpine.store('app') && Alpine.store('app').points) || {};
        const buf = bufFor(this.kind);
        const now = Date.now();

        for (const s of this.series) {
          if (!buf.byKey[s.key]) buf.byKey[s.key] = [];
          const arr = buf.byKey[s.key];
          const v = pts[s.key];
          // null, not 0 — a point the poller failed to read is a gap in the
          // line, and plotting it as zero invents a fault that isn't there.
          arr.push({ x: now, y: typeof v === 'number' ? v : null });
        }
        this._trim();
        this._refreshMeta();

        const ch = _charts[this.kind];
        if (!ch) return;
        this._applyWindow();
        const chosen = this.series.filter((x) => x.on);
        ch.data.datasets.forEach((ds, i) => {
          const s = chosen[i];
          if (s) ds.data = [...(buf.byKey[s.key] || [])];
        });
        ch.update('none');
      },
    };
  };

  window.FWHMetricsCatalog = CATALOG;
})();
