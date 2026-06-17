(function () {
  'use strict';

  var API_URL = window.CHATBOT_API_URL || '';
  var PROJECT = window.CHATBOT_PROJECT || 'agencia';
  var WHATSAPP_NUMBER = window.CHATBOT_WHATSAPP || '';
  var INSTAGRAM_USER = window.CHATBOT_INSTAGRAM || '';
  var SESSION_ID = 'cb_' + PROJECT + '_' + Math.random().toString(36).substr(2, 9);
  var messageCount = 0;

  /* ── Estilos ── */
  var css = `
    #cb-launcher {
      position: fixed; bottom: 24px; right: 24px; z-index: 9999;
      width: 56px; height: 56px; border-radius: 50%;
      background: #1a73e8; border: none; cursor: pointer;
      box-shadow: 0 4px 12px rgba(0,0,0,.25);
      display: flex; align-items: center; justify-content: center;
      transition: transform .2s;
    }
    #cb-launcher:hover { transform: scale(1.08); }
    #cb-launcher svg { width: 28px; height: 28px; fill: #fff; }

    #cb-panel {
      position: fixed; bottom: 92px; right: 24px; z-index: 9998;
      width: 340px; max-height: 520px;
      background: #fff; border-radius: 16px;
      box-shadow: 0 8px 32px rgba(0,0,0,.18);
      display: none; flex-direction: column; overflow: hidden;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    }
    #cb-panel.open { display: flex; }

    #cb-header {
      background: #1a73e8; color: #fff;
      padding: 14px 16px; display: flex; align-items: center; gap: 10px;
    }
    #cb-header .cb-avatar {
      width: 36px; height: 36px; border-radius: 50%;
      background: rgba(255,255,255,.25);
      display: flex; align-items: center; justify-content: center;
      font-size: 18px;
    }
    #cb-header .cb-title { font-weight: 600; font-size: 15px; }
    #cb-header .cb-subtitle { font-size: 12px; opacity: .85; }
    #cb-close {
      margin-left: auto; background: none; border: none;
      color: #fff; cursor: pointer; font-size: 20px; line-height: 1; padding: 4px;
    }

    #cb-messages {
      flex: 1; overflow-y: auto; padding: 16px;
      display: flex; flex-direction: column; gap: 10px;
    }
    .cb-msg {
      max-width: 82%; padding: 10px 14px;
      border-radius: 14px; font-size: 14px; line-height: 1.45;
      word-break: break-word;
    }
    .cb-msg.bot {
      background: #f1f3f4; color: #202124; border-bottom-left-radius: 4px;
      align-self: flex-start;
    }
    .cb-msg.user {
      background: #1a73e8; color: #fff; border-bottom-right-radius: 4px;
      align-self: flex-end;
    }
    .cb-typing {
      display: flex; gap: 5px; padding: 10px 14px;
      background: #f1f3f4; border-radius: 14px; border-bottom-left-radius: 4px;
      align-self: flex-start;
    }
    .cb-typing span {
      width: 8px; height: 8px; background: #888; border-radius: 50%;
      animation: cb-bounce .9s infinite;
    }
    .cb-typing span:nth-child(2) { animation-delay: .15s; }
    .cb-typing span:nth-child(3) { animation-delay: .3s; }
    @keyframes cb-bounce {
      0%, 60%, 100% { transform: translateY(0); }
      30% { transform: translateY(-6px); }
    }

    #cb-channels {
      margin: 0 16px 12px;
      display: none; flex-direction: column; gap: 8px;
    }
    #cb-channels.visible { display: flex; }
    #cb-channels p { font-size: 12px; color: #5f6368; margin: 0 0 4px; text-align: center; }
    .cb-channel-btn {
      display: flex; align-items: center; gap: 10px;
      padding: 10px 14px; border-radius: 10px; border: 1.5px solid #dadce0;
      background: #fff; cursor: pointer; font-size: 14px; font-weight: 500;
      transition: border-color .15s, background .15s; text-decoration: none; color: #202124;
    }
    .cb-channel-btn:hover { background: #f8f9fa; border-color: #1a73e8; }
    .cb-channel-btn.wa { border-color: #25d366; }
    .cb-channel-btn.wa:hover { background: #f0fff4; }
    .cb-channel-btn.ig { border-color: #e1306c; }
    .cb-channel-btn.ig:hover { background: #fff0f4; }

    #cb-input-row {
      display: flex; gap: 8px; padding: 12px 16px;
      border-top: 1px solid #e8eaed;
    }
    #cb-input {
      flex: 1; border: 1.5px solid #dadce0; border-radius: 24px;
      padding: 9px 16px; font-size: 14px; outline: none;
      transition: border-color .15s;
    }
    #cb-input:focus { border-color: #1a73e8; }
    #cb-send {
      background: #1a73e8; border: none; border-radius: 50%;
      width: 38px; height: 38px; cursor: pointer; flex-shrink: 0;
      display: flex; align-items: center; justify-content: center;
    }
    #cb-send svg { fill: #fff; width: 18px; height: 18px; }
    #cb-send:disabled { background: #dadce0; cursor: default; }
  `;

  var LAUNCHER_ICON = '<svg viewBox="0 0 24 24"><path d="M20 2H4C2.9 2 2 2.9 2 4v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm-2 12H6v-2h12v2zm0-3H6V9h12v2zm0-3H6V6h12v2z"/></svg>';
  var SEND_ICON = '<svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>';
  var WA_ICON = '<svg width="20" viewBox="0 0 24 24" fill="#25d366"><path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347z"/><path d="M11.999 2C6.477 2 2 6.477 2 12c0 1.895.525 3.668 1.438 5.187L2 22l4.985-1.407A9.945 9.945 0 0012 22c5.522 0 10-4.477 10-10S17.522 2 11.999 2zm0 18a7.946 7.946 0 01-4.291-1.254l-.308-.183-3.187.898.906-3.093-.2-.317A7.963 7.963 0 014 12c0-4.411 3.589-8 8-8s8 3.589 8 8-3.588 8-7.999 8z"/></svg>';
  var IG_ICON = '<svg width="20" viewBox="0 0 24 24" fill="#e1306c"><path d="M12 2.163c3.204 0 3.584.012 4.85.07 3.252.148 4.771 1.691 4.919 4.919.058 1.265.069 1.645.069 4.849 0 3.205-.012 3.584-.069 4.849-.149 3.225-1.664 4.771-4.919 4.919-1.266.058-1.644.07-4.85.07-3.204 0-3.584-.012-4.849-.07-3.26-.149-4.771-1.699-4.919-4.92-.058-1.265-.07-1.644-.07-4.849 0-3.204.013-3.583.07-4.849.149-3.227 1.664-4.771 4.919-4.919 1.266-.057 1.645-.069 4.849-.069zm0-2.163c-3.259 0-3.667.014-4.947.072-4.358.2-6.78 2.618-6.98 6.98-.059 1.281-.073 1.689-.073 4.948 0 3.259.014 3.668.072 4.948.2 4.358 2.618 6.78 6.98 6.98 1.281.058 1.689.072 4.948.072 3.259 0 3.668-.014 4.948-.072 4.354-.2 6.782-2.618 6.979-6.98.059-1.28.073-1.689.073-4.948 0-3.259-.014-3.667-.072-4.947-.196-4.354-2.617-6.78-6.979-6.98-1.281-.059-1.69-.073-4.949-.073zm0 5.838c-3.403 0-6.162 2.759-6.162 6.162s2.759 6.163 6.162 6.163 6.162-2.759 6.162-6.163c0-3.403-2.759-6.162-6.162-6.162zm0 10.162c-2.209 0-4-1.79-4-4 0-2.209 1.791-4 4-4s4 1.791 4 4c0 2.21-1.791 4-4 4zm6.406-11.845c-.796 0-1.441.645-1.441 1.44s.645 1.44 1.441 1.44c.795 0 1.439-.645 1.439-1.44s-.644-1.44-1.439-1.44z"/></svg>';

  function buildWidget() {
    var styleEl = document.createElement('style');
    styleEl.textContent = css;
    document.head.appendChild(styleEl);

    // Launcher button
    var launcher = document.createElement('button');
    launcher.id = 'cb-launcher';
    launcher.innerHTML = LAUNCHER_ICON;
    launcher.title = 'Chatear';
    document.body.appendChild(launcher);

    // Panel
    var panel = document.createElement('div');
    panel.id = 'cb-panel';
    panel.innerHTML = `
      <div id="cb-header">
        <div class="cb-avatar">💬</div>
        <div>
          <div class="cb-title" id="cb-header-title">Hola 👋</div>
          <div class="cb-subtitle">Estamos para ayudarte</div>
        </div>
        <button id="cb-close" title="Cerrar">✕</button>
      </div>
      <div id="cb-messages"></div>
      <div id="cb-channels">
        <p>¿Continuamos por otro canal?</p>
        ${WHATSAPP_NUMBER ? `<a class="cb-channel-btn wa" id="cb-wa-btn" href="#" target="_blank">${WA_ICON} Continuar por WhatsApp</a>` : ''}
        ${INSTAGRAM_USER ? `<a class="cb-channel-btn ig" id="cb-ig-btn" href="https://ig.me/m/${INSTAGRAM_USER}" target="_blank">${IG_ICON} Continuar por Instagram</a>` : ''}
      </div>
      <div id="cb-input-row">
        <input id="cb-input" type="text" placeholder="Escribí tu mensaje..." autocomplete="off" />
        <button id="cb-send" disabled>${SEND_ICON}</button>
      </div>
    `;
    document.body.appendChild(panel);

    bindEvents(launcher, panel);
    sendWelcome();
  }

  function bindEvents(launcher, panel) {
    launcher.addEventListener('click', function () {
      panel.classList.toggle('open');
      if (panel.classList.contains('open')) {
        document.getElementById('cb-input').focus();
      }
    });

    document.getElementById('cb-close').addEventListener('click', function () {
      panel.classList.remove('open');
    });

    var input = document.getElementById('cb-input');
    var sendBtn = document.getElementById('cb-send');

    input.addEventListener('input', function () {
      sendBtn.disabled = !input.value.trim();
    });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey && !sendBtn.disabled) {
        e.preventDefault();
        sendMessage();
      }
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

  function addMessage(role, text) {
    var messages = document.getElementById('cb-messages');
    var div = document.createElement('div');
    div.className = 'cb-msg ' + role;
    div.textContent = text;
    messages.appendChild(div);
    messages.scrollTop = messages.scrollHeight;
  }

  function showTyping() {
    var messages = document.getElementById('cb-messages');
    var div = document.createElement('div');
    div.className = 'cb-typing';
    div.id = 'cb-typing';
    div.innerHTML = '<span></span><span></span><span></span>';
    messages.appendChild(div);
    messages.scrollTop = messages.scrollHeight;
    return div;
  }

  function removeTyping() {
    var t = document.getElementById('cb-typing');
    if (t) t.remove();
  }

  function sendMessage() {
    var input = document.getElementById('cb-input');
    var text = input.value.trim();
    if (!text) return;

    input.value = '';
    document.getElementById('cb-send').disabled = true;
    addMessage('user', text);
    messageCount++;

    var typing = showTyping();

    fetch(API_URL + '/chat/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: SESSION_ID,
        project: PROJECT,
        message: text,
        channel: 'web',
      }),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        removeTyping();
        addMessage('bot', data.response);

        if (data.suggest_channels) {
          showChannelSuggestions(text);
        }
      })
      .catch(function () {
        removeTyping();
        addMessage('bot', 'Lo siento, hubo un error. Intentá de nuevo en un momento.');
      });
  }

  function showChannelSuggestions(lastMessage) {
    var channels = document.getElementById('cb-channels');
    if (!channels || channels.classList.contains('visible')) return;

    // Build WhatsApp link with pre-filled message
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
