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

const VENDOR_EXT_16000 = [
  { addr: 16000, name: 'HomeLoadHiRes', desc: 'Home Load (High Resolution)', keys: ['vreg_16000'], unit: 'W', access: 'R', type: 'uint16' },
  { addr: 16001, name: 'Reg16001',      desc: 'Unknown',                     keys: ['vreg_16001'], unit: '',  access: 'R', type: 'uint16' },
  { addr: 16002, name: 'Reg16002',      desc: 'Unknown',                     keys: ['vreg_16002'], unit: '',  access: 'R', type: 'uint16' },
];

// PICS status codes and cycle order
const PICS_CODES = ['U', 'S', 'T', 'F', 'X'];
const PICS_LABELS = { U: 'Unimplemented', S: 'Supported', T: 'Tested', F: 'Failed', X: 'N/A' };

// Default layout constants for explorer
const EXPLORER_DEFAULTS = { treeWidth: 256, extHeight: 280 };
const EXPLORER_LAYOUT_KEY = 'fwh-layout-explorer';

// Last-known extension register values, keyed by address. Non-reactive on
// purpose: the vendorExtensions getter records values here without triggering
// re-render. Extension registers (raw 15000-range + 15510/16000) aren't in
// every poll, so a poll that omits one would otherwise read null and make the
// row flicker out under "Hide zeros". Falling back to the last value keeps it
// stable. A genuine 0 is still recorded (and hidden by Hide zeros); only an
// absent (null) reading falls back.
const EXT_VALUE_CACHE = {};

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

    // Point filters (model points table): '' = all
    accessFilter: '',  // '' | 'R' | 'RW'
    picsFilter: '',    // '' | 'U' | 'S' | 'T' | 'F' | 'X'

    // PICS compliance state: { "model_id:point_name": "S"|"T"|"F"|"U"|"X" }
    picsData: {},

    // On-demand model read state
    readingModel: false,

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

      await Promise.all([this.loadGateway(), this.loadModels(), this.loadPicsData()]);
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

    async readModel() {
      if (!this.selectedModelId || this.readingModel) return;
      this.readingModel = true;
      try {
        const data = await fetchJSON(`api/models/${this.selectedModelId}/read`, { method: 'POST' });
        if (data && !data.error) {
          // Refresh points so the values appear
          await Alpine.store('app').refresh();
          Alpine.store('app').toast(
            `Model ${this.selectedModelId}: ${data.points_read} points read`,
            'info'
          );
        } else {
          Alpine.store('app').toast(
            'Read failed: ' + (data?.detail || data?.error || 'unknown'),
            'error'
          );
        }
      } catch (e) {
        Alpine.store('app').toast('Read failed: ' + e.message, 'error');
      } finally {
        this.readingModel = false;
      }
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

    modelHasValues(model) {
      // Check if any point in this model has a live value
      const pts = Alpine.store('app').points;
      for (const pt of model.points) {
        const mqKey = `${model.model_id}.${pt.name}`;
        if (pts[mqKey] !== undefined) return true;
      }
      return false;
    },

    get selectedModelValueCount() {
      if (!this.selectedModel) return 0;
      const pts = Alpine.store('app').points;
      let count = 0;
      for (const pt of this.selectedModel.points) {
        const mqKey = `${this.selectedModelId}.${pt.name}`;
        if (pts[mqKey] !== undefined) count++;
      }
      return count;
    },

    get selectedModelPoints() {
      if (!this.selectedModel) return [];
      const sortByAddr = (pts) => [...pts].sort((a, b) => (a.address ?? 0) - (b.address ?? 0));
      const m = this.selectedModel;
      let pts = m.points;

      // Access filter (R / RW)
      if (this.accessFilter) {
        pts = pts.filter(p => (p.access || 'R') === this.accessFilter);
      }
      // PICS filter (U / S / T / F / X)
      if (this.picsFilter) {
        pts = pts.filter(p => this.getPicsStatus(p.name) === this.picsFilter);
      }

      // Text filter — unless it matches the model itself (then keep all points
      // that passed the access/PICS filters).
      const q = this.filter.toLowerCase().trim();
      if (q) {
        const modelMatch =
          ('m' + m.model_id).includes(q) || String(m.model_id).includes(q) ||
          (m.label || '').toLowerCase().includes(q);
        if (!modelMatch) {
          pts = pts.filter(
            p =>
              p.name.toLowerCase().includes(q) ||
              (p.label || '').toLowerCase().includes(q) ||
              (p.unit || '').toLowerCase().includes(q)
          );
        }
      }
      return sortByAddr(pts);
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

      const ptype = (pt.type || '').toLowerCase();

      // Bitfield expansion: show decimal + hex + set bit names
      if (ptype.startsWith('bitfield') && pt.symbols && typeof val === 'number') {
        const bits = [];
        for (const [bitStr, name] of Object.entries(pt.symbols)) {
          const bit = parseInt(bitStr, 10);
          if (!isNaN(bit) && (val & (1 << bit))) {
            bits.push(name);
          }
        }
        const hex = '0x' + (val >>> 0).toString(16).toUpperCase();
        if (bits.length > 0) {
          return val + ' (' + hex + ') [' + bits.join(', ') + ']';
        }
        return val + ' (' + hex + ')';
      }

      // Resolve enum symbol if available
      if (ptype === 'enum16' && pt.symbols && pt.symbols[String(val)]) {
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

    // ── Symbols tooltip ───────────────────────────────────

    getSymbolsTooltip(pt) {
      if (!pt.symbols) return '';
      const ptype = (pt.type || '').toLowerCase();
      const entries = Object.entries(pt.symbols);
      if (entries.length === 0) return '';

      if (ptype.startsWith('bitfield')) {
        return entries
          .sort((a, b) => parseInt(a[0]) - parseInt(b[0]))
          .map(([bit, name]) => `Bit ${bit}: ${name}`)
          .join('\n');
      }
      // enum16
      return entries
        .sort((a, b) => parseInt(a[0]) - parseInt(b[0]))
        .map(([val, name]) => `${val} = ${name}`)
        .join('\n');
    },

    // ── PICS compliance ───────────────────────────────────

    _picsKey(pointName) {
      return this.selectedModelId != null ? `${this.selectedModelId}:${pointName}` : null;
    },

    getPicsStatus(pointName) {
      const key = this._picsKey(pointName);
      if (!key) return 'U';
      // Explicit DB override takes priority
      if (this.picsData[key]) return this.picsData[key];
      // Auto-detect: if we have a live value, it's at least Supported
      if (this.getPointValue(pointName) != null) return 'S';
      return 'U';
    },

    picsStatusClass(pointName) {
      const s = this.getPicsStatus(pointName);
      switch (s) {
        case 'S': return 'bg-emerald-500/20 text-emerald-400';
        case 'T': return 'bg-cyan-500/20 text-cyan-400';
        case 'F': return 'bg-red-500/20 text-red-400';
        case 'X': return 'bg-slate-700/50 text-slate-500';
        default:  return 'bg-amber-500/15 text-amber-400/60';
      }
    },

    picsStatusTitle(pointName) {
      const s = this.getPicsStatus(pointName);
      return PICS_LABELS[s] || 'Unknown';
    },

    async cyclePicsStatus(pointName) {
      const key = this._picsKey(pointName);
      if (!key) return;
      const current = this.getPicsStatus(pointName);
      const idx = PICS_CODES.indexOf(current);
      const next = PICS_CODES[(idx + 1) % PICS_CODES.length];
      this.picsData[key] = next;

      // Persist to backend
      await fetchJSON('api/pics', {
        method: 'PUT',
        body: JSON.stringify({
          model_id: this.selectedModelId,
          point_name: pointName,
          status: next,
        }),
      });
    },

    async loadPicsData() {
      const data = await fetchJSON('api/pics');
      if (data && data.pics) {
        const map = {};
        for (const row of data.pics) {
          map[`${row.model_id}:${row.point_name}`] = row.status;
        }
        this.picsData = map;
      }
    },

    // ── Vendor Extensions ─────────────────────────────────

    _resolveExtValue(keys) {
      const pts = Alpine.store('app').points;
      for (const k of keys) {
        if (pts[k] !== undefined && pts[k] !== null) return pts[k];
      }
      return null;
    },

    // Stabilise a register value: cache any live (non-null) reading and fall
    // back to the last cached value when this poll omitted the register, so
    // "Hide zeros" doesn't flicker rows in and out.
    _stableExt(addr, live) {
      if (live !== undefined && live !== null) {
        EXT_VALUE_CACHE[addr] = live;
        return live;
      }
      return EXT_VALUE_CACHE[addr] ?? null;
    },

    get vendorExtensions() {
      const pts = Alpine.store('app').points;
      const regs = [];

      // 15000-15039: Undocumented vendor range
      for (let a = 15000; a <= 15039; a++) {
        const value = this._stableExt(a, pts['vreg_' + a]);
        regs.push({
          addr: a,
          name: 'Reg' + a,
          desc: 'Unknown',
          value: value,
          type: 'uint16',
          access: 'R',
          unit: '',
          hex: value != null ? ('0000' + value.toString(16).toUpperCase()).slice(-4) : null,
        });
      }

      // 15500-15513: Documented FranklinWH extensions
      for (const def of VENDOR_EXT_15500) {
        const value = this._stableExt(def.addr, this._resolveExtValue(def.keys));
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

      // 16000-16002: High-resolution extension registers
      for (const def of VENDOR_EXT_16000) {
        const value = this._stableExt(def.addr, this._resolveExtValue(def.keys));
        let hex = null;
        if (value != null) {
          hex = ('0000' + (value & 0xFFFF).toString(16).toUpperCase()).slice(-4);
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
