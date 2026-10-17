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
  // Per franklinwh-modbus QUIRKS "Register 16000": the block mirrors 15508/15509.
  // Undocumented by FranklinWH; one site's readings — labelled as such.
  { addr: 16001, name: 'SelfReserveMirror', desc: 'Self-Consumption reserve, mirrors 15508 (unconfirmed)', keys: ['vreg_16001'], unit: '%', access: 'R', type: 'uint16' },
  // 16002 is NOT the TOU reserve (#256): reference site 2026-10-17 read 20 with
  // the TOU reserve at 15 (FranklinWH, via Direct Connect). 20 is the local
  // API's 1405 BBBackupSoc (VPP backup SoC), as another site reported.
  { addr: 16002, name: 'BBBackupSoc',  desc: 'Backup SoC (BBBackupSoc, VPP?), not the TOU reserve (unconfirmed)', keys: ['vreg_16002'], unit: '%', access: 'R', type: 'uint16' },
];

// 15000-15039 is undocumented. These are what one site's recorded readings
// suggest (PR #73, #76, #77) — NOT FranklinWH documentation. Values stay raw;
// the description says what was observed so it can be tested, not trusted.
const VENDOR_EXT_15000_NOTES = {
  15013: { name: 'BatteryPowerRaw', unit: 'W',
           desc: 'Battery power at ~1 W (unconfirmed; one site). Sign while charging not established — raw 65269 seen = −267 if int16' },
  15016: { name: 'StatusRaw',
           desc: 'Status code (unconfirmed; one site): 2 on-grid, 6 with WSet active, 3 islanded' },
  15035: { name: 'SocX10Raw', unit: '‰',
           desc: 'State of charge ×10 (unconfirmed; one site)' },
};

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
    extKnown: '',  // '' all | 'known' (labelled from docs or notes) | 'unknown'

    // Point filters (model points table): '' = all
    accessFilter: '',  // '' | 'R' | 'RW'
    picsFilter: '',    // '' | 'U' | 'S' | 'T' | 'F' | 'X'

    // View toggles (persisted with layout)
    hideEmptyModels: false,  // hide models not actively polled (no live values)
    showExtCard: true,       // show the FranklinWH Extension Registers panel

    // PICS compliance state: { "model_id:point_name": "S"|"T"|"F"|"U"|"X" }
    picsData: {},

    // On-demand model read state
    readingModel: false,

    // Export modal (#228): GET api/models/export builds and redacts it.
    exp: {
      open: false, scope: 'model', chosen: [], format: 'json', redact: true,
      loading: false, error: '', result: null, msg: '',
    },

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

      // When a filter (search or hide-unpolled) narrows the model list and the
      // currently selected model drops out, jump to the first match so its
      // matching points show immediately. Without this the right pane keeps
      // the stale selection — e.g. searching "L1" while "Common" is selected
      // filters Common's points to none, looking like "nothing found".
      this.$watch('filter', () => this._reselectIfStale());
      this.$watch('hideEmptyModels', () => this._reselectIfStale());

      // Follow the gateway selector: each aGate has its own SunSpec catalog.
      // Compare ids ourselves: Alpine 3.14's $watch fires whenever the getter
      // re-runs, with no equality check, and gwId reads gatewayList, which the
      // app replaces every 10s. Unguarded, this reset the selection and
      // reloaded the catalog on every tick (#69).
      this.$watch(() => this.gwId, (id, prev) => {
        if (id === prev) return;
        this.selectedModelId = null;
        this.models = [];
        this.loadGateway();
        this.loadModels();
      });
    },

    /** The gateway the Explorer works on: the one being viewed, when it is a
     *  real aGate; otherwise the primary gateway. (A demo gateway has no
     *  registers, and "Site" is an aggregate, not a device.) */
    get gwId() {
      const app = Alpine.store('app');
      const gw = app.gatewayList.find(g => g.id === app.activeGateway);
      return gw && !gw.mock ? gw.id : 'default';
    },

    _gwQuery() { return 'gateway_id=' + encodeURIComponent(this.gwId); },

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
          if (typeof saved.hideEmptyModels === 'boolean') this.hideEmptyModels = saved.hideEmptyModels;
          if (typeof saved.showExtCard === 'boolean') this.showExtCard = saved.showExtCard;
        }
      } catch (_) {}
    },

    _saveLayout() {
      localStorage.setItem(EXPLORER_LAYOUT_KEY, JSON.stringify({
        treeWidth: this.treeWidth,
        extHeight: this.extHeight,
        hideEmptyModels: this.hideEmptyModels,
        showExtCard: this.showExtCard,
      }));
    },

    resetLayout() {
      this.treeWidth = EXPLORER_DEFAULTS.treeWidth;
      this.extHeight = EXPLORER_DEFAULTS.extHeight;
      localStorage.removeItem(EXPLORER_LAYOUT_KEY);
      Alpine.store('app').toast('Explorer layout reset', 'info');
    },

    async loadGateway() {
      const data = await fetchJSON('api/gateway?' + this._gwQuery());
      if (data && !data.error) {
        this.gateway = data;
      }
    },

    async loadModels() {
      this.loading = true;
      const data = await fetchJSON('api/models?' + this._gwQuery());
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
      const data = await fetchJSON('api/models/refresh?' + this._gwQuery(), { method: 'POST' });
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

    _reselectIfStale() {
      // If the selected model is no longer in the filtered list, select the
      // first one that is (keeps the current selection when it still matches).
      const matches = this.filteredModels;
      if (matches.length && !matches.some(m => m.model_id === this.selectedModelId)) {
        this.selectedModelId = matches[0].model_id;
      }
    },

    async readModel() {
      if (!this.selectedModelId || this.readingModel) return;
      this.readingModel = true;
      try {
        const data = await fetchJSON(`api/models/${this.selectedModelId}/read?${this._gwQuery()}`, { method: 'POST' });
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
      let models = this.models;
      // Hide models that aren't actively polled (no live values).
      if (this.hideEmptyModels) {
        models = models.filter(m => this.modelHasValues(m));
      }

      const q = this.filter.toLowerCase().trim();
      if (!q) return models;

      return models.filter(m => {
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
        const note = VENDOR_EXT_15000_NOTES[a];
        regs.push({
          addr: a,
          known: !!note,
          name: note ? note.name : 'Reg' + a,
          desc: note ? note.desc : 'Unknown',
          value: value,
          type: 'uint16',
          access: 'R',
          unit: (note && note.unit) || '',
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
          known: true,
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
          known: true,
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

      // Identified (a name from FranklinWH docs or site notes) vs unknown
      if (this.extKnown === 'known') regs = regs.filter(r => r.known);
      if (this.extKnown === 'unknown') regs = regs.filter(r => !r.known);

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

    // ── SunSpec export modal (#228) ─────────────────────────

    openExport() {
      const e = this.exp;
      e.scope = 'model';
      e.chosen = this.selectedModelId != null ? [this.selectedModelId] : [];
      e.msg = '';
      e.open = true;
      this.generateExport();
    },

    exportScopeLabel() {
      const m = this.selectedModel;
      return m ? `This model (${m.model_id} ${m.label || ''})` : 'This model';
    },

    toggleExportModel(id) {
      const c = this.exp.chosen;
      const i = c.indexOf(id);
      if (i >= 0) c.splice(i, 1); else c.push(id);
      this.generateExport();
    },

    /** Ask the bridge for the export; the preview shows exactly what it returns. */
    async generateExport() {
      const e = this.exp;
      let models = '';
      if (e.scope === 'model') models = String(this.selectedModelId ?? '');
      if (e.scope === 'choose') models = e.chosen.join(',');
      e.error = ''; e.msg = '';
      if (e.scope !== 'all' && !models) {
        e.result = null;
        e.error = 'Choose at least one model.';
        return;
      }
      e.loading = true;
      const q = new URLSearchParams({
        gateway_id: this.gwId, models, format: e.format, redact_serial: e.redact ? 'true' : 'false',
      });
      try {
        e.result = await fetchJSON('api/models/export?' + q.toString());
      } catch (err) {
        e.result = null;
        e.error = 'Could not build the export: ' + (err.message || err);
      } finally {
        e.loading = false;
      }
    },

    /** navigator.clipboard needs a secure context, which HA ingress over plain
     *  http is not; fall back to selecting the preview and execCommand. */
    async _copyExport() {
      const text = this.exp.result?.content;
      if (!text) return false;
      if (navigator.clipboard && window.isSecureContext) {
        try { await navigator.clipboard.writeText(text); return true; } catch (_) { /* fall through */ }
      }
      const ta = this.$refs.exportPreview;
      if (!ta) return false;
      ta.focus();
      ta.select();
      try { return document.execCommand('copy'); } catch (_) { return false; }
    },

    async copyExport() {
      this.exp.msg = (await this._copyExport())
        ? 'Copied.' : 'Press Ctrl/Cmd+C to copy the selected text.';
    },

    downloadExport() {
      const r = this.exp.result;
      if (r) this._downloadFile(r.filename, r.content, r.media_type);
    },

    /** Copy, then open GitHub's new-Gist page. Nothing is sent from here:
     *  the user pastes it and picks secret or public on GitHub. */
    async gistExport() {
      const r = this.exp.result;
      if (!r) return;
      const copied = await this._copyExport();
      window.open(GIST_NEW_URL, '_blank', 'noopener');
      this.exp.msg = (copied ? 'Copied. ' : 'Copy the preview (Ctrl/Cmd+C), then: ')
        + `in the GitHub tab, paste it, name the file ${r.filename}, then choose `
        + 'Create secret gist (unlisted: anyone with the link can see it) or Create public gist.';
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
