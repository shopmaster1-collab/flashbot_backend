/*
  Flashbot / Master Electronics widget
  - Chat de texto: backend DeepSeek/Catálogo en /api/chat
  - Voz: ElevenLabs ConvAI embebido dentro de la pestaña Voz
  - Pedidos: consulta independiente en /api/orders usando FOLIO de Google Sheets
*/
(function () {
  'use strict';

  if (window.__MASTER_FLASHBOT_WIDGET_LOADED__) return;
  window.__MASTER_FLASHBOT_WIDGET_LOADED__ = true;

  const DEFAULT_BACKEND = 'https://flashbot-backend-25b6.onrender.com';
  const ELEVENLABS_AGENT_ID = 'agent_0801k6azj1rxe3arwjrs5y4rsrk4';
  const ELEVENLABS_SCRIPT_ID = 'master-elevenlabs-convai-script';

  const currentScript = document.currentScript || Array.from(document.scripts).find((s) => /widget\.js(\?|$)/.test(s.src || ''));
  const BACKEND = normalizeUrl(
    (currentScript && currentScript.dataset && currentScript.dataset.backend) ||
    window.FLASHBOT_BACKEND_URL ||
    window.MAXTER_BASE_URL ||
    DEFAULT_BACKEND
  );

  autoLoadCss();

  const state = {
    tab: 'chat',
    isOpen: false,
    isLoading: false,
    currentQuery: '',
    currentPage: 1,
    pagination: null
  };

  const root = document.createElement('div');
  root.id = 'masterFlashbotWidget';
  root.className = 'mf-root';
  root.innerHTML = `
    <button class="mf-fab" type="button" aria-label="Abrir asistente Master" title="Abrir asistente Master">
      <span class="mf-fab-icon" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none"><path d="M4.25 11.75c0-4.004 3.496-7.25 7.75-7.25s7.75 3.246 7.75 7.25S16.254 19 12 19H7.5L4 21l1.18-3.54a7.02 7.02 0 0 1-.93-5.71Z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></svg>
      </span>
    </button>

    <section class="mf-panel" aria-label="Asistente Master" aria-hidden="true">
      <header class="mf-header">
        <div class="mf-avatar" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none"><path d="M4.25 11.75c0-4.004 3.496-7.25 7.75-7.25s7.75 3.246 7.75 7.25S16.254 19 12 19H7.5L4 21l1.18-3.54a7.02 7.02 0 0 1-.93-5.71Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>
        </div>
        <div class="mf-title-wrap">
          <strong>Asistente</strong>
          <span><i aria-hidden="true"></i> En línea</span>
        </div>
        <button class="mf-close" type="button" aria-label="Cerrar asistente">×</button>
      </header>

      <nav class="mf-tabs" role="tablist" aria-label="Opciones del asistente">
        <button class="mf-tab" type="button" data-tab="voice" role="tab" aria-selected="false">
          <svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 14.5a3 3 0 0 0 3-3v-5a3 3 0 0 0-6 0v5a3 3 0 0 0 3 3Z" stroke="currentColor" stroke-width="1.6"/><path d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v3M9 21h6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
          Voz
        </button>
        <button class="mf-tab mf-tab-active" type="button" data-tab="chat" role="tab" aria-selected="true">
          <svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M6 8.5h12M6 12h8M4.5 5.5h15v10.8h-8.1L7 19v-2.7H4.5V5.5Z" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>
          Chat
        </button>
        <button class="mf-tab" type="button" data-tab="orders" role="tab" aria-selected="false">
          <svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="m12 3 7.5 4.2v9.2L12 21l-7.5-4.6V7.2L12 3Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M4.9 7.4 12 11.6l7.1-4.2M12 21v-9.4" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>
          Pedidos
        </button>
      </nav>

      <main class="mf-content">
        <div class="mf-chat-view" data-view="chat">
          <div class="mf-body" id="mfChatBody" aria-live="polite"></div>
          <div class="mf-pagination" id="mfPagination" hidden>
            <button type="button" class="mf-page-btn" id="mfPrevBtn">‹ Anterior</button>
            <span id="mfPageInfo">Página 1 de 1</span>
            <button type="button" class="mf-page-btn" id="mfNextBtn">Siguiente ›</button>
          </div>
          <form class="mf-inputbar" id="mfChatForm">
            <input id="mfChatInput" type="text" autocomplete="off" placeholder="Escribe tu mensaje..." aria-label="Escribe tu mensaje" required>
            <button type="submit" id="mfChatSubmit" aria-label="Enviar mensaje">
              <svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="m4 12 15-7-4.8 14-3.1-5.8L4 12Z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></svg>
            </button>
          </form>
        </div>

        <div class="mf-voice-view" data-view="voice" hidden>
          <div class="mf-voice-card">
            <div class="mf-voice-orb" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none"><path d="M12 14.5a3 3 0 0 0 3-3v-5a3 3 0 0 0-6 0v5a3 3 0 0 0 3 3Z" stroke="currentColor" stroke-width="1.8"/><path d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v3M9 21h6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>
            </div>
            <h3>Toca para hablar</h3>
            <p>Conversación por voz con ElevenLabs</p>
            <div class="mf-elevenlabs-holder" id="mfElevenLabsHolder"></div>
          </div>
        </div>

        <div class="mf-orders-view" data-view="orders" hidden>
          <div class="mf-orders-intro">
            <h3>Consulta tu pedido</h3>
            <p>Ingresa el número de folio para ver su estado.</p>
          </div>
          <form class="mf-order-form" id="mfOrderForm">
            <label for="mfOrderInput">Número de folio</label>
            <div class="mf-order-row">
              <input id="mfOrderInput" type="text" autocomplete="off" placeholder="Ej. #A1BC3" required>
              <button type="submit" id="mfOrderSubmit" aria-label="Consultar pedido">
                <svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="m21 21-4.35-4.35M10.8 18.1a7.3 7.3 0 1 1 0-14.6 7.3 7.3 0 0 1 0 14.6Z" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>
              </button>
            </div>
          </form>
          <div class="mf-order-result" id="mfOrderResult">
            Aquí aparecerá el detalle de tu pedido.
          </div>
        </div>
      </main>
    </section>
  `;

  function init() {
    document.body.appendChild(root);
    bindEvents();
    renderWelcome();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    init();
  }

  function bindEvents() {
    $('.mf-fab').addEventListener('click', togglePanel);
    $('.mf-close').addEventListener('click', closePanel);

    $$('.mf-tab').forEach((btn) => {
      btn.addEventListener('click', () => switchTab(btn.dataset.tab));
    });

    $('#mfChatForm').addEventListener('submit', onChatSubmit);
    $('#mfOrderForm').addEventListener('submit', onOrderSubmit);
    $('#mfPrevBtn').addEventListener('click', () => changePage(-1));
    $('#mfNextBtn').addEventListener('click', () => changePage(1));
  }

  function togglePanel() {
    state.isOpen ? closePanel() : openPanel();
  }

  function openPanel() {
    state.isOpen = true;
    $('.mf-panel').classList.add('mf-open');
    $('.mf-panel').setAttribute('aria-hidden', 'false');
    $('.mf-fab').classList.add('mf-fab-hidden');
    if (state.tab === 'chat') setTimeout(() => $('#mfChatInput').focus(), 120);
  }

  function closePanel() {
    state.isOpen = false;
    $('.mf-panel').classList.remove('mf-open');
    $('.mf-panel').setAttribute('aria-hidden', 'true');
    $('.mf-fab').classList.remove('mf-fab-hidden');
  }

  function switchTab(tab) {
    if (!tab || tab === state.tab) return;
    state.tab = tab;

    $$('.mf-tab').forEach((btn) => {
      const active = btn.dataset.tab === tab;
      btn.classList.toggle('mf-tab-active', active);
      btn.setAttribute('aria-selected', active ? 'true' : 'false');
    });

    $$('[data-view]').forEach((view) => {
      view.hidden = view.dataset.view !== tab;
    });

    if (tab === 'voice') mountElevenLabs();
    if (tab === 'chat') setTimeout(() => $('#mfChatInput').focus(), 90);
    if (tab === 'orders') setTimeout(() => $('#mfOrderInput').focus(), 90);
  }

  function renderWelcome() {
    const body = $('#mfChatBody');
    body.innerHTML = '';
    appendChatMessage('¡Hola! Soy tu asistente. ¿En qué puedo ayudarte hoy?', 'bot');
  }

  function appendChatMessage(text, from) {
    const body = $('#mfChatBody');
    const bubble = document.createElement('div');
    bubble.className = `mf-msg ${from === 'user' ? 'mf-msg-user' : 'mf-msg-bot'}`;
    bubble.textContent = text || '';
    body.appendChild(bubble);
    body.scrollTop = body.scrollHeight;
  }

  function showChatLoading(label) {
    const body = $('#mfChatBody');
    const loading = document.createElement('div');
    loading.className = 'mf-loading';
    loading.id = 'mfChatLoading';
    loading.innerHTML = `<span></span><span></span><span></span><em>${escapeHtml(label || 'Escribiendo')}</em>`;
    body.appendChild(loading);
    body.scrollTop = body.scrollHeight;
  }

  function hideChatLoading() {
    const node = $('#mfChatLoading');
    if (node) node.remove();
  }

  async function onChatSubmit(event) {
    event.preventDefault();
    if (state.isLoading) return;

    const input = $('#mfChatInput');
    const message = input.value.trim();
    if (!message) return;

    appendChatMessage(message, 'user');
    input.value = '';
    state.currentQuery = message;
    state.currentPage = 1;
    await performChat(message, 1, true);
  }

  async function performChat(message, page, isNewSearch) {
    state.isLoading = true;
    setChatSubmitState(true);
    showChatLoading('Buscando respuesta');

    try {
      const res = await fetch(`${BACKEND}/api/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message, page, per_page: 6 })
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);

      hideChatLoading();
      if (isNewSearch && data.answer) appendChatMessage(data.answer, 'bot');
      if (Array.isArray(data.products) && data.products.length) renderProductCards(data.products, isNewSearch);
      if (!data.answer && (!data.products || !data.products.length)) {
        appendChatMessage('No encontré información suficiente. Escríbeme el producto, marca o característica que buscas y te ayudo a ubicarlo.', 'bot');
      }
      updatePagination(data.pagination || null);
    } catch (err) {
      console.error('[Flashbot] chat error:', err);
      hideChatLoading();
      appendChatMessage('Hubo un problema al generar la respuesta. Intenta de nuevo en unos segundos.', 'bot');
      updatePagination(null);
    } finally {
      state.isLoading = false;
      setChatSubmitState(false);
    }
  }

  function renderProductCards(products, isNewSearch) {
    const body = $('#mfChatBody');
    if (isNewSearch) $$('.mf-products', body).forEach((n) => n.remove());

    const wrap = document.createElement('div');
    wrap.className = 'mf-products';
    products.forEach((p) => {
      const card = document.createElement('article');
      card.className = 'mf-product-card';
      const price = p.compare_at_price
        ? `<p class="mf-product-price"><s>${escapeHtml(p.compare_at_price)}</s> ${escapeHtml(p.price || '')}</p>`
        : `<p class="mf-product-price">${escapeHtml(p.price || '')}</p>`;
      const inv = Array.isArray(p.inventory) && p.inventory.length
        ? `<p class="mf-product-inv"><strong>Inventario:</strong> ${p.inventory.map((x) => `${escapeHtml(x.name || 'Almacén')}: ${escapeHtml(String(x.available ?? '0'))}`).join(' · ')}</p>`
        : '';
      card.innerHTML = `
        <img src="${escapeAttribute(p.image || '')}" alt="${escapeAttribute(p.title || 'Producto Master')}" loading="lazy">
        <div>
          <h4>${escapeHtml(p.title || 'Producto Master')}</h4>
          ${price}
          <div class="mf-product-actions">
            <a class="mf-buy" href="${escapeAttribute(p.buy_url || p.product_url || '#')}" target="_blank" rel="noopener noreferrer">Comprar ahora</a>
            <a class="mf-view" href="${escapeAttribute(p.product_url || p.buy_url || '#')}" target="_blank" rel="noopener noreferrer">Ver producto</a>
          </div>
          ${inv}
        </div>`;
      wrap.appendChild(card);
    });
    body.appendChild(wrap);
    body.scrollTop = body.scrollHeight;
  }

  function updatePagination(pagination) {
    const box = $('#mfPagination');
    if (!pagination || pagination.total_pages <= 1) {
      box.hidden = true;
      state.pagination = null;
      return;
    }
    state.pagination = pagination;
    state.currentPage = pagination.page;
    box.hidden = false;
    $('#mfPageInfo').textContent = `Página ${pagination.page} de ${pagination.total_pages}`;
    $('#mfPrevBtn').disabled = !pagination.has_prev;
    $('#mfNextBtn').disabled = !pagination.has_next;
  }

  function changePage(delta) {
    if (state.isLoading || !state.pagination) return;
    const nextPage = state.currentPage + delta;
    if (nextPage < 1 || nextPage > state.pagination.total_pages) return;
    performChat(state.currentQuery, nextPage, false);
  }

  function setChatSubmitState(loading) {
    const btn = $('#mfChatSubmit');
    btn.disabled = loading;
    btn.classList.toggle('mf-btn-loading', loading);
  }

  async function onOrderSubmit(event) {
    event.preventDefault();
    if (state.isLoading) return;

    const input = $('#mfOrderInput');
    const folio = input.value.trim();
    if (!folio) return;

    const result = $('#mfOrderResult');
    result.className = 'mf-order-result mf-order-loading';
    result.textContent = 'Consultando pedido...';
    state.isLoading = true;
    $('#mfOrderSubmit').disabled = true;

    try {
      const res = await fetch(`${BACKEND}/api/orders`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ folio, order: folio })
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      renderOrderResult(data, folio);
    } catch (err) {
      console.error('[Flashbot] order error:', err);
      result.className = 'mf-order-result mf-order-error';
      result.textContent = 'No fue posible consultar el pedido en este momento. Intenta nuevamente en unos segundos.';
    } finally {
      state.isLoading = false;
      $('#mfOrderSubmit').disabled = false;
    }
  }

  function renderOrderResult(data, requestedFolio) {
    const result = $('#mfOrderResult');
    const items = Array.isArray(data.items) ? data.items : [];
    if (!items.length) {
      result.className = 'mf-order-result mf-order-empty';
      result.textContent = data.answer || `No encontramos información para el folio ${requestedFolio}. Verifica que esté escrito tal como aparece en tu comprobante.`;
      return;
    }

    const first = items[0] || {};
    const folio = first.Folio || data.folio || data.order || requestedFolio;
    const orden = first['Orden de compra'] || '';
    const columns = ['Orden de compra', 'SKU de producto', 'Cantidad', 'Total', 'Paquetería', 'Guía'];

    result.className = 'mf-order-result mf-order-success';
    result.innerHTML = `
      <div class="mf-order-title">
        <strong>Pedido correspondiente al folio: ${escapeHtml(folio)}</strong>
        ${orden ? `<span>Orden de compra: ${escapeHtml(orden)}</span>` : ''}
      </div>
      <div class="mf-table-scroll">
        <table class="mf-order-table">
          <thead><tr>${columns.map((col) => `<th>${escapeHtml(col)}</th>`).join('')}</tr></thead>
          <tbody>
            ${items.map((row) => `<tr>${columns.map((col) => `<td>${escapeHtml(row[col] || '—')}</td>`).join('')}</tr>`).join('')}
          </tbody>
        </table>
      </div>`;
  }

  function mountElevenLabs() {
    const holder = $('#mfElevenLabsHolder');
    if (!holder.dataset.mounted) {
      holder.innerHTML = `<elevenlabs-convai agent-id="${ELEVENLABS_AGENT_ID}"></elevenlabs-convai>`;
      holder.dataset.mounted = '1';
    }
    if (!document.getElementById(ELEVENLABS_SCRIPT_ID)) {
      const script = document.createElement('script');
      script.id = ELEVENLABS_SCRIPT_ID;
      script.src = 'https://unpkg.com/@elevenlabs/convai-widget-embed';
      script.async = true;
      script.type = 'text/javascript';
      document.head.appendChild(script);
    }
  }

  function autoLoadCss() {
    if (document.getElementById('master-flashbot-widget-css')) return;
    const scriptSrc = currentScript && currentScript.src ? currentScript.src : '';
    if (!scriptSrc) return;
    const cssHref = scriptSrc.replace(/widget\.js(\?.*)?$/, 'widget.css$1');
    if (!cssHref || cssHref === scriptSrc) return;
    const link = document.createElement('link');
    link.id = 'master-flashbot-widget-css';
    link.rel = 'stylesheet';
    link.href = cssHref;
    document.head.appendChild(link);
  }

  function normalizeUrl(url) {
    return String(url || '').replace(/\/+$/, '');
  }

  function $(selector, parent) {
    return (parent || root).querySelector(selector);
  }

  function $$(selector, parent) {
    return Array.from((parent || root).querySelectorAll(selector));
  }

  function escapeHtml(value) {
    return String(value ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  function escapeAttribute(value) {
    return escapeHtml(value).replace(/`/g, '&#096;');
  }
})();
