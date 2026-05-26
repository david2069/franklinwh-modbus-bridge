/**
 * Sequencer Panel — inline execution component (used in dashboard bottom tab)
 */
function sequencerPanel() {
  return {
    seqInput: '',
    seqOutput: [],
    seqRunning: false,
    dryRun: false,

    async executeInline() {
      if (this.seqRunning) return;

      // Read from x-model binding; fallback to DOM textarea ref
      let input = (this.seqInput || '').trim();
      if (!input && this.$refs.seqTextarea) {
        input = this.$refs.seqTextarea.value.trim();
      }
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
        const el = this.$refs.seqTerminal;
        if (el) el.scrollTop = el.scrollHeight;
      });
    },
  };
}
