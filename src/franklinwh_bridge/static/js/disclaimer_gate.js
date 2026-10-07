/**
 * Legal-notice gate.
 *
 * The notice is logged at startup and on the first UI connection, but neither
 * puts it in front of anyone: a bridge someone else installed can run for
 * months without its operator ever opening a console. This is the one place
 * the words are unavoidable.
 *
 * Deliberately NOT localStorage. Acknowledgement is a record — it belongs in
 * the database, against a user and a notice version, where it survives a
 * browser change and can be looked up afterwards. A legal acknowledgement
 * nobody can produce later is barely one at all.
 *
 * Fetches its own copy of the text rather than rendering a hardcoded block, so
 * the words shown and the version they are recorded against cannot drift.
 */
(function () {
  'use strict';

  window.disclaimerGate = function disclaimerGate() {
    return {
      open: false,
      saving: false,
      agreed: false,
      title: '',
      paragraphs: [],
      agreeLabel: '',
      docsUrl: '',
      issuesUrl: '',
      termsUrl: '',

      async load() {
        try {
          const resp = await fetch('api/disclaimer');
          // 401 simply means nobody is signed in yet — the login page is not
          // the place for this, and the gate will run once they are.
          if (!resp.ok) return;
          const d = await resp.json();
          this.title = d.title || 'Unofficial software';
          this.paragraphs = d.paragraphs || [];
          this.agreeLabel = d.agree_label || 'I have read and agree to the above.';
          this.docsUrl = d.docs_url || '';
          this.issuesUrl = d.issues_url || '';
          this.termsUrl = d.terms_url || '';
          this.open = !d.acknowledged;
        } catch (_) {
          // A notice that can't load must not wedge the UI behind a blank
          // dialog. Startup and first-connection logging still carry it, and
          // the gate tries again on the next page load.
        }
      },

      async submit() {
        // Closing without ticking is a real answer, not a skip: it is reported
        // so the server can log that the notice was shown and declined, and it
        // is not stored — so it comes back next time.
        this.saving = true;
        try {
          await fetch('api/disclaimer', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ agreed: !!this.agreed }),
          });
        } catch (_) {
          // Network failure must not trap the user in the dialog. Nothing was
          // stored, so the notice returns on the next load — which is the
          // correct outcome for an acknowledgement that never reached us.
        } finally {
          this.saving = false;
          this.open = false;
          // The setup wizard waits for this, so it never opens on top of us.
          window.dispatchEvent(new CustomEvent('disclaimer-settled'));
        }
      },
    };
  };
})();
