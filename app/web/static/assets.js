/* Industry › Assets: the Places tree.
   - Watch stars and pins save in the background; the tree is refreshed in place, so the
     page keeps its scroll position and whatever is open.
   - Systems and structures remember being open or closed (this browser).
   - Expand all / Collapse all, and a search box that finds structures, divisions and
     containers. */
(() => {
  const tree = document.getElementById('places-tree');
  if (!tree) return;
  const panel = tree.closest('.places');
  const KEY = 'cmass.assets.open';
  let open = {};
  try { open = JSON.parse(localStorage.getItem(KEY)) || {}; } catch (e) {}
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(open)); } catch (e) {} };

  // ---- open / closed state
  const setSys = (sec, on, remember = true) => {
    sec.classList.toggle('closed', !on);
    sec.querySelector('.sys-toggle').setAttribute('aria-expanded', on);
    if (remember) open['s:' + sec.dataset.sys] = on;
  };
  const apply = () => {
    tree.querySelectorAll('.place-sys').forEach(sec => {
      const k = 's:' + sec.dataset.sys;
      setSys(sec, k in open ? open[k] : true, false);
    });
    tree.querySelectorAll('.place-site').forEach(d => {
      const k = 'p:' + d.dataset.site;
      if (k in open && !d.querySelector('a.sel')) d.open = open[k];
    });
  };
  tree.addEventListener('click', e => {
    const t = e.target.closest('.sys-toggle');
    if (!t) return;
    const sec = t.closest('.place-sys');
    setSys(sec, sec.classList.contains('closed'));
    save();
  });
  // <details> 'toggle' doesn't bubble: listen in the capture phase.
  tree.addEventListener('toggle', e => {
    if (searching) return;
    const d = e.target;
    if (d.classList && d.classList.contains('place-site')) { open['p:' + d.dataset.site] = d.open; save(); }
  }, true);
  panel.querySelectorAll('[data-tree]').forEach(b => b.addEventListener('click', () => {
    const on = b.dataset.tree === 'expand';
    tree.querySelectorAll('.place-sys').forEach(sec => setSys(sec, on));
    tree.querySelectorAll('.place-site').forEach(d => { d.open = on; open['p:' + d.dataset.site] = on; });
    save();
  }));

  // ---- search the tree (names of systems, structures, divisions, containers)
  const q = document.getElementById('place-q');
  let searching = false;
  const filter = () => {
    const needle = q.value.trim().toLowerCase();
    searching = !!needle;
    let any = false;
    tree.querySelectorAll('.place-sys').forEach(sec => {
      if (!needle) {
        sec.hidden = false;
        sec.querySelectorAll('.place-site, .place-owner, .place-cont').forEach(el => { el.hidden = false; });
        return;
      }
      const sysHit = sec.dataset.text.includes(needle);
      let secHit = sysHit;
      sec.querySelectorAll('.place-site').forEach(site => {
        const siteHit = sysHit || site.dataset.text.includes(needle);
        let rowHit = false;
        site.querySelectorAll('.place-owner, .place-cont').forEach(row => {
          const hit = siteHit || row.dataset.text.includes(needle);
          row.hidden = !hit;
          if (!siteHit && hit) rowHit = true;
        });
        // A container hit also shows the division / owner it sits under.
        if (rowHit) site.querySelectorAll('.place-cont:not([hidden])').forEach(c => {
          let p = c.previousElementSibling;
          while (p && !p.classList.contains('place-owner')) p = p.previousElementSibling;
          if (p) p.hidden = false;
        });
        site.hidden = !(siteHit || rowHit);
        if (rowHit || (siteHit && !sysHit)) site.open = true;
        if (!site.hidden) secHit = true;
      });
      sec.hidden = !secHit;
      if (secHit) setSys(sec, true, false);
      any = any || secHit;
    });
    const none = tree.querySelector('.place-none');
    if (none) none.hidden = !needle || any;
    if (!needle) apply();
  };
  q.addEventListener('input', filter);
  q.addEventListener('keydown', e => { if (e.key === 'Escape') { q.value = ''; filter(); } });

  // ---- stars and pins: save without leaving the page
  tree.addEventListener('submit', async e => {
    const form = e.target;
    if (!form.matches('.watch-form, .pin-form')) return;
    e.preventDefault();
    const btn = form.querySelector('button');
    btn.disabled = true;
    try {
      const r = await fetch(form.action, { method: 'POST', body: new FormData(form),
        headers: { 'X-Requested-With': 'fetch' }, credentials: 'same-origin' });
      if (!r.ok) throw new Error(r.status);
      await refresh();
    } catch (err) {
      btn.disabled = false;
      form.submit();          // fall back to the normal way
    }
  });

  // Re-render the tree from the server (stars further down can change: "already
  // counted" marks, pin order) and put it back exactly as it was on screen.
  async function refresh() {
    const top = panel.scrollTop, winY = window.scrollY;
    const r = await fetch(location.href, { credentials: 'same-origin' });
    const doc = new DOMParser().parseFromString(await r.text(), 'text/html');
    const fresh = doc.getElementById('places-tree');
    if (fresh) tree.innerHTML = fresh.innerHTML;
    // If the item list depends on watched places, swap it too.
    if (/[?&]watched=1/.test(location.search)) {
      const a = document.querySelector('.items'), b = doc.querySelector('.items');
      if (a && b) { a.querySelector('tbody').innerHTML = b.querySelector('tbody').innerHTML;
        a.querySelector('.scope').innerHTML = b.querySelector('.scope').innerHTML; }
    }
    const count = document.querySelector('.watch-count'), c2 = doc.querySelector('.watch-count');
    if (count && c2) count.textContent = c2.textContent;
    apply();
    if (q.value) filter();
    panel.scrollTop = top;
    window.scrollTo(0, winY);
  }

  apply();
})();
