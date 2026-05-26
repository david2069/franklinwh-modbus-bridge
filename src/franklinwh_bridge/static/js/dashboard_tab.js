/**
 * Dashboard Tab — power flow cards, SOC gauge, real-time + historic chart
 *
 * IMPORTANT: The Chart.js instance is stored in a closure variable (_chart),
 * NOT as an Alpine reactive property. Alpine wraps reactive properties in
 * deep Proxies; Chart.js has a massive internal object graph, and Alpine's
 * proxy handler recurses infinitely through it → stack overflow.
 */
function dashboardTab() {
  // Chart.js instance — kept outside Alpine's reactive scope
  let _chart = null;

  return {
    deviceIp: '--',
    deviceUnit: '--',
    showDiagnostics: false,

    // Bottom section tab (chart vs sequencer)
    bottomTab: 'chart',

    // Chart range selector
    chartRange: '30m',
    chartRanges: [
      { value: '30m', label: '30m' },
      { value: '1h',  label: '1h' },
      { value: '6h',  label: '6h' },
      { value: '24h', label: '24h' },
      { value: '7d',  label: '7d' },
      { value: '30d', label: '30d' },
    ],

    // Live in-memory buffer for real-time (last 30m)
    liveHistory: { labels: [], battery: [], grid: [], solar: [], home: [] },
    maxLivePoints: 180,

    async init() {
      await this._loadGateway();
      // Defer chart creation until canvas is visible and sized
      this.$nextTick(() => {
        requestAnimationFrame(() => {
          this._createChart();
          this._loadMetrics();
        });
      });
      this._startPolling();
    },

    async _loadGateway() {
      const data = await fetchJSON('api/gateway');
      if (data && !data.error) {
        this.deviceIp = data.host || '--';
        this.deviceUnit = data.unit_id ?? '--';
      }
    },

    async setChartRange(range) {
      this.chartRange = range;
      await this._loadMetrics();
    },

    _formatChartLabel(ts) {
      const d = new Date(ts * 1000);
      const now = new Date();
      const sameDay = d.toDateString() === now.toDateString();
      const range = this.chartRange;

      if (range === '30m' || range === '1h') {
        return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
      }
      if (range === '6h' || range === '24h') {
        if (!sameDay) {
          return d.toLocaleDateString([], { day: 'numeric', month: 'short' }) + ' ' +
                 d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        }
        return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      }
      // 7d / 30d — always show date + time
      return d.toLocaleDateString([], { day: 'numeric', month: 'short' }) + ' ' +
             d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    },

    async _loadMetrics() {
      const data = await fetchJSON('api/metrics?range=' + this.chartRange);
      if (data && !data.error && data.points && data.points.length > 0) {
        const labels = data.points.map(p => this._formatChartLabel(p.ts));

        this._updateChartData(
          labels,
          data.points.map(p => p.battery_w),
          data.points.map(p => p.grid_w),
          data.points.map(p => p.solar_w),
          data.points.map(p => p.home_w),
        );
      } else if (this.chartRange === '30m') {
        // Fallback to live buffer if no stored metrics yet
        this._updateChartData(
          this.liveHistory.labels,
          this.liveHistory.battery,
          this.liveHistory.grid,
          this.liveHistory.solar,
          this.liveHistory.home,
        );
      }
    },

    _updateChartData(labels, battery, grid, solar, home) {
      if (!_chart) return;
      _chart.data.labels = labels;
      _chart.data.datasets[0].data = battery;
      _chart.data.datasets[1].data = grid;
      _chart.data.datasets[2].data = solar;
      _chart.data.datasets[3].data = home;

      // Scale Y-axis to max charge/discharge rating
      const yScale = _chart.options?.scales?.y;
      if (yScale) {
        const maxRating = Alpine.store('app').points.max_discharge_rate_w || 5000;
        yScale.suggestedMin = -maxRating;
        yScale.suggestedMax = maxRating;
      }

      _chart.resize();
      _chart.update();
    },

    _createChart() {
      const ctx = this.$refs.powerChart;
      if (!ctx) return;

      // Verify canvas is visible and sized (Chart.js fails on 0x0 canvas)
      const rect = ctx.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) {
        requestAnimationFrame(() => this._createChart());
        return;
      }

      // Destroy previous instance if any (e.g. hot-reload)
      if (_chart) {
        _chart.destroy();
        _chart = null;
      }

      const maxRating = Alpine.store('app').points.max_discharge_rate_w || 5000;

      _chart = new Chart(ctx, {
        type: 'line',
        data: {
          labels: [],
          datasets: [
            {
              label: 'Battery',
              data: [],
              borderColor: '#06b6d4',
              backgroundColor: 'rgba(6, 182, 212, 0.1)',
              borderWidth: 1.5,
              tension: 0.3,
              pointRadius: 0,
              fill: true,
            },
            {
              label: 'Grid',
              data: [],
              borderColor: '#ef4444',
              backgroundColor: 'rgba(239, 68, 68, 0.05)',
              borderWidth: 1.5,
              tension: 0.3,
              pointRadius: 0,
              fill: false,
            },
            {
              label: 'Solar',
              data: [],
              borderColor: '#f59e0b',
              backgroundColor: 'rgba(245, 158, 11, 0.1)',
              borderWidth: 1.5,
              tension: 0.3,
              pointRadius: 0,
              fill: true,
            },
            {
              label: 'Home',
              data: [],
              borderColor: '#8b5cf6',
              backgroundColor: 'rgba(139, 92, 246, 0.05)',
              borderWidth: 1.5,
              tension: 0.3,
              pointRadius: 0,
              fill: false,
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: 'index', intersect: false },
          plugins: {
            legend: {
              position: 'top',
              labels: { color: '#94a3b8', font: { size: 11 }, boxWidth: 12, padding: 16 },
            },
            tooltip: {
              backgroundColor: '#1e293b',
              borderColor: 'rgba(255,255,255,0.1)',
              borderWidth: 1,
              titleColor: '#f8fafc',
              bodyColor: '#cbd5e1',
              callbacks: {
                label: (item) => `${item.dataset.label}: ${(item.parsed.y / 1000).toFixed(2)} kW`,
              },
            },
          },
          scales: {
            x: {
              ticks: { color: '#64748b', font: { size: 10 }, maxTicksLimit: 10 },
              grid: { color: 'rgba(255,255,255,0.04)' },
            },
            y: {
              suggestedMin: -maxRating,
              suggestedMax: maxRating,
              ticks: {
                color: '#64748b',
                font: { size: 10 },
                callback: (v) => (v / 1000).toFixed(1) + ' kW',
              },
              grid: { color: 'rgba(255,255,255,0.04)' },
            },
          },
        },
      });
    },

    _startPolling() {
      this._recordPoint();
      setInterval(() => {
        if (Alpine.store('app').activeTab === 'dashboard') {
          this._recordPoint();
          // Refresh metrics from DB every 30s for stored ranges
          if (this.chartRange !== '30m') {
            this._loadMetrics();
          }
        }
      }, 10000);
    },

    _recordPoint() {
      const pts = Alpine.store('app').points;
      const now = new Date();
      const label = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

      this.liveHistory.labels.push(label);
      this.liveHistory.battery.push(pts.battery_power_w ?? null);
      this.liveHistory.grid.push(pts.grid_power_w ?? null);
      this.liveHistory.solar.push(pts.total_solar ?? null);
      this.liveHistory.home.push(pts.home_load_ext ?? null);

      if (this.liveHistory.labels.length > this.maxLivePoints) {
        this.liveHistory.labels.shift();
        this.liveHistory.battery.shift();
        this.liveHistory.grid.shift();
        this.liveHistory.solar.shift();
        this.liveHistory.home.shift();
      }

      // Only update chart from live data when showing 30m range
      if (this.chartRange === '30m') {
        this._updateChartData(
          this.liveHistory.labels,
          this.liveHistory.battery,
          this.liveHistory.grid,
          this.liveHistory.solar,
          this.liveHistory.home,
        );
      }
    },

    // Export chart data to CSV or JSON
    exportChartData(format) {
      if (!_chart || !_chart.data.labels.length) {
        Alpine.store('app').toast('No chart data to export', 'error');
        return;
      }
      const labels = _chart.data.labels;
      const ds = _chart.data.datasets;
      let content, filename, mime;

      if (format === 'csv') {
        const header = 'Time,Battery (W),Grid (W),Solar (W),Home (W)';
        const rows = labels.map((l, i) =>
          `${l},${ds[0].data[i] ?? ''},${ds[1].data[i] ?? ''},${ds[2].data[i] ?? ''},${ds[3].data[i] ?? ''}`
        );
        content = header + '\n' + rows.join('\n');
        filename = `power_history_${this.chartRange}.csv`;
        mime = 'text/csv';
      } else {
        const data = labels.map((l, i) => ({
          time: l,
          battery_w: ds[0].data[i],
          grid_w: ds[1].data[i],
          solar_w: ds[2].data[i],
          home_w: ds[3].data[i],
        }));
        content = JSON.stringify({ range: this.chartRange, points: data }, null, 2);
        filename = `power_history_${this.chartRange}.json`;
        mime = 'application/json';
      }

      const blob = new Blob([content], { type: mime });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
      Alpine.store('app').toast(`Exported ${labels.length} points as ${format.toUpperCase()}`, 'info');
    },

    // Diagnostics computed property
    get diagnosticItems() {
      const pts = Alpine.store('app').points;
      const fmt_val = (v, unit) => v != null ? v + ' ' + unit : '--';
      return [
        { icon: '🌡', label: 'Ambient Temperature', value: fmt_val(pts.ambient_temp_c, '°C') },
        { icon: '🌡', label: 'Cabinet Temperature', value: fmt_val(pts.cabinet_temp_c, '°C') },
        { icon: '⏱', label: 'Command Elapsed Time', value: fmt_val(pts.command_elapsed_s, 's') },
        { icon: '📡', label: 'Control Mode', value: pts.loc_rem_ctl_name || '--' },
        { icon: '🔌', label: 'Grid Connection', value: pts.connection_state || '--' },
        { icon: '⏪', label: 'HW Revert Remaining', value: fmt_val(pts.wset_revert_remain_s, 's') },
        { icon: '⏰', label: 'HW Revert Timer', value: fmt_val(pts.wset_revert_time_s, 's') },
        { icon: '⚡', label: 'Inverter State', value: pts.inverter_state || '--' },
        { icon: '📋', label: 'Last Command Result', value: pts.last_command_result || '--' },
        { icon: '🔋', label: 'Max Charge Rate', value: pts.max_charge_rate_w != null ? (pts.max_charge_rate_w / 1000).toFixed(2) + ' kW' : '--' },
        { icon: '🔋', label: 'Max Discharge Rate', value: pts.max_discharge_rate_w != null ? (pts.max_discharge_rate_w / 1000).toFixed(2) + ' kW' : '--' },
        { icon: '⚙️', label: 'Operating Mode', value: pts.mode_name || '--' },
        { icon: '🎯', label: 'Power Setpoint', value: pts.wset_watts != null ? (pts.wset_watts / 1000).toFixed(2) + ' kW' : '--' },
        { icon: '📊', label: 'Power Setpoint %', value: pts.wset_pct != null ? pts.wset_pct + '%' : '--' },
        { icon: '🔘', label: 'Remote Power Control', value: pts.wset_enabled ?? '--' },
        { icon: '⚠️', label: 'SW Watchdog Remaining', value: fmt_val(pts.sw_watchdog_remain_s, 's') },
        { icon: '🔋', label: 'Total Capacity', value: pts.wh_rating != null ? (pts.wh_rating / 1000).toFixed(3) + ' kWh' : '--' },
        { icon: '⚪', label: 'WSet Enabled', value: pts.wset_enabled === 2 || pts.wset_enabled === true ? 'On' : 'Off' },
        { icon: '⚙️', label: 'WSet Mode', value: pts.wset_mode ?? '--' },
      ];
    },
  };
}
