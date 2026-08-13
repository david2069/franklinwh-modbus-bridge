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

  function renderEnergy(sensors) {
    if (!sensors || sensors.error || !Array.isArray(sensors.sensors)) return;
    const byKey = {};
    for (const s of sensors.sensors) byKey[s.id] = s.value;
    const solar = byKey['energy.solar.today_kwh'];
    const imp = byKey['energy.grid_import.today_kwh'];
    const exp = byKey['energy.grid_export.today_kwh'];
    if (solar == null && imp == null && exp == null) return;
    $('energyCard').style.display = 'block';
    $('enSolar').textContent = fmtKwh(solar);
    $('enImport').textContent = fmtKwh(imp);
    $('enExport').textContent = fmtKwh(exp);
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

  loadMe();
  loadSite();
  tick();
  setInterval(tick, 10000);
})();
