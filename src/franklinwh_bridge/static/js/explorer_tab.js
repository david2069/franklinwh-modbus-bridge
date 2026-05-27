/**
 * SunSpec Explorer Tab — two-panel model tree + point detail
 * + FranklinWH vendor extension registers
 */

// ── Vendor Extension Register Definitions ──────────────────────
const VENDOR_EXT_15500 = [
  { addr: 15500, name: 'PVUse',             desc: 'PV Installed',                   keys: ['vreg_15500'],                         unit: '',   access: 'R',  type: 'uint16' },
  { addr: 15501, name: 'apBoxPVUse',        desc: 'Remote PV Installed',            keys: ['vreg_15501'],                         unit: '',   access: 'R',  type: 'uint16' },
  { addr: 15502, name: 'PVOutputP',         desc: 'PV Total Power',                 keys: ['pv_total', 'total_solar', 'vreg_15502'], unit: 'W',  access: 'R',  type: 'uint16' },
  { addr: 15503, name: 'proximalPVOutputP', desc: 'PV Proximal Power',              keys: ['pv_proximal', 'vreg_15503'],          unit: 'W',  access: 'R',  type: 'uint16' },
  { addr: 15504, name: 'Remote1PV',         desc: 'PV Remote 1 Power',              keys: ['pv_remote1', 'vreg_15504'],           unit: 'W',  access: 'R',  type: 'uint16' },
  { addr: 15505, name: 'Remote2PV',         desc: 'PV Remote 2 Power',              keys: ['pv_remote2', 'vreg_15505'],           unit: 'W',  access: 'R',  type: 'uint16' },
  { addr: 15506, name: 'LoadActiveP',       desc: 'Home Load',                      keys: ['home_load_ext_quantized', 'home_load_ext', 'vreg_15506'], unit: 'W', access: 'R', type: 'uint16' },
  { addr: 15507, name: 'OnGridMode',        desc: 'Operating Mode',                 keys: ['ongrid_mode', 'vreg_15507'],          unit: '',   access: 'RW', type: 'uint16',
    symbols: { 1: 'Emergency Backup', 2: 'Self-Consumption', 3: 'TOU' } },
  { addr: 15508, name: 'SelfReserve',       desc: 'Self-Consumption SOC Reserve',   keys: ['self_reserve_pct', 'vreg_15508'],     unit: '%',  access: 'RW', type: 'uint16' },
  { addr: 15509, name: 'TouReserve',        desc: 'TOU SOC Reserve',                keys: ['tou_reserve_pct', 'vreg_15509'],      unit: '%',  access: 'RW', type: 'uint16' },
  { addr: 15510, name: 'PVOutputWh',        desc: 'PV Energy Total',                keys: ['pv_energy_total_wh'],                 unit: 'Wh', access: 'R',  type: 'uint32', span: 2 },
  { addr: 15512, name: 'proximalOutputWh',  desc: 'PV Energy Proximal',             keys: ['pv_energy_proximal_wh'],              unit: 'Wh', access: 'R',  type: 'uint32', span: 2 },
];

// Default layout constants for explorer
const EXPLORER_DEFAULTS = { treeWidth: 256, extHeight: 280 };
const EXPLORER_LAYOUT_KEY = 'fwh-layout-explorer';

function explorerTab() {
  return {
    models: [],
    selectedModelId: null,
    filter: '',
    loading: true,
    refreshing: false,
    addrMode: 'sunspec', // 'sunspec' or 'base0'
    gateway: { host: '--', port: 502, unit_id: 1 },

    // Vendor extensions state
    extFilter: '',
    extSuppressZeros: true,

    // Resizable panel dimensions
    treeWidth: EXPLORER_DEFAULTS.treeWidth,
    extHeight: EXPLORER_DEFAULTS.extHeight,
    resizing: null,
    _startX: 0,
    _startY: 0,
    _startVal: 0,

    async init() {
      // Restore saved layout
      this._loadLayout();

      this._onMouseMove = (e) => this._handleResize(e);
      this._onMouseUp = () => this._stopResize();
      document.addEventListener('mousemove', this._onMouseMove);
      document.addEventListener('mouseup', this._onMouseUp);

      await Promise.all([this.loadGateway(), this.loadModels()]);
      // Force-refresh point values so they're populated immediately
      Alpine.store('app').refresh();
    },

    destroy() {
      document.removeEventListener('mousemove', this._onMouseMove);
      document.removeEventListener('mouseup', this._onMouseUp);
    },

    // ── Panel resize ──────────────────────────────────────

    startResize(which, e) {
      this.resizing = which;
      this._startX = e.clientX;
      this._startY = e.clientY;
      this._startVal = which === 'tree' ? this.treeWidth : this.extHeight;
      document.body.style.cursor = which === 'tree' ? 'col-resize' : 'row-resize';
      document.body.style.userSelect = 'none';
    },

    _handleResize(e) {
      if (!this.resizing) return;
      if (this.resizing === 'tree') {
        const dx = e.clientX - this._startX;
        this.treeWidth = Math.max(160, Math.min(500, this._startVal + dx));
      } else if (this.resizing === 'ext') {
        const dy = this._startY - e.clientY;
        this.extHeight = Math.max(100, Math.min(600, this._startVal + dy));
      }
    },

    _stopResize() {
      if (!this.resizing) return;
      this.resizing = null;
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      this._saveLayout();
    },

    _loadLayout() {
      try {
        const saved = JSON.parse(localStorage.getItem(EXPLORER_LAYOUT_KEY));
        if (saved) {
          if (saved.treeWidth) this.treeWidth = saved.treeWidth;
          if (saved.extHeight) this.extHeight = saved.extHeight;
        }
      } catch (_) {}
    },

    _saveLayout() {
      localStorage.setItem(EXPLORER_LAYOUT_KEY, JSON.stringify({
        treeWidth: this.treeWidth,
        extHeight: this.extHeight,
      }));
    },

    resetLayout() {
      this.treeWidth = EXPLORER_DEFAULTS.treeWidth;
      this.extHeight = EXPLORER_DEFAULTS.extHeight;
      localStorage.removeItem(EXPLORER_LAYOUT_KEY);
      Alpine.store('app').toast('Explorer layout reset', 'info');
    },

    async loadGateway() {
      const data = await fetchJSON('api/gateway');
      if (data && !data.error) {
        this.gateway = data;
      }
    },

    async loadModels() {
      this.loading = true;
      const data = await fetchJSON('api/models');
      if (data && data.models) {
        this.models = data.models;
        // Auto-select first model if none selected
        if (!this.selectedModelId && this.models.length > 0) {
          this.selectedModelId = this.models[0].model_id;
        }
      }
      this.loading = false;
    },

    async refreshCatalog() {
      this.refreshing = true;
      const data = await fetchJSON('api/models/refresh', { method: 'POST' });
      if (data && !data.error) {
        const msg = data.changes
          ? `Catalog updated: +${data.added_models?.length || 0} models`
          : 'Catalog unchanged';
        Alpine.store('app').toast(msg, 'info');
        await this.loadModels();
      } else {
        Alpine.store('app').toast(
          'Refresh failed: ' + (data?.error || 'unknown'),
          'error'
        );
      }
      this.refreshing = false;
    },

    selectModel(modelId) {
      this.selectedModelId = modelId;
    },

    // ── Computed properties ────────────────────────────────

    get modelCount() {
      return this.models.length;
    },

    get pointCount() {
      return this.models.reduce((n, m) => n + m.points.length, 0);
    },

    get filteredModels() {
      const q = this.filter.toLowerCase().trim();
      if (!q) return this.models;

      return this.models.filter(m => {
        const modelMatch =
          ('m' + m.model_id).includes(q) ||
          String(m.model_id).includes(q) ||
          (m.label || '').toLowerCase().includes(q);
        const pointMatch = m.points.some(
          p =>
            p.name.toLowerCase().includes(q) ||
            (p.label || '').toLowerCase().includes(q) ||
            (p.unit || '').toLowerCase().includes(q)
        );
        return modelMatch || pointMatch;
      });
    },

    get selectedModel() {
      if (!this.selectedModelId) return null;
      return this.models.find(m => m.model_id === this.selectedModelId) || null;
    },

    get selectedModelPoints() {
      if (!this.selectedModel) return [];
      const sortByAddr = (pts) => [...pts].sort((a, b) => (a.address ?? 0) - (b.address ?? 0));
      const q = this.filter.toLowerCase().trim();
      if (!q) return sortByAddr(this.selectedModel.points);

      // If filter is active and matches model-level, show all points
      const m = this.selectedModel;
      const modelMatch =
        ('m' + m.model_id).includes(q) || String(m.model_id).includes(q) ||
        (m.label || '').toLowerCase().includes(q);
      if (modelMatch) return sortByAddr(m.points);

      // Otherwise filter points
      return sortByAddr(m.points.filter(
        p =>
          p.name.toLowerCase().includes(q) ||
          (p.label || '').toLowerCase().includes(q) ||
          (p.unit || '').toLowerCase().includes(q)
      ));
    },

    // ── Value helpers ──────────────────────────────────────

    getPointValue(pointName) {
      const pts = Alpine.store('app').points;

      // 1. Model-qualified key: "{model_id}.{point_name}" (from raw model values)
      if (this.selectedModelId != null) {
        const mqKey = `${this.selectedModelId}.${pointName}`;
        if (pts[mqKey] !== undefined) return pts[mqKey];
      }

      // 2. Direct key match
      if (pts[pointName] !== undefined) return pts[pointName];

      // 3. snake_case fallback (SunSpec CamelCase → bridge snake_case)
      const snake = pointName
        .replace(/([A-Z])/g, '_$1')
        .toLowerCase()
        .replace(/^_/, '');
      if (pts[snake] !== undefined) return pts[snake];

      return null;
    },

    formatPointValue(pt) {
      const val = this.getPointValue(pt.name);
      if (val == null) return '--';

      // Resolve enum symbol if available
      if (pt.symbols && pt.symbols[String(val)]) {
        return val + ' (' + pt.symbols[String(val)] + ')';
      }
      // Format numbers reasonably
      if (typeof val === 'number') {
        return Number.isInteger(val) ? String(val) : val.toFixed(2);
      }
      return String(val);
    },

    formatAddress(addr) {
      if (addr == null) return '';
      // SunSpec addresses are 1-based (40001+), Base-0 are the raw register number
      if (this.addrMode === 'base0') return String(addr);
      return String(40001 + addr);
    },

    // ── Vendor Extensions ─────────────────────────────────

    _resolveExtValue(keys) {
      const pts = Alpine.store('app').points;
      for (const k of keys) {
        if (pts[k] !== undefined && pts[k] !== null) return pts[k];
      }
      return null;
    },

    get vendorExtensions() {
      const pts = Alpine.store('app').points;
      const regs = [];

      // 15000-15039: Undocumented vendor range
      for (let a = 15000; a <= 15039; a++) {
        regs.push({
          addr: a,
          name: 'Reg' + a,
          desc: 'Unknown',
          value: pts['vreg_' + a] ?? null,
          type: 'uint16',
          access: 'R',
          unit: '',
          hex: pts['vreg_' + a] != null ? ('0000' + pts['vreg_' + a].toString(16).toUpperCase()).slice(-4) : null,
        });
      }

      // 15500-15513: Documented FranklinWH extensions
      for (const def of VENDOR_EXT_15500) {
        const value = this._resolveExtValue(def.keys);
        let hex = null;
        if (value != null) {
          hex = def.type === 'uint32'
            ? ('00000000' + value.toString(16).toUpperCase()).slice(-8)
            : ('0000' + (value & 0xFFFF).toString(16).toUpperCase()).slice(-4);
        }
        regs.push({
          addr: def.addr,
          name: def.name,
          desc: def.desc,
          value: value,
          type: def.type,
          access: def.access,
          unit: def.unit || '',
          hex: hex,
          symbols: def.symbols,
          span: def.span,
        });
      }

      return regs;
    },

    get filteredExtensions() {
      let regs = this.vendorExtensions;

      // Suppress zeros
      if (this.extSuppressZeros) {
        regs = regs.filter(r => r.value != null && r.value !== 0);
      }

      // Search filter
      const q = (this.extFilter || '').toLowerCase().trim();
      if (q) {
        regs = regs.filter(r =>
          String(r.addr).includes(q) ||
          r.name.toLowerCase().includes(q) ||
          r.desc.toLowerCase().includes(q) ||
          (r.unit || '').toLowerCase().includes(q) ||
          (r.hex || '').toLowerCase().includes(q)
        );
      }

      return regs;
    },

    formatExtDisplay(reg) {
      if (reg.value == null) return '--';

      // Enum symbols
      if (reg.symbols && reg.symbols[String(reg.value)]) {
        return reg.value + ' (' + reg.symbols[String(reg.value)] + ')';
      }
      // Energy in Wh → kWh
      if (reg.unit === 'Wh' && reg.value > 1000) {
        return (reg.value / 1000).toFixed(1) + ' kWh';
      }
      // Append unit
      const v = typeof reg.value === 'number' && !Number.isInteger(reg.value)
        ? reg.value.toFixed(2) : String(reg.value);
      return reg.unit ? v + ' ' + reg.unit : v;
    },

    // ── Export ─────────────────────────────────────────────

    _downloadFile(filename, content, mimeType) {
      const blob = new Blob([content], { type: mimeType });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
      Alpine.store('app').toast('Exported: ' + filename, 'info');
    },

    exportSunSpecJSON() {
      const data = {};
      if (this.selectedModel) {
        const pts = this.selectedModelPoints.map(pt => ({
          name: pt.name,
          label: pt.label || '',
          address: pt.address,
          type: pt.type || '',
          unit: pt.unit || '',
          access: pt.access || 'R',
          value: this.getPointValue(pt.name),
        }));
        data.model_id = this.selectedModel.model_id;
        data.label = this.selectedModel.label;
        data.points = pts;
      }
      this._downloadFile(
        'sunspec_model_' + (this.selectedModel?.model_id || 'all') + '.json',
        JSON.stringify(data, null, 2),
        'application/json'
      );
    },

    exportSunSpecCSV() {
      const rows = [['Point', 'Label', 'Value', 'Type', 'Unit', 'Address', 'Access']];
      for (const pt of this.selectedModelPoints) {
        rows.push([
          pt.name,
          pt.label || '',
          this.getPointValue(pt.name) ?? '',
          pt.type || '',
          pt.unit || '',
          pt.address ?? '',
          pt.access || 'R',
        ]);
      }
      const csv = rows.map(r => r.map(c => '"' + String(c).replace(/"/g, '""') + '"').join(',')).join('\n');
      this._downloadFile(
        'sunspec_model_' + (this.selectedModel?.model_id || 'all') + '.csv',
        csv,
        'text/csv'
      );
    },

    exportExtJSON() {
      const data = this.filteredExtensions.map(r => ({
        address: r.addr,
        name: r.name,
        description: r.desc,
        value: r.value,
        hex: r.hex,
        type: r.type,
        access: r.access,
        unit: r.unit,
      }));
      this._downloadFile(
        'franklinwh_extensions.json',
        JSON.stringify(data, null, 2),
        'application/json'
      );
    },

    exportExtCSV() {
      const rows = [['Address', 'Name', 'Description', 'Value', 'Hex', 'Type', 'Access', 'Unit']];
      for (const r of this.filteredExtensions) {
        rows.push([r.addr, r.name, r.desc, r.value ?? '', r.hex || '', r.type, r.access, r.unit]);
      }
      const csv = rows.map(r => r.map(c => '"' + String(c).replace(/"/g, '""') + '"').join(',')).join('\n');
      this._downloadFile('franklinwh_extensions.csv', csv, 'text/csv');
    },
  };
}
