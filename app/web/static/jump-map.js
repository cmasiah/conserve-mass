/* Jump Planner route map, in the style of Dotlan's region maps: systems as diamonds,
   stargates as grey lines (dashed across constellation / region borders), jumps as bright
   arcs. Zoom with the wheel, pinch or the buttons; drag to pan. The step list on the side
   walks through each jump and zooms to it.

   Positions are real (top-down, north up), nudged apart just enough that names don't
   overlap, so the layout stays recognisable while reading like a schematic. */
(() => {
  const svg = document.getElementById('jmap');
  if (!svg) return;
  const D = JSON.parse(document.getElementById('jmap-data').textContent);
  const STEPS = JSON.parse(document.getElementById('jmap-steps').textContent);
  const NS = 'http://www.w3.org/2000/svg';
  const N = D.nodes;
  const idxOf = new Map(N.map((n, i) => [n.id, i]));
  N.forEach(n => { n.ox = n.x; n.oy = n.y; });

  let W = svg.clientWidth || 900, H = svg.clientHeight || 600;
  const view = { cx: 0, cy: 0, k: 1 };

  const bbox = list => {
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const n of list) { x0 = Math.min(x0, n.x); y0 = Math.min(y0, n.y); x1 = Math.max(x1, n.x); y1 = Math.max(y1, n.y); }
    return { x0, y0, x1, y1 };
  };
  const fitView = (list, pad = 70, maxZoom = Infinity) => {
    const b = bbox(list);
    const w = Math.max(b.x1 - b.x0, 0.5), h = Math.max(b.y1 - b.y0, 0.5);
    const k = Math.min((W - 2 * pad) / w, (H - 2 * pad) / h, maxZoom);
    return { cx: (b.x0 + b.x1) / 2, cy: (b.y0 + b.y1) / 2, k };
  };
  const routeNodes = () => { const r = N.filter(n => n.on); return r.length ? r : N; };

  // ---- de-clutter: push systems apart until names fit (wider than tall), but keep each
  // one pulled toward where it really is.
  const k0 = fitView(routeNodes()).k;
  const REF = k0 * 1.8;                 // zoom at which labels must not overlap
  const DX = 92 / REF, DY = 40 / REF;   // space a label needs, in map units
  (function relax(iters) {
    for (let it = 0; it < iters; it++) {
      const grid = new Map();
      const cell = (x, y) => Math.floor(x / DX) + ',' + Math.floor(y / DY);
      N.forEach((n, i) => { const c = cell(n.x, n.y); (grid.get(c) || grid.set(c, []).get(c)).push(i); });
      let moved = 0;
      N.forEach((a, i) => {
        const gx = Math.floor(a.x / DX), gy = Math.floor(a.y / DY);
        for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) {
          const list = grid.get((gx + dx) + ',' + (gy + dy));
          if (!list) continue;
          for (const j of list) {
            if (j <= i) continue;
            const b = N[j];
            let vx = b.x - a.x, vy = b.y - a.y;
            const ex = Math.abs(vx) / DX, ey = Math.abs(vy) / DY;  // elliptical distance
            const d = Math.hypot(ex, ey);
            if (d >= 1) continue;
            if (d < 1e-6) { vx = (Math.random() - 0.5) * DX; vy = (Math.random() - 0.5) * DY; }
            const len = Math.hypot(vx / DX, vy / DY) || 1;
            const push = (1 - d) / 2;
            const ux = (vx / DX) / len * DX * push, uy = (vy / DY) / len * DY * push;
            a.x -= ux; a.y -= uy; b.x += ux; b.y += uy; moved++;
          }
        }
      });
      N.forEach(n => { n.x += (n.ox - n.x) * 0.03; n.y += (n.oy - n.y) * 0.03; });
      if (!moved) break;
    }
  })(90);

  // ---- build the drawing once; update() only moves things.
  const el = (tag, attrs, parent) => {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  };
  svg.innerHTML = '';
  const root = el('g', {}, svg);
  const L = {};
  for (const name of ['regions', 'gates', 'gpath', 'ring', 'jumps', 'nodes', 'labels'])
    L[name] = el('g', { class: 'jm-' + name }, root);

  // Region names at the middle of their systems.
  const regions = new Map();
  N.forEach(n => { if (!n.rn) return; const r = regions.get(n.rn) || { x: 0, y: 0, c: 0 }; r.x += n.x; r.y += n.y; r.c++; regions.set(n.rn, r); });
  const regionEls = [...regions].filter(([, r]) => r.c >= 4).map(([name, r]) => {
    const t = el('text', { class: 'jm-region' }, L.regions);
    t.textContent = name.toUpperCase();
    return { t, x: r.x / r.c, y: r.y / r.c };
  });

  const gateEls = D.gates.map(([a, b, kind]) => ({ a, b, e: el('line', { class: 'jm-gate g' + kind }, L.gates) }));
  const gpathEls = D.legs.map((leg, li) => leg.gates.length > 1
    ? el('polyline', { class: 'jm-gpath leg' + li, 'data-leg': li }, L.gpath) : null);
  const ring = el('circle', { class: 'jm-ring', r: 0 }, L.ring);
  const jumpEls = [];
  D.legs.forEach((leg, li) => leg.jumps.forEach(([a, b, ly], ji) => {
    const p = el('path', { class: 'jm-jump leg' + li, 'marker-end': `url(#jm-arrow${li})` }, L.jumps);
    const t = el('text', { class: 'jm-ly leg' + li }, L.jumps);
    t.textContent = ly.toFixed(1) + ' LY';
    jumpEls.push({ li, ji, a, b, p, t });
  }));
  const defs = el('defs', {}, svg);
  D.legs.forEach((_, li) => {
    const m = el('marker', { id: 'jm-arrow' + li, viewBox: '0 0 10 10', refX: 9, refY: 5,
      markerWidth: 6, markerHeight: 6, orient: 'auto-start-reverse' }, defs);
    el('path', { d: 'M0,0 L10,5 L0,10 z', class: 'jm-arrowhead leg' + li }, m);
  });

  const roles = new Map();
  D.legs.forEach(leg => {
    leg.jumps.forEach(([a, b]) => { if (!roles.has(a)) roles.set(a, 'mid'); roles.set(b, 'mid'); });
  });
  if (D.legs.length) {
    const first = D.legs[0];
    const start = first.jumps.length ? first.jumps[0][0] : first.gates[0];
    const lastJ = first.jumps[first.jumps.length - 1];
    const end = first.gates.length ? first.gates[first.gates.length - 1] : (lastJ ? lastJ[1] : null);
    if (start != null) roles.set(start, 'start');
    if (end != null) roles.set(end, 'end');
  }
  const secClass = s => s >= 0.45 ? 'hs' : s > 0 ? 'ls' : 'ns';
  const nodeEls = N.map((n, i) => {
    const role = roles.get(i);
    const g = el('g', { class: 'jm-node ' + (role ? 'on ' + role : 'off') }, L.nodes);
    const shape = role ? el('circle', {}, g) : el('polygon', {}, g);
    el('title', {}, g).textContent = `${n.n} (${n.s.toFixed(1)})${n.rn ? ' · ' + n.rn : ''}`;
    const t = el('text', { class: 'jm-label' + (role ? ' on' : '') }, L.labels);
    t.textContent = n.n + ' ';
    const sec = el('tspan', { class: 'jm-sec ' + secClass(n.s) }, t);
    sec.textContent = n.s.toFixed(1);
    return { g, shape, t, role };
  });

  // ---- drawing
  const sx = n => (n.x - view.cx) * view.k + W / 2;
  const sy = n => (n.y - view.cy) * view.k + H / 2;
  let active = -1;

  function update() {
    const z = view.k / k0;
    const h = Math.max(2.2, Math.min(9, 4.2 * Math.sqrt(z)));
    const showLabels = view.k >= REF * 0.72;
    svg.classList.toggle('zoomed-out', !showLabels);
    for (const r of regionEls) { r.t.setAttribute('x', (r.x - view.cx) * view.k + W / 2); r.t.setAttribute('y', (r.y - view.cy) * view.k + H / 2); }
    for (const g of gateEls) {
      const a = N[g.a], b = N[g.b];
      g.e.setAttribute('x1', sx(a)); g.e.setAttribute('y1', sy(a)); g.e.setAttribute('x2', sx(b)); g.e.setAttribute('y2', sy(b));
    }
    D.legs.forEach((leg, li) => {
      const pl = gpathEls[li];
      if (pl) pl.setAttribute('points', leg.gates.map(i => sx(N[i]) + ',' + sy(N[i])).join(' '));
    });
    for (const j of jumpEls) {
      const a = N[j.a], b = N[j.b];
      const ax = sx(a), ay = sy(a), bx = sx(b), by = sy(b);
      const mx = (ax + bx) / 2, my = (ay + by) / 2, dx = bx - ax, dy = by - ay;
      const bend = j.li ? -0.2 : 0.2;   // the return leg bows the other way
      const cx = mx - dy * bend, cy = my + dx * bend;
      j.p.setAttribute('d', `M${ax},${ay} Q${cx},${cy} ${bx},${by}`);
      const len = Math.hypot(dx, dy) || 1, off = (j.li ? -1 : 1) * 12;
      j.t.setAttribute('x', (mx + cx) / 2 - dy / len * off);
      j.t.setAttribute('y', (my + cy) / 2 + dx / len * off + 4);
    }
    N.forEach((n, i) => {
      const e = nodeEls[i], x = sx(n), y = sy(n);
      if (e.role) {
        e.shape.setAttribute('cx', x); e.shape.setAttribute('cy', y);
        e.shape.setAttribute('r', e.role === 'mid' ? h + 1.5 : h + 3);
      } else {
        e.shape.setAttribute('points', `${x},${y - h} ${x + h},${y} ${x},${y + h} ${x - h},${y}`);
      }
      e.t.setAttribute('x', x); e.t.setAttribute('y', y + h + 13);
      e.t.style.display = (showLabels || e.role) ? '' : 'none';
    });
    if (active >= 0) placeRing();
  }

  // ---- step-by-step
  const items = [...document.querySelectorAll('.jstep')];
  const pos = document.getElementById('jstep-pos');
  function placeRing() {
    const st = STEPS[active];
    if (!st || st.kind !== 'jump') { ring.style.display = 'none'; return; }
    const a = N[idxOf.get(st.from_id)];
    ring.style.display = '';
    ring.setAttribute('cx', sx(a)); ring.setAttribute('cy', sy(a));
    // Range ring around where this jump starts. Map units are LY, but systems were nudged
    // apart a little, so treat it as a guide.
    ring.setAttribute('r', D.range * view.k);
  }
  function setStep(i) {
    active = Math.max(-1, Math.min(STEPS.length - 1, i));
    items.forEach((li, j) => li.classList.toggle('active', j === active));
    svg.classList.toggle('focus', active >= 0);
    jumpEls.forEach(j => { j.p.classList.remove('active'); j.t.classList.remove('active'); });
    gpathEls.forEach(p => p && p.classList.remove('active'));
    nodeEls.forEach(e => e.g.classList.remove('active'));
    if (active < 0) {
      pos.textContent = 'All';
      ring.style.display = 'none';
      animateTo(fitView(routeNodes()));
      return;
    }
    const st = STEPS[active];
    pos.textContent = `${active + 1} / ${STEPS.length}`;
    const a = idxOf.get(st.from_id), b = idxOf.get(st.to_id);
    [a, b].forEach(i => i != null && nodeEls[i].g.classList.add('active'));
    let focus = [N[a], N[b]].filter(Boolean);
    if (st.kind === 'jump') {
      const j = jumpEls.find(j => j.li === st.leg && j.ji === st.index);
      if (j) { j.p.classList.add('active'); j.t.classList.add('active'); }
    } else {
      const pl = gpathEls[st.leg];
      if (pl) pl.classList.add('active');
      focus = D.legs[st.leg].gates.map(i => N[i]);
    }
    animateTo(fitView(focus, 110, REF * 2.2));
    items[active].scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }
  items.forEach((li, i) => {
    li.addEventListener('click', () => setStep(active === i ? -1 : i));
    li.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setStep(i); } });
  });
  document.querySelector('[data-step="prev"]').addEventListener('click', () => setStep(active <= 0 ? -1 : active - 1));
  document.querySelector('[data-step="next"]').addEventListener('click', () => setStep(active + 1));
  pos.addEventListener('click', () => setStep(-1));
  document.addEventListener('keydown', e => {
    if (/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) return;
    const r = svg.getBoundingClientRect();
    if (r.bottom < 0 || r.top > innerHeight) return;
    if (e.key === 'ArrowRight') { e.preventDefault(); setStep(active + 1); }
    if (e.key === 'ArrowLeft') { e.preventDefault(); setStep(active <= 0 ? -1 : active - 1); }
    if (e.key === 'Escape') setStep(-1);
  });

  // ---- zoom and pan
  const clampK = k => Math.max(k0 * 0.25, Math.min(k0 * 40, k));
  let anim = 0;
  function animateTo(t, ms = 380) {
    cancelAnimationFrame(anim);
    const f = { ...view }, t0 = performance.now();
    t.k = clampK(t.k);
    const step = now => {
      const p = Math.min(1, (now - t0) / ms), e = p < 0.5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2;
      view.cx = f.cx + (t.cx - f.cx) * e; view.cy = f.cy + (t.cy - f.cy) * e;
      view.k = Math.exp(Math.log(f.k) + (Math.log(t.k) - Math.log(f.k)) * e);
      update();
      if (p < 1) anim = requestAnimationFrame(step);
    };
    anim = requestAnimationFrame(step);
  }
  function zoomAt(px, py, factor) {
    const k = clampK(view.k * factor);
    const wx = (px - W / 2) / view.k + view.cx, wy = (py - H / 2) / view.k + view.cy;
    view.k = k;
    view.cx = wx - (px - W / 2) / k; view.cy = wy - (py - H / 2) / k;
    update();
  }
  svg.addEventListener('wheel', e => {
    e.preventDefault();
    const r = svg.getBoundingClientRect();
    zoomAt(e.clientX - r.left, e.clientY - r.top, Math.exp(-e.deltaY * 0.0016));
  }, { passive: false });
  svg.addEventListener('dblclick', e => {
    const r = svg.getBoundingClientRect();
    zoomAt(e.clientX - r.left, e.clientY - r.top, 1.8);
  });
  const ptrs = new Map();
  let pinch = null;
  svg.addEventListener('pointerdown', e => {
    svg.setPointerCapture(e.pointerId);
    ptrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
    svg.classList.add('dragging');
  });
  svg.addEventListener('pointermove', e => {
    const prev = ptrs.get(e.pointerId);
    if (!prev) return;
    const cur = { x: e.clientX, y: e.clientY };
    ptrs.set(e.pointerId, cur);
    if (ptrs.size === 2) {
      const [p1, p2] = [...ptrs.values()];
      const d = Math.hypot(p1.x - p2.x, p1.y - p2.y);
      const r = svg.getBoundingClientRect();
      if (pinch) zoomAt((p1.x + p2.x) / 2 - r.left, (p1.y + p2.y) / 2 - r.top, d / pinch);
      pinch = d;
      return;
    }
    view.cx -= (cur.x - prev.x) / view.k; view.cy -= (cur.y - prev.y) / view.k;
    update();
  });
  const up = e => { ptrs.delete(e.pointerId); if (ptrs.size < 2) pinch = null; if (!ptrs.size) svg.classList.remove('dragging'); };
  svg.addEventListener('pointerup', up);
  svg.addEventListener('pointercancel', up);
  document.querySelectorAll('[data-zoom]').forEach(b => b.addEventListener('click', () => {
    const z = b.dataset.zoom;
    if (z === 'fit') return setStep(-1);
    animateTo({ cx: view.cx, cy: view.cy, k: view.k * (z === 'in' ? 1.6 : 1 / 1.6) }, 220);
  }));

  new ResizeObserver(() => { W = svg.clientWidth; H = svg.clientHeight; update(); }).observe(svg);
  Object.assign(view, fitView(routeNodes()));
  update();
})();
