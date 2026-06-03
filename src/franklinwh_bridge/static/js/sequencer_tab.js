/**
 * Sequencer Tab — manage, edit, validate, and execute sequence files
 *
 * Syntax highlighting shown when not editing (read-only <pre>).
 * Click to switch to a plain <textarea> for editing; blur switches back.
 */
// Default layout constants for sequencer
const SEQUENCER_DEFAULTS = { listWidth: 220, syntaxRefHeight: 160, outputFlex: 300 };
const SEQUENCER_LAYOUT_KEY = 'fwh-layout-sequencer';

function sequencerTab() {
  return {
    sequences: [],
    selectedName: null,
    editor: '',
    editorDirty: false,
    loading: true,
    saving: false,
    executing: false,
    dryRun: false,
    output: [],
    showNewDialog: false,
    newName: '',
    renameMode: false,
    renameName: '',
    validationError: null,
    showSyntaxRef: false,
    editing: false,

    // Resizable panel dimensions
    listWidth: SEQUENCER_DEFAULTS.listWidth,
    syntaxRefHeight: SEQUENCER_DEFAULTS.syntaxRefHeight,
    outputFlex: SEQUENCER_DEFAULTS.outputFlex,
    resizing: null,
    _startX: 0,
    _startY: 0,
    _startVal: 0,

    async init() {
      // Restore saved layout
      this._loadLayout();

      await this.loadSequences();

      this._onMouseMove = (e) => this._handleResize(e);
      this._onMouseUp = () => this._stopResize();
      document.addEventListener('mousemove', this._onMouseMove);
      document.addEventListener('mouseup', this._onMouseUp);
    },

    destroy() {
      document.removeEventListener('mousemove', this._onMouseMove);
      document.removeEventListener('mouseup', this._onMouseUp);
    },

    startResize(which, e) {
      this.resizing = which;
      this._startX = e.clientX;
      this._startY = e.clientY;
      this._startVal = which === 'list' ? this.listWidth
                     : which === 'syntaxRef' ? this.syntaxRefHeight
                     : this.outputFlex;
      document.body.style.cursor = which === 'list' ? 'col-resize' : 'row-resize';
      document.body.style.userSelect = 'none';
    },

    _handleResize(e) {
      if (!this.resizing) return;
      if (this.resizing === 'list') {
        const dx = e.clientX - this._startX;
        this.listWidth = Math.max(140, Math.min(500, this._startVal + dx));
      } else if (this.resizing === 'syntaxRef') {
        const dy = this._startY - e.clientY;
        this.syntaxRefHeight = Math.max(60, Math.min(400, this._startVal + dy));
      } else if (this.resizing === 'output') {
        const dy = this._startY - e.clientY;
        this.outputFlex = Math.max(80, Math.min(700, this._startVal + dy));
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
        const saved = JSON.parse(localStorage.getItem(SEQUENCER_LAYOUT_KEY));
        if (saved) {
          if (saved.listWidth) this.listWidth = saved.listWidth;
          if (saved.syntaxRefHeight) this.syntaxRefHeight = saved.syntaxRefHeight;
          if (saved.outputFlex) this.outputFlex = saved.outputFlex;
        }
      } catch (_) {}
    },

    _saveLayout() {
      localStorage.setItem(SEQUENCER_LAYOUT_KEY, JSON.stringify({
        listWidth: this.listWidth,
        syntaxRefHeight: this.syntaxRefHeight,
        outputFlex: this.outputFlex,
      }));
    },

    resetLayout() {
      this.listWidth = SEQUENCER_DEFAULTS.listWidth;
      this.syntaxRefHeight = SEQUENCER_DEFAULTS.syntaxRefHeight;
      this.outputFlex = SEQUENCER_DEFAULTS.outputFlex;
      localStorage.removeItem(SEQUENCER_LAYOUT_KEY);
      Alpine.store('app').toast('Sequencer layout reset', 'info');
    },

    // ── Line numbers ──────────────────────────────────────────
    get lineNumbers() {
      const count = (this.editor || '').split('\n').length;
      let html = '';
      for (let i = 1; i <= count; i++) {
        html += i + '\n';
      }
      return html;
    },

    syncLineNumbers(e) {
      const lines = this.$refs.lineNums;
      if (lines) lines.scrollTop = e.target.scrollTop;
    },

    // ── Syntax highlighting (read-only view) ────────────────
    get highlightedHtml() {
      return this._highlightJson(this.editor);
    },

    _highlightJson(text) {
      if (!text) return '';
      let html = text
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');

      // Model.Point references: "704.WSetEna" etc
      html = html.replace(
        /(&quot;|")((\d{3,4})\.([A-Za-z]\w*))(&quot;|")/g,
        '$1<span class="sh-model">$3</span>.<span class="sh-point">$4</span>$5'
      );

      // JSON keys
      const keywords = ['step','writes','reads','verify','verify_timeout_ms','wait_for',
                         'sleep_ms','abort_on_failure','require_transition','note','dry_run'];
      const kwPat = new RegExp(
        '(&quot;|")(' + keywords.join('|') + ')(&quot;|")(\\s*:)', 'g'
      );
      html = html.replace(kwPat, '$1<span class="sh-keyword">$2</span>$3$4');

      // String values
      html = html.replace(
        /(&quot;|")([^"<]*?)(&quot;|")/g,
        (m, q1, val, q2) => {
          if (val.includes('sh-')) return m;
          return q1 + '<span class="sh-string">' + val + '</span>' + q2;
        }
      );

      // Numbers
      html = html.replace(/\b(-?\d+\.?\d*)\b/g, '<span class="sh-number">$1</span>');

      // Booleans / null
      html = html.replace(/\b(true|false|null)\b/g, '<span class="sh-bool">$1</span>');

      // Brackets
      html = html.replace(/([{}\[\]])/g, '<span class="sh-bracket">$1</span>');

      return html;
    },

    startEditing() {
      this.editing = true;
      this.$nextTick(() => {
        const ta = this.$refs.editorTextarea;
        if (ta) ta.focus();
      });
    },

    // ── CRUD operations ─────────────────────────────────────

    async loadSequences() {
      this.loading = true;
      const data = await fetchJSON('api/sequences');
      if (data && data.sequences) {
        this.sequences = data.sequences;
      }
      this.loading = false;
    },

    async selectSequence(name) {
      if (this.editorDirty && !confirm('Discard unsaved changes?')) return;
      this.selectedName = name;
      this.editorDirty = false;
      this.validationError = null;
      this.renameMode = false;

      const data = await fetchJSON('api/sequences/' + name);
      if (data && data.raw) {
        this.editor = data.raw;
        this.validateJson();
      }
    },

    onEditorInput() {
      this.editorDirty = true;
      this.validateJson();
    },

    validateJson() {
      try {
        const parsed = JSON.parse(this.editor);
        if (!Array.isArray(parsed)) {
          this.validationError = 'Expected a JSON array of step objects';
          return false;
        }
        this.validationError = null;
        return true;
      } catch (e) {
        this.validationError = e.message;
        return false;
      }
    },

    get stepSummary() {
      try {
        const parsed = JSON.parse(this.editor);
        if (!Array.isArray(parsed)) return '';
        const writes = parsed.reduce((n, s) => n + (s.writes ? Object.keys(s.writes).length : 0), 0);
        const reads = parsed.reduce((n, s) => n + (Array.isArray(s.reads) ? s.reads.length : 0), 0);
        const waits = parsed.filter(s => s.wait_for || s.sleep_ms).length;
        const parts = [];
        if (writes) parts.push(writes + ' write' + (writes > 1 ? 's' : ''));
        if (reads) parts.push(reads + ' read' + (reads > 1 ? 's' : ''));
        if (waits) parts.push(waits + ' wait' + (waits > 1 ? 's' : ''));
        return parts.length ? parts.join(', ') : '';
      } catch {
        return '';
      }
    },

    async saveSequence() {
      if (!this.selectedName) return;
      if (!this.validateJson()) {
        Alpine.store('app').toast('Fix JSON errors before saving', 'error');
        return;
      }

      this.saving = true;
      const data = await fetchJSON('api/sequences/' + this.selectedName, {
        method: 'PUT',
        body: JSON.stringify({ content: this.editor }),
      });
      this.saving = false;

      if (data && data.ok) {
        this.editorDirty = false;
        Alpine.store('app').toast('Sequence saved', 'info');
        await this.loadSequences();
      } else {
        Alpine.store('app').toast('Save failed: ' + (data?.error || 'unknown'), 'error');
      }
    },

    async createSequence() {
      const name = this.newName.trim().replace(/[^a-zA-Z0-9_-]/g, '_');
      if (!name) return;

      const template = JSON.stringify([
        {
          step: 'Step 1',
          writes: { '704.WSetEna': 1 },
          verify: true,
          note: 'Enable VPP control',
        },
      ], null, 2);

      const data = await fetchJSON('api/sequences/' + name, {
        method: 'PUT',
        body: JSON.stringify({ content: template }),
      });

      if (data && data.ok) {
        this.showNewDialog = false;
        this.newName = '';
        await this.loadSequences();
        await this.selectSequence(name);
        Alpine.store('app').toast('Created sequence: ' + name, 'info');
      }
    },

    async deleteSequence() {
      if (!this.selectedName) return;
      if (!confirm(`Delete "${this.selectedName}"? This cannot be undone.`)) return;

      const data = await fetchJSON('api/sequences/' + this.selectedName, {
        method: 'DELETE',
      });

      if (data && data.ok) {
        this.selectedName = null;
        this.editor = '';
        this.editorDirty = false;
        await this.loadSequences();
        Alpine.store('app').toast('Sequence deleted', 'info');
      }
    },

    startRename() {
      this.renameMode = true;
      this.renameName = this.selectedName;
    },

    async confirmRename() {
      const newName = this.renameName.trim().replace(/[^a-zA-Z0-9_-]/g, '_');
      if (!newName || newName === this.selectedName) {
        this.renameMode = false;
        return;
      }

      const data = await fetchJSON('api/sequences/' + this.selectedName + '/rename', {
        method: 'POST',
        body: JSON.stringify({ new_name: newName }),
      });

      if (data && data.ok) {
        this.renameMode = false;
        this.selectedName = newName;
        await this.loadSequences();
        Alpine.store('app').toast('Renamed to ' + newName, 'info');
      } else {
        Alpine.store('app').toast('Rename failed: ' + (data?.error || 'unknown'), 'error');
      }
    },

    async executeSequence() {
      if (this.executing || !this.editor.trim()) return;
      if (!this.validateJson()) {
        Alpine.store('app').toast('Fix JSON errors before executing', 'error');
        return;
      }

      this.executing = true;
      this.output = ['>>> Executing sequence' + (this.dryRun ? ' (DRY RUN)' : '') + '...'];

      const parsed = JSON.parse(this.editor);
      const data = await fetchJSON('api/sequence/execute', {
        method: 'POST',
        body: JSON.stringify({ sequence: parsed, dry_run: this.dryRun }),
      });

      this.executing = false;

      if (data && data.output) {
        this.output = data.output;
      } else {
        this.output = ['ERROR: ' + (data?.error || 'Unknown error')];
      }

      this.$nextTick(() => {
        const el = this.$refs.seqTabTerminal;
        if (el) el.scrollTop = el.scrollHeight;
      });
    },

    get parsedStepCount() {
      try {
        const parsed = JSON.parse(this.editor);
        return Array.isArray(parsed) ? parsed.length : 0;
      } catch {
        return 0;
      }
    },
  };
}
