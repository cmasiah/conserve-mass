/* Shared page behaviour: foldable sections, click-to-sort columns, expandable groups.
   Folding uses a CSS class (not <details>), so large tables always reappear on expand. */
(() => {
  const store = {
    get(k) { try { return JSON.parse(localStorage.getItem(k)); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} },
  };

  // ---- Foldable sections: <section class="fold open" data-fold="id"><button class="fold-head">
  const folds = store.get('cmass.folds') || {};
  document.querySelectorAll('.fold').forEach(sec => {
    const id = sec.dataset.fold;
    if (id && id in folds) sec.classList.toggle('open', folds[id]);
    sec.querySelector('.fold-head').addEventListener('click', () => {
      const open = sec.classList.toggle('open');
      if (id) { folds[id] = open; store.set('cmass.folds', folds); }
    });
  });

  // ---- Sortable tables: <table class="sortable"><th data-sort="num|text" [data-default="desc"]>
  // A table with several <tbody> elements (grouped rows) sorts whole groups by their first row.
  const value = (row, i, type) => {
    const cell = row.cells[i];
    if (!cell) return type === 'num' ? -Infinity : '';
    const raw = cell.dataset.v ?? cell.textContent.trim();
    return type === 'num' ? (parseFloat(raw) || 0) : raw.toLowerCase();
  };
  document.querySelectorAll('table.sortable').forEach((table, ti) => {
    const heads = [...table.tHead.rows[0].cells];
    const key = 'cmass.sort.' + location.pathname + '.' + ti;
    const sortBy = (i, dir, remember = true) => {
      const th = heads[i], type = th.dataset.sort;
      const grouped = table.tBodies.length > 1;
      const units = grouped ? [...table.tBodies] : [...table.tBodies[0].rows];
      const rowOf = u => grouped ? u.rows[0] : u;
      units.sort((a, b) => {
        const x = value(rowOf(a), i, type), y = value(rowOf(b), i, type);
        const c = type === 'num' ? x - y : x.localeCompare(y);
        return dir === 'asc' ? c : -c;
      });
      const parent = grouped ? table : table.tBodies[0];
      units.forEach(u => parent.appendChild(u));
      heads.forEach(h => h.removeAttribute('aria-sort'));
      th.setAttribute('aria-sort', dir === 'asc' ? 'ascending' : 'descending');
      if (remember) store.set(key, [i, dir]);
    };
    heads.forEach((th, i) => {
      if (!th.dataset.sort) return;
      th.classList.add('sort-th');
      th.addEventListener('click', () => {
        const cur = th.getAttribute('aria-sort');
        const dir = cur ? (cur === 'ascending' ? 'desc' : 'asc')
                        : (th.dataset.default || (th.dataset.sort === 'num' ? 'desc' : 'asc'));
        sortBy(i, dir);
      });
    });
    const saved = store.get(key);
    if (saved && heads[saved[0]] && heads[saved[0]].dataset.sort) sortBy(saved[0], saved[1], false);
  });

  // ---- Expandable groups: <tbody class="grp"><tr class="grp-row">…</tr><tr class="grp-item">…
  document.querySelectorAll('tbody.grp').forEach(tb => {
    const head = tb.querySelector('.grp-row');
    if (!head || !tb.querySelector('.grp-item')) return;
    head.addEventListener('click', e => {
      if (e.target.closest('a, button, form')) return;
      tb.classList.toggle('open');
    });
  });
})();
