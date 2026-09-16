// Energy-flow Sankey — hand-rolled SVG, no chart library.
//
// Chart.js ships no Sankey and the plugin that adds one would be another
// vendored bundle to keep offline-safe under HA ingress. The topology here is
// fixed and tiny — three sources, three sinks, seven possible arcs — so the
// layout is a dozen lines of arithmetic rather than a general graph solver,
// and it inherits the page's CSS variables instead of fighting a theme API.
//
// Consumed by both dashboards: the admin SPA (Alpine) and /user (vanilla), so
// this deliberately depends on neither.
(function () {
  'use strict';

  const NS = 'http://www.w3.org/2000/svg';

  // Left-hand sources and right-hand sinks. `key` matches the API's node
  // totals; `arcs` lists the flow ids that leave (or arrive at) that node.
  const SOURCES = [
    { id: 'solar', label: 'Solar', colour: 'var(--solar, #f59e0b)',
      arcs: ['solar_to_home', 'solar_to_battery', 'solar_to_grid'] },
    { id: 'battery_discharge', label: 'Battery', colour: 'var(--ok, #34d399)',
      arcs: ['battery_to_home', 'battery_to_grid'] },
    { id: 'grid_import', label: 'Grid', colour: 'var(--danger, #f87171)',
      arcs: ['grid_to_home', 'grid_to_battery'] },
  ];
  const SINKS = [
    { id: 'home', label: 'Home', colour: 'var(--accent, #22d3ee)',
      arcs: ['solar_to_home', 'battery_to_home', 'grid_to_home'] },
    { id: 'battery_charge', label: 'Battery', colour: 'var(--ok, #34d399)',
      arcs: ['solar_to_battery', 'grid_to_battery'] },
    { id: 'grid_export', label: 'Export', colour: 'var(--violet, #a78bfa)',
      arcs: ['solar_to_grid', 'battery_to_grid'] },
  ];

  const el = (name, attrs) => {
    const n = document.createElementNS(NS, name);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    return n;
  };

  const fmt = (kwh) => `${Number(kwh).toFixed(kwh >= 10 ? 1 : 2)} kWh`;

  /**
   * Draw the diagram into `host`.
   * @param {Element} host      container (cleared first)
   * @param {Object}  flows     arc id -> kWh
   * @param {Object}  [opts]    {height, minShare}
   */
  function render(host, flows, opts) {
    const o = opts || {};
    host.innerHTML = '';
    flows = flows || {};

    const total = Object.values(flows).reduce((a, b) => a + (Number(b) || 0), 0);
    if (!(total > 0)) {
      const p = document.createElement('p');
      p.className = 'sankey-empty';
      p.textContent = 'No energy recorded for this day yet.';
      host.appendChild(p);
      return;
    }

    const W = host.clientWidth || 360;
    const H = o.height || 260;
    const NODE_W = 10;
    const PAD_Y = 8;
    // Stacked nodes need enough vertical separation that two *thin* neighbours
    // (a 0.1 kWh grid import next to a small discharge) don't collide once
    // their labels are centred on them.
    const GAP = 16;
    const labelPad = 7;
    // Labels live in their own margins rather than over the ribbons. Drawn
    // inside the plot they sit on saturated fills at whatever contrast the
    // ribbon happens to have, and the right-hand ones read as truncated
    // because the ribbon continues underneath them.
    const MARGIN = Math.min(66, Math.max(44, Math.round(W * 0.17)));

    const sum = (node) => node.arcs.reduce((a, k) => a + (Number(flows[k]) || 0), 0);

    // A column's arcs must fill the same height on both sides, or the ribbons
    // wouldn't meet. Scale each column independently by its own total: the two
    // totals are equal by construction (every arc has one source and one sink).
    const live = (list) => list.map((n) => ({ ...n, value: sum(n) }))
                                .filter((n) => n.value > 0);
    const left = live(SOURCES);
    const right = live(SINKS);

    const colHeight = (col) =>
      H - 2 * PAD_Y - GAP * Math.max(col.length - 1, 0);
    const scaleFor = (col) => {
      const t = col.reduce((a, n) => a + n.value, 0);
      return t > 0 ? colHeight(col) / t : 0;
    };
    const sL = scaleFor(left);
    const sR = scaleFor(right);

    // Assign each node a y-span, then hand out sub-spans to its arcs in the
    // declared order so ribbons don't cross more than they must.
    const place = (col, scale, x) => {
      let y = PAD_Y;
      const byId = {};
      for (const n of col) {
        const h = n.value * scale;
        n.y = y; n.h = h; n.x = x;
        let cursor = y;
        n.slots = {};
        for (const k of n.arcs) {
          const v = Number(flows[k]) || 0;
          if (v <= 0) continue;
          const sh = v * scale;
          n.slots[k] = { y: cursor, h: sh };
          cursor += sh;
        }
        byId[n.id] = n;
        y += h + GAP;
      }
      return byId;
    };

    const leftX = MARGIN;
    const rightX = W - MARGIN - NODE_W;
    const L = place(left, sL, leftX);
    const R = place(right, sR, rightX);

    const svg = el('svg', {
      width: '100%', height: String(H),
      viewBox: `0 0 ${W} ${H}`, class: 'sankey-svg',
      role: 'img', 'aria-label': 'Energy flow diagram',
    });

    // Gradient per arc so a ribbon reads as leaving one colour and arriving at
    // another — the quickest way to see "solar became export" at a glance.
    const defs = el('defs', {});
    svg.appendChild(defs);

    const arcs = [];
    for (const src of left) {
      for (const k of src.arcs) {
        const v = Number(flows[k]) || 0;
        if (v <= 0) continue;
        const dst = right.find((n) => n.arcs.includes(k));
        if (!dst) continue;
        arcs.push({ k, v, src, dst });
      }
    }
    // Draw the fattest first so thin ribbons stay clickable on top.
    arcs.sort((a, b) => b.v - a.v);

    arcs.forEach((a, i) => {
      const gid = `sk-${i}-${Math.round(a.v * 1000)}`;
      const grad = el('linearGradient', {
        id: gid, x1: '0', x2: '1', y1: '0', y2: '0',
      });
      const s0 = el('stop', { offset: '0%', 'stop-color': a.src.colour });
      const s1 = el('stop', { offset: '100%', 'stop-color': a.dst.colour });
      grad.appendChild(s0); grad.appendChild(s1);
      defs.appendChild(grad);

      const ls = a.src.slots[a.k];
      const rs = a.dst.slots[a.k];
      const x0 = leftX + NODE_W;
      const x1 = rightX;
      const cx = (x0 + x1) / 2;

      // Two cubics enclosing the ribbon: along the top edge, down the right,
      // back along the bottom edge.
      const d = [
        `M ${x0} ${ls.y}`,
        `C ${cx} ${ls.y}, ${cx} ${rs.y}, ${x1} ${rs.y}`,
        `L ${x1} ${rs.y + rs.h}`,
        `C ${cx} ${rs.y + rs.h}, ${cx} ${ls.y + ls.h}, ${x0} ${ls.y + ls.h}`,
        'Z',
      ].join(' ');

      const path = el('path', {
        d, fill: `url(#${gid})`, 'fill-opacity': '0.45',
        class: 'sankey-arc', tabindex: '0',
      });
      const title = el('title', {});
      title.textContent = `${a.src.label} → ${a.dst.label}: ${fmt(a.v)}`;
      path.appendChild(title);
      svg.appendChild(path);
    });

    // Nodes last, so they sit above the ribbons they anchor.
    const drawNodes = (col, anchorEnd) => {
      for (const n of col) {
        const rect = el('rect', {
          x: String(n.x), y: String(n.y), width: String(NODE_W),
          height: String(Math.max(n.h, 2.5)), rx: '3',
          fill: n.colour, class: 'sankey-node',
        });
        const t = el('title', {});
        t.textContent = `${n.label}: ${fmt(n.value)}`;
        rect.appendChild(t);
        svg.appendChild(rect);

        // Left column labels sit in the left margin (anchored end, so they
        // grow away from the plot); right column mirrors that.
        const tx = anchorEnd ? n.x + NODE_W + labelPad : n.x - labelPad;
        const anchor = anchorEnd ? 'start' : 'end';
        const mid = n.y + n.h / 2;
        // A tall node carries name over value; a thin one has room for the
        // name only, centred — stacking two lines on a 6px band just produces
        // overlapping text.
        const twoLine = n.h >= 30;

        const label = el('text', {
          x: String(tx), y: String(twoLine ? mid - 6 : mid),
          'text-anchor': anchor, 'dominant-baseline': 'middle',
          class: 'sankey-label',
        });
        label.textContent = n.label;
        svg.appendChild(label);

        if (twoLine) {
          const value = el('text', {
            x: String(tx), y: String(mid + 7),
            'text-anchor': anchor, 'dominant-baseline': 'middle',
            class: 'sankey-value',
          });
          value.textContent = fmt(n.value);
          svg.appendChild(value);
        }
      }
    };
    drawNodes(left, false);
    drawNodes(right, true);

    host.appendChild(svg);
  }

  window.FWHSankey = { render, SOURCES, SINKS };
})();
