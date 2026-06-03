/**
 * Live Points Panel — configurable real-time model point display
 *
 * Inline dashboard component. User selects which points to display;
 * defaults to a curated set of essential readings. Refreshes every 10s
 * via the global Alpine store.
 */
function livePointsPanel() {
  // Storage key for persisting user selection
  const STORAGE_KEY = 'franklinwh_live_points';

  // All available point definitions: key → display metadata
  const POINT_CATALOG = {
    // ── Core status ──
    soc:                { label: 'State of Charge',      unit: '%',   group: 'Battery',  default: true },
    battery_state:      { label: 'Battery State',        unit: '',    group: 'Battery',  default: true },
    battery_power_w:    { label: 'Battery Power',        unit: 'W',   group: 'Battery',  default: true },
    battery_dc_power_w: { label: 'Battery DC Power',     unit: 'W',   group: 'Battery',  default: false },
    battery_current_a:  { label: 'Battery Current',      unit: 'A',   group: 'Battery',  default: false },
    total_solar:        { label: 'Total Solar',          unit: 'W',   group: 'Solar',    default: true },
    pv_proximal:        { label: 'Solar Proximal',       unit: 'W',   group: 'Solar',    default: false },
    pv_remote1:         { label: 'Solar Remote 1',       unit: 'W',   group: 'Solar',    default: false },
    pv_remote2:         { label: 'Solar Remote 2',       unit: 'W',   group: 'Solar',    default: false },
    home_load_ext:      { label: 'Home Load',            unit: 'W',   group: 'Load',     default: true },
    grid_power_w:       { label: 'Grid Power',           unit: 'W',   group: 'Grid',     default: true },
    grid_mode:          { label: 'Grid Status',          unit: '',    group: 'Grid',     default: true },
    inverter_state:     { label: 'Inverter State',       unit: '',    group: 'Inverter', default: true },
    connection_state:   { label: 'Connection State',     unit: '',    group: 'Grid',     default: false },

    // ── AC Electrical ──
    voltage_v:          { label: 'Voltage',              unit: 'V',   group: 'AC',       default: false },
    current_a:          { label: 'Current',              unit: 'A',   group: 'AC',       default: false },
    frequency_hz:       { label: 'Frequency',            unit: 'Hz',  group: 'AC',       default: false },
    power_factor:       { label: 'Power Factor',         unit: '',    group: 'AC',       default: false },
    grid_va:            { label: 'Apparent Power',       unit: 'VA',  group: 'AC',       default: false },
    grid_var:           { label: 'Reactive Power',       unit: 'VAR', group: 'AC',       default: false },

    // ── Control ──
    mode_name:             { label: 'Operating Mode',       unit: '',    group: 'Control',  default: false },
    wset_enabled:          { label: 'WSet Enabled',         unit: '',    group: 'Control',  default: false },
    wset_mode_name:        { label: 'WSet Mode',            unit: '',    group: 'Control',  default: false },
    wset_pct:              { label: 'Power Setpoint %',     unit: '%',   group: 'Control',  default: false },
    wset_watts:            { label: 'Power Setpoint',       unit: 'W',   group: 'Control',  default: false },
    sw_watchdog_remain_s:  { label: 'Watchdog Remaining',   unit: 's',   group: 'Control',  default: false },
    loc_rem_ctl_name:      { label: 'Control Mode',         unit: '',    group: 'Control',  default: false },
    wset_revert_time_s:    { label: 'HW Revert Timer',      unit: 's',   group: 'Control',  default: false },
    wset_revert_remain_s:  { label: 'HW Revert Remain',     unit: 's',   group: 'Control',  default: false },

    // ── Health ──
    soh:                  { label: 'State of Health',      unit: '%',   group: 'Battery',  default: false },
    ambient_temp_c:       { label: 'Ambient Temp',         unit: '°C',  group: 'Health',   default: false },
    cabinet_temp_c:       { label: 'Cabinet Temp',         unit: '°C',  group: 'Health',   default: false },
    wh_available:         { label: 'Energy Available',     unit: 'Wh',  group: 'Battery',  default: false },
    wh_rating:            { label: 'Total Capacity',       unit: 'Wh',  group: 'Battery',  default: false },
    max_charge_rate_w:    { label: 'Max Charge Rate',      unit: 'W',   group: 'Battery',  default: false },
    max_discharge_rate_w: { label: 'Max Discharge Rate',   unit: 'W',   group: 'Battery',  default: false },
    self_reserve_pct:     { label: 'Self Reserve',         unit: '%',   group: 'Control',  default: false },

    // ── Energy totals ──
    pv_energy_total_wh:       { label: 'PV Energy Total',       unit: 'Wh', group: 'Energy', default: false },
    dc_energy_discharged_wh:  { label: 'Energy Discharged',     unit: 'Wh', group: 'Energy', default: false },
    dc_energy_charged_wh:     { label: 'Energy Charged',        unit: 'Wh', group: 'Energy', default: false },
    grid_export_wh:           { label: 'Grid Exported',         unit: 'Wh', group: 'Energy', default: false },
    grid_import_wh:           { label: 'Grid Imported',         unit: 'Wh', group: 'Energy', default: false },
  };

  // Get unique groups in order
  const groups = [];
  const seen = new Set();
  for (const def of Object.values(POINT_CATALOG)) {
    if (!seen.has(def.group)) { seen.add(def.group); groups.push(def.group); }
  }

  return {
    expanded: true,
    showConfig: false,
    selectedPoints: [],
    filterText: '',

    init() {
      this._loadSelection();
    },

    _loadSelection() {
      try {
        const stored = localStorage.getItem(STORAGE_KEY);
        if (stored) {
          const parsed = JSON.parse(stored);
          // Validate keys still exist
          this.selectedPoints = parsed.filter(k => POINT_CATALOG[k]);
          if (this.selectedPoints.length > 0) return;
        }
      } catch (_) {}
      // Defaults
      this.selectedPoints = Object.keys(POINT_CATALOG).filter(k => POINT_CATALOG[k].default);
    },

    _saveSelection() {
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(this.selectedPoints));
      } catch (_) {}
    },

    togglePoint(key) {
      const idx = this.selectedPoints.indexOf(key);
      if (idx >= 0) {
        this.selectedPoints.splice(idx, 1);
      } else {
        this.selectedPoints.push(key);
      }
      this._saveSelection();
    },

    isSelected(key) {
      return this.selectedPoints.includes(key);
    },

    resetDefaults() {
      this.selectedPoints = Object.keys(POINT_CATALOG).filter(k => POINT_CATALOG[k].default);
      this._saveSelection();
    },

    get groups() { return groups; },

    pointsForGroup(group) {
      const filter = this.filterText.toLowerCase();
      return Object.entries(POINT_CATALOG)
        .filter(([k, d]) => d.group === group)
        .filter(([k, d]) => !filter || d.label.toLowerCase().includes(filter) || k.includes(filter));
    },

    // Active points for display
    get activePoints() {
      const pts = Alpine.store('app').points;
      return this.selectedPoints
        .filter(k => POINT_CATALOG[k])
        .map(k => {
          const def = POINT_CATALOG[k];
          const raw = pts[k];
          return {
            key: k,
            label: def.label,
            unit: def.unit,
            group: def.group,
            value: this._formatValue(k, raw, def),
            colorClass: this._colorClass(k, raw),
            source: POINT_SOURCES[k] || '',
          };
        });
    },

    _formatValue(key, raw, def) {
      if (raw == null) return '--';
      // Large watt-hour values → kWh or MWh
      if (def.unit === 'Wh') {
        if (Math.abs(raw) >= 1000000) return (raw / 1000000).toFixed(2) + ' MWh';
        if (Math.abs(raw) >= 1000) return (raw / 1000).toFixed(1) + ' kWh';
        return raw + ' Wh';
      }
      // Watt values → format with kW for large
      if (def.unit === 'W') {
        if (Math.abs(raw) >= 10000) return (raw / 1000).toFixed(1) + ' kW';
        return Math.round(raw) + ' W';
      }
      // WSet enabled — show On/Off (M704.WSetEna: 0=Off, 1=Enabled)
      if (key === 'wset_enabled') {
        return raw ? 'On' : 'Off';
      }
      // WSet mode — already a string from wset_mode_name derivation
      if (key === 'wset_mode_name') {
        return raw || '--';
      }
      // Default: value + unit
      return raw + (def.unit ? ' ' + def.unit : '');
    },

    _colorClass(key, raw) {
      if (raw == null) return 'text-slate-600';

      // Battery power: positive = discharge (amber), negative = charge (green)
      if (key === 'battery_power_w' || key === 'battery_dc_power_w') {
        return raw > 0 ? 'text-amber-400' : raw < 0 ? 'text-emerald-400' : 'text-slate-400';
      }
      // Grid power: positive = import (red), negative = export (green)
      if (key === 'grid_power_w') {
        return raw > 0 ? 'text-red-400' : raw < 0 ? 'text-emerald-400' : 'text-slate-400';
      }
      // Solar: positive = generating (amber)
      if (key === 'total_solar' || key === 'pv_proximal' || key === 'pv_remote1' || key === 'pv_remote2') {
        return raw > 0 ? 'text-amber-400' : 'text-slate-400';
      }
      // SOC color
      if (key === 'soc') {
        return raw >= 80 ? 'text-emerald-400' : raw >= 30 ? 'text-cyan-400' : raw >= 15 ? 'text-amber-400' : 'text-red-400';
      }
      // Battery state
      if (key === 'battery_state') {
        const s = String(raw).toLowerCase();
        if (s.includes('charg') && !s.includes('discharg')) return 'text-emerald-400';
        if (s.includes('discharg')) return 'text-amber-400';
        return 'text-slate-400';
      }
      // Grid mode
      if (key === 'grid_mode') {
        return String(raw).includes('Forming') ? 'text-amber-400' : 'text-emerald-400';
      }
      // Inverter
      if (key === 'inverter_state') {
        return raw === 'Running' ? 'text-emerald-400' : raw === 'Fault' ? 'text-red-400' : 'text-amber-400';
      }

      return 'text-slate-200';
    },

    get selectedCount() {
      return this.selectedPoints.length;
    },
  };
}
