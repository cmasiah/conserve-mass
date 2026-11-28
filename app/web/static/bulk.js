/* Bulk job page: tick plan steps without reloading (falls back to a normal post), and a
   busy label on the Plan button. */
(() => {
  document.querySelectorAll('button[data-busy]').forEach(b => b.closest('form').addEventListener('submit', () => {
    b.textContent = b.dataset.busy; b.classList.add('busy');
  }));
  const total = document.querySelectorAll('.step').length;
  const sync = () => {
    const n = document.querySelectorAll('.step.done').length;
    const bar = document.querySelector('.plan-progress .meter > span');
    const txt = document.querySelector('.plan-progress');
    if (bar) bar.style.width = (total ? Math.round(100 * n / total) : 0) + '%';
    if (txt) txt.lastChild.textContent = ` ${n} / ${total} steps`;
    document.querySelectorAll('details.trip').forEach(d => {
      const steps = d.querySelectorAll('.step'), done = d.querySelectorAll('.step.done');
      d.querySelector('.trip-count').textContent = `${done.length}/${steps.length}`;
      d.classList.toggle('trip-done', steps.length && done.length === steps.length);
    });
  };
  document.addEventListener('submit', async e => {
    const f = e.target;
    if (!f.matches('.step-form')) return;
    e.preventDefault();
    const li = f.closest('.step'), btn = f.querySelector('button'), flag = f.querySelector('[name=done]');
    btn.disabled = true;
    try {
      const r = await fetch(f.action, { method: 'POST', body: new FormData(f),
        headers: { 'X-Requested-With': 'fetch' }, credentials: 'same-origin' });
      if (!r.ok) throw new Error(r.status);
      const nowDone = flag.value === '1';
      li.classList.toggle('done', nowDone);
      btn.textContent = nowDone ? '✓' : '';
      btn.setAttribute('aria-pressed', nowDone);
      flag.value = nowDone ? '0' : '1';
      sync();
    } catch (err) { f.submit(); }
    btn.disabled = false;
  });
})();
