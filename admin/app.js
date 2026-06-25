(function () {
  'use strict';

  var TOKEN = sessionStorage.getItem('cb_admin_token') || '';
  var STATUS_OPTS = ['new', 'contacted', 'qualified', 'lost'];
  var STATUS_LABEL = { new: 'Nuevo', contacted: 'Contactado', qualified: 'Calificado', lost: 'Perdido',
    warm: 'Tibio', hot: 'Caliente', converted: 'Convertido' };
  var PROJECTS = ['agencia', 'mesa', 'ticketera'];

  function $(id) { return document.getElementById(id); }
  function esc(s) { var d = document.createElement('div'); d.textContent = (s == null ? '' : String(s)); return d.innerHTML; }
  function authHeaders() { return { 'Authorization': 'Bearer ' + TOKEN }; }

  function api(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign({}, opts.headers || {}, authHeaders());
    return fetch(path, opts).then(function (r) {
      if (r.status === 401) { sessionStorage.removeItem('cb_admin_token'); location.reload(); throw new Error('401'); }
      return r;
    });
  }

  function toast(msg, bad) {
    var t = document.createElement('div');
    t.className = 'toast' + (bad ? ' bad' : '');
    t.textContent = msg;
    $('toast-wrap').appendChild(t);
    setTimeout(function () { t.remove(); }, 2600);
  }

  // ── Gate ──
  function showApp() {
    $('gate').classList.add('hidden');
    document.querySelector('header').classList.remove('hidden');
    document.querySelector('main').classList.remove('hidden');
    loadStats();
    loadLeads();
    // Foco a la primera tab tras login OK (accesibilidad: el flujo de teclado sigue).
    var first = document.querySelector('.tab');
    if (first) first.focus();
  }
  function tryLogin(token) {
    return fetch('/leads/stats', { headers: { 'Authorization': 'Bearer ' + token } }).then(function (r) {
      if (r.status === 401) throw new Error('Token incorrecto.');
      if (r.status === 503) throw new Error('Falta configurar ADMIN_API_KEY en el servidor.');
      if (!r.ok) throw new Error('Error ' + r.status);
      return true;
    });
  }
  function doLogin() {
    var btn = $('gate-btn'), err = $('gate-err');
    var t = $('gate-token').value.trim();
    err.textContent = ''; err.className = 'err';
    if (!t) { err.textContent = 'Ingresá el token.'; return; }
    btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Validando…';
    tryLogin(t).then(function () {
      TOKEN = t; sessionStorage.setItem('cb_admin_token', t); showApp();
    }).catch(function (e) {
      err.textContent = e.message; err.className = 'err bad';
      btn.disabled = false; btn.textContent = 'Entrar';
    });
  }

  // ── Tabs ──
  // Patrón ARIA tablist: una sola tab es foco-able (roving tabindex); ←/→ mueven y activan.
  function tabList() { return Array.prototype.slice.call(document.querySelectorAll('.tab')); }
  function activateTab(tab, focus) {
    tabList().forEach(function (t) {
      var on = t === tab;
      t.classList.toggle('active', on);
      t.setAttribute('aria-selected', on ? 'true' : 'false');
      t.tabIndex = on ? 0 : -1;
    });
    document.querySelectorAll('.section').forEach(function (s) { s.classList.add('hidden'); });
    $('tab-' + tab.dataset.tab).classList.remove('hidden');
    if (focus) tab.focus();
    if (tab.dataset.tab === 'resumen') loadStats();
  }
  function initTabs() {
    var tabs = tabList();
    tabs.forEach(function (tab, i) {
      tab.tabIndex = tab.classList.contains('active') ? 0 : -1;
      tab.addEventListener('click', function () { activateTab(tab); });
      tab.addEventListener('keydown', function (e) {
        var next = null;
        if (e.key === 'ArrowRight' || e.key === 'ArrowDown') next = tabs[(i + 1) % tabs.length];
        else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') next = tabs[(i - 1 + tabs.length) % tabs.length];
        else if (e.key === 'Home') next = tabs[0];
        else if (e.key === 'End') next = tabs[tabs.length - 1];
        if (next) { e.preventDefault(); activateTab(next, true); }
      });
    });
  }

  // ── Dashboard ──
  function loadStats() {
    var box = $('dash');
    api('/leads/stats').then(function (r) { return r.json(); }).then(renderStats)
      .catch(function () { box.innerHTML = '<div class="error-box">No se pudieron cargar las métricas.</div>'; });
  }
  function statCard(num, lbl, cls, delta) {
    return '<div class="stat ' + cls + '"><div class="num">' + num + '</div><div class="lbl">' + esc(lbl) + '</div>' +
      (delta ? '<div class="delta">' + esc(delta) + '</div>' : '') + '</div>';
  }
  function renderStats(d) {
    var box = $('dash');
    var bs = d.by_status || {};
    if (!d.total) {
      box.innerHTML = '<div class="card empty"><div class="big">📊</div>Todavía no hay leads.<br>' +
        '<span class="muted">Cuando lleguen, vas a ver acá el resumen y el embudo.</span></div>';
      return;
    }
    var cards = '<div class="stat-grid">' +
      statCard(d.total, 'Leads totales', 'accent', d.last_7d ? '+' + d.last_7d + ' últimos 7 días' : '') +
      statCard(bs.new || 0, 'Nuevos sin atender', 'warn') +
      statCard(bs.qualified || 0, 'Calificados', 'good') +
      statCard(bs.lost || 0, 'Perdidos', 'bad') +
    '</div>';

    // Embudo: new -> contacted -> qualified
    var stages = [['new', 'Nuevos'], ['contacted', 'Contactados'], ['qualified', 'Calificados']];
    var maxv = Math.max(1, bs.new || 0, bs.contacted || 0, bs.qualified || 0);
    var funnelRows = stages.map(function (s) {
      var v = bs[s[0]] || 0;
      var pct = Math.round((v / maxv) * 100);
      return '<div class="funnel-row"><div class="fname">' + s[1] + '</div>' +
        '<div class="funnel-bar-track"><div class="funnel-bar bar-' + s[0] + '" data-w="' + pct + '"></div></div>' +
        '<div class="fpct">' + v + '</div></div>';
    }).join('');

    var bp = d.by_project || {};
    var maxp = Math.max(1, PROJECTS.reduce(function (m, p) { return Math.max(m, bp[p] || 0); }, 0));
    var projRows = PROJECTS.map(function (p) {
      var v = bp[p] || 0; var pct = Math.round((v / maxp) * 100);
      return '<div class="proj-row"><div class="pname">' + p + '</div>' +
        '<div class="ptrack"><div class="pbar" data-w="' + pct + '"></div></div>' +
        '<div class="pcount">' + v + '</div></div>';
    }).join('');

    box.innerHTML = cards +
      '<div class="dash-cols">' +
        '<div class="card"><div class="section-title">Embudo de conversión</div><div class="funnel">' + funnelRows + '</div></div>' +
        '<div class="card"><div class="section-title">Leads por proyecto</div><div class="proj-bars">' + projRows + '</div></div>' +
      '</div>';

    // Setear anchos vía CSSOM (compatible con CSP estricta).
    box.querySelectorAll('[data-w]').forEach(function (el) { el.style.width = el.dataset.w + '%'; });
  }

  // ── Leads ──
  function skeleton(n) {
    var s = ''; for (var i = 0; i < n; i++) s += '<div class="sk-row"></div>'; return s;
  }
  // Arma el querystring de filtros activos (lo comparten /leads/ y /leads/export.csv).
  function leadsQuery() {
    var proj = $('filter-project').value, status = $('filter-status').value;
    var q = $('filter-q').value.trim(), sort = $('filter-sort').value;
    var qs = [];
    if (proj) qs.push('project=' + encodeURIComponent(proj));
    if (status) qs.push('status=' + encodeURIComponent(status));
    if (q) qs.push('q=' + encodeURIComponent(q));
    if (sort && sort !== 'recent') qs.push('sort=' + encodeURIComponent(sort));
    return qs.join('&');
  }
  function loadLeads() {
    var content = $('leads-content');
    content.innerHTML = skeleton(5);
    var qs = leadsQuery();
    api('/leads/' + (qs ? '?' + qs : ''))
      .then(function (r) { if (!r.ok) throw new Error('Error ' + r.status); return r.json(); })
      .then(renderLeads)
      .catch(function (e) {
        if (e.message === '401') return;
        content.innerHTML = '<div class="error-box">No se pudo cargar (' + esc(e.message) + ')' +
          '<br><button class="btn-ghost" id="retry">↻ Reintentar</button></div>';
        $('retry').addEventListener('click', loadLeads);
      });
  }
  function renderLeads(leads) {
    var content = $('leads-content');
    if (!leads.length) {
      content.innerHTML = '<div class="empty"><div class="big">🌱</div>Todavía no hay leads.<br>' +
        '<span class="muted">Aparecen acá cuando alguien deja sus datos en el chat o un anuncio.</span></div>';
      return;
    }
    var COLS = 6;
    var rows = leads.map(function (l, idx) {
      var contact = [l.phone, l.email, l.instagram ? '@' + l.instagram : ''].filter(Boolean).map(esc).join('<br>');
      var tags = (l.interests && l.interests.length)
        ? '<div class="tags">' + l.interests.slice(0, 3).map(function (i) { return '<span class="tag">' + esc(i) + '</span>'; }).join('') +
          (l.interests.length > 3 ? '<span class="tag">+' + (l.interests.length - 3) + '</span>' : '') + '</div>'
        : '';
      var opts = STATUS_OPTS.map(function (s) {
        return '<option value="' + s + '"' + (s === l.status ? ' selected' : '') + '>' + STATUS_LABEL[s] + '</option>';
      }).join('');
      var date = l.created_at ? new Date(l.created_at).toLocaleDateString('es-AR', { day: '2-digit', month: 'short' }) : '';
      // El chevron solo aparece si hay notas (afford de expandir); si no, va el nombre suelto.
      var nameCell = l.notes
        ? '<div class="name expander"><span class="chev" aria-hidden="true">▸</span>' + esc(l.name || '—') + '</div>'
        : '<div class="name">' + esc(l.name || '—') + '</div>';
      var main = '<tr class="lead-row' + (l.notes ? ' clickable' : '') + '" data-idx="' + idx + '"' +
        (l.notes ? ' title="Click para ver notas"' : '') + '>' +
        '<td>' + nameCell + '<div class="proj">' + esc(l.project) + '</div>' + tags + '</td>' +
        '<td>' + (contact || '<span class="muted">—</span>') + '</td>' +
        '<td><span class="chip ' + esc(l.status) + '">' + (STATUS_LABEL[l.status] || esc(l.status)) + '</span></td>' +
        '<td><select class="status-sel" data-id="' + esc(l.id) + '" aria-label="Cambiar estado del lead">' + opts + '</select></td>' +
        '<td class="muted">' + esc(date) + '</td>' +
        '<td><div class="lead-actions"><button class="btn-mini transcript-btn" data-id="' + esc(l.id) + '" data-idx="' + idx + '"' +
          ' aria-expanded="false">💬 Ver conversación</button></div></td></tr>';
      var notes = l.notes ? '<tr class="notes-row hidden" data-notes="' + idx + '"><td colspan="' + COLS + '">' +
        '<div class="nlabel">Notas</div>' + esc(l.notes) + '</td></tr>' : '';
      var transcript = '<tr class="transcript-row hidden" data-transcript="' + idx + '"><td colspan="' + COLS + '"></td></tr>';
      return main + notes + transcript;
    }).join('');

    content.innerHTML = '<div class="muted leads-count">' + leads.length + ' lead' + (leads.length > 1 ? 's' : '') + '</div>' +
      '<div class="table-scroll"><table><thead><tr><th>Lead</th><th>Contacto</th><th>Estado</th><th>Cambiar</th><th>Fecha</th><th>Conversación</th></tr></thead><tbody>' + rows + '</tbody></table></div>';

    // Toggle notas al click en la fila (sin disparar al usar el select o el botón de conversación).
    content.querySelectorAll('.lead-row.clickable').forEach(function (row) {
      row.addEventListener('click', function (e) {
        if (e.target.closest('select') || e.target.closest('button')) return;
        var nr = content.querySelector('.notes-row[data-notes="' + row.dataset.idx + '"]');
        if (nr) { nr.classList.toggle('hidden'); row.classList.toggle('is-open', !nr.classList.contains('hidden')); }
      });
    });
    // Botón "Ver conversación": carga el transcript en una fila expandible.
    content.querySelectorAll('.transcript-btn').forEach(function (btn) {
      btn.addEventListener('click', function () { toggleTranscript(btn, content); });
    });
    // Cambio de estado optimista.
    content.querySelectorAll('.status-sel').forEach(function (sel) {
      sel.dataset.prev = sel.value;
      sel.addEventListener('change', function () { updateStatus(sel); });
    });
  }

  // ── Transcript ──
  var CHANNEL_LABEL = { web: 'Web', whatsapp: 'WhatsApp', instagram: 'Instagram', facebook: 'Facebook' };
  function fmtTime(s) {
    if (!s) return '';
    var d = new Date(s);
    if (isNaN(d)) return '';
    return d.toLocaleString('es-AR', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
  }
  function toggleTranscript(btn, content) {
    var idx = btn.dataset.idx;
    var row = content.querySelector('.transcript-row[data-transcript="' + idx + '"]');
    if (!row) return;
    var cell = row.firstElementChild;
    var open = !row.classList.contains('hidden');
    if (open) {  // ya estaba abierto -> colapsar
      row.classList.add('hidden');
      btn.setAttribute('aria-expanded', 'false');
      return;
    }
    row.classList.remove('hidden');
    btn.setAttribute('aria-expanded', 'true');
    if (row.dataset.loaded) return;  // ya cargado, solo re-mostramos
    cell.innerHTML = '<div class="tr-loading"><span class="spinner"></span> Cargando conversación…</div>';
    api('/leads/' + btn.dataset.id + '/messages')
      .then(function (r) { if (!r.ok) throw new Error('Error ' + r.status); return r.json(); })
      .then(function (data) {
        row.dataset.loaded = '1';
        renderTranscript(cell, data);
      })
      .catch(function (e) {
        if (e.message === '401') return;
        cell.innerHTML = '<div class="error-box">No se pudo cargar la conversación (' + esc(e.message) + ')' +
          '<br><button class="btn-ghost tr-retry">↻ Reintentar</button></div>';
        var rb = cell.querySelector('.tr-retry');
        if (rb) rb.addEventListener('click', function () { delete row.dataset.loaded; row.classList.add('hidden'); toggleTranscript(btn, content); });
      });
  }
  function renderTranscript(cell, data) {
    var msgs = (data && data.messages) || [];
    if (!msgs.length) {
      cell.innerHTML = '<div class="tr-loading">Esta conversación todavía no tiene mensajes.</div>';
      return;
    }
    var ch = CHANNEL_LABEL[data.channel] || (data.channel ? esc(data.channel) : '');
    var head = '<div class="tr-head">Conversación' + (ch ? ' · ' + ch : '') + '</div>';
    var bubbles = msgs.map(function (m) {
      var role = m.role === 'user' ? 'user' : 'assistant';
      var t = fmtTime(m.created_at);
      return '<div class="tr-bubble ' + role + '">' + esc(m.content) +
        (t ? '<span class="tr-time">' + esc(t) + '</span>' : '') + '</div>';
    }).join('');
    cell.innerHTML = head + '<div class="transcript">' + bubbles + '</div>';
  }
  function updateStatus(sel) {
    var prev = sel.dataset.prev, next = sel.value, id = sel.dataset.id;
    var chip = sel.closest('tr').querySelector('.chip');
    if (chip) { chip.textContent = STATUS_LABEL[next] || next; chip.className = 'chip ' + next; }  // optimista
    api('/leads/' + id + '/status', {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status: next }),
    }).then(function (r) {
      if (!r.ok) throw new Error('Error ' + r.status);
      sel.dataset.prev = next; toast('Estado actualizado'); loadStats();
    }).catch(function (e) {
      if (e.message === '401') return;
      sel.value = prev;
      if (chip) { chip.textContent = STATUS_LABEL[prev] || prev; chip.className = 'chip ' + prev; }
      toast('No se pudo actualizar', true);
    });
  }

  // ── Export CSV ──
  // Un <a href> NO manda el Bearer, así que descargamos por fetch + blob y respetamos
  // los filtros activos (project/status/q/sort).
  function exportCsv() {
    var btn = $('export-csv');
    if (btn.disabled) return;
    var prev = btn.textContent;
    btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Exportando…';
    var qs = leadsQuery();
    api('/leads/export.csv' + (qs ? '?' + qs : ''))
      .then(function (r) { if (!r.ok) throw new Error('Error ' + r.status); return r.blob(); })
      .then(function (blob) {
        var url = URL.createObjectURL(blob);
        var a = document.createElement('a');
        a.href = url;
        a.download = 'leads-' + new Date().toISOString().slice(0, 10) + '.csv';
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
        toast('CSV descargado');
      })
      .catch(function (e) { if (e.message !== '401') toast('No se pudo exportar el CSV', true); })
      .finally(function () { btn.disabled = false; btn.textContent = prev; });
  }

  // ── Ads ──
  function doGenerate() {
    var btn = $('ad-generate'), err = $('ad-err'), results = $('ad-results');
    err.textContent = '';
    var brief = $('ad-brief').value.trim();
    if (!brief) { err.textContent = 'Escribí qué querés promocionar.'; return; }
    btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Generando…';
    results.innerHTML = '<div class="grid">' +
      '<div class="sk-row sk-row-ad"></div>'.repeat(3) + '</div>';
    api('/ads/generate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project: $('ad-project').value, brief: brief, channel: $('ad-channel').value }),
    }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) { if (!res.ok) throw new Error((res.d && res.d.detail) || 'Error generando'); renderAds(res.d); })
      .catch(function (e) { if (e.message !== '401') { results.innerHTML = ''; err.textContent = e.message; } })
      .finally(function () { btn.disabled = false; btn.textContent = 'Generar 3 variantes'; });
  }
  function renderAds(data) {
    var variants = (data.variantes || []).map(function (v, i) {
      var copyText = (v.titular || '') + '\n' + (v.texto_principal || '') + '\n' + (v.descripcion || '');
      return '<div class="card variant"><button class="copy-btn" data-copy="' + esc(copyText) + '">Copiar</button>' +
        '<h4>' + esc(v.titular) + '</h4><div class="muted">' + esc(v.descripcion) + '</div>' +
        '<div class="body">' + esc(v.texto_principal) + '</div>' +
        '<div class="meta">🎨 ' + esc(v.concepto_visual) + '</div>' +
        '<span class="cta">' + esc(v.cta) + '</span></div>';
    }).join('');
    var pub = data.publico_sugerido || {};
    var aud = '<div class="card aud-card"><div class="section-title">Público sugerido</div>' +
      '<div class="muted">Edad: ' + esc(pub.edad || '—') + ' · Ubicación: ' + esc(pub.ubicacion || '—') + '<br>' +
      'Intereses: ' + esc((pub.intereses || []).join(', ')) + '<br>' +
      'Presupuesto sugerido: ' + esc(data.presupuesto_sugerido_ars_dia || '—') + ' /día</div></div>';
    var results = $('ad-results');
    results.innerHTML = '<div class="grid">' + variants + '</div>' + aud;
    results.querySelectorAll('.copy-btn').forEach(function (b) {
      b.addEventListener('click', function () {
        navigator.clipboard.writeText(b.dataset.copy).then(function () { toast('Copiado'); });
      });
    });
  }

  // Debounce genérico (para la búsqueda: no pegamos al backend en cada tecla).
  function debounce(fn, ms) {
    var t;
    return function () { clearTimeout(t); t = setTimeout(fn, ms); };
  }

  // ── Init ──
  $('gate-btn').addEventListener('click', doLogin);
  $('gate-token').addEventListener('keydown', function (e) { if (e.key === 'Enter') doLogin(); });
  $('logout').addEventListener('click', function () { sessionStorage.removeItem('cb_admin_token'); location.reload(); });
  $('reload').addEventListener('click', loadLeads);
  $('export-csv').addEventListener('click', exportCsv);
  $('filter-project').addEventListener('change', loadLeads);
  $('filter-status').addEventListener('change', loadLeads);
  $('filter-sort').addEventListener('change', loadLeads);
  $('filter-q').addEventListener('input', debounce(loadLeads, 300));
  $('ad-generate').addEventListener('click', doGenerate);
  initTabs();

  if (TOKEN) {
    tryLogin(TOKEN).then(showApp).catch(function () { sessionStorage.removeItem('cb_admin_token'); });
  } else {
    // Foco al input del gate al cargar (accesibilidad: el usuario teclea el token sin clickear).
    $('gate-token').focus();
  }
})();
