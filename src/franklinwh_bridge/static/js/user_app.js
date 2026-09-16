// Simplified mobile-first end-user dashboard. Standalone (no Alpine / admin
// chrome) — polls the same read-only JSON APIs the admin SPA uses, but shows
// only SOC, power flow, operating mode, today's energy and connectivity.
(function () {
  'use strict';

  const base = document.querySelector('base').getAttribute('href').replace(/\/$/, '');
  const RING_C = 490.088; // 2 * pi * r(78)
  const $ = (id) => document.getElementById(id);

  async function getJSON(path) {
    try {
      const r = await fetch(`${base}/${path}`, { headers: { Accept: 'application/json' } });
      if (!r.ok) return { _status: r.status, error: `HTTP ${r.status}` };
      return await r.json();
    } catch (e) {
      return { error: e.message };
    }
  }

  async function postJSON(path, body) {
    try {
      const r = await fetch(`${base}/${path}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) return { error: data.detail || `HTTP ${r.status}`, _status: r.status };
      return data;
    } catch (e) {
      return { error: e.message };
    }
  }

  // ── Formatting helpers ──────────────────────────────────────
  function fmtPower(w) {
    if (w == null || isNaN(w)) return { val: '--', unit: '' };
    const a = Math.abs(w);
    if (a >= 1000) return { val: (a / 1000).toFixed(a >= 10000 ? 1 : 2), unit: 'kW' };
    return { val: String(Math.round(a)), unit: 'W' };
  }
  function fmtKwh(v) {
    if (v == null || isNaN(v)) return '—';
    return `${Number(v).toFixed(1)} kWh`;
  }
  function setPower(elId, subId, w, sub, colour) {
    const el = $(elId);
    const p = fmtPower(w);
    el.innerHTML = p.unit ? `${p.val}<small>${p.unit}</small>` : p.val;
    el.style.color = colour || 'var(--text)';
    if (subId) $(subId).textContent = sub || '';
  }
  function socColour(soc) {
    if (soc == null) return 'var(--muted)';
    if (soc >= 80) return 'var(--ok)';
    if (soc >= 30) return 'var(--accent)';
    if (soc >= 15) return 'var(--warn)';
    return 'var(--danger)';
  }

  // ── Render ──────────────────────────────────────────────────
  function renderPoints(pts) {
    // SOC ring
    const soc = pts.soc;
    const arc = $('socArc');
    if (soc != null && !isNaN(soc)) {
      const pct = Math.min(100, Math.max(0, soc)) / 100;
      arc.style.strokeDashoffset = String(RING_C * (1 - pct));
      arc.style.stroke = socColour(soc);
      $('socVal').textContent = Math.round(soc);
    } else {
      arc.style.strokeDashoffset = String(RING_C);
      $('socVal').textContent = '--';
    }

    // Battery power → big state line + tile
    const bw = pts.battery_power_w;
    if (bw != null && !isNaN(bw)) {
      const p = fmtPower(bw);
      if (bw > 25) {
        $('battState').innerHTML = `Discharging · ${p.val} ${p.unit}`;
        $('battState').style.color = 'var(--warn)';
        setPower('battVal', 'battSub', bw, 'Discharging', 'var(--warn)');
      } else if (bw < -25) {
        $('battState').innerHTML = `Charging · ${p.val} ${p.unit}`;
        $('battState').style.color = 'var(--ok)';
        setPower('battVal', 'battSub', bw, 'Charging', 'var(--ok)');
      } else {
        $('battState').textContent = 'Idle';
        $('battState').style.color = 'var(--dim)';
        setPower('battVal', 'battSub', 0, 'Idle', 'var(--muted)');
      }
    } else {
      $('battState').textContent = '—';
      setPower('battVal', 'battSub', null, '');
    }

    // Solar (production, always ≥0)
    const sw = pts.total_solar;
    setPower('solarVal', 'solarSub', sw, sw > 25 ? 'Producing' : 'Idle',
      sw > 25 ? 'var(--solar)' : 'var(--muted)');

    // Grid: +import (danger) / −export (ok)
    const gw = pts.grid_power_w;
    if (gw != null && !isNaN(gw)) {
      if (gw > 25) setPower('gridVal', 'gridSub', gw, 'Importing', 'var(--danger)');
      else if (gw < -25) setPower('gridVal', 'gridSub', gw, 'Exporting', 'var(--ok)');
      else setPower('gridVal', 'gridSub', 0, 'No flow', 'var(--muted)');
    } else {
      setPower('gridVal', 'gridSub', null, '');
    }

    // Home load
    const hw = pts.home_load_ext;
    setPower('homeVal', 'homeSub', hw, hw != null ? 'Consuming' : '', 'var(--text)');

    // Operating mode
    if (pts.mode_name) $('modeVal').textContent = pts.mode_name;
    $('gridModeVal').textContent = pts.grid_mode || '—';
    const reserve = pts.self_reserve_pct;
    if (reserve != null && !isNaN(reserve)) {
      $('reserveRow').style.display = 'flex';
      $('reserveVal').textContent = `${Math.round(reserve)}%`;
    }

    $('foot').textContent = 'Updated ' + new Date().toLocaleTimeString();
  }

  function renderConnectivity(c) {
    const dot = $('connDot');
    const text = $('connText');
    if (c && c.error) { dot.className = 'dot'; text.textContent = 'Unknown'; return; }
    const online = !c || c.connected !== false;
    dot.className = 'dot ' + (online ? 'on' : 'off');
    text.textContent = online ? 'Online' : 'Offline';
  }

  // ── Energy flow (Sankey) + day selection ────────────────────
  const todayISO = () => {
    // Local date, not toISOString(): that converts to UTC first, so anywhere
    // east of Greenwich the "today" button would ask for yesterday after 10am.
    const d = new Date();
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  };

  let selectedDay = todayISO();

  function shiftDay(days) {
    const [y, m, d] = selectedDay.split('-').map(Number);
    const dt = new Date(y, m - 1, d + days);
    const p = (n) => String(n).padStart(2, '0');
    selectedDay = `${dt.getFullYear()}-${p(dt.getMonth() + 1)}-${p(dt.getDate())}`;
    syncDayControls();
    loadFlow();
  }

  function syncDayControls() {
    const isToday = selectedDay === todayISO();
    $('dayInput').value = selectedDay;
    $('dayNext').disabled = isToday;
    $('dayToday').disabled = isToday;
    $('energyHead').textContent = isToday ? 'Today' : selectedDay;
  }

  function renderQuality(q) {
    const el = $('flowQuality');
    if (!q) { el.textContent = ''; return; }
    if (!q.samples) {
      el.className = 'quality';
      el.textContent = 'No samples recorded for this day.';
      return;
    }
    // Coverage below ~95% means the bridge wasn't polling for part of the day,
    // so the totals are an under-report. Saying so beats showing a short day
    // as if it were a quiet one.
    const pct = Math.round((q.coverage || 0) * 100);
    if (pct < 95) {
      el.className = 'quality warn';
      el.textContent = `Partial data — the bridge recorded ${pct}% of this day.`;
    } else {
      el.className = 'quality';
      el.textContent = 'Derived from power samples; totals below are metered.';
    }
  }

  async function loadFlow() {
    const host = $('sankey');
    const data = await getJSON(`api/energy/flow?day=${encodeURIComponent(selectedDay)}`);
    if (!data || data.error) {
      host.innerHTML = '<p class="sankey-empty">Energy flow unavailable.</p>';
      $('flowQuality').textContent = '';
      return;
    }
    if (window.FWHSankey) window.FWHSankey.render(host, data.flows, { height: 250 });
    renderQuality(data.quality);
    renderDayTotals(data.nodes);
  }

  function renderEnergy(sensors) {
    if (!sensors || sensors.error || !Array.isArray(sensors.sensors)) return;
    const byKey = {};
    for (const s of sensors.sensors) byKey[s.id] = s.value;

    // The per-day totals are only published for *today*; for any other day the
    // Sankey's own node totals are what we have, so let the card follow the
    // selection rather than silently keep showing today under a past date.
    const isToday = selectedDay === todayISO();
    if (!isToday) return;

    const rows = {
      enSolar: 'energy.solar.today_kwh',
      enImport: 'energy.grid_import.today_kwh',
      enExport: 'energy.grid_export.today_kwh',
      enCharge: 'energy.battery_charge.today_kwh',
      enDischarge: 'energy.battery_discharge.today_kwh',
    };
    if (Object.values(rows).every((k) => byKey[k] == null)) return;
    $('energyCard').style.display = 'block';
    for (const id in rows) $(id).textContent = fmtKwh(byKey[rows[id]]);

    const life = {
      ltSolar: 'energy.solar.total_kwh',
      ltImport: 'energy.grid_import.total_kwh',
      ltExport: 'energy.grid_export.total_kwh',
      ltCharge: 'energy.battery_charge.total_kwh',
      ltDischarge: 'energy.battery_discharge.total_kwh',
    };
    if (Object.values(life).some((k) => byKey[k] != null)) {
      $('lifeCard').style.display = 'block';
      for (const id in life) {
        const v = byKey[life[id]];
        // Lifetime figures run to five digits; kWh precision is noise there.
        $(id).textContent = v == null ? '—' : `${Math.round(v).toLocaleString()} kWh`;
      }
    }
  }

  function renderDayTotals(nodes) {
    if (!nodes || selectedDay === todayISO()) return;
    $('energyCard').style.display = 'block';
    $('enSolar').textContent = fmtKwh(nodes.solar);
    $('enImport').textContent = fmtKwh(nodes.grid_import);
    $('enExport').textContent = fmtKwh(nodes.grid_export);
    $('enCharge').textContent = fmtKwh(nodes.battery_charge);
    $('enDischarge').textContent = fmtKwh(nodes.battery_discharge);
  }

  // ── Battery dispatch ────────────────────────────────────────
  // Mirrors the admin Controls tab, reduced to the two things an end user
  // actually reaches for. The API is the same; the capability gate is what
  // decides whether this card exists at all.
  let dispatchAction = null;   // 'Force Charge' | 'Force Discharge' | null
  let dispatchActive = false;
  let busy = false;

  function fmtKw(w) { return `${(w / 1000).toFixed(1)} kW`; }

  function fmtDuration(min) {
    if (min < 60) return `${min} min`;
    const h = Math.floor(min / 60);
    const m = min % 60;
    return m ? `${h}h ${m}m` : `${h}h`;
  }

  function syncDispatchControls() {
    $('btnCharge').classList.toggle('on', dispatchAction === 'Force Charge');
    $('btnDischarge').classList.toggle('on', dispatchAction === 'Force Discharge');
    $('powLabel').textContent = fmtKw(Number($('powRange').value));
    $('durLabel').textContent = fmtDuration(Number($('durRange').value));
    // Start needs a chosen direction; Stop needs something running. Leaving
    // Start live with no selection is how you dispatch the wrong direction.
    $('btnStart').disabled = busy || !dispatchAction;
    $('btnStop').disabled = busy || !dispatchActive;
  }

  function setDispatchState(msg, cls) {
    const el = $('dispatchState');
    el.textContent = msg || '';
    el.className = 'dispatch-state' + (cls ? ' ' + cls : '');
  }

  async function loadLimits() {
    const lim = await getJSON('api/gateways/default/battery/limits');
    if (!lim || lim.error) return;
    const max = Math.max(Number(lim.max_charge_w) || 0,
                         Number(lim.max_discharge_w) || 0);
    if (max > 0) {
      const r = $('powRange');
      r.max = String(max);
      // Clamp, don't reset: a device with a lower rating than the default must
      // not leave the slider parked above its own ceiling.
      if (Number(r.value) > max) r.value = String(max);
      $('powMax').textContent = fmtKw(max);
      $('powMin').textContent = fmtKw(Number(r.min));
    }
    syncDispatchControls();
  }

  async function startDispatch() {
    if (!dispatchAction || busy) return;
    busy = true; syncDispatchControls();
    setDispatchState('Sending…');

    const watts = Number($('powRange').value);
    const seconds = Number($('durRange').value) * 60;

    // Power and duration first: the action command is what arms the dispatch,
    // so sending it last means it is never armed with stale limits.
    const steps = [
      { slug: 'battery_command_power', value: watts },
      { slug: 'battery_command_duration', value: seconds },
      { slug: 'battery_command', value: dispatchAction },
    ];
    for (const s of steps) {
      const r = await postJSON('api/gateways/default/command', s);
      if (!r || r.error || r.ok === false) {
        busy = false;
        setDispatchState((r && (r.error || r.result)) || 'Command failed', 'err');
        syncDispatchControls();
        return;
      }
    }

    busy = false;
    dispatchActive = true;
    const verb = dispatchAction === 'Force Charge' ? 'Charging' : 'Discharging';
    setDispatchState(`${verb} at ${fmtKw(watts)} for ${fmtDuration(seconds / 60)}`, 'on');
    syncDispatchControls();
  }

  async function stopDispatch() {
    if (busy) return;
    busy = true; syncDispatchControls();
    setDispatchState('Stopping…');

    const r = await postJSON('api/gateways/default/command',
                             { slug: 'battery_command', value: 'Stop' });
    busy = false;
    if (!r || r.error || r.ok === false) {
      setDispatchState((r && (r.error || r.result)) || 'Stop failed', 'err');
    } else {
      dispatchActive = false;
      dispatchAction = null;
      setDispatchState('Released — the gateway is back on its own schedule.');
    }
    syncDispatchControls();
  }

  function wireDispatch() {
    $('btnCharge').addEventListener('click', () => {
      dispatchAction = dispatchAction === 'Force Charge' ? null : 'Force Charge';
      syncDispatchControls();
    });
    $('btnDischarge').addEventListener('click', () => {
      dispatchAction = dispatchAction === 'Force Discharge' ? null : 'Force Discharge';
      syncDispatchControls();
    });
    $('powRange').addEventListener('input', syncDispatchControls);
    $('durRange').addEventListener('input', syncDispatchControls);
    $('btnStart').addEventListener('click', startDispatch);
    $('btnStop').addEventListener('click', stopDispatch);
    syncDispatchControls();
  }

  // ── User chip / menu ────────────────────────────────────────
  async function loadMe() {
    const me = await getJSON('api/auth/me');
    const u = me && me.user;
    if (!u) { window.location.href = `${base}/login`; return; }
    $('userName').textContent = u.username;
    $('menuUser').textContent = u.username;
    $('menuRole').textContent = u.role;
    $('avatar').textContent = (u.username || '?').charAt(0).toUpperCase();
    if (u.role === 'admin') $('adminLink').style.display = 'block';

    // Show the dispatch card only to roles that may actually use it. A viewer
    // seeing buttons that always 403 is worse than not seeing them.
    if ((me.capabilities || []).includes('control')) {
      $('dispatchCard').style.display = 'block';
      wireDispatch();
      loadLimits();
    }
  }

  async function loadSite() {
    const site = await getJSON('api/site');
    if (site && !site.error && site.battery_label) $('siteName').textContent = site.battery_label;
  }

  // ── Poll loop ───────────────────────────────────────────────
  async function tick() {
    const [pts, conn, sensors] = await Promise.all([
      getJSON('api/points'),
      getJSON('api/health/connectivity'),
      getJSON('api/sensors'), // optional — 403 if automations module disabled
    ]);
    if (pts && pts.points) renderPoints(pts.points);
    renderConnectivity(conn);
    renderEnergy(sensors);
  }

  // ── Wire up ─────────────────────────────────────────────────
  const menu = $('userMenu');
  $('userBtn').addEventListener('click', (e) => { e.stopPropagation(); menu.classList.toggle('open'); });
  document.addEventListener('click', () => menu.classList.remove('open'));
  $('logoutBtn').addEventListener('click', async () => {
    await fetch(`${base}/api/auth/logout`, { method: 'POST' });
    window.location.href = `${base}/login`;
  });

  $('dayPrev').addEventListener('click', () => shiftDay(-1));
  $('dayNext').addEventListener('click', () => shiftDay(1));
  $('dayToday').addEventListener('click', () => {
    selectedDay = todayISO();
    syncDayControls();
    loadFlow();
  });
  $('dayInput').addEventListener('change', (e) => {
    if (!e.target.value) return;
    selectedDay = e.target.value;
    syncDayControls();
    loadFlow();
  });
  $('dayInput').max = todayISO();

  loadMe();
  loadSite();
  syncDayControls();
  loadFlow();
  tick();
  setInterval(tick, 10000);
  // The Sankey is a whole-day integral; refreshing it on the 10s power tick
  // would redraw constantly for a figure that moves by minutes.
  setInterval(() => { if (selectedDay === todayISO()) loadFlow(); }, 120000);
})();
