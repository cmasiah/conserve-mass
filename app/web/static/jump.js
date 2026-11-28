/* Jump Planner: system autocomplete (fuzzy, from /jump/systems) and busy buttons. */
(() => {
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  document.querySelectorAll('input[data-ac]').forEach((input) => {
    const box = document.createElement('div');
    box.className = 'ac-list'; box.hidden = true;
    input.parentElement.appendChild(box);
    let items = [], active = -1, timer = null, seq = 0;
    const close = () => { box.hidden = true; active = -1; };
    const pick = (i) => { if (items[i]) { input.value = items[i].name; close(); input.dispatchEvent(new Event('change')); } };
    const draw = () => {
      if (!items.length) { box.innerHTML = '<div class="ac-empty">No matching system' +
        '</div>'; box.hidden = !input.value.trim(); return; }
      box.innerHTML = items.map((s, i) => {
        const cls = s.sec >= 0.45 ? 'hs' : (s.sec > 0 ? 'ls' : 'ns');
        return `<div class="ac-item${i === active ? ' on' : ''}" data-i="${i}">` +
          `<span>${esc(s.name)}</span><span class="sec ${cls}">${s.sec.toFixed(1)}</span>` +
          `<span class="ac-region">${esc(s.region)}${s.cyno ? '' : ' · no cyno'}</span></div>`;
      }).join('');
      box.hidden = false;
    };
    const load = async () => {
      const q = input.value.trim(); const mine = ++seq;
      if (!q) { items = []; close(); return; }
      try {
        const r = await fetch('/jump/systems?q=' + encodeURIComponent(q));
        if (mine !== seq) return;
        items = r.ok ? await r.json() : []; active = items.length ? 0 : -1; draw();
      } catch (_) { /* offline: leave the box closed */ }
    };
    input.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(load, 120); });
    input.addEventListener('focus', () => { if (input.value.trim()) load(); });
    input.addEventListener('keydown', (e) => {
      if (box.hidden) return;
      if (e.key === 'ArrowDown') { active = Math.min(items.length - 1, active + 1); draw(); e.preventDefault(); }
      else if (e.key === 'ArrowUp') { active = Math.max(0, active - 1); draw(); e.preventDefault(); }
      else if (e.key === 'Enter' && active >= 0) { pick(active); e.preventDefault(); }
      else if (e.key === 'Escape') close();
    });
    box.addEventListener('mousedown', (e) => { const el = e.target.closest('.ac-item'); if (el) { pick(+el.dataset.i); e.preventDefault(); } });
    input.addEventListener('blur', () => setTimeout(close, 150));
  });
  // Show that something is happening once a form is submitted.
  document.querySelectorAll('form').forEach((f) => f.addEventListener('submit', () => {
    const b = f.querySelector('button[data-busy]');
    if (b) { b.dataset.label = b.textContent; b.textContent = b.dataset.busy; b.classList.add('busy'); setTimeout(() => { b.disabled = true; }, 0); }
  }));
})();
