/**
 * Sequencer Panel — inline execution component (used in dashboard bottom tab)
 *
 * NOTE: We do NOT use x-model for the textarea. Alpine's x-model in nested
 * x-data scopes that start hidden (x-show=false) can fail to sync.
 * Instead we read the textarea value directly from the DOM via $el.querySelector.
 */
function sequencerPanel() {
  return {
    seqInput: '',   // x-model bound — avoids DOM querying issues
    seqOutput: [],
    seqRunning: false,
    dryRun: false,

    async executeInline() {
      console.log('[SeqPanel] executeInline called');

      if (this.seqRunning) {
        console.log('[SeqPanel] BLOCKED: already running');
        return;
      }

      // Read input from x-model binding
      const input = (this.seqInput || '').trim();
      console.log('[SeqPanel] input length:', input.length, 'preview:', input.substring(0, 60));

      if (!input) {
        this.seqOutput = ['ERROR: No sequence input provided'];
        return;
      }

      this.seqRunning = true;
      this.seqOutput = ['>>> Executing' + (this.dryRun ? ' (DRY RUN)' : '') + '...'];

      try {
        const data = await fetchJSON('api/sequence/execute', {
          method: 'POST',
          body: JSON.stringify({ inline: input, dry_run: this.dryRun }),
        });

        console.log('[SeqPanel] API response:', JSON.stringify(data).substring(0, 200));

        if (data && data.output) {
          this.seqOutput = data.output;
        } else {
          this.seqOutput = ['ERROR: ' + (data?.error || 'Unknown error')];
        }
      } catch (e) {
        console.error('[SeqPanel] Exception:', e);
        this.seqOutput = ['ERROR: Exception: ' + e.message];
      } finally {
        this.seqRunning = false;
      }

      // Scroll terminal to bottom
      this.$nextTick(() => {
        const el = this.$refs.seqTerminal ||
                   this.$el.querySelector('.bg-slate-950');
        if (el) el.scrollTop = el.scrollHeight;
      });
    },
  };
}
