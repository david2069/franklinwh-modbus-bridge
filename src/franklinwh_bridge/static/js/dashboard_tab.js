/**
 * Dashboard Tab — power flow cards, SOC gauge, real-time chart
 */
function dashboardTab() {
  return {
    chart: null,
    history: { labels: [], battery: [], grid: [], solar: [], home: [] },
    maxPoints: 180, // 30 min at 10s intervals

    init() {
      this._createChart();
      this._startPolling();
    },

    _createChart() {
      const ctx = this.$refs.powerChart;
      if (!ctx) return;

      this.chart = new Chart(ctx, {
        type: 'line',
        data: {
          labels: this.history.labels,
          datasets: [
            {
              label: 'Battery',
              data: this.history.battery,
              borderColor: '#06b6d4',
              backgroundColor: 'rgba(6, 182, 212, 0.1)',
              borderWidth: 2,
              tension: 0.3,
              pointRadius: 0,
              fill: true,
            },
            {
              label: 'Grid',
              data: this.history.grid,
              borderColor: '#ef4444',
              backgroundColor: 'rgba(239, 68, 68, 0.05)',
              borderWidth: 2,
              tension: 0.3,
              pointRadius: 0,
              fill: false,
            },
            {
              label: 'Solar',
              data: this.history.solar,
              borderColor: '#f59e0b',
              backgroundColor: 'rgba(245, 158, 11, 0.1)',
              borderWidth: 2,
              tension: 0.3,
              pointRadius: 0,
              fill: true,
            },
            {
              label: 'Home',
              data: this.history.home,
              borderColor: '#8b5cf6',
              backgroundColor: 'rgba(139, 92, 246, 0.05)',
              borderWidth: 2,
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
                label: (ctx) => `${ctx.dataset.label}: ${(ctx.parsed.y / 1000).toFixed(2)} kW`,
              },
            },
          },
          scales: {
            x: {
              ticks: { color: '#64748b', font: { size: 10 }, maxTicksLimit: 10 },
              grid: { color: 'rgba(255,255,255,0.04)' },
            },
            y: {
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
      // Record a data point every refresh cycle
      this._recordPoint();
      window.addEventListener('tab:changed', () => {});

      setInterval(() => {
        if (Alpine.store('app').activeTab === 'dashboard') {
          this._recordPoint();
        }
      }, 10000);
    },

    _recordPoint() {
      const pts = Alpine.store('app').points;
      const now = new Date();
      const label = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

      this.history.labels.push(label);
      this.history.battery.push(pts.battery_power_w ?? null);
      this.history.grid.push(pts.grid_power_w ?? null);
      this.history.solar.push(pts.solar_power_w ?? null);
      this.history.home.push(pts.home_load_w ?? null);

      // Trim to maxPoints
      if (this.history.labels.length > this.maxPoints) {
        this.history.labels.shift();
        this.history.battery.shift();
        this.history.grid.shift();
        this.history.solar.shift();
        this.history.home.shift();
      }

      if (this.chart) {
        this.chart.update('none'); // 'none' = no animation for real-time
      }
    },
  };
}
