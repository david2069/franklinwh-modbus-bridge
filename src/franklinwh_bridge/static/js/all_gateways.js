// All gateways (#296 item 43): a read-only overview that replaces the
// "Site (All Gateways)" picker view. One row per gateway and a site total;
// clicking a row makes that gateway the active one. No controls: commands
// only ever go to the active gateway.

function allGateways() {
  return {
    points: {},      // gateway id -> latest points
    site: {},        // api/site/status points
    _timer: null,

    get rows() { return this.$store.app.gatewayList; },
    get counts() {
      const rows = this.rows;
      const polling = rows.filter(g => g.polling).length;
      const stopped = rows.filter(g => !g.polling && ['stopped', 'disabled', 'unconfigured'].includes(g.health)).length;
      return { registered: rows.length, polling, stopped,
               unavailable: rows.length - polling - stopped };
    },

    watchOpen() {
      this.$watch('$store.app.allGatewaysOpen', (open) => {
        clearInterval(this._timer);
        if (open) {
          this.load();
          this._timer = setInterval(() => this.load(), 5000);
        }
      });
    },

    async load() {
      const polling = this.rows.filter(g => g.polling);
      const [site, ...each] = await Promise.all([
        fetchJSON('api/site/status', { background: true }),
        ...polling.map(g => fetchJSON(`api/gateways/${encodeURIComponent(g.id)}/points`,
                                      { background: true })),
      ]);
      const points = {};
      polling.forEach((g, i) => { points[g.id] = (each[i] && each[i].points) || {}; });
      this.points = points;
      this.site = (site && site.points) || {};
    },

    p(gw) { return this.points[gw.id] || {}; },
    kw(v) { return v != null ? fmt.kw(v) : '—'; },

    command(gw) {
      const c = this.p(gw).battery_command_state;
      return c && c !== 'Not Active' ? c : '';
    },

    pick(gw) {
      this.$store.app.setGateway(gw.id);
      this.close();
    },

    close() { this.$store.app.allGatewaysOpen = false; },
  };
}
