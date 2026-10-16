# Restarts and the clock

What a bridge restart does to running commands and open windows, and which clock every
schedule runs on. The operating practice for restarts is in
[Restarts & shutdown](../../restarts-and-shutdown.md).

## Bridge restart

| Step | What happens |
|---|---|
| Graceful shutdown | The schedule engine stops first. Then each gateway releases its active command (a fast release, no ramp-down) and logs *shutdown* in the control log. The Activity Log gets no row, and exit-time HA actions do not run. |
| Crash or power loss | Nothing is released. The setpoint stays on the aGate, which does not revert by itself. |
| Gateway start-up | Each gateway reads the command state it saved. If a command was active, it is **released** ("Releasing now for safety") and the revert timer is cleared. |
| Engine start | The engine starts with **no memory**: no owners, no *done* marks, no hold-for or retry timers. |
| First ticks | Entries whose window is open **fire again** (gate and hold-for permitting; the hold-for timer starts from zero). Always entries are eligible again. A hold (API `release: hold`) is not resumed. |
| Catch-up | If the bridge was down for more than 60 s, fires that started **and** ended while it was down are handled by each entry's [missed policy](triggers.md#after-an-outage-or-a-restart-catch-up). |

<!-- @b RST-01 ;; src: publish/command_handler.py:1057-1067, publish/command_handler.py:701-707 ;; tested: tests/unit/test_force_commands.py::test_fast_release_passes_zero_handshake ;; write: yes ;; b: Graceful shutdown releases an active command with reason shutdown using the fast release (no handshake ramp-down) ;; test: — -->
<!-- @b RST-02 ;; src: gateway/instance.py:264-270, gateway/instance.py:391-420 ;; untested ;; write: yes ;; b: On gateway start-up a persisted active command is released for safety (reset_control_state, WSetRvrtTms and WSetEnaRvrt cleared) and the saved state is set inactive ;; test: unit — GatewayInstance with a saved active control state and a mock controller; start; assert reset_control_state was called and the state saved inactive -->
<!-- @b RST-03 ;; src: gateway/scheduler.py:406-452, gateway/scheduler.py:661-664 ;; tested: tests/unit/test_scheduler.py::test_dispatch_on_window_enter ;; write: yes ;; b: A freshly started engine has no memory, so an entry whose window is open dispatches on the first tick ;; test: VM/mock — restart the bridge during an open window on a mock; expect a startup release then a new dispatch -->
<!-- @b RST-04 ;; src: main.py:946-985 ;; untested ;; write: yes ;; b: Start-up order: gateways start (and release stale commands), then the engine starts, then startup catch-up runs ;; test: VM/mock — read the log order at start-up -->

So a restart during an open window usually means **two writes**: the start-up release, then a
new dispatch a few seconds later. Keep restarts outside your windows.

!!! note "Not linked in the audit trail yet"
    A restart is not linked to the command it cut short or to the window it re-fired. See
    *Audit trail* in [Restarts & shutdown](../../restarts-and-shutdown.md).

## The clock

Every trigger, window, tariff window and `time.*` sensor uses **the bridge's local clock**:
the timezone of the bridge's process (the container's `TZ` or `/etc/localtime`, or the Home
Assistant host's timezone for the add-on). One clock for every gateway.
<!-- @b CLK-01 ;; src: gateway/scheduler.py:396, gateway/scheduler_triggers.py:16-21, api/schedules_api.py:290, gateway/scheduler_sensors.py:909-911 ;; untested ;; write: yes ;; b: The engine, the API previews and the time sensors use naive datetime.now(): the bridge process's local clock, the same for every gateway ;; test: unit — run tick with TZ set to two zones (monkeypatch time.tzset) and compare activation -->

![The timeline header names the bridge's zone](../../images/scheduler/timeline.png)

- **The browser's timezone does not matter.** The form, *Next fire*, the timeline and the
  Activity Log show the bridge's clock; the zone is printed in the timeline header and the
  Activity Log's *Time* column, for example *(AEDT)*.
  <!-- @b CLK-02 ;; src: templates/tabs/schedule.html:53, templates/tabs/schedule.html:503, static/js/schedule_tab.js:867-884, static/js/schedule_tab.js:72-74 ;; untested ;; write: no ;; b: The Schedule page renders times and the one-off default date on the bridge's clock, with the zone label in the timeline and Activity Log headers ;; test: browser — browser in a different zone; Next fire and the default date follow the bridge -->
- On first start the bridge records its timezone; on every start it compares. A mismatch
  (for example a container that fell back to UTC) means every schedule fires at the wrong
  wall-clock time. The top bar asks you to **confirm** the detected timezone or **change** it.
  <!-- @b CLK-03 ;; src: config/clock.py:96-150, config/clock.py:175-217 ;; tested: tests/unit/test_clock_guard.py::test_drift_to_utc_is_reported ;; write: no ;; b: The timezone is recorded at install and compared at every start; a mismatch or a likely container default (UTC) is reported for confirmation ;; test: — -->
- `time.tz_ok` reads 0 when the clock no longer matches the recorded timezone, and
  `time.utc_offset_h` shows the current offset. Use them in a condition, for example to send
  a notification.
- `service.tz_matches_clock` reads 0 when a utility service's timezone differs from the bridge
  clock, which means its TOU windows apply at the wrong time.

!!! note "Per-gateway clocks are planned"
    Today one clock serves every gateway. Per-gateway clocks, for sites whose gateways are in
    different timezones, are tracked in issue #162. They are not in this release.

### Daylight saving

The engine works in local wall-clock time, with no timezone attached. Around a daylight-saving
change:

- **Clocks go forward** (the hour from 02:00 does not exist): a fire time or window inside the
  missing hour can be **skipped** that day, or start late; no catch-up runs for it.
- **Clocks go back** (the hour from 02:00 happens twice): a window or fire time inside the
  repeated hour can be **active twice** and fire twice.
- An *Every N minutes* trigger can be off by the hour for one step.

Keep schedules out of 02:00–03:00 on change days, or check the Activity Log afterwards.
<!-- @b CLK-04 ;; src: gateway/scheduler.py:180-199, gateway/scheduler.py:129-138, gateway/scheduler_triggers.py:23-27 ;; unclear ;; write: yes ;; b: Activation is computed on naive local time, so a window in the skipped DST hour may not run and one in the repeated hour may run twice ;; test: unit — with TZ Australia/Sydney, tick through the spring-forward and fall-back nights at 15 s steps for a daily 02:30 entry with 30 min duration; count dispatches -->
