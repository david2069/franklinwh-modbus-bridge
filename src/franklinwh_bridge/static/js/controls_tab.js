/**
 * Controls Tab — battery command, power, duration, operating mode, reserves
 */
function controlsTab() {
  return {
    command: 'Not Active',
    powerW: 0,
    powerPct: 0,
    duration: 0,
    targetSoc: 0,
    operatingMode: '',
    selfReserve: 20,
    touReserve: 20,
    lastResult: '',
    sending: false,
    commandLog: [],

    init() {
      // Sync initial state from points (may not be loaded yet)
      this._syncFromPoints();
      // Re-sync when data refreshes — also use $watch for immediate reactivity
      this.$watch('$store.app.points', () => this._syncFromPoints());
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
      if (pts.battery_command_target_soc != null) {
        this.targetSoc = pts.battery_command_target_soc;
      }
      if (pts.last_command_result) {
        this.lastResult = pts.last_command_result;
      }
      // Sync operating mode from live points
      if (pts.mode_name) {
        this.operatingMode = pts.mode_name;
      }
    },

    _logEntry(slug, value, ok, message) {
      const ts = new Date().toLocaleTimeString('en-US', { hour12: false });
      const label = slug.replace(/^battery_command_/, '').replace(/_/g, ' ');
      this.commandLog.push({ ts, slug: label, value, ok, message });
      // Cap at 50 entries
      if (this.commandLog.length > 50) {
        this.commandLog.splice(0, this.commandLog.length - 50);
      }
      // Auto-scroll after Alpine renders
      this.$nextTick(() => {
        const el = this.$refs?.logScroll;
        if (el) el.scrollTop = el.scrollHeight;
      });
    },

    setMaxCharge() {
      const v = Alpine.store('app').points.max_charge_rate_w ?? 5000;
      this.powerW = v;
      this.sendCommand('battery_command_power', v);
    },

    setMaxDischarge() {
      const v = Alpine.store('app').points.max_discharge_rate_w ?? 5000;
      this.powerW = v;
      this.sendCommand('battery_command_power', v);
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
        this._logEntry(slug, value, true, data.result || 'Sent');
        Alpine.store('app').toast(`${slug}: ${data.result}`, 'info');
      } else {
        this.lastResult = data?.result || data?.error || 'Failed';
        this._logEntry(slug, value, false, this.lastResult);
        Alpine.store('app').toast(`Command failed: ${this.lastResult}`, 'error');
      }

      // Refresh state
      setTimeout(() => Alpine.store('app').refresh(), 500);
    },
  };
}
