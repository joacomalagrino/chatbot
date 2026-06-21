(function () {
  'use strict';

  var API_URL = window.CHATBOT_API_URL || '';
  var PROJECT = window.CHATBOT_PROJECT || 'agencia';
  var WHATSAPP_NUMBER = window.CHATBOT_WHATSAPP || '';
  var INSTAGRAM_USER = window.CHATBOT_INSTAGRAM || '';
  // Sesión persistente por proyecto: si se recarga la página, se reusa la misma
  // conversación (si no, el lead quedaba huérfano). sessionStorage = dura la pestaña.
  var SESSION_ID = (function () {
    var key = 'cb_sid_' + PROJECT;
    try {
      var stored = sessionStorage.getItem(key);
      if (stored) return stored;
      var sid = 'cb_' + PROJECT + '_' + Math.random().toString(36).substr(2, 9);
      sessionStorage.setItem(key, sid);
      return sid;
    } catch (e) {
      // sessionStorage puede no estar disponible (modo privado/3rd-party); seguimos sin persistir.
      return 'cb_' + PROJECT + '_' + Math.random().toString(36).substr(2, 9);
    }
  })();
  var messageCount = 0;
  var sending = false;

  // Accent por proyecto (se puede pisar con window.CHATBOT_ACCENT).
  var ACCENTS = { agencia: '#1a73e8', mesa: '#0ea5e9', ticketera: '#6366f1' };
  var ACCENT = window.CHATBOT_ACCENT || ACCENTS[PROJECT] || '#1a73e8';

  // Header por proyecto (avatar / título / subtítulo). Se puede pisar con
  // window.CHATBOT_HEADER = { avatar, title, subtitle }.
  var HEADERS = {
    agencia: { avatar: '🚗', title: 'Gonzalo Ferraro', subtitle: 'Estamos para ayudarte' },
    mesa: { avatar: '💬', title: 'Mesa', subtitle: 'Soporte para tu equipo' },
    ticketera: { avatar: '🛠️', title: 'Soporte Dedalus', subtitle: 'Estamos para ayudarte' },
  };
  var DEFAULT_HEADER = { avatar: '💬', title: 'Hola 👋', subtitle: 'Estamos para ayudarte' };
  var HEADER = Object.assign({}, DEFAULT_HEADER, HEADERS[PROJECT] || {}, window.CHATBOT_HEADER || {});

  // Prompts sugeridos por proyecto (reducen la fricción de arranque).
  var SUGGESTIONS = {
    agencia: ['Ver autos 0km', 'Financiación', 'Permuto mi usado'],
    mesa: ['Ver planes', '¿Cómo funciona?', 'Prueba gratis'],
    ticketera: ['Abrir un ticket', 'Estado de mi caso'],
  };
  var REDUCED_MOTION = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);

  /* ── Estilos ── */
  var css = `
    .cb-root {
      --cb-accent: ${ACCENT};
      --cb-bg: #ffffff;
      --cb-text: #202124;
      --cb-muted: #5f6368;
      --cb-surface: #f1f3f4;
      --cb-border: #e8eaed;
    }
    @media (prefers-color-scheme: dark) {
      .cb-root {
        --cb-bg: #1f2228; --cb-text: #e8eaed; --cb-muted: #9aa0a6;
        --cb-surface: #2a2e36; --cb-border: #343941;
      }
    }

    #cb-launcher {
      position: fixed; bottom: 24px; right: 24px; z-index: 9999;
      width: 56px; height: 56px; border-radius: 50%;
      background: var(--cb-accent); border: none; cursor: pointer;
      box-shadow: 0 4px 12px rgba(0,0,0,.25);
      display: flex; align-items: center; justify-content: center;
      transition: transform .2s;
    }
    #cb-launcher:hover { transform: scale(1.08); }
    #cb-launcher:focus-visible { outline: 3px solid var(--cb-accent); outline-offset: 3px; }
    #cb-launcher svg { width: 28px; height: 28px; fill: #fff; }

    #cb-panel {
      position: fixed; bottom: 92px; right: 24px; z-index: 9998;
      width: 340px; max-height: min(520px, calc(100vh - 120px));
      background: var(--cb-bg); color: var(--cb-text); border-radius: 16px;
      box-shadow: 0 8px 32px rgba(0,0,0,.18);
      display: flex; flex-direction: column; overflow: hidden;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      opacity: 0; transform: translateY(12px) scale(.98); transform-origin: bottom right;
      pointer-events: none; visibility: hidden;
      transition: opacity .22s ease, transform .22s ease, visibility 0s linear .22s;
    }
    #cb-panel.open {
      opacity: 1; transform: none; pointer-events: auto; visibility: visible;
      transition: opacity .22s ease, transform .22s ease;
    }
    @media (max-width: 420px) {
      #cb-panel { right: 0; left: 0; bottom: 0; width: 100%;
        max-height: 85vh; border-radius: 16px 16px 0 0; }
      #cb-launcher { bottom: 16px; right: 16px; }
    }

    #cb-header {
      background: var(--cb-accent); color: #fff;
      padding: 14px 16px; display: flex; align-items: center; gap: 10px;
    }
    #cb-header .cb-avatar {
      width: 36px; height: 36px; border-radius: 50%;
      background: rgba(255,255,255,.25);
      display: flex; align-items: center; justify-content: center; font-size: 18px;
    }
    #cb-header .cb-title { font-weight: 600; font-size: 15px; }
    #cb-header .cb-subtitle { font-size: 12px; opacity: .85; }
    #cb-close {
      margin-left: auto; background: none; border: none;
      color: #fff; cursor: pointer; font-size: 20px; line-height: 1; padding: 6px; border-radius: 6px;
    }
    #cb-close:hover { background: rgba(255,255,255,.15); }
    #cb-close:focus-visible { outline: 2px solid #fff; outline-offset: 1px; }

    #cb-messages {
      flex: 1; overflow-y: auto; padding: 16px;
      display: flex; flex-direction: column; gap: 10px;
    }
    .cb-msg {
      max-width: 82%; padding: 10px 14px;
      border-radius: 14px; font-size: 14px; line-height: 1.45;
      word-break: break-word; white-space: pre-wrap;
      animation: cb-in .22s ease both;
    }
    .cb-msg.bot { background: var(--cb-surface); color: var(--cb-text); border-bottom-left-radius: 4px; align-self: flex-start; transform-origin: left bottom; }
    .cb-msg.user { background: var(--cb-accent); color: #fff; border-bottom-right-radius: 4px; align-self: flex-end; transform-origin: right bottom; }
    @keyframes cb-in { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }

    #cb-quick { display: flex; flex-wrap: wrap; gap: 8px; padding: 0 16px 12px; }
    .cb-quick-chip {
      border: 1.5px solid var(--cb-border); background: var(--cb-bg); color: var(--cb-accent);
      border-radius: 16px; padding: 7px 13px; font-size: 13px; font-weight: 500; cursor: pointer;
      transition: border-color .15s, background .15s;
    }
    .cb-quick-chip:hover { border-color: var(--cb-accent); background: var(--cb-surface); }
    .cb-quick-chip:focus-visible { outline: 2px solid var(--cb-accent); outline-offset: 1px; }

    #cb-badge {
      position: absolute; top: -2px; right: -2px; width: 14px; height: 14px;
      background: #ea4335; border-radius: 50%; border: 2px solid var(--cb-bg);
    }

    .cb-typing {
      display: flex; gap: 5px; padding: 12px 14px;
      background: var(--cb-surface); border-radius: 14px; border-bottom-left-radius: 4px; align-self: flex-start;
    }
    .cb-typing span { width: 8px; height: 8px; background: var(--cb-muted); border-radius: 50%; animation: cb-bounce .9s infinite; }
    .cb-typing span:nth-child(2) { animation-delay: .15s; }
    .cb-typing span:nth-child(3) { animation-delay: .3s; }
    @keyframes cb-bounce { 0%, 60%, 100% { transform: translateY(0); } 30% { transform: translateY(-6px); } }

    .cb-retry { align-self: flex-start; }
    .cb-retry-btn {
      border: 1.5px solid var(--cb-border); background: var(--cb-bg); color: var(--cb-accent);
      border-radius: 16px; padding: 6px 13px; font-size: 13px; font-weight: 500; cursor: pointer;
      transition: border-color .15s, background .15s;
    }
    .cb-retry-btn:hover { border-color: var(--cb-accent); background: var(--cb-surface); }
    .cb-retry-btn:focus-visible { outline: 2px solid var(--cb-accent); outline-offset: 1px; }

    #cb-channels { margin: 0 16px 12px; display: none; flex-direction: column; gap: 8px; }
    #cb-channels.visible { display: flex; }
    #cb-channels p { font-size: 12px; color: var(--cb-muted); margin: 0 0 4px; text-align: center; }
    .cb-channel-btn {
      display: flex; align-items: center; gap: 10px;
      padding: 10px 14px; border-radius: 10px; border: 1.5px solid var(--cb-border);
      background: var(--cb-bg); cursor: pointer; font-size: 14px; font-weight: 500;
      transition: border-color .15s, background .15s; text-decoration: none; color: var(--cb-text);
    }
    .cb-channel-btn.wa { border-color: #25d366; }
    .cb-channel-btn.ig { border-color: #e1306c; }
    .cb-channel-btn:focus-visible { outline: 2px solid var(--cb-accent); outline-offset: 1px; }

    #cb-input-row { display: flex; gap: 8px; padding: 12px 16px; border-top: 1px solid var(--cb-border); }
    #cb-input {
      flex: 1; border: 1.5px solid var(--cb-border); border-radius: 24px;
      padding: 9px 16px; font-size: 14px; outline: none; background: var(--cb-bg); color: var(--cb-text);
      transition: border-color .15s;
    }
    #cb-input:focus { border-color: var(--cb-accent); }
    #cb-send {
      background: var(--cb-accent); border: none; border-radius: 50%;
      width: 38px; height: 38px; cursor: pointer; flex-shrink: 0;
      display: flex; align-items: center; justify-content: center;
    }
    #cb-send svg { fill: #fff; width: 18px; height: 18px; }
    #cb-send:disabled { background: var(--cb-border); cursor: default; }
    #cb-send:focus-visible { outline: 2px solid var(--cb-accent); outline-offset: 2px; }

    @media (prefers-reduced-motion: reduce) {
      #cb-launcher, .cb-channel-btn, #cb-input, .cb-quick-chip, .cb-retry-btn { transition: none; }
      #cb-panel { transition: visibility 0s; transform: none; }
      #cb-panel.open { transition: none; }
      .cb-typing span, .cb-msg { animation: none; }
    }
  `;

  var LAUNCHER_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 2H4C2.9 2 2 2.9 2 4v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm-2 12H6v-2h12v2zm0-3H6V9h12v2zm0-3H6V6h12v2z"/></svg>';
  var SEND_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>';
  var WA_ICON = '<svg width="20" viewBox="0 0 24 24" fill="#25d366" aria-hidden="true"><path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347z"/><path d="M11.999 2C6.477 2 2 6.477 2 12c0 1.895.525 3.668 1.438 5.187L2 22l4.985-1.407A9.945 9.945 0 0012 22c5.522 0 10-4.477 10-10S17.522 2 11.999 2zm0 18a7.946 7.946 0 01-4.291-1.254l-.308-.183-3.187.898.906-3.093-.2-.317A7.963 7.963 0 014 12c0-4.411 3.589-8 8-8s8 3.589 8 8-3.588 8-7.999 8z"/></svg>';
  var IG_ICON = '<svg width="20" viewBox="0 0 24 24" fill="#e1306c" aria-hidden="true"><path d="M12 2.163c3.204 0 3.584.012 4.85.07 3.252.148 4.771 1.691 4.919 4.919.058 1.265.069 1.645.069 4.849 0 3.205-.012 3.584-.069 4.849-.149 3.225-1.664 4.771-4.919 4.919-1.266.058-1.644.07-4.85.07-3.204 0-3.584-.012-4.849-.07-3.26-.149-4.771-1.699-4.919-4.92-.058-1.265-.07-1.644-.07-4.849 0-3.204.013-3.583.07-4.849.149-3.227 1.664-4.771 4.919-4.919 1.266-.057 1.645-.069 4.849-.069zm0-2.163c-3.259 0-3.667.014-4.947.072-4.358.2-6.78 2.618-6.98 6.98-.059 1.281-.073 1.689-.073 4.948 0 3.259.014 3.668.072 4.948.2 4.358 2.618 6.78 6.98 6.98 1.281.058 1.689.072 4.948.072 3.259 0 3.668-.014 4.948-.072 4.354-.2 6.782-2.618 6.979-6.98.059-1.28.073-1.689.073-4.948 0-3.259-.014-3.667-.072-4.947-.196-4.354-2.617-6.78-6.979-6.98-1.281-.059-1.69-.073-4.949-.073zm0 5.838c-3.403 0-6.162 2.759-6.162 6.162s2.759 6.163 6.162 6.163 6.162-2.759 6.162-6.163c0-3.403-2.759-6.162-6.162-6.162zm0 10.162c-2.209 0-4-1.79-4-4 0-2.209 1.791-4 4-4s4 1.791 4 4c0 2.21-1.791 4-4 4zm6.406-11.845c-.796 0-1.441.645-1.441 1.44s.645 1.44 1.441 1.44c.795 0 1.439-.645 1.439-1.44s-.644-1.44-1.439-1.44z"/></svg>';

  var launcherEl, panelEl;

  // Escape para texto que va a innerHTML (el HEADER puede venir de window.CHATBOT_HEADER).
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function buildWidget() {
    var root = document.createElement('div');
    root.className = 'cb-root';
    document.body.appendChild(root);

    var styleEl = document.createElement('style');
    styleEl.textContent = css;
    document.head.appendChild(styleEl);

    launcherEl = document.createElement('button');
    launcherEl.id = 'cb-launcher';
    launcherEl.innerHTML = LAUNCHER_ICON;
    launcherEl.setAttribute('aria-label', 'Abrir chat');
    launcherEl.setAttribute('aria-expanded', 'false');
    var badge = document.createElement('span');
    badge.id = 'cb-badge';
    launcherEl.appendChild(badge);
    root.appendChild(launcherEl);

    panelEl = document.createElement('div');
    panelEl.id = 'cb-panel';
    panelEl.setAttribute('role', 'dialog');
    panelEl.setAttribute('aria-label', 'Chat de ayuda');
    panelEl.innerHTML = `
      <div id="cb-header">
        <div class="cb-avatar" aria-hidden="true">${esc(HEADER.avatar)}</div>
        <div>
          <div class="cb-title">${esc(HEADER.title)}</div>
          <div class="cb-subtitle">${esc(HEADER.subtitle)}</div>
        </div>
        <button id="cb-close" aria-label="Cerrar chat">✕</button>
      </div>
      <div id="cb-messages" role="log" aria-live="polite" aria-atomic="false"></div>
      <div id="cb-quick"></div>
      <div id="cb-channels">
        <p>¿Continuamos por otro canal?</p>
        ${WHATSAPP_NUMBER ? `<a class="cb-channel-btn wa" id="cb-wa-btn" href="#" target="_blank" rel="noopener">${WA_ICON} Continuar por WhatsApp</a>` : ''}
        ${INSTAGRAM_USER ? `<a class="cb-channel-btn ig" id="cb-ig-btn" href="https://ig.me/m/${encodeURIComponent(INSTAGRAM_USER)}" target="_blank" rel="noopener">${IG_ICON} Continuar por Instagram</a>` : ''}
      </div>
      <div id="cb-input-row">
        <input id="cb-input" type="text" placeholder="Escribí tu mensaje..." autocomplete="off" aria-label="Tu mensaje" maxlength="2000" />
        <button id="cb-send" aria-label="Enviar mensaje" disabled>${SEND_ICON}</button>
      </div>
    `;
    root.appendChild(panelEl);

    bindEvents();
    sendWelcome();
    renderQuickReplies();
  }

  function openPanel() {
    panelEl.classList.add('open');
    panelEl.setAttribute('aria-modal', 'true');
    launcherEl.setAttribute('aria-expanded', 'true');
    var b = document.getElementById('cb-badge');
    if (b) b.remove();
    document.getElementById('cb-input').focus();
  }
  function closePanel() {
    panelEl.classList.remove('open');
    panelEl.removeAttribute('aria-modal');
    launcherEl.setAttribute('aria-expanded', 'false');
    launcherEl.focus();
  }

  // Focus-trap básico: cicla Tab entre los elementos interactivos visibles del panel.
  function focusables() {
    var sel = '#cb-close, .cb-quick-chip, .cb-channel-btn, .cb-retry-btn, #cb-input, #cb-send';
    return Array.prototype.filter.call(
      panelEl.querySelectorAll(sel),
      function (el) { return !el.disabled && el.offsetParent !== null; }
    );
  }
  function trapFocus(e) {
    if (e.key !== 'Tab' || !panelEl.classList.contains('open')) return;
    var items = focusables();
    if (!items.length) return;
    var first = items[0];
    var last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }

  function bindEvents() {
    launcherEl.addEventListener('click', function () {
      panelEl.classList.contains('open') ? closePanel() : openPanel();
    });
    document.getElementById('cb-close').addEventListener('click', closePanel);

    // Escape cierra el panel.
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && panelEl.classList.contains('open')) closePanel();
    });

    // Tab cicla dentro del panel mientras está abierto (focus-trap).
    panelEl.addEventListener('keydown', trapFocus);

    var input = document.getElementById('cb-input');
    var sendBtn = document.getElementById('cb-send');
    input.addEventListener('input', function () { sendBtn.disabled = !input.value.trim(); });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey && !sendBtn.disabled) { e.preventDefault(); sendMessage(); }
    });
    sendBtn.addEventListener('click', sendMessage);
  }

  function sendWelcome() {
    var welcomes = {
      agencia: '¡Hola! Soy el asistente de Gonzalo Ferraro Automóviles 🚗\n¿En qué te puedo ayudar hoy?',
      mesa: '¡Hola! Soy el asistente de Mesa 👋\n¿Querés saber cómo Mesa puede ayudar a tu equipo de soporte?',
      ticketera: '¡Hola! Soy el asistente de soporte de Dedalus 🛠️\n¿En qué puedo ayudarte hoy?',
    };
    addMessage('bot', welcomes[PROJECT] || welcomes.agencia);
  }

  function scrollToBottom(el) {
    el.scrollTo({ top: el.scrollHeight, behavior: REDUCED_MOTION ? 'auto' : 'smooth' });
  }

  function addMessage(role, text) {
    var messages = document.getElementById('cb-messages');
    var div = document.createElement('div');
    div.className = 'cb-msg ' + role;
    div.textContent = text;
    messages.appendChild(div);
    scrollToBottom(messages);
  }

  function renderQuickReplies() {
    var box = document.getElementById('cb-quick');
    var sugg = SUGGESTIONS[PROJECT] || [];
    if (!box || !sugg.length) return;
    sugg.forEach(function (txt) {
      var chip = document.createElement('button');
      chip.className = 'cb-quick-chip';
      chip.type = 'button';
      chip.textContent = txt;
      chip.addEventListener('click', function () {
        document.getElementById('cb-input').value = txt;
        sendMessage();
      });
      box.appendChild(chip);
    });
  }

  function showTyping() {
    var messages = document.getElementById('cb-messages');
    var div = document.createElement('div');
    div.className = 'cb-typing';
    div.id = 'cb-typing';
    div.setAttribute('aria-label', 'Escribiendo…');
    div.innerHTML = '<span></span><span></span><span></span>';
    messages.appendChild(div);
    scrollToBottom(messages);
  }
  function removeTyping() {
    var t = document.getElementById('cb-typing');
    if (t) t.remove();
  }

  function sendMessage() {
    if (sending) return;
    var input = document.getElementById('cb-input');
    var text = input.value.trim();
    if (!text) return;

    sending = true;
    var quick = document.getElementById('cb-quick');
    if (quick) quick.innerHTML = '';
    input.value = '';
    document.getElementById('cb-send').disabled = true;
    // Guardamos el texto antes del fetch: si la red falla no se pierde (item Reintentar).
    var lastText = text;
    addMessage('user', text);
    messageCount++;
    showTyping();

    fetch(API_URL + '/chat/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: SESSION_ID, project: PROJECT, message: text, channel: 'web' }),
    })
      .then(function (r) {
        if (!r.ok) throw new Error('http ' + r.status);
        return r.json();
      })
      .then(function (data) {
        removeTyping();
        addMessage('bot', (data && data.response) || 'Lo siento, hubo un error. Intentá de nuevo.');
        if (data && data.suggest_channels) showChannelSuggestions();
      })
      .catch(function () {
        removeTyping();
        addMessage('bot', 'Lo siento, hubo un error. Intentá de nuevo en un momento.');
        showRetry(lastText);
      })
      .finally(function () { sending = false; });
  }

  // Tras un fallo de red ofrecemos reintentar: repuebla el input con el texto perdido.
  function showRetry(lastText) {
    var messages = document.getElementById('cb-messages');
    var wrap = document.createElement('div');
    wrap.className = 'cb-retry';
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'cb-retry-btn';
    btn.textContent = '↻ Reintentar';
    btn.addEventListener('click', function () {
      wrap.remove();
      var input = document.getElementById('cb-input');
      input.value = lastText;
      document.getElementById('cb-send').disabled = !lastText.trim();
      input.focus();
    });
    wrap.appendChild(btn);
    messages.appendChild(wrap);
    scrollToBottom(messages);
  }

  function showChannelSuggestions() {
    var channels = document.getElementById('cb-channels');
    if (!channels || channels.classList.contains('visible')) return;
    var waBtn = document.getElementById('cb-wa-btn');
    if (waBtn && WHATSAPP_NUMBER) {
      var waText = encodeURIComponent('Hola, vengo del sitio web y quiero continuar la consulta.');
      waBtn.href = 'https://wa.me/' + WHATSAPP_NUMBER.replace(/\D/g, '') + '?text=' + waText;
    }
    channels.classList.add('visible');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', buildWidget);
  } else {
    buildWidget();
  }
})();
