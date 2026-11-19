// Progressive enhancement only — every form works without JavaScript.
// No inline handlers anywhere (strict CSP); all behaviour is wired here.
(function () {
  'use strict';
  var csrf = document.body ? document.body.getAttribute('data-csrf') || '' : '';

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function el(tag, text) { var n = document.createElement(tag); if (text != null) n.textContent = text; return n; }

  // theme toggle
  $$('#theme-toggle').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var root = document.documentElement;
      var cur = root.getAttribute('data-theme');
      if (!cur) cur = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
      var next = cur === 'dark' ? 'light' : 'dark';
      root.setAttribute('data-theme', next);
      try { localStorage.setItem('sto_theme', next); } catch (e) { /* ignore */ }
    });
  });

  // auto-submit selects (branch switcher)
  $$('select[data-autosubmit]').forEach(function (s) {
    s.addEventListener('change', function () { s.form.submit(); });
  });

  // demo accounts fill the login form
  $$('.demo-acc').forEach(function (b) {
    b.addEventListener('click', function () {
      $('#email').value = b.getAttribute('data-email');
      $('#password').value = b.getAttribute('data-password');
      $('#password').focus();
    });
  });

  // invoice print
  var pb = $('#print-btn');
  if (pb) pb.addEventListener('click', function () { window.print(); });

  // licence-plate lookup on the intake form
  var lookup = $('#plate-lookup');
  if (lookup) lookup.addEventListener('click', function () {
    var plate = ($('#plate').value || '').trim();
    var out = $('#plate-result');
    if (!plate) { out.textContent = '—'; return; }
    fetch('/integrations/plate?plate=' + encodeURIComponent(plate), { headers: { Accept: 'application/json' } })
      .then(function (r) { return r.ok ? r.json() : { found: false }; })
      .then(function (d) {
        if (!d.found) { out.textContent = '—'; return; }
        [['make', d.make], ['model', d.model], ['year', d.year], ['vin', d.vin]].forEach(function (p) {
          var f = document.getElementsByName(p[0])[0];
          if (f && p[1] != null && !f.value) f.value = p[1];
        });
        out.textContent = '✓ ' + [d.make, d.model, d.year].filter(Boolean).join(' ') + ' (' + d.source + ')';
      })
      .catch(function () { out.textContent = '—'; });
  });

  // AI: note → draft (fills the form; rendered with textContent only — no HTML injection)
  var aiBtn = $('#ai-draft-btn');
  if (aiBtn) aiBtn.addEventListener('click', function () {
    var text = ($('#ai-text').value || '').trim();
    var note = $('#ai-note');
    if (!text) { note.textContent = '—'; return; }
    note.textContent = '…';
    aiBtn.disabled = true;
    var body = new URLSearchParams();
    body.append('text', text);
    fetch('/ai/work-order-draft', {
      method: 'POST', body: body,
      headers: { 'X-CSRF-Token': csrf, Accept: 'application/json' }
    }).then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (res) {
        var d = res.draft || {};
        var v = d.vehicle || {};
        var setIf = function (name, val) { var f = document.getElementsByName(name)[0]; if (f && val) f.value = val; };
        setIf('plate', v.plate); setIf('make', v.make_model); setIf('complaint', d.complaint);
        note.textContent = Math.round((res.confidence || 0) * 100) + '% · ' + (res.fallback ? 'rules' : 'LLM');
        var works = $('#ai-works-list'); var need = $('#ai-need-list');
        works.replaceChildren(); need.replaceChildren();
        (d.works || []).forEach(function (w) {
          works.appendChild(el('li', w.service + (w.norm_hours ? ' · ' + w.norm_hours + ' h' : '')));
        });
        if (!works.children.length) works.appendChild(el('li', '—'));
        (d.needs_clarification || []).forEach(function (n) { need.appendChild(el('li', n)); });
        $('#ai-works').hidden = false;
      })
      .catch(function () { note.textContent = '⚠'; })
      .then(function () { aiBtn.disabled = false; });
  });

  // Kanban drag-and-drop with optimistic UI and rollback on any failure
  var board = $('.kb');
  if (board) {
    var toast = $('#kb-toast');
    var toastTimer = null;
    var showToast = function (msg) {
      toast.textContent = msg; toast.hidden = false;
      clearTimeout(toastTimer); toastTimer = setTimeout(function () { toast.hidden = true; }, 6000);
    };
    // column headers show the TOTAL per status (not just the visible cards): shift by one
    var bump = function (zone, d) {
      var cnt = $('.cnt', zone.parentElement);
      cnt.textContent = String(Math.max(0, (parseInt(cnt.textContent, 10) || 0) + d));
    };
    var dragged = null;
    $$('.kb-card', board).forEach(function (card) {
      card.addEventListener('dragstart', function (e) {
        dragged = card; card.classList.add('drag');
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', card.getAttribute('data-oid'));
      });
      card.addEventListener('dragend', function () { card.classList.remove('drag'); dragged = null; });
    });
    $$('.kb-body', board).forEach(function (zone) {
      zone.addEventListener('dragover', function (e) { e.preventDefault(); zone.classList.add('over'); });
      zone.addEventListener('dragleave', function () { zone.classList.remove('over'); });
      zone.addEventListener('drop', function (e) {
        e.preventDefault(); zone.classList.remove('over');
        var card = dragged;
        if (!card) return;
        var from = card.parentElement, next = card.nextSibling;
        if (from === zone) return;
        zone.insertBefore(card, zone.firstChild);
        card.classList.add('saving');
        bump(from, -1); bump(zone, +1);
        var rollback = function (msg) {
          from.insertBefore(card, next);
          bump(zone, -1); bump(from, +1);
          showToast(msg);
        };
        fetch('/orders/' + card.getAttribute('data-oid') + '/status', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'X-CSRF-Token': csrf },
          body: JSON.stringify({ status: zone.getAttribute('data-status'),
                                 expected_version: parseInt(card.getAttribute('data-version'), 10) })
        }).then(function (r) {
          return r.json().catch(function () { return {}; }).then(function (d) { return { r: r, d: d }; });
        }).then(function (x) {
          if (x.r.ok && x.d.ok) { card.setAttribute('data-version', x.d.version); return; }
          var key = x.r.status === 409 ? 'data-msg-conflict' : (x.r.status === 403 ? 'data-msg-forbidden' : 'data-msg-error');
          if (x.r.status === 409 && x.d.version) card.setAttribute('data-version', x.d.version);
          rollback(board.getAttribute(key));
        }).catch(function () {
          rollback(board.getAttribute('data-msg-error'));
        }).then(function () { card.classList.remove('saving'); });
      });
    });
  }
})();
