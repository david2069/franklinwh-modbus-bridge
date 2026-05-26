/**
 * Sequencer Panel — inline execution component (used in dashboard bottom tab)
 *
 * NOTE: We do NOT use x-model for the textarea. Alpine's x-model in nested
 * x-data scopes that start hidden (x-show=false) can fail to sync.
 * Instead we read the textarea value directly from the DOM via $el.querySelector.
 */
function sequencerPanel() {
  return {
    seqOutput: [],
    seqRunning: false,
    dryRun: false,

    _getInput() {
      const ta = this.$el.querySelector('textarea');
      return ta ? ta.value.trim() : '';
    },

    async executeInline() {
      if (this.seqRunning) return;

      const input = this._getInput();
      if (!input) {
        this.seqOutput = ['ERROR: No sequence input provided'];
        return;
      }

      this.seqRunning = true;
      this.seqOutput = ['>>> Executing' + (this.dryRun ? ' (DRY RUN)' : '') + '...'];

      const data = await fetchJSON('api/sequence/execute', {
        method: 'POST',
        body: JSON.stringify({ inline: input, dry_run: this.dryRun }),
      });

      this.seqRunning = false;

      if (data && data.output) {
        this.seqOutput = data.output;
      } else {
        this.seqOutput = ['ERROR: ' + (data?.error || 'Unknown error')];
      }

      // Scroll terminal to bottom
      this.$nextTick(() => {
        const el = this.$el.querySelector('[x-ref="seqTerminal"]') ||
                   this.$el.querySelector('.bg-slate-950');
        if (el) el.scrollTop = el.scrollHeight;
      });
    },
  };
}
