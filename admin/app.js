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
    $('gate').style.display = 'none';
    document.querySelector('header').style.display = 'flex';
    document.querySelector('main').style.display = 'block';
    loadStats();
    loadLeads();
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
  function initTabs() {
    document.querySelectorAll('.tab').forEach(function (tab) {
      tab.addEventListener('click', function () {
        document.querySelectorAll('.tab').forEach(function (t) { t.classList.remove('active'); });
        tab.classList.add('active');
        document.querySelectorAll('.section').forEach(function (s) { s.style.display = 'none'; });
        $('tab-' + tab.dataset.tab).style.display = 'block';
        if (tab.dataset.tab === 'resumen') loadStats();
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
        '<div class="funnel-bar-track"><div class="funnel-bar bar-' + s[0] + '" data-w="' + pct + '">' + (v || '') + '</div></div>' +
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
  function loadLeads() {
    var proj = $('filter-project').value, status = $('filter-status').value;
    var qs = []; if (proj) qs.push('project=' + encodeURIComponent(proj)); if (status) qs.push('status=' + encodeURIComponent(status));
    var content = $('leads-content');
    content.innerHTML = skeleton(5);
    api('/leads/' + (qs.length ? '?' + qs.join('&') : ''))
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
      var main = '<tr class="lead-row' + (l.notes ? ' clickable' : '') + '" data-idx="' + idx + '">' +
        '<td><div class="name">' + esc(l.name || '—') + '</div><div class="proj">' + esc(l.project) + '</div>' + tags + '</td>' +
        '<td>' + (contact || '<span class="muted">—</span>') + '</td>' +
        '<td><span class="chip ' + esc(l.status) + '">' + (STATUS_LABEL[l.status] || esc(l.status)) + '</span></td>' +
        '<td><select class="status-sel" data-id="' + esc(l.id) + '">' + opts + '</select></td>' +
        '<td class="muted">' + esc(date) + '</td></tr>';
      var notes = l.notes ? '<tr class="notes-row" data-notes="' + idx + '" style="display:none"><td colspan="5">' +
        '<div class="nlabel">Notas</div>' + esc(l.notes) + '</td></tr>' : '';
      return main + notes;
    }).join('');

    content.innerHTML = '<div class="muted" style="margin-bottom:10px">' + leads.length + ' lead' + (leads.length > 1 ? 's' : '') + '</div>' +
      '<table><thead><tr><th>Lead</th><th>Contacto</th><th>Estado</th><th>Cambiar</th><th>Fecha</th></tr></thead><tbody>' + rows + '</tbody></table>';

    // Toggle notas al click en la fila (sin disparar al usar el select).
    content.querySelectorAll('.lead-row.clickable').forEach(function (row) {
      row.addEventListener('click', function (e) {
        if (e.target.closest('select')) return;
        var nr = content.querySelector('.notes-row[data-notes="' + row.dataset.idx + '"]');
        if (nr) nr.style.display = nr.style.display === 'none' ? 'table-row' : 'none';
      });
    });
    // Cambio de estado optimista.
    content.querySelectorAll('.status-sel').forEach(function (sel) {
      sel.dataset.prev = sel.value;
      sel.addEventListener('change', function () { updateStatus(sel); });
    });
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

  // ── Ads ──
  function doGenerate() {
    var btn = $('ad-generate'), err = $('ad-err'), results = $('ad-results');
    err.textContent = '';
    var brief = $('ad-brief').value.trim();
    if (!brief) { err.textContent = 'Escribí qué querés promocionar.'; return; }
    btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Generando…';
    results.innerHTML = '<div class="grid">' +
      '<div class="sk-row" style="height:150px"></div>'.repeat(3) + '</div>';
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
    var aud = '<div class="card" style="margin-top:16px"><div class="section-title">Público sugerido</div>' +
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

  // ── Init ──
  $('gate-btn').addEventListener('click', doLogin);
  $('gate-token').addEventListener('keydown', function (e) { if (e.key === 'Enter') doLogin(); });
  $('logout').addEventListener('click', function () { sessionStorage.removeItem('cb_admin_token'); location.reload(); });
  $('reload').addEventListener('click', loadLeads);
  $('filter-project').addEventListener('change', loadLeads);
  $('filter-status').addEventListener('change', loadLeads);
  $('ad-generate').addEventListener('click', doGenerate);
  initTabs();

  if (TOKEN) {
    tryLogin(TOKEN).then(showApp).catch(function () { sessionStorage.removeItem('cb_admin_token'); });
  }
})();
