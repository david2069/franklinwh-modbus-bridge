/**
 * Controls Tab — battery command, power, duration, operating mode, reserves
 */
function controlsTab() {
  return {
    command: 'Not Active',
    powerW: 0,
    powerPct: 0,
    duration: 3600,
    operatingMode: '',
    selfReserve: 20,
    touReserve: 20,
    lastResult: '',
    sending: false,

    init() {
      // Sync initial state from points
      this._syncFromPoints();
      // Re-sync when data refreshes
      setInterval(() => this._syncFromPoints(), 10000);
    },

    _syncFromPoints() {
      const pts = Alpine.store('app').points;
      if (pts.battery_command_state) {
        this.command = pts.battery_command_state;
      }
      if (pts.battery_command_power_w != null) {
        this.powerW = pts.battery_command_power_w;
      }
      if (pts.battery_command_power_pct != null) {
        this.powerPct = pts.battery_command_power_pct;
      }
      if (pts.battery_command_duration_s != null) {
        this.duration = pts.battery_command_duration_s;
      }
      if (pts.last_command_result) {
        this.lastResult = pts.last_command_result;
      }
    },

    async sendCommand(slug, value) {
      if (this.sending) return;
      this.sending = true;

      const data = await fetchJSON('api/command', {
        method: 'POST',
        body: JSON.stringify({ slug, value: String(value) }),
      });

      this.sending = false;

      if (data && data.ok) {
        this.lastResult = data.result || 'Sent';
        Alpine.store('app').toast(`${slug}: ${data.result}`, 'info');
      } else {
        this.lastResult = data?.result || data?.error || 'Failed';
        Alpine.store('app').toast(`Command failed: ${this.lastResult}`, 'error');
      }

      // Refresh state
      setTimeout(() => Alpine.store('app').refresh(), 500);
    },
  };
}
