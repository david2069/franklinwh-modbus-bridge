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
  // kind -> {labels: [], byKey: {key: []}} ring buffers.
  const _buf = {};
  const MAX_POINTS = 240;   // 2s cadence → ~8 minutes of history

  function bufFor(kind) {
    if (!_buf[kind]) _buf[kind] = { labels: [], byKey: {} };
    return _buf[kind];
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
          this._timer = setInterval(() => this._sample(), 2000);
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
        _buf[this.kind] = { labels: [], byKey: {} };
        this.sampleCount = 0;
        this._build();
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
        const scales = {
          x: {
            ticks: { maxTicksLimit: 6, color: '#94a3b8', font: { size: 10 } },
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
            labels: [...buf.labels],
            datasets: chosen.map((s) => ({
              label: `${s.label} (${s.unit})`,
              data: [...(buf.byKey[s.key] || [])],
              borderColor: s.colour,
              backgroundColor: s.colour,
              yAxisID: axisOf[s.unit],
              borderWidth: 1.6,
              pointRadius: 0,
              tension: 0.25,
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
              tooltip: { enabled: true },
            },
            scales,
          },
        });
      },

      _sample() {
        const pts = (window.Alpine && Alpine.store('app') && Alpine.store('app').points) || {};
        const buf = bufFor(this.kind);
        const now = new Date();
        buf.labels.push(now.toLocaleTimeString([], { hour12: false }));
        if (buf.labels.length > MAX_POINTS) buf.labels.shift();

        for (const s of this.series) {
          if (!buf.byKey[s.key]) buf.byKey[s.key] = [];
          const arr = buf.byKey[s.key];
          const v = pts[s.key];
          // null, not 0 — a point the poller failed to read is a gap in the
          // line, and plotting it as zero invents a fault that isn't there.
          arr.push(typeof v === 'number' ? v : null);
          if (arr.length > MAX_POINTS) arr.shift();
        }
        this.sampleCount = buf.labels.length;

        const ch = _charts[this.kind];
        if (!ch) return;
        ch.data.labels = [...buf.labels];
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
