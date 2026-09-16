/**
 * Dashboard Tab — power flow cards, SOC gauge, real-time + historic chart
 *
 * IMPORTANT: The Chart.js instance is stored in a closure variable (_chart),
 * NOT as an Alpine reactive property. Alpine wraps reactive properties in
 * deep Proxies; Chart.js has a massive internal object graph, and Alpine's
 * proxy handler recurses infinitely through it → stack overflow.
 */
function dashboardTab() {
  // Chart.js instances — kept outside Alpine's reactive scope (prevents proxy recursion)
  let _chart = null;
  let _modalChart = null;

  // Live in-memory buffer — also outside Alpine scope to avoid Proxy arrays
  // leaking into Chart.js (Chart.js traverses array elements → Alpine proxy
  // getter fires → Chart.js re-reads → infinite recursion → stack overflow)
  const _liveHistory = { labels: [], battery: [], grid: [], solar: [], home: [], soc: [], ambient: [], cabinet: [], mode: [], selfReserve: [], touReserve: [], gridMode: [] };
  const MAX_LIVE_POINTS = 180;

  // Operating-mode colours used for both background shading and the legend
  const MODE_ABBR = {
    'TOU':              'TOU',
    'Time of Use':      'TOU',
    'Self-Consumption': 'Self',
    'Emergency Backup': 'Backup',
  };
  // Grid-mode / alarm marker LINE colors — shared between the chart's
  // afterDraw (which draws the dashed vertical lines) and its legend (which
  // explains what they mean). See Events card/modal for readable detail.
  const GRID_MODE_COLORS = {
    'Grid Following': 'rgba(34,197,94,0.9)',
    'Grid Forming':   'rgba(251,191,36,0.9)',
    'PV Clipped':     'rgba(251,146,60,0.9)',
  };
  const GRID_MODE_ABBR = {
    'Grid Following': 'Following',
    'Grid Forming':   'Forming',
    'PV Clipped':     'PV Clip',
  };
  const ALARM_COLORS = {
    fault:   'rgba(239,68,68,0.9)',
    warning: 'rgba(245,158,11,0.9)',
    info:    'rgba(148,163,184,0.9)',
  };
  const ALARM_SEVERITY_LABELS = {
    fault:   'Fault',
    warning: 'Warning',
    info:    'Info',
  };
  // Human-readable labels for the Events source filter dropdown — the four
  // possible values of an event row's `source` field.
  const EVENT_SOURCE_LABELS = {
    'M701_Alrm':     'M701 System Alarm',
    'M714_PrtAlrms': 'M714 DC Port Alarm',
    'M713_Sta':      'M713 Battery State',
    'M701_DERMode':  'M701 Grid Mode',
  };

  // Mirrors mock_gateway.py synthetic_points() — used to build fake historical
  // time-series for mock gateways in the compare chart (they are never stored
  // to the DB, so we generate them client-side using the same deterministic formula).
  // Mirrors mock_gateway.py synthetic_points() — realistic time-of-day patterns.
  // Seconds covered by each chart range — shared by the main chart and the
  // Gateway Comparison modal, so a synthetic series spans the same window a
  // real fetch would have.

  const modeBackgroundPlugin = {
    id: 'modeBackground',
    beforeDraw(chart) {
      const { ctx, chartArea } = chart;
      if (!chartArea) return;
      const modeData = chart._modeData;
      if (!modeData || !modeData.length) return;

      const n = modeData.length;
      const slotW = (chartArea.right - chartArea.left) / Math.max(n - 1, 1);

      ctx.save();
      let i = 0;
      while (i < n) {
        const mode = modeData[i];
        if (!mode || !MODE_BG_COLORS[mode]) { i++; continue; }
        let j = i + 1;
        while (j < n && modeData[j] === mode) j++;
        const x1 = chartArea.left + i * slotW;
        const x2 = chartArea.left + (j - 1) * slotW + slotW;
        ctx.fillStyle = MODE_BG_COLORS[mode];
        ctx.fillRect(x1, chartArea.top, x2 - x1, chartArea.bottom - chartArea.top);
        i = j;
      }
      ctx.restore();
    },
    afterDraw(chart) {
      const { ctx, chartArea } = chart;
      if (!chartArea) return;
      const modeData = chart._modeData;
      const gridModeData = chart._gridModeData;
      const alarmData = chart._alarmData;
      const tsRaw = chart._tsRaw;

      // ── Operating-mode background legend (filled-square swatches) ──────
      if (modeData && modeData.length) {
        // Collect unique abbreviated mode labels in order of first appearance
        const seen = new Map();
        for (const m of modeData) {
          if (m && MODE_ABBR[m]) {
            const abbr = MODE_ABBR[m];
            if (!seen.has(abbr)) seen.set(abbr, MODE_LEGEND_COLORS[m]);
          }
        }
        if (seen.size) {
          ctx.save();
          ctx.font = '9px sans-serif';
          let x = chartArea.left + 6;
          const y = chartArea.bottom - 5;
          for (const [label, color] of seen) {
            ctx.fillStyle = color;
            ctx.fillRect(x, y - 8, 8, 8);
            ctx.fillStyle = color;
            ctx.textAlign = 'left';
            ctx.fillText(label, x + 10, y);
            x += 10 + ctx.measureText(label).width + 10;
          }
          ctx.restore();
        }
      }

      // ── Grid-mode / alarm marker legend (dashed-line swatches, distinct
      // style from the filled squares above since these represent the
      // vertical dashed lines drawn below, not an area fill). Only shown
      // for colors actually present in the current window. Second row, so
      // it doesn't collide with the mode-background legend. ─────────────
      const lineLegend = new Map();
      if (gridModeData && gridModeData.length) {
        for (const m of gridModeData) {
          if (m && GRID_MODE_ABBR[m] && !lineLegend.has(GRID_MODE_ABBR[m])) {
            lineLegend.set(GRID_MODE_ABBR[m], GRID_MODE_COLORS[m]);
          }
        }
      }
      if (alarmData && alarmData.length) {
        for (const ev of alarmData) {
          const label = ALARM_SEVERITY_LABELS[ev.severity] || 'Info';
          if (!lineLegend.has(label)) {
            lineLegend.set(label, ALARM_COLORS[ev.severity] || ALARM_COLORS.info);
          }
        }
      }
      if (lineLegend.size) {
        ctx.save();
        ctx.font = '9px sans-serif';
        let x2 = chartArea.left + 6;
        const y2 = chartArea.bottom - 18;
        for (const [label, color] of lineLegend) {
          ctx.strokeStyle = color;
          ctx.lineWidth = 1.5;
          ctx.setLineDash([3, 2]);
          ctx.beginPath();
          ctx.moveTo(x2, y2 - 4);
          ctx.lineTo(x2, y2 + 4);
          ctx.stroke();
          ctx.setLineDash([]);
          ctx.fillStyle = color;
          ctx.textAlign = 'left';
          ctx.fillText(label, x2 + 6, y2 + 3);
          x2 += 6 + ctx.measureText(label).width + 10;
        }
        ctx.restore();
      }

      // ── Grid-mode event markers — thin vertical tick (color explained by
      // the legend above; see the Events card/modal for readable detail,
      // since closely-spaced events made on-chart text chips overlap and
      // become unreadable). ───────────────────────────────────────────────
      if (gridModeData && gridModeData.length >= 2) {
        const n = gridModeData.length;
        const slotW = (chartArea.right - chartArea.left) / Math.max(n - 1, 1);
        ctx.save();
        for (let i = 1; i < n; i++) {
          const prev = gridModeData[i - 1];
          const curr = gridModeData[i];
          if (!curr || !prev || curr === prev) continue;
          const color = GRID_MODE_COLORS[curr] || 'rgba(148,163,184,0.9)';
          const xPos = chartArea.left + i * slotW;
          ctx.strokeStyle = color;
          ctx.lineWidth = 1.5;
          ctx.setLineDash([3, 2]);
          ctx.beginPath();
          ctx.moveTo(xPos, chartArea.top);
          ctx.lineTo(xPos, chartArea.bottom);
          ctx.stroke();
          ctx.setLineDash([]);
        }
        ctx.restore();
      }

      // ── Alarm event markers — thin vertical tick (color explained by the
      // legend above; see the Events card/modal for readable detail). ────
      if (!alarmData || !alarmData.length || !tsRaw || tsRaw.length < 2) return;

      // Helper: map a Unix timestamp to an x pixel position via linear interpolation
      function tsToX(ts) {
        const n2 = tsRaw.length;
        if (ts <= tsRaw[0]) return chartArea.left;
        if (ts >= tsRaw[n2 - 1]) return chartArea.right;
        let lo = 0, hi = n2 - 1;
        while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (tsRaw[mid] <= ts) lo = mid; else hi = mid; }
        const frac = (ts - tsRaw[lo]) / (tsRaw[hi] - tsRaw[lo]);
        return chartArea.left + ((lo + frac) / (n2 - 1)) * (chartArea.right - chartArea.left);
      }

      ctx.save();
      for (const ev of alarmData) {
        const xPos = tsToX(ev.ts);
        const color = ALARM_COLORS[ev.severity] || ALARM_COLORS.info;

        ctx.strokeStyle = color;
        ctx.lineWidth = 1;
        ctx.setLineDash([2, 2]);
        ctx.beginPath();
        ctx.moveTo(xPos, chartArea.top);
        ctx.lineTo(xPos, chartArea.bottom);
        ctx.stroke();
        ctx.setLineDash([]);
      }
      ctx.restore();
    },
  };

  // Default card visibility (Bridge Status hidden by default)
  const DEFAULT_CARDS = {
    bridgeStatus: false,
    powerFlow: true,
    acPower: true,
    batterySoc: true,
    solarInputs: true,
    battery: true,
    lifetimeEnergy: true,
    energyFlow: true,
    batteryControl: true,
    operatingMode: true,
    livePoints: true,
    eventsHistory: true,
  };

  const DEFAULT_CARD_ORDER = [
    'bridgeStatus', 'powerFlow', 'acPower', 'batterySoc',
    'solarInputs', 'battery', 'lifetimeEnergy', 'energyFlow', 'batteryControl',
    'operatingMode', 'livePoints', 'eventsHistory',
  ];

  function loadCardPrefs() {
    try {
      const saved = localStorage.getItem('fwh-dashboard-cards');
      if (saved) return { ...DEFAULT_CARDS, ...JSON.parse(saved) };
    } catch (_) {}
    return { ...DEFAULT_CARDS };
  }

  function loadCardOrder() {
    try {
      const saved = localStorage.getItem('fwh-dashboard-order');
      if (saved) {
        const parsed = JSON.parse(saved);
        // Merge: keep saved order, append any new keys not yet in saved order
        const known = new Set(parsed);
        const merged = [...parsed];
        for (const k of DEFAULT_CARD_ORDER) { if (!known.has(k)) merged.push(k); }
        return merged;
      }
    } catch (_) {}
    return [...DEFAULT_CARD_ORDER];
  }

  return {
    deviceIp: '--',
    deviceUnit: '--',
    showDiagnostics: false,
    showCardConfig: false,
    cardVisible: loadCardPrefs(),

    // Card ordering (drag-to-reorder)
    cardOrder: loadCardOrder(),
    _dragKey: null,

    // Bottom section tab (chart vs sequencer)
    bottomTab: 'chart',

    // Chart popup modal
    showChartModal: false,

    // Multi-gateway compare modal
    showCompareModal: false,
    _compareCharts: [],

    // Events History — persistent card (always visible) + Expand modal,
    // sharing one filter state so the modal is a pure larger display of
    // whatever the card currently has loaded, not a separate view.
    showEventsModal: false,
    eventsData: [],
    eventsLoading: false,
    eventsRange: localStorage.getItem('fwh-events-range') || '7d',
    // Same preset list as the Power History chart's own Time Span dropdown,
    // minus 'live' (events are historical, there's no "live" mode for them).
    eventsRanges: [
      { value: '30m',  label: '30m' },
      { value: '1h',   label: '1h' },
      { value: '2h',   label: '2h' },
      { value: '4h',   label: '4h' },
      { value: '6h',   label: '6h' },
      { value: '8h',   label: '8h' },
      { value: '12h',  label: '12h' },
      { value: '18h',  label: '18h' },
      { value: '24h',  label: '24h' },
      { value: '3d',   label: '3d' },
      { value: '5d',   label: '5d' },
      { value: '7d',   label: '7d' },
      { value: '30d',  label: '30d' },
    ],
    // Custom date range (mirrors the chart's own dateStart/dateEnd/
    // showDateRange/loadDateRange pattern exactly).
    showEventsDateRange: false,
    eventsDateStart: '',
    eventsDateEnd: '',

    // Severity + source filters (mirror the Logs tab's levelFilter/
    // sourceFilter pattern — clickable severity chips double as the
    // legend, since the chip labels themselves answer "what severities
    // exist" instead of leaving it to be guessed from row colors alone).
    eventsSeverityFilter: '',
    eventsSourceFilter: '',

    // Chart range selector
    // Set when a metrics fetch succeeds but returns nothing for this gateway.
    // Without it the chart silently keeps the PREVIOUS gateway's data, which
    // reads as "the chart didn't redraw" but is worse — it attributes one
    // gateway's history to another. Mock gateways never persist metrics, so
    // this is their normal state, not a fault.
    // Chart controls are collapsed on a phone (see the Options toggle) — the
    // chart itself is what you came for; the seven controls rarely are.
    chartOptionsOpen: false,
    chartEmpty: false,
    get chartEmptyMsg() {
      const gw = (Alpine.store('app').gatewayList || [])
        .find((g) => g.id === Alpine.store('app').activeGateway);
      if (gw && gw.mock) return `${gw.name} is a mock gateway — its metrics aren't recorded.`;
      return 'No history recorded for this gateway in this range.';
    },

    // Last choice wins, else 24h — 30m showed a sliver of the day and almost
    // everyone widened it immediately. 'custom' is never restored: it depends
    // on dates we don't store, so it would reopen as an empty range.
    chartRange: localStorage.getItem('fwh-chart-range') || '24h',
    chartRanges: [
      { value: 'live', label: 'Live' },
      { value: '30m',  label: '30m' },
      { value: '1h',   label: '1h' },
      { value: '2h',   label: '2h' },
      { value: '4h',   label: '4h' },
      { value: '6h',   label: '6h' },
      { value: '8h',   label: '8h' },
      { value: '12h',  label: '12h' },
      { value: '18h',  label: '18h' },
      { value: '24h',  label: '24h' },
      { value: '3d',   label: '3d' },
      { value: '5d',   label: '5d' },
      { value: '7d',   label: '7d' },
      { value: '30d',  label: '30d' },
    ],

    // Time scale (bucket) selector
    chartBucket: '',  // empty = auto
    chartBuckets: [
      { value: '',    label: 'Auto' },
      { value: '1m',  label: '1m' },
      { value: '5m',  label: '5m' },
      { value: '10m', label: '10m' },
      { value: '15m', label: '15m' },
      { value: '30m', label: '30m' },
      { value: '1h',  label: '1h' },
      { value: '1d',  label: '1d' },
    ],

    // Export dropdown
    showExportMenu: false,

    // Chart overlays (each independently toggleable)
    showSocOverlay: false,
    showAmbientOverlay: false,
    showCabinetOverlay: false,
    showOverlayMenu: false,

    // Date range picker
    showDateRange: false,
    dateStart: '',
    dateEnd: '',

    // Fast poll interval for live mode
    _fastInterval: null,

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
      this.loadEvents();  // Events History card is persistent, load on page load

      // Reload chart data whenever the active gateway changes
      this.$watch(
        () => Alpine.store('app').activeGateway,
        () => {
          // Live mode: live buffer is per-session, reset it so old gateway
          // data doesn't bleed through; then fall back to 30m for the new gw
          if (this.chartRange === 'live') {
            _liveHistory.labels = []; _liveHistory.battery = []; _liveHistory.grid = [];
            _liveHistory.solar = []; _liveHistory.home = []; _liveHistory.soc = [];
            _liveHistory.ambient = []; _liveHistory.cabinet = []; _liveHistory.mode = [];
            _liveHistory.selfReserve = []; _liveHistory.touReserve = []; _liveHistory.gridMode = [];
          }
          this._loadMetrics();
          this.loadEvents();
        },
      );
    },

    toggleCard(key) {
      this.cardVisible[key] = !this.cardVisible[key];
      localStorage.setItem('fwh-dashboard-cards', JSON.stringify(this.cardVisible));
    },

    resetCardDefaults() {
      this.cardVisible = { ...DEFAULT_CARDS };
      localStorage.removeItem('fwh-dashboard-cards');
    },

    resetCardOrder() {
      this.cardOrder = [...DEFAULT_CARD_ORDER];
      localStorage.removeItem('fwh-dashboard-order');
    },

    dragStart(key) {
      this._dragKey = key;
    },

    dragOver(e, key) {
      e.preventDefault();
      if (!this._dragKey || this._dragKey === key) return;
      const from = this.cardOrder.indexOf(this._dragKey);
      const to = this.cardOrder.indexOf(key);
      if (from < 0 || to < 0) return;
      this.cardOrder.splice(from, 1);
      this.cardOrder.splice(to, 0, this._dragKey);
    },

    dragEnd() {
      this._dragKey = null;
      localStorage.setItem('fwh-dashboard-order', JSON.stringify(this.cardOrder));
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
      if (range !== 'custom') localStorage.setItem('fwh-chart-range', range);
      this.showDateRange = false;  // close date picker when selecting preset

      // Fast polling for live mode (2s), normal for everything else
      if (range === 'live') {
        this._startFastPoll();
      } else {
        this._stopFastPoll();
        await this._loadMetrics();
      }
    },

    async setChartBucket(bucket) {
      this.chartBucket = bucket;
      if (this.chartRange !== 'live') {
        await this._loadMetrics();
      }
    },

    async loadDateRange() {
      if (!this.dateStart || !this.dateEnd) {
        Alpine.store('app').toast('Select both start and end dates', 'error');
        return;
      }
      const startTs = new Date(this.dateStart).getTime() / 1000;
      const endTs = new Date(this.dateEnd).getTime() / 1000;
      if (endTs <= startTs) {
        Alpine.store('app').toast('End date must be after start date', 'error');
        return;
      }
      // Cap to 90 days
      if ((endTs - startTs) > 90 * 86400) {
        Alpine.store('app').toast('Date range cannot exceed 90 days', 'error');
        return;
      }

      this.chartRange = 'custom';
      this._stopFastPoll();

      let url = `api/metrics?start=${startTs}&end=${endTs}`;
      if (this.chartBucket) url += `&bucket=${this.chartBucket}`;
      const activeGw = Alpine.store('app')?.activeGateway;
      if (activeGw && activeGw !== 'site') url += `&gateway_id=${encodeURIComponent(activeGw)}`;

      let alarmUrl = `api/alarm-events?start=${startTs}&end=${endTs}`;
      if (activeGw && activeGw !== 'site') alarmUrl += `&gateway_id=${encodeURIComponent(activeGw)}`;

      const gwMetaC = (Alpine.store('app')?.gatewayList || []).find((g) => g.id === activeGw);
      let data, alarmResp;
      if (gwMetaC && gwMetaC.mock) {
        const secs = Math.max(600, endTs - startTs);
        data = { points: mockSyntheticSeries(activeGw, endTs, secs,
                                             Math.min(180, Math.max(12, Math.floor(secs / 10)))) };
        alarmResp = null;
      } else {
        [data, alarmResp] = await Promise.all([fetchJSON(url), fetchJSON(alarmUrl).catch(() => null)]);
      }
      if (data && !data.error && (!data.points || data.points.length === 0)) {
        // Succeeded, but this gateway has nothing here — blank the chart so it
        // can't keep showing the gateway we just switched away from.
        this.chartEmpty = true;
        const none = [];
        this._updateChartData(none, none, none, none, none, none, none, none, none, none, none, none, [], none);
        this._updateModalChart(none, none, none, none, none, none, none, none, none, none, none, none, [], none);
        return;
      }
      if (data && !data.error && data.points && data.points.length > 0) {
        this.chartEmpty = false;
        const tsRaw = data.points.map(p => p.ts);
        const labels = tsRaw.map(ts => this._formatChartLabel(ts));
        const battery = data.points.map(p => p.battery_w);
        const grid = data.points.map(p => p.grid_w);
        const solar = data.points.map(p => p.solar_w);
        const home = data.points.map(p => p.home_w);
        const soc = data.points.map(p => p.soc ?? null);
        const ambient = data.points.map(p => p.ambient_temp_c ?? null);
        const cabinet = data.points.map(p => p.cabinet_temp_c ?? null);
        const mode = data.points.map(p => p.mode_name ?? null);
        const selfReserve = data.points.map(p => p.self_reserve_pct ?? null);
        const touReserve = data.points.map(p => p.tou_reserve_pct ?? null);
        const gridMode = data.points.map(p => p.grid_mode ?? null);
        const alarmEvents = alarmResp?.events || [];
        this._updateChartData(labels, battery, grid, solar, home, soc, ambient, cabinet, mode, selfReserve, touReserve, gridMode, alarmEvents, tsRaw);
        this._updateModalChart(labels, battery, grid, solar, home, soc, ambient, cabinet, mode, selfReserve, touReserve, gridMode, alarmEvents, tsRaw);
        Alpine.store('app').toast(`Loaded ${data.points.length} points`, 'info');
      } else {
        Alpine.store('app').toast('No data found for selected range', 'error');
      }
    },

    _startFastPoll() {
      this._stopFastPoll();
      // Immediate update
      this._recordPoint();
      this._fastInterval = setInterval(() => {
        this._recordPoint();
      }, 2000);
    },

    _stopFastPoll() {
      if (this._fastInterval) {
        clearInterval(this._fastInterval);
        this._fastInterval = null;
      }
    },

    _formatChartLabel(ts) {
      const d = new Date(ts * 1000);
      const now = new Date();
      const sameDay = d.toDateString() === now.toDateString();
      const range = this.chartRange;

      if (range === 'live' || range === '30m' || range === '1h') {
        return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
      }
      if (range === '2h' || range === '4h' || range === '6h' || range === '8h' || range === '12h' || range === '18h' || range === '24h') {
        if (!sameDay) {
          return d.toLocaleDateString([], { day: 'numeric', month: 'short' }) + ' ' +
                 d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        }
        return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      }
      // 7d, 30d, custom — always show date + time
      return d.toLocaleDateString([], { day: 'numeric', month: 'short' }) + ' ' +
             d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    },

    async _loadMetrics() {
      // Live mode uses in-memory buffer only, no DB fetch
      if (this.chartRange === 'live') {
        this._updateChartData(
          _liveHistory.labels, _liveHistory.battery, _liveHistory.grid,
          _liveHistory.solar, _liveHistory.home,
          _liveHistory.soc, _liveHistory.ambient, _liveHistory.cabinet, _liveHistory.mode, _liveHistory.selfReserve, _liveHistory.touReserve, _liveHistory.gridMode,
        );
        this._updateModalChart(
          _liveHistory.labels, _liveHistory.battery, _liveHistory.grid,
          _liveHistory.solar, _liveHistory.home,
          _liveHistory.soc, _liveHistory.ambient, _liveHistory.cabinet, _liveHistory.mode, _liveHistory.selfReserve, _liveHistory.touReserve, _liveHistory.gridMode,
        );
        return;
      }

      // Custom date range is loaded via loadDateRange(), skip here
      if (this.chartRange === 'custom') return;

      let url = 'api/metrics?range=' + this.chartRange;
      if (this.chartBucket) url += '&bucket=' + this.chartBucket;
      const activeGw = Alpine.store('app')?.activeGateway;
      if (activeGw && activeGw !== 'site') url += `&gateway_id=${encodeURIComponent(activeGw)}`;

      let alarmUrl = `api/alarm-events?range=${this.chartRange}`;
      if (activeGw && activeGw !== 'site') alarmUrl += `&gateway_id=${encodeURIComponent(activeGw)}`;

      // A mock gateway never persists metrics, so a fetch returns nothing and
      // the chart would be blank while the Comparison modal — which already
      // synthesises — shows a full curve for the same gateway. Generate the
      // same series here so the two views agree.
      const gwMeta = (Alpine.store('app')?.gatewayList || []).find((g) => g.id === activeGw);
      let data, alarmResp;
      if (gwMeta && gwMeta.mock) {
        const secs = RANGE_SECS[this.chartRange] || 1800;
        data = { points: mockSyntheticSeries(activeGw, Date.now() / 1000, secs,
                                             Math.min(180, Math.max(12, Math.floor(secs / 10)))) };
        alarmResp = null;
      } else {
        [data, alarmResp] = await Promise.all([fetchJSON(url), fetchJSON(alarmUrl).catch(() => null)]);
      }
      if (data && !data.error && (!data.points || data.points.length === 0)) {
        // Succeeded, but this gateway has nothing here — blank the chart so it
        // can't keep showing the gateway we just switched away from.
        this.chartEmpty = true;
        const none = [];
        this._updateChartData(none, none, none, none, none, none, none, none, none, none, none, none, [], none);
        this._updateModalChart(none, none, none, none, none, none, none, none, none, none, none, none, [], none);
        return;
      }
      if (data && !data.error && data.points && data.points.length > 0) {
        this.chartEmpty = false;
        const tsRaw = data.points.map(p => p.ts);
        const labels = tsRaw.map(ts => this._formatChartLabel(ts));
        const battery = data.points.map(p => p.battery_w);
        const grid = data.points.map(p => p.grid_w);
        const solar = data.points.map(p => p.solar_w);
        const home = data.points.map(p => p.home_w);
        const soc = data.points.map(p => p.soc ?? null);
        const ambient = data.points.map(p => p.ambient_temp_c ?? null);
        const cabinet = data.points.map(p => p.cabinet_temp_c ?? null);
        const mode = data.points.map(p => p.mode_name ?? null);
        const selfReserve = data.points.map(p => p.self_reserve_pct ?? null);
        const touReserve = data.points.map(p => p.tou_reserve_pct ?? null);
        const gridMode = data.points.map(p => p.grid_mode ?? null);
        const alarmEvents = alarmResp?.events || [];

        this._updateChartData(labels, battery, grid, solar, home, soc, ambient, cabinet, mode, selfReserve, touReserve, gridMode, alarmEvents, tsRaw);
        this._updateModalChart(labels, battery, grid, solar, home, soc, ambient, cabinet, mode, selfReserve, touReserve, gridMode, alarmEvents, tsRaw);
      }
    },

    _updateChartData(labels, battery, grid, solar, home, soc = [], ambient = [], cabinet = [], mode = [], selfReserve = [], touReserve = [], gridMode = [], alarmEvents = [], tsRaw = []) {
      if (!_chart) return;
      _chart._modeData = mode;
      _chart._selfReserveData = selfReserve;
      _chart._touReserveData = touReserve;
      _chart._gridModeData = gridMode;
      _chart._alarmData = alarmEvents;
      _chart._tsRaw = tsRaw;
      _chart.data.labels = labels;
      _chart.data.datasets[0].data = battery;
      _chart.data.datasets[1].data = grid;
      _chart.data.datasets[2].data = solar;
      _chart.data.datasets[3].data = home;
      const socData    = this.showSocOverlay     ? soc     : [];
      const ambData    = this.showAmbientOverlay ? ambient : [];
      const cabData    = this.showCabinetOverlay ? cabinet : [];
      _chart.data.datasets[4].data = socData;
      _chart.data.datasets[5].data = ambData;
      _chart.data.datasets[6].data = cabData;
      // Mode indicator dataset: 0 where mode is known (generates tooltip item), null elsewhere
      _chart.data.datasets[7].data = mode.map(m => (m && MODE_ABBR[m]) ? 0 : null);

      // Scale Y-axis to max charge/discharge rating
      const yScale = _chart.options?.scales?.y;
      if (yScale) {
        const maxRating = Alpine.store('app').points.max_discharge_rate_w || 5000;
        yScale.suggestedMin = -maxRating;
        yScale.suggestedMax = maxRating;
      }

      // Show y2 only when at least one overlay has actual data points
      const y2 = _chart.options?.scales?.y2;
      if (y2) {
        const hasData = socData.some(v => v != null) || ambData.some(v => v != null) || cabData.some(v => v != null);
        y2.display = hasData;
      }

      _chart.resize();
      _chart.update();
    },

    toggleSocOverlay() {
      this.showSocOverlay = !this.showSocOverlay;
      this._refreshOverlays();
    },

    toggleAmbientOverlay() {
      this.showAmbientOverlay = !this.showAmbientOverlay;
      this._refreshOverlays();
    },

    toggleCabinetOverlay() {
      this.showCabinetOverlay = !this.showCabinetOverlay;
      this._refreshOverlays();
    },

    _refreshOverlays() {
      if (this.chartRange === 'live' || this.chartRange === '30m') {
        this._updateChartData(
          _liveHistory.labels, _liveHistory.battery, _liveHistory.grid,
          _liveHistory.solar, _liveHistory.home,
          _liveHistory.soc, _liveHistory.ambient, _liveHistory.cabinet, _liveHistory.mode, _liveHistory.selfReserve, _liveHistory.touReserve, _liveHistory.gridMode,
        );
        this._updateModalChart(
          _liveHistory.labels, _liveHistory.battery, _liveHistory.grid,
          _liveHistory.solar, _liveHistory.home,
          _liveHistory.soc, _liveHistory.ambient, _liveHistory.cabinet, _liveHistory.mode, _liveHistory.selfReserve, _liveHistory.touReserve, _liveHistory.gridMode,
        );
      } else {
        this._loadMetrics();
      }
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
        plugins: [modeBackgroundPlugin],
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
            // Overlay datasets — distinct colors: lime, fuchsia, yellow (clear of main series)
            {
              label: 'SoC',
              data: [],
              borderColor: '#84cc16',
              backgroundColor: 'transparent',
              borderWidth: 2,
              borderDash: [6, 3],
              tension: 0.3,
              pointRadius: 0,
              fill: false,
              yAxisID: 'y2',
            },
            {
              label: 'Ambient',
              data: [],
              borderColor: '#e879f9',
              backgroundColor: 'transparent',
              borderWidth: 2,
              borderDash: [6, 3],
              tension: 0.3,
              pointRadius: 0,
              fill: false,
              yAxisID: 'y2',
            },
            {
              label: 'Cabinet',
              data: [],
              borderColor: '#facc15',
              backgroundColor: 'transparent',
              borderWidth: 2,
              borderDash: [6, 3],
              tension: 0.3,
              pointRadius: 0,
              fill: false,
              yAxisID: 'y2',
            },
            // Mode indicator — invisible on chart; provides color-boxed tooltip row
            {
              label: 'Mode',
              data: [],
              borderColor: 'transparent',
              backgroundColor: 'transparent',
              borderWidth: 0,
              pointRadius: 0,
              fill: false,
              yAxisID: 'yMode',
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
              labels: {
                color: '#94a3b8', font: { size: 11 }, boxWidth: 12, padding: 16,
                filter: (item) => item.datasetIndex !== 7 && (item.datasetIndex < 4 || _chart?.data?.datasets[item.datasetIndex]?.data?.length > 0),
              },
            },
            tooltip: {
              backgroundColor: '#1e293b',
              borderColor: 'rgba(255,255,255,0.1)',
              borderWidth: 1,
              titleColor: '#f8fafc',
              bodyColor: '#cbd5e1',
              filter: (item) => {
                if (item.datasetIndex === 7) {
                  const mode = item.chart._modeData?.[item.dataIndex];
                  return !!(mode && MODE_ABBR[mode]);
                }
                return true;
              },
              callbacks: {
                label: (item) => {
                  if (item.datasetIndex === 7) {
                    const mode = item.chart._modeData?.[item.dataIndex];
                    return MODE_ABBR[mode] || mode;
                  }
                  if (item.datasetIndex >= 4) {
                    const isSoc = item.dataset.label === 'SoC';
                    const unit = isSoc ? '%' : '°C';
                    return `${item.dataset.label}: ${item.parsed.y?.toFixed(1)}${unit}`;
                  }
                  return `${item.dataset.label}: ${(item.parsed.y / 1000).toFixed(2)} kW`;
                },
                labelColor: (item) => {
                  if (item.datasetIndex === 7) {
                    const mode = item.chart._modeData?.[item.dataIndex];
                    const color = MODE_LEGEND_COLORS[mode] || '#64748b';
                    return { borderColor: color, backgroundColor: color };
                  }
                },
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
            y2: {
              position: 'right',
              display: false,
              suggestedMin: 0,
              suggestedMax: 100,
              ticks: { color: '#64748b', font: { size: 9 }, callback: (v) => v },
              grid: { drawOnChartArea: false },
            },
            yMode: { display: false, min: -1, max: 1 },
          },
        },
      });
    },

    _startPolling() {
      this._recordPoint();
      setInterval(() => {
        if (Alpine.store('app').activeTab === 'dashboard') {
          this._recordPoint();
          // Refresh metrics from DB every 30s for all stored ranges (not live)
          if (this.chartRange !== 'live') {
            this._loadMetrics();
          }
        }
      }, 10000);
    },

    _recordPoint() {
      const pts = Alpine.store('app').points;
      const now = new Date();
      const label = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

      _liveHistory.labels.push(label);
      _liveHistory.battery.push(pts.battery_power_w ?? null);
      _liveHistory.grid.push(pts.grid_power_w ?? null);
      _liveHistory.solar.push(pts.total_solar ?? null);
      _liveHistory.home.push(pts.home_load_ext ?? null);
      _liveHistory.soc.push(pts.soc ?? null);
      _liveHistory.ambient.push(pts.ambient_temp_c ?? null);
      _liveHistory.cabinet.push(pts.cabinet_temp_c ?? null);
      _liveHistory.mode.push(pts.mode_name ?? null);
      _liveHistory.selfReserve.push(pts.self_reserve_pct ?? null);
      _liveHistory.touReserve.push(pts.tou_reserve_pct ?? null);
      _liveHistory.gridMode.push(pts.grid_mode ?? null);

      if (_liveHistory.labels.length > MAX_LIVE_POINTS) {
        _liveHistory.labels.shift();
        _liveHistory.battery.shift();
        _liveHistory.grid.shift();
        _liveHistory.solar.shift();
        _liveHistory.home.shift();
        _liveHistory.soc.shift();
        _liveHistory.ambient.shift();
        _liveHistory.cabinet.shift();
        _liveHistory.mode.shift();
        _liveHistory.selfReserve.shift();
        _liveHistory.touReserve.shift();
        _liveHistory.gridMode.shift();
      }

      // Update chart from live buffer only in 'live' mode.
      // '30m' uses DB data (refreshed every 30s below) so it doesn't blank when device is idle.
      if (this.chartRange === 'live') {
        this._updateChartData(
          _liveHistory.labels, _liveHistory.battery, _liveHistory.grid,
          _liveHistory.solar, _liveHistory.home,
          _liveHistory.soc, _liveHistory.ambient, _liveHistory.cabinet, _liveHistory.mode, _liveHistory.selfReserve, _liveHistory.touReserve, _liveHistory.gridMode,
        );
        this._updateModalChart(
          _liveHistory.labels, _liveHistory.battery, _liveHistory.grid,
          _liveHistory.solar, _liveHistory.home,
          _liveHistory.soc, _liveHistory.ambient, _liveHistory.cabinet, _liveHistory.mode, _liveHistory.selfReserve, _liveHistory.touReserve, _liveHistory.gridMode,
        );
      }
    },

    // Export chart data to CSV or JSON
    exportChartData(format) {
      // Use modal chart data if modal is open, otherwise main chart
      const src = (this.showChartModal && _modalChart) ? _modalChart : _chart;
      if (!src || !src.data.labels.length) {
        Alpine.store('app').toast('No chart data to export', 'error');
        return;
      }
      const labels = src.data.labels;
      const ds = src.data.datasets;
      let content, filename, mime;

      if (format === 'csv') {
        const header = 'Time,Battery (W),Grid (W),Solar (W),Home (W),SoC (%),Ambient (°C),Cabinet (°C),Mode,Self Reserve (%),TOU Reserve (%)';
        const modeData = src._modeData || [];
        const selfRes = src._selfReserveData || [];
        const touRes = src._touReserveData || [];
        const rows = labels.map((l, i) =>
          `${l},${ds[0].data[i] ?? ''},${ds[1].data[i] ?? ''},${ds[2].data[i] ?? ''},${ds[3].data[i] ?? ''},${ds[4].data[i] ?? ''},${ds[5].data[i] ?? ''},${ds[6].data[i] ?? ''},${modeData[i] ?? ''},${selfRes[i] ?? ''},${touRes[i] ?? ''}`
        );
        content = header + '\n' + rows.join('\n');
        filename = `power_history_${this.chartRange}.csv`;
        mime = 'text/csv';
      } else {
        const modeData = src._modeData || [];
        const selfRes = src._selfReserveData || [];
        const touRes = src._touReserveData || [];
        const data = labels.map((l, i) => ({
          time: l,
          battery_w: ds[0].data[i],
          grid_w: ds[1].data[i],
          solar_w: ds[2].data[i],
          home_w: ds[3].data[i],
          soc: ds[4].data[i] ?? null,
          ambient_temp_c: ds[5].data[i] ?? null,
          cabinet_temp_c: ds[6].data[i] ?? null,
          mode_name: modeData[i] ?? null,
          self_reserve_pct: selfRes[i] ?? null,
          tou_reserve_pct: touRes[i] ?? null,
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

    // ── Chart Modal ─────────────────────────────────────────
    openChartModal() {
      this.showChartModal = true;
      this.$nextTick(() => {
        requestAnimationFrame(() => this._createModalChart());
      });
    },

    closeChartModal() {
      this.showChartModal = false;
      if (_modalChart) {
        _modalChart.destroy();
        _modalChart = null;
      }
    },

    // ── Multi-gateway Compare Modal ──────────────────────────
    openCompareModal() {
      this.showCompareModal = true;
      this.$nextTick(() => requestAnimationFrame(() => this._loadCompareCharts()));
    },

    closeCompareModal() {
      this.showCompareModal = false;
      for (const c of this._compareCharts) { try { c.destroy(); } catch (_) {} }
      this._compareCharts = [];
    },

    // ── Events History (persistent card + Expand modal, shared state) ────
    // The card is the source of truth; the modal just displays the same
    // eventsData/eventsRange larger — see openEventsModal().
    openEventsModal() {
      this.showEventsModal = true;
      // Card already loads on init/range-change; this is just a safety
      // refresh in case the card was hidden (cardVisible.eventsHistory off).
      this.loadEvents();
    },

    closeEventsModal() {
      this.showEventsModal = false;
    },

    setEventsRange(range) {
      this.eventsRange = range;
      if (range !== 'custom') localStorage.setItem('fwh-events-range', range);
      this.showEventsDateRange = false;  // close date picker when selecting preset
      this.loadEvents();
    },

    async loadEventsDateRange() {
      if (!this.eventsDateStart || !this.eventsDateEnd) {
        Alpine.store('app').toast('Select both start and end dates', 'error');
        return;
      }
      const startTs = new Date(this.eventsDateStart).getTime() / 1000;
      const endTs = new Date(this.eventsDateEnd).getTime() / 1000;
      if (endTs <= startTs) {
        Alpine.store('app').toast('End date must be after start date', 'error');
        return;
      }
      // Cap to 90 days, matching the chart's own custom-range cap.
      if ((endTs - startTs) > 90 * 86400) {
        Alpine.store('app').toast('Date range cannot exceed 90 days', 'error');
        return;
      }
      this.eventsRange = 'custom';
      await this.loadEvents();
    },

    async loadEvents() {
      this.eventsLoading = true;
      try {
        const activeGw = Alpine.store('app')?.activeGateway;
        let url;
        if (this.eventsRange === 'custom') {
          const startTs = new Date(this.eventsDateStart).getTime() / 1000;
          const endTs = new Date(this.eventsDateEnd).getTime() / 1000;
          url = `api/events?start=${startTs}&end=${endTs}`;
        } else {
          url = `api/events?range=${this.eventsRange}`;
        }
        if (activeGw && activeGw !== 'site') url += `&gateway_id=${encodeURIComponent(activeGw)}`;
        const resp = await fetchJSON(url);
        this.eventsData = resp?.events || [];
      } catch (e) {
        console.error('Failed to load events', e);
        this.eventsData = [];
      } finally {
        this.eventsLoading = false;
      }
    },

    formatDuration(seconds) {
      if (seconds == null) return '—';
      const s = Math.max(0, Math.round(seconds));
      const h = Math.floor(s / 3600);
      const m = Math.floor((s % 3600) / 60);
      const sec = s % 60;
      return `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
    },

    formatEventTime(ts) {
      if (!ts) return '';
      return new Date(ts * 1000).toLocaleString();
    },

    severityChipClass(severity) {
      switch (severity) {
        case 'fault':   return 'error';
        case 'warning': return 'warn';
        case 'info':    return 'muted';
        default:        return 'muted';
      }
    },

    eventSourceLabel(source) {
      return EVENT_SOURCE_LABELS[source] || source || '—';
    },

    eventSeverityLabel(severity) {
      return ALARM_SEVERITY_LABELS[severity] || severity || '—';
    },

    toggleEventsSeverityFilter(severity) {
      this.eventsSeverityFilter = this.eventsSeverityFilter === severity ? '' : severity;
    },

    // ── Events filtering (severity + source) ──────────────────
    // Severity counts double as both filter chips AND the answer to "what
    // severities exist" -- the chip labels are the legend, no guessing
    // required. Only counts what's actually in the current eventsData, like
    // the Logs tab's per-level summary.
    get eventsSeverityCounts() {
      const counts = { fault: 0, warning: 0, info: 0 };
      for (const ev of this.eventsData) {
        if (ev.severity in counts) counts[ev.severity]++;
      }
      return counts;
    },

    get availableEventSources() {
      const seen = new Set();
      for (const ev of this.eventsData) {
        if (ev.source) seen.add(ev.source);
      }
      return [...seen].sort();
    },

    get filteredEventsData() {
      let result = this.eventsData;
      if (this.eventsSeverityFilter) {
        result = result.filter(ev => ev.severity === this.eventsSeverityFilter);
      }
      if (this.eventsSourceFilter) {
        result = result.filter(ev => ev.source === this.eventsSourceFilter);
      }
      return result;
    },

    async _loadCompareCharts() {
      const gateways = Alpine.store('app').gatewayList.filter(g => g.enabled);
      if (!gateways.length) return;

      // Parallel fetch for real gateways; synthetic generation for mock gateways
      const range = this.chartRange === 'live' ? '30m' : this.chartRange;
      const nowTs = Date.now() / 1000;
      const rangeSecs = RANGE_SECS[range] || 1800;
      const results = await Promise.all(gateways.map(async gw => {
        if (gw.mock) {
          const pts = mockSyntheticSeries(gw.id, nowTs, rangeSecs, Math.min(180, Math.floor(rangeSecs / 10)));
          return { gw, data: { points: pts } };
        }
        let url = `api/metrics?range=${range}`;
        if (this.chartBucket) url += `&bucket=${this.chartBucket}`;
        url += `&gateway_id=${encodeURIComponent(gw.id)}`;
        const data = await fetchJSON(url).catch(() => null);
        return { gw, data };
      }));

      // Compute shared Y-axis range across all gateways
      let maxW = 5000;
      for (const { data } of results) {
        if (!data?.points?.length) continue;
        for (const p of data.points) {
          const vals = [Math.abs(p.battery_w||0), Math.abs(p.grid_w||0), p.solar_w||0, p.home_w||0];
          const m = Math.max(...vals);
          if (m > maxW) maxW = m;
      }}
      maxW = Math.ceil(maxW / 1000) * 1000;

      // Destroy any prior charts
      for (const c of this._compareCharts) { try { c.destroy(); } catch (_) {} }
      this._compareCharts = [];

      // Create one chart per gateway
      for (const { gw, data } of results) {
        const canvasId = `compare-chart-${gw.id.replace(/[^a-z0-9]/gi, '_')}`;
        const ctx = document.getElementById(canvasId);
        if (!ctx) continue;
        const pts = data?.points || [];
        const labels  = pts.map(p => this._formatChartLabel(p.ts));
        const battery = pts.map(p => p.battery_w);
        const grid    = pts.map(p => p.grid_w);
        const solar   = pts.map(p => p.solar_w);
        const soc     = pts.map(p => p.soc ?? null);
        const gridMode= pts.map(p => p.grid_mode ?? null);
        const modeArr = pts.map(p => p.mode_name ?? null);

        const chart = new Chart(ctx, {
          type: 'line',
          plugins: [modeBackgroundPlugin],
          data: {
            labels,
            datasets: [
              { label: 'Battery', data: battery, borderColor: '#06b6d4', backgroundColor: 'rgba(6,182,212,0.08)', borderWidth: 1.5, tension: 0.3, pointRadius: 0, fill: true },
              { label: 'Grid',    data: grid,    borderColor: '#ef4444', backgroundColor: 'transparent', borderWidth: 1.5, tension: 0.3, pointRadius: 0, fill: false },
              { label: 'Solar',   data: solar,   borderColor: '#f59e0b', backgroundColor: 'rgba(245,158,11,0.08)', borderWidth: 1.5, tension: 0.3, pointRadius: 0, fill: true },
              { label: 'SoC',     data: soc,     borderColor: '#84cc16', backgroundColor: 'transparent', borderWidth: 1.5, borderDash: [5,3], tension: 0.3, pointRadius: 0, fill: false, yAxisID: 'y2' },
              { label: 'Mode',    data: modeArr.map(m => (m && MODE_ABBR[m]) ? 0 : null), borderColor: 'transparent', backgroundColor: 'transparent', borderWidth: 0, pointRadius: 0, fill: false, yAxisID: 'yMode' },
            ],
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            plugins: {
              legend: { display: false },
              tooltip: {
                backgroundColor: '#1e293b', borderColor: 'rgba(255,255,255,0.1)', borderWidth: 1,
                titleColor: '#f8fafc', bodyColor: '#cbd5e1',
                filter: item => item.datasetIndex !== 4,
                callbacks: {
                  label: item => {
                    if (item.datasetIndex === 3) return `SoC: ${item.parsed.y?.toFixed(1)}%`;
                    return `${item.dataset.label}: ${(item.parsed.y/1000).toFixed(2)} kW`;
                  },
                },
              },
            },
            scales: {
              x: { ticks: { color: '#64748b', font: { size: 9 }, maxTicksLimit: 8 }, grid: { color: 'rgba(255,255,255,0.03)' } },
              y: { suggestedMin: -maxW, suggestedMax: maxW, ticks: { color: '#64748b', font: { size: 9 }, callback: v => (v/1000).toFixed(1)+'k' }, grid: { color: 'rgba(255,255,255,0.04)' } },
              y2: { position: 'right', display: soc.some(v => v != null), suggestedMin: 0, suggestedMax: 100, ticks: { color: '#64748b', font: { size: 8 }, callback: v => v+'%' }, grid: { drawOnChartArea: false } },
              yMode: { display: false, min: -1, max: 1 },
            },
          },
        });

        chart._modeData = modeArr;
        chart._gridModeData = gridMode;
        chart._tsRaw = pts.map(p => p.ts);
        chart._alarmData = [];
        this._compareCharts.push(chart);
      }
    },

    _createModalChart() {
      const ctx = this.$refs.modalChart;
      if (!ctx) return;

      const rect = ctx.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) {
        requestAnimationFrame(() => this._createModalChart());
        return;
      }

      if (_modalChart) { _modalChart.destroy(); _modalChart = null; }

      const maxRating = Alpine.store('app').points.max_discharge_rate_w || 5000;

      // Copy current data from main chart (or live buffer)
      let labels = [], battery = [], grid = [], solar = [], home = [];
      if (_chart && _chart.data.labels.length) {
        labels = [..._chart.data.labels];
        battery = [..._chart.data.datasets[0].data];
        grid = [..._chart.data.datasets[1].data];
        solar = [..._chart.data.datasets[2].data];
        home = [..._chart.data.datasets[3].data];
      }

      _modalChart = new Chart(ctx, {
        type: 'line',
        plugins: [modeBackgroundPlugin],
        data: {
          labels,
          datasets: [
            { label: 'Battery', data: battery, borderColor: '#06b6d4', backgroundColor: 'rgba(6, 182, 212, 0.1)', borderWidth: 2, tension: 0.3, pointRadius: 0, fill: true },
            { label: 'Grid', data: grid, borderColor: '#ef4444', backgroundColor: 'rgba(239, 68, 68, 0.05)', borderWidth: 2, tension: 0.3, pointRadius: 0, fill: false },
            { label: 'Solar', data: solar, borderColor: '#f59e0b', backgroundColor: 'rgba(245, 158, 11, 0.1)', borderWidth: 2, tension: 0.3, pointRadius: 0, fill: true },
            { label: 'Home', data: home, borderColor: '#8b5cf6', backgroundColor: 'rgba(139, 92, 246, 0.05)', borderWidth: 2, tension: 0.3, pointRadius: 0, fill: false },
            { label: 'SoC', data: [], borderColor: '#84cc16', backgroundColor: 'transparent', borderWidth: 2, borderDash: [6,3], tension: 0.3, pointRadius: 0, fill: false, yAxisID: 'y2' },
            { label: 'Ambient', data: [], borderColor: '#e879f9', backgroundColor: 'transparent', borderWidth: 2, borderDash: [6,3], tension: 0.3, pointRadius: 0, fill: false, yAxisID: 'y2' },
            { label: 'Cabinet', data: [], borderColor: '#facc15', backgroundColor: 'transparent', borderWidth: 2, borderDash: [6,3], tension: 0.3, pointRadius: 0, fill: false, yAxisID: 'y2' },
            { label: 'Mode', data: [], borderColor: 'transparent', backgroundColor: 'transparent', borderWidth: 0, pointRadius: 0, fill: false, yAxisID: 'yMode' },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: true,
          aspectRatio: 2.2,
          animation: { duration: 300 },
          interaction: { mode: 'index', intersect: false },
          plugins: {
            legend: {
              position: 'top',
              labels: {
                color: '#94a3b8', font: { size: 12 }, boxWidth: 14, padding: 20,
                filter: (item) => item.datasetIndex !== 7 && (item.datasetIndex < 4 || _modalChart?.data?.datasets[item.datasetIndex]?.data?.length > 0),
              },
            },
            tooltip: {
              backgroundColor: '#1e293b',
              borderColor: 'rgba(255,255,255,0.1)',
              borderWidth: 1,
              titleColor: '#f8fafc',
              bodyColor: '#cbd5e1',
              titleFont: { size: 13 },
              bodyFont: { size: 12 },
              filter: (item) => {
                if (item.datasetIndex === 7) {
                  const mode = item.chart._modeData?.[item.dataIndex];
                  return !!(mode && MODE_ABBR[mode]);
                }
                return true;
              },
              callbacks: {
                label: (item) => {
                  if (item.datasetIndex === 7) {
                    const mode = item.chart._modeData?.[item.dataIndex];
                    return MODE_ABBR[mode] || mode;
                  }
                  if (item.datasetIndex >= 4) {
                    const isSoc = item.dataset.label === 'SoC';
                    const unit = isSoc ? '%' : '°C';
                    return `${item.dataset.label}: ${item.parsed.y?.toFixed(1)}${unit}`;
                  }
                  return `${item.dataset.label}: ${(item.parsed.y / 1000).toFixed(2)} kW`;
                },
                labelColor: (item) => {
                  if (item.datasetIndex === 7) {
                    const mode = item.chart._modeData?.[item.dataIndex];
                    const color = MODE_LEGEND_COLORS[mode] || '#64748b';
                    return { borderColor: color, backgroundColor: color };
                  }
                },
              },
            },
          },
          scales: {
            x: {
              ticks: { color: '#64748b', font: { size: 11 }, maxTicksLimit: 15 },
              grid: { color: 'rgba(255,255,255,0.04)' },
            },
            y: {
              suggestedMin: -maxRating,
              suggestedMax: maxRating,
              ticks: {
                color: '#64748b',
                font: { size: 11 },
                callback: (v) => (v / 1000).toFixed(1) + ' kW',
              },
              grid: { color: 'rgba(255,255,255,0.04)' },
            },
            y2: {
              position: 'right',
              display: false,
              suggestedMin: 0,
              suggestedMax: 100,
              ticks: { color: '#64748b', font: { size: 10 }, callback: (v) => v },
              grid: { drawOnChartArea: false },
            },
            yMode: { display: false, min: -1, max: 1 },
          },
        },
      });
    },

    _updateModalChart(labels, battery, grid, solar, home, soc = [], ambient = [], cabinet = [], mode = [], selfReserve = [], touReserve = [], gridMode = [], alarmEvents = [], tsRaw = []) {
      if (!_modalChart) return;
      _modalChart._modeData = mode;
      _modalChart._selfReserveData = selfReserve;
      _modalChart._touReserveData = touReserve;
      _modalChart._gridModeData = gridMode;
      _modalChart._alarmData = alarmEvents;
      _modalChart._tsRaw = tsRaw;
      _modalChart.data.labels = labels;
      _modalChart.data.datasets[0].data = battery;
      _modalChart.data.datasets[1].data = grid;
      _modalChart.data.datasets[2].data = solar;
      _modalChart.data.datasets[3].data = home;
      const socData2 = this.showSocOverlay     ? soc     : [];
      const ambData2 = this.showAmbientOverlay ? ambient : [];
      const cabData2 = this.showCabinetOverlay ? cabinet : [];
      _modalChart.data.datasets[4].data = socData2;
      _modalChart.data.datasets[5].data = ambData2;
      _modalChart.data.datasets[6].data = cabData2;
      _modalChart.data.datasets[7].data = mode.map(m => (m && MODE_ABBR[m]) ? 0 : null);

      const yScale = _modalChart.options?.scales?.y;
      if (yScale) {
        const maxRating = Alpine.store('app').points.max_discharge_rate_w || 5000;
        yScale.suggestedMin = -maxRating;
        yScale.suggestedMax = maxRating;
      }
      const y2 = _modalChart.options?.scales?.y2;
      if (y2) {
        const hasData2 = socData2.some(v => v != null) || ambData2.some(v => v != null) || cabData2.some(v => v != null);
        y2.display = hasData2;
      }

      _modalChart.resize();
      _modalChart.update();
    },

    // Diagnostics computed property
    get diagnosticItems() {
      const pts = Alpine.store('app').points;
      const s = Alpine.store('app').showSources;
      const fmt_val = (v, unit) => v != null ? v + ' ' + unit : '--';
      const lbl = (name, key) => s ? name + ' (' + (POINT_SOURCES[key] || '') + ')' : name;
      return [
        { label: lbl('Ambient Temperature', 'ambient_temp_c'), value: fmt_val(pts.ambient_temp_c, '°C') },
        { label: lbl('Cabinet Temperature', 'cabinet_temp_c'), value: fmt_val(pts.cabinet_temp_c, '°C') },
        { label: lbl('Command Elapsed Time', 'command_elapsed_s'), value: fmt_val(pts.command_elapsed_s, 's') },
        { label: lbl('Control Mode', 'loc_rem_ctl_name'), value: pts.loc_rem_ctl_name || '--' },
        { label: lbl('Grid Connection', 'connection_state'), value: pts.connection_state || '--' },
        { label: lbl('Grid Frequency', 'frequency_hz'), value: fmt_val(pts.frequency_hz, 'Hz') },
        { label: lbl('HW Revert Remaining', 'wset_revert_remain_s'), value: fmt_val(pts.wset_revert_remain_s, 's') },
        { label: lbl('HW Revert Timer', 'wset_revert_time_s'), value: fmt_val(pts.wset_revert_time_s, 's') },
        { label: lbl('Inverter State', 'inverter_state'), value: pts.inverter_state || '--' },
        { label: lbl('Last Command Result', 'last_command_result'), value: pts.last_command_result || '--' },
        { label: lbl('Max Charge Rate', 'max_charge_rate_w'), value: pts.max_charge_rate_w != null ? (pts.max_charge_rate_w / 1000).toFixed(2) + ' kW' : '--' },
        { label: lbl('Max Discharge Rate', 'max_discharge_rate_w'), value: pts.max_discharge_rate_w != null ? (pts.max_discharge_rate_w / 1000).toFixed(2) + ' kW' : '--' },
        { label: lbl('Operating Mode', 'mode_name'), value: pts.mode_name || '--' },
        { label: lbl('Power Setpoint', 'wset_watts'), value: pts.wset_watts != null ? (pts.wset_watts / 1000).toFixed(2) + ' kW' : '--' },
        { label: lbl('Power Setpoint %', 'wset_pct'), value: pts.wset_pct != null ? pts.wset_pct + '%' : '--' },
        { label: lbl('SW Watchdog Remaining', 'sw_watchdog_remain_s'), value: fmt_val(pts.sw_watchdog_remain_s, 's') },
        { label: lbl('Total Capacity', 'wh_rating'), value: pts.wh_rating != null ? (pts.wh_rating / 1000).toFixed(3) + ' kWh' : '--' },
        { label: lbl('WSet Enabled', 'wset_enabled'), value: pts.wset_enabled != null ? (pts.wset_enabled ? 'On' : 'Off') : '--' },
        { label: lbl('WSet Mode', 'wset_mode_name'), value: pts.wset_mode_name || '--' },
      ];
    },
  };
}
