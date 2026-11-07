/**
 * Customise dialog (#296 item 50, owner 2026-11-07): one place, opened from the gear,
 * to show, hide and reorder the top bar's items and the Dashboard's cards. Replaces the
 * Topbar Preferences dropdown and the Dashboard's Cards panel.
 *
 * Each row: a drag handle, big up/down buttons (finger-sized) and an on/off switch.
 * Dragging uses pointer events on the handle, so it works with a finger too: HTML5 drag
 * events never fire from a touch drag. Every change is saved at once; Undo (the store's)
 * steps back through this visit's changes, one step per click, toggle or drag.
 *
 * Lists: 'left' (top bar, show/hide only), 'center', 'right' (top bar) and 'cards'.
 */
function customiseDialog() {
  return {
    _drag: null,   // {list, key} while a handle is held

    get app() { return Alpine.store('app'); },

    items(list) {
      const app = this.app;
      if (list === 'cards') {
        return app.cards.order.map(key => ({
          key, label: DASH_CARD_LABELS[key] || key, visible: app.cards.visible[key] !== false,
        }));
      }
      return app.topbarPrefs[list];
    },

    toggle(list, key) {
      this.app.custSnapshot();
      if (list === 'cards') {
        this.app.cards.visible[key] = !(this.app.cards.visible[key] !== false);
        this.app.saveCards();
      } else {
        const item = this.app.topbarPrefs[list].find(i => i.key === key);
        item.visible = item.visible === false;
        this.app.saveTopbarPrefs();
      }
    },

    _keys(list) {
      return list === 'cards' ? this.app.cards.order : this.app.topbarPrefs[list].map(i => i.key);
    },

    canMove(list, key, delta) {
      const keys = this._keys(list), at = keys.indexOf(key);
      return at >= 0 && at + delta >= 0 && at + delta < keys.length;
    },

    // Put `key` at index `to` in its list; saves. No snapshot: callers take one.
    _place(list, key, to) {
      if (list === 'cards') {
        const order = this.app.cards.order, from = order.indexOf(key);
        if (from < 0 || from === to) return;
        order.splice(from, 1);
        order.splice(to, 0, key);
        this.app.saveCards();
      } else {
        const arr = this.app.topbarPrefs[list], from = arr.findIndex(i => i.key === key);
        if (from < 0 || from === to) return;
        const [item] = arr.splice(from, 1);
        arr.splice(to, 0, item);
        this.app.saveTopbarPrefs();
      }
    },

    move(list, key, delta) {
      if (!this.canMove(list, key, delta)) return;
      this.app.custSnapshot();
      this._place(list, key, this._keys(list).indexOf(key) + delta);
    },

    // ── drag by the handle ──
    dragStart(e, list, key) {
      e.preventDefault();
      this.app.custSnapshot();
      this._drag = { list, key };
      e.target.setPointerCapture?.(e.pointerId);
    },

    dragMove(e) {
      if (!this._drag) return;
      const row = document.elementFromPoint(e.clientX, e.clientY)?.closest('[data-cust-row]');
      if (!row || row.dataset.list !== this._drag.list || row.dataset.key === this._drag.key) return;
      this._place(this._drag.list, this._drag.key, this._keys(this._drag.list).indexOf(row.dataset.key));
    },

    dragEnd() { this._drag = null; },

    dragging(list, key) { return !!this._drag && this._drag.list === list && this._drag.key === key; },
  };
}
