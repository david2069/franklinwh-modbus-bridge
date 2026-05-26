/**
 * Sequencer Tab — manage, edit, validate, and execute sequence files
 */
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

    async init() {
      await this.loadSequences();
    },

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

      // Scroll terminal to bottom
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
