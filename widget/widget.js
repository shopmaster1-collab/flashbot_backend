/*
  Flashbot / Master Electronics widget
  ------------------------------------------------------------
  - Chat de texto: backend DeepSeek/Catálogo en /api/chat
  - Voz: ElevenLabs ConvAI, montado únicamente dentro de la pestaña Voz
  - Pedidos: consulta independiente en /api/orders usando ORDEN_COMPRA
  - Versión robusta para Shopify: no depende de scripts externos de ElevenLabs
*/
(function () {
  'use strict';

  if (window.__MASTER_FLASHBOT_WIDGET_LOADED__) return;
  window.__MASTER_FLASHBOT_WIDGET_LOADED__ = true;

  const DEFAULT_BACKEND = 'https://flashbot-backend-25b6.onrender.com';
  const DEFAULT_AGENT_ID = 'agent_0801k6azj1rxe3arwjrs5y4rsrk4';
  const ELEVENLABS_SCRIPT_ID = 'master-elevenlabs-convai-script';
  const CSS_LINK_ID = 'master-flashbot-widget-css';
  const CSS_FALLBACK_ID = 'master-flashbot-widget-critical-css';
  const CHAT_PRODUCTS_PER_PAGE = 20;

  const currentScript = document.currentScript || Array.from(document.scripts).find((s) => /widget\.js(\?|$)/.test(s.src || ''));
  const scriptDataset = (currentScript && currentScript.dataset) || {};
  const options = window.MASTER_FLASHBOT_OPTIONS || {};

  const BACKEND = normalizeUrl(
    scriptDataset.backend ||
    window.FLASHBOT_BACKEND_URL ||
    window.MAXTER_BASE_URL ||
    options.backendUrl ||
    DEFAULT_BACKEND
  );

  const ELEVENLABS_AGENT_ID =
    scriptDataset.agentId ||
    options.elevenlabsAgentId ||
    window.ELEVENLABS_AGENT_ID ||
    DEFAULT_AGENT_ID;

  const PRODUCT_PANEL_ID = options.productPanelId || 'agent-product-panel';

  installCss();
  installElevenLabsClientToolsBridge();

  const state = {
    tab: 'chat',
    isOpen: false,
    chatLoading: false,
    orderLoading: false,
    currentQuery: '',
    currentPage: 1,
    pagination: null,
    elevenLabsMounted: false
  };

  const root = document.createElement('div');
  root.id = 'masterFlashbotWidget';
  root.className = 'mf-root';
  root.dataset.activeTab = 'chat';
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
          <strong>Tu Asistente Maxter</strong>
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
        <div class="mf-chat-view mf-view-active" data-view="chat">
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

        <div class="mf-voice-view mf-view-hidden" data-view="voice" hidden>
          <div class="mf-voice-card">
            <div class="mf-voice-orb" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none"><path d="M12 14.5a3 3 0 0 0 3-3v-5a3 3 0 0 0-6 0v5a3 3 0 0 0 3 3Z" stroke="currentColor" stroke-width="1.8"/><path d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v3M9 21h6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>
            </div>
            <h3>Toca para hablar</h3>
            <p>Conversación por voz con ElevenLabs</p>
            <div class="mf-elevenlabs-holder" id="mfElevenLabsHolder"></div>
          </div>
        </div>

        <div class="mf-orders-view mf-view-hidden" data-view="orders" hidden>
          <div class="mf-orders-intro">
            <h3>Consulta tu pedido</h3>
            <p>Por favor ingrese su Número de pedido para conocer el estatus.</p>
          </div>
          <form class="mf-order-form" id="mfOrderForm">
            <label for="mfOrderInput">Número de Pedido</label>
            <div class="mf-order-row">
              <input id="mfOrderInput" type="text" autocomplete="off" placeholder="Ej. 702-7300318-1033843" required>
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
    if (!document.body) {
      setTimeout(init, 30);
      return;
    }

    document.body.appendChild(root);
    ensureProductPanel();
    bindEvents();
    renderWelcome();
    console.info('[Flashbot] Widget listo', { backend: BACKEND, elevenlabsAgentId: ELEVENLABS_AGENT_ID });

    window.MasterFlashbotWidget = {
      open: openPanel,
      close: closePanel,
      switchTab,
      backend: BACKEND,
      version: '2026-05-11.4'
    };
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    init();
  }

  function bindEvents() {
    const fab = $('.mf-fab');
    const close = $('.mf-close');
    const chatForm = $('#mfChatForm');
    const orderForm = $('#mfOrderForm');
    const prev = $('#mfPrevBtn');
    const next = $('#mfNextBtn');

    if (fab) fab.addEventListener('click', togglePanel);
    if (close) close.addEventListener('click', closePanel);

    $$('.mf-tab').forEach((btn) => {
      btn.addEventListener('click', () => switchTab(btn.dataset.tab));
    });

    if (chatForm) chatForm.addEventListener('submit', onChatSubmit);
    if (orderForm) orderForm.addEventListener('submit', onOrderSubmit);
    if (prev) prev.addEventListener('click', () => changePage(-1));
    if (next) next.addEventListener('click', () => changePage(1));
  }

  function togglePanel() {
    state.isOpen ? closePanel() : openPanel();
  }

  function openPanel() {
    state.isOpen = true;
    const panel = $('.mf-panel');
    const fab = $('.mf-fab');
    if (panel) {
      panel.classList.add('mf-open');
      panel.setAttribute('aria-hidden', 'false');
    }
    if (fab) fab.classList.add('mf-fab-hidden');
    if (state.tab === 'chat') setTimeout(() => safeFocus('#mfChatInput'), 120);
  }

  function closePanel() {
    state.isOpen = false;
    const panel = $('.mf-panel');
    const fab = $('.mf-fab');
    if (panel) {
      panel.classList.remove('mf-open');
      panel.setAttribute('aria-hidden', 'true');
    }
    if (fab) fab.classList.remove('mf-fab-hidden');
  }

  function switchTab(tab) {
    if (!tab || !['chat', 'voice', 'orders'].includes(tab)) return;
    if (tab === state.tab) {
      if (tab === 'chat') setTimeout(() => safeFocus('#mfChatInput'), 90);
      if (tab === 'orders') setTimeout(() => safeFocus('#mfOrderInput'), 90);
      return;
    }
    state.tab = tab;
    root.dataset.activeTab = tab;

    $$('.mf-tab').forEach((btn) => {
      const active = btn.dataset.tab === tab;
      btn.classList.toggle('mf-tab-active', active);
      btn.setAttribute('aria-selected', active ? 'true' : 'false');
    });

    $$('[data-view]').forEach((view) => {
      const active = view.dataset.view === tab;
      view.hidden = !active;
      view.classList.toggle('mf-view-active', active);
      view.classList.toggle('mf-view-hidden', !active);
      view.setAttribute('aria-hidden', active ? 'false' : 'true');
    });

    if (tab !== 'chat') hideChatLoading();
    if (tab === 'voice') mountElevenLabs();
    if (tab === 'chat') setTimeout(() => safeFocus('#mfChatInput'), 90);
    if (tab === 'orders') setTimeout(() => safeFocus('#mfOrderInput'), 90);
  }

  function renderWelcome() {
    const body = $('#mfChatBody');
    if (!body) return;
    body.innerHTML = '';
    appendChatMessage('¡Hola! Soy tu asistente inteligente Maxter. ¿En qué puedo ayudarte hoy?', 'bot');
  }

  function appendChatMessage(text, from) {
    const body = $('#mfChatBody');
    if (!body) return;
    const bubble = document.createElement('div');
    bubble.className = `mf-msg ${from === 'user' ? 'mf-msg-user' : 'mf-msg-bot'}`;
    bubble.textContent = text || '';
    body.appendChild(bubble);
    body.scrollTop = body.scrollHeight;
  }

  function showChatLoading(label) {
    const body = $('#mfChatBody');
    if (!body) return;
    hideChatLoading();
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
    if (state.chatLoading) return;

    const input = $('#mfChatInput');
    const message = input ? input.value.trim() : '';
    if (!message) return;

    appendChatMessage(message, 'user');
    input.value = '';
    state.currentQuery = message;
    state.currentPage = 1;
    clearChatProducts();
    updatePagination(null);
    await performChat(message, 1, true);
  }

  async function performChat(message, page, isNewSearch) {
    state.chatLoading = true;
    setChatSubmitState(true);
    showChatLoading('Buscando respuesta');

    try {
      const data = await postJson(`${BACKEND}/api/chat`, { message, page, per_page: CHAT_PRODUCTS_PER_PAGE }, 30000);

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
      appendChatMessage(buildFriendlyError(err, 'chat'), 'bot');
      updatePagination(null);
    } finally {
      state.chatLoading = false;
      setChatSubmitState(false);
    }
  }

  function clearChatProducts() {
    const body = $('#mfChatBody');
    if (!body) return;
    $$('.mf-products', body).forEach((n) => n.remove());
  }

  function renderProductCards(products, isNewSearch) {
    const body = $('#mfChatBody');
    if (!body) return;
    // La paginación reemplaza los productos actuales; nunca debe duplicar tarjetas.
    clearChatProducts();

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
    if (!box) return;

    const total = Number(pagination && pagination.total ? pagination.total : 0);
    const totalPages = Number(pagination && pagination.total_pages ? pagination.total_pages : 0);

    // La numeración sólo debe aparecer cuando existen más de 20 productos.
    if (!pagination || total <= CHAT_PRODUCTS_PER_PAGE || totalPages <= 1) {
      box.hidden = true;
      state.pagination = null;
      return;
    }

    state.pagination = pagination;
    state.currentPage = Number(pagination.page || 1);
    box.hidden = false;
    const info = $('#mfPageInfo');
    const prev = $('#mfPrevBtn');
    const next = $('#mfNextBtn');
    if (info) info.textContent = `Página ${state.currentPage} de ${totalPages}`;
    if (prev) prev.disabled = !pagination.has_prev;
    if (next) next.disabled = !pagination.has_next;
  }

  function changePage(delta) {
    if (state.chatLoading || !state.pagination) return;
    const nextPage = state.currentPage + delta;
    if (nextPage < 1 || nextPage > state.pagination.total_pages) return;
    performChat(state.currentQuery, nextPage, false);
  }

  function setChatSubmitState(loading) {
    const btn = $('#mfChatSubmit');
    if (!btn) return;
    btn.disabled = loading;
    btn.classList.toggle('mf-btn-loading', loading);
  }

  async function onOrderSubmit(event) {
    event.preventDefault();
    if (state.orderLoading) return;

    const input = $('#mfOrderInput');
    const orderNo = input ? input.value.trim() : '';
    if (!orderNo) return;

    const result = $('#mfOrderResult');
    if (result) {
      result.className = 'mf-order-result mf-order-loading';
      result.textContent = 'Consultando número de pedido...';
    }
    state.orderLoading = true;
    const submit = $('#mfOrderSubmit');
    if (submit) submit.disabled = true;

    try {
      const data = await postJson(`${BACKEND}/api/orders`, { order: orderNo }, 30000);
      renderOrderResult(data, orderNo);
    } catch (err) {
      console.error('[Flashbot] order error:', err);
      if (result) {
        result.className = 'mf-order-result mf-order-error';
        result.textContent = buildFriendlyError(err, 'orders');
      }
    } finally {
      state.orderLoading = false;
      if (submit) submit.disabled = false;
    }
  }

  function renderOrderResult(data, requestedOrder) {
    const result = $('#mfOrderResult');
    if (!result) return;
    const items = Array.isArray(data.items) ? data.items : [];
    if (!items.length) {
      result.className = 'mf-order-result mf-order-empty';
      result.textContent = data.answer || `No encontramos información para el número de pedido ${requestedOrder}. Verifica que esté escrito tal como aparece en tu comprobante.`;
      return;
    }

    const first = items[0] || {};
    const orderNo = first['Orden de compra'] || data.order || data.folio || requestedOrder;
    const columns = ['Orden de compra', 'SKU de producto', 'Cantidad', 'Total', 'Paquetería', 'Guía'];

    result.className = 'mf-order-result mf-order-success';
    result.innerHTML = `
      <div class="mf-order-title">
        <strong>Pedido correspondiente al pedido: ${escapeHtml(orderNo)}</strong>
        <span>Consulta por ORDEN_COMPRA</span>
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
    if (!holder) return;

    if (!state.elevenLabsMounted) {
      holder.innerHTML = '';
      const convai = document.createElement('elevenlabs-convai');
      convai.setAttribute('agent-id', ELEVENLABS_AGENT_ID);
      convai.addEventListener('elevenlabs-convai:call', injectElevenLabsClientTools);
      holder.appendChild(convai);
      state.elevenLabsMounted = true;
    }

    loadElevenLabsScript();
  }

  function loadElevenLabsScript() {
    if (document.getElementById(ELEVENLABS_SCRIPT_ID)) return;
    const script = document.createElement('script');
    script.id = ELEVENLABS_SCRIPT_ID;
    script.src = 'https://unpkg.com/@elevenlabs/convai-widget-embed';
    script.async = true;
    script.type = 'text/javascript';
    script.onerror = function () {
      console.error('[Flashbot] No se pudo cargar el script de ElevenLabs.');
      const holder = $('#mfElevenLabsHolder');
      if (holder) {
        holder.innerHTML = '<p class="mf-voice-error">No fue posible cargar la conversación por voz. Revisa la conexión o intenta más tarde.</p>';
      }
    };
    document.head.appendChild(script);
  }

  function installElevenLabsClientToolsBridge() {
    document.addEventListener('elevenlabs-convai:call', injectElevenLabsClientTools, true);
  }

  function injectElevenLabsClientTools(event) {
    try {
      if (!event || !event.detail) return;
      event.detail.config = event.detail.config || {};
      event.detail.config.clientTools = Object.assign({}, event.detail.config.clientTools || {}, CLIENT_TOOLS);
      console.info('[Flashbot] ElevenLabs clientTools conectados');
    } catch (err) {
      console.warn('[Flashbot] No se pudieron inyectar clientTools en ElevenLabs:', err);
    }
  }

  const CLIENT_TOOLS = {
    show_product(payload) {
      const data = payload || {};
      const name = String(data.name || data.title || 'Producto Master');
      const url = String(data.url || data.product_url || data.buy_url || 'https://master.com.mx/');
      const imageUrl = String(data.image_url || data.image || '');
      const price = String(data.price || '');
      const panel = ensureProductPanel();

      if (!panel) return 'No se pudo mostrar el producto porque falta el panel en la página.';

      panel.innerHTML = `
        <a href="${escapeAttribute(url)}" target="_blank" rel="noopener noreferrer" class="agent-product-card">
          <div style="display:flex;gap:12px;align-items:center;padding:12px;border:1px solid #ddd;border-radius:12px;margin-top:12px;background:#fff;box-shadow:0 8px 20px rgba(0,0,0,.18);">
            ${imageUrl ? `<img src="${escapeAttribute(imageUrl)}" alt="${escapeAttribute(name)}" style="width:72px;height:72px;object-fit:cover;border-radius:8px;">` : ''}
            <div>
              <div style="font-weight:700;margin-bottom:4px;color:#111827;">${escapeHtml(name)}</div>
              ${price ? `<div style="color:#0a7e3b;font-weight:700;margin-bottom:4px;">${escapeHtml(price)}</div>` : ''}
              <div style="color:#006DFF;text-decoration:underline;font-size:13px;font-weight:700;">Ver producto</div>
            </div>
          </div>
        </a>`;

      return 'Mostrando el producto recomendado en la página.';
    }
  };

  function ensureProductPanel() {
    let panel = document.getElementById(PRODUCT_PANEL_ID);
    if (!panel && document.body) {
      panel = document.createElement('div');
      panel.id = PRODUCT_PANEL_ID;
      panel.style.position = 'fixed';
      panel.style.bottom = '120px';
      panel.style.left = '20px';
      panel.style.maxWidth = '360px';
      panel.style.zIndex = '2147482500';
      document.body.appendChild(panel);
    }
    return panel;
  }

  async function postJson(url, payload, timeoutMs) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs || 30000);
    try {
      const res = await fetch(url, {
        method: 'POST',
        mode: 'cors',
        credentials: 'omit',
        cache: 'no-store',
        headers: {
          'Accept': 'application/json',
          'Content-Type': 'application/json'
        },
        body: JSON.stringify(payload || {}),
        signal: controller.signal
      });

      const text = await res.text();
      let data = {};
      if (text) {
        try {
          data = JSON.parse(text);
        } catch (err) {
          throw new Error(`Respuesta no JSON del servidor: ${text.slice(0, 160)}`);
        }
      }

      if (!res.ok) {
        throw new Error(data.error || data.message || `HTTP ${res.status}`);
      }
      return data;
    } catch (err) {
      if (err && err.name === 'AbortError') {
        throw new Error('Tiempo de espera agotado al conectar con el servidor.');
      }
      throw err;
    } finally {
      clearTimeout(timer);
    }
  }

  function buildFriendlyError(err, type) {
    const message = String((err && err.message) || err || '');
    if (/Failed to fetch|NetworkError|Load failed|CORS/i.test(message)) {
      return 'No pude conectar con el servidor del asistente. Verifica que el backend esté activo en Render y que la URL del widget sea correcta.';
    }
    if (/Tiempo de espera/i.test(message)) {
      return 'El servidor tardó demasiado en responder. Intenta nuevamente en unos segundos.';
    }
    if (type === 'orders') {
      return 'No fue posible consultar el pedido en este momento. Intenta nuevamente en unos segundos.';
    }
    return 'Hubo un problema al generar la respuesta. Intenta de nuevo en unos segundos.';
  }

  function installCss() {
    autoLoadCss();
    // Fallback para Shopify: si el CSS externo tarda, el botón y panel siguen funcionando.
    setTimeout(() => {
      const linked = document.getElementById(CSS_LINK_ID);
      if (!linked || linked.dataset.loaded !== '1') injectCriticalCss();
    }, 2500);
  }

  function autoLoadCss() {
    if (document.getElementById(CSS_LINK_ID)) return;
    const scriptSrc = currentScript && currentScript.src ? currentScript.src : '';
    if (!scriptSrc) {
      injectCriticalCss();
      return;
    }
    const cssHref = scriptSrc.replace(/widget\.js(\?.*)?$/, 'widget.css$1');
    if (!cssHref || cssHref === scriptSrc) {
      injectCriticalCss();
      return;
    }
    const link = document.createElement('link');
    link.id = CSS_LINK_ID;
    link.rel = 'stylesheet';
    link.href = cssHref;
    link.onload = function () { link.dataset.loaded = '1'; };
    link.onerror = injectCriticalCss;
    document.head.appendChild(link);
  }

  function injectCriticalCss() {
    if (document.getElementById(CSS_FALLBACK_ID)) return;
    const style = document.createElement('style');
    style.id = CSS_FALLBACK_ID;
    style.textContent = `
      .mf-root,.mf-root *{box-sizing:border-box}.mf-root [hidden],.mf-view-hidden{display:none!important}.mf-root{font-family:Inter,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;color:#111827}.mf-fab{position:fixed!important;right:22px!important;bottom:92px!important;width:64px;height:64px;border:0;border-radius:999px;display:flex;align-items:center;justify-content:center;cursor:pointer;color:#fff;background:linear-gradient(135deg,#006DFF,#00A8FF);box-shadow:0 0 0 24px rgba(0,109,255,.08),0 18px 44px rgba(0,82,204,.30);z-index:2147483000!important}.mf-fab-icon svg{width:31px;height:31px;display:block}.mf-fab-hidden{opacity:0;pointer-events:none}.mf-panel{position:fixed!important;right:12px;bottom:14px;width:min(386px,calc(100vw - 24px));height:min(600px,calc(100vh - 28px));background:#fff;border-radius:12px;overflow:hidden;z-index:2147483001!important;display:flex;flex-direction:column;box-shadow:0 26px 70px rgba(15,23,42,.24);border:1px solid rgba(148,163,184,.20);opacity:0;transform:translateY(18px) scale(.985);pointer-events:none;transition:opacity .2s ease,transform .2s ease}.mf-panel.mf-open{opacity:1;transform:translateY(0) scale(1);pointer-events:auto}.mf-header{min-height:74px;padding:14px 18px;display:flex;align-items:center;gap:12px;color:#fff;background:linear-gradient(135deg,#0047CC,#006DFF 52%,#00A8FF)}.mf-avatar{width:34px;height:34px;border-radius:50%;display:flex;align-items:center;justify-content:center;background:rgba(255,255,255,.16)}.mf-avatar svg{width:24px;height:24px}.mf-title-wrap{display:flex;flex-direction:column;line-height:1.15;min-width:0;flex:1}.mf-title-wrap strong{font-size:15px;font-weight:800}.mf-title-wrap span{margin-top:4px;display:flex;align-items:center;gap:6px;font-size:12px;font-weight:500}.mf-title-wrap i{width:7px;height:7px;border-radius:999px;background:#22C55E}.mf-close{width:30px;height:30px;border:0;background:transparent;color:#fff;font-size:28px;line-height:1;cursor:pointer}.mf-tabs{height:62px;display:grid;grid-template-columns:repeat(3,1fr);background:#fff;border-bottom:1px solid #E5E7EB}.mf-tab{position:relative;border:0;background:#fff;cursor:pointer;color:#64748B;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;font-size:12px;font-weight:650}.mf-tab svg{width:18px;height:18px}.mf-tab-active{color:#006DFF}.mf-tab-active:after{content:"";position:absolute;left:22%;right:22%;bottom:0;height:2px;background:linear-gradient(90deg,#006DFF,#00A8FF)}.mf-content{flex:1;min-height:0;background:#fff}.mf-chat-view,.mf-voice-view,.mf-orders-view{height:100%}.mf-root[data-active-tab=chat] .mf-chat-view{display:flex!important}.mf-root[data-active-tab=voice] .mf-voice-view{display:block!important}.mf-root[data-active-tab=orders] .mf-orders-view{display:block!important}.mf-chat-view{display:flex;flex-direction:column;min-height:0}.mf-body{flex:1;overflow:auto;padding:18px;background:#fff}.mf-msg{width:fit-content;max-width:82%;padding:12px 14px;margin:0 0 10px;border-radius:16px;font-size:14px;line-height:1.55;word-break:break-word;white-space:pre-line}.mf-msg-bot{background:#F2F5FA;color:#111827;border-bottom-left-radius:6px}.mf-msg-user{margin-left:auto;color:#fff;background:linear-gradient(135deg,#006DFF,#00A8FF);border-bottom-right-radius:6px}.mf-inputbar{display:flex;align-items:center;gap:10px;padding:10px 14px 12px;border-top:1px solid #E5E7EB;background:#fff}.mf-inputbar input{flex:1;height:46px;min-width:0;border:1px solid #CBD5E1;border-radius:16px;padding:0 48px 0 14px;color:#334155;font-size:14px;outline:none}.mf-inputbar button{width:42px;height:42px;margin-left:-54px;border:0;border-radius:999px;display:flex;align-items:center;justify-content:center;color:#fff;background:linear-gradient(135deg,#006DFF,#00A8FF);cursor:pointer}.mf-inputbar svg{width:20px;height:20px}.mf-products{display:flex;flex-direction:column;gap:10px;margin:8px 0 12px}.mf-product-card{display:grid;grid-template-columns:72px 1fr;gap:12px;padding:10px;border:1px solid #E5E7EB;border-radius:14px;background:#fff}.mf-product-card img{width:72px;height:72px;border-radius:12px;object-fit:contain;background:#F8FAFC}.mf-product-card h4{margin:0;font-size:13px}.mf-product-price{margin:5px 0 7px;color:#006DFF;font-size:14px;font-weight:900}.mf-product-actions{display:flex;gap:8px;flex-wrap:wrap}.mf-product-actions a{text-decoration:none;font-size:12px;font-weight:800}.mf-buy{padding:7px 10px;border-radius:999px;color:#fff;background:#16A34A}.mf-view{color:#334155;text-decoration:underline!important}.mf-pagination{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:9px 12px;border-top:1px solid #E5E7EB;color:#64748B;font-size:12px;background:#fff}.mf-pagination[hidden]{display:none!important}.mf-page-btn{border:1px solid #CBD5E1;border-radius:10px;padding:7px 10px;background:#fff;color:#334155;font-size:12px;font-weight:800;cursor:pointer}.mf-voice-card{height:100%;min-height:420px;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:24px;text-align:center}.mf-voice-orb{width:112px;height:112px;border-radius:999px;display:flex;align-items:center;justify-content:center;color:#fff;background:linear-gradient(135deg,#006DFF,#00A8FF);box-shadow:0 0 0 24px rgba(0,109,255,.07),0 24px 54px rgba(0,82,204,.22);margin-bottom:26px}.mf-voice-orb svg{width:45px;height:45px}.mf-voice-card h3{margin:0 0 8px;font-size:15px}.mf-voice-card p{margin:0 0 18px;color:#6B7280;font-size:13px}.mf-elevenlabs-holder{width:100%;display:flex;justify-content:center}.mf-voice-error{color:#991B1B;background:#FEF2F2;border:1px solid #FCA5A5;border-radius:12px;padding:10px;font-size:13px}.mf-orders-view{padding:24px 22px;overflow:auto}.mf-orders-intro h3{margin:0 0 8px;font-size:16px}.mf-orders-intro p{margin:0 0 22px;color:#6B7280;font-size:13px}.mf-order-form label{display:block;margin:0 0 8px;color:#64748B;font-size:12px;font-weight:900;text-transform:uppercase}.mf-order-row{display:flex;align-items:center;gap:8px;margin-bottom:14px}.mf-order-row input{flex:1;min-width:0;height:40px;border:0;border-radius:12px;outline:none;padding:0 12px;color:#334155;background:#F1F5F9;font-size:13px}.mf-order-row button{width:42px;height:40px;border:0;border-radius:12px;color:#fff;background:linear-gradient(135deg,#006DFF,#00A8FF);display:flex;align-items:center;justify-content:center;cursor:pointer}.mf-order-row svg{width:19px;height:19px}.mf-order-result{min-height:54px;border:1px dashed #CBD5E1;border-radius:14px;padding:14px;color:#64748B;font-size:13px;line-height:1.45;display:flex;align-items:center;justify-content:center;text-align:center}.mf-order-error{border-style:solid;border-color:#FCA5A5;background:#FEF2F2;color:#991B1B}.mf-order-empty{border-style:solid;border-color:#FDE68A;background:#FFFBEB;color:#92400E}.mf-order-success{display:block;text-align:left;padding:0;overflow:hidden;border-style:solid;border-color:#DBEAFE;background:#fff}.mf-order-title{padding:12px 14px;background:linear-gradient(180deg,#EFF6FF,#F8FAFC);border-bottom:1px solid #DBEAFE}.mf-order-title strong{display:block;color:#0F172A;font-size:13px}.mf-order-title span{display:block;margin-top:4px;color:#2563EB;font-size:12px;font-weight:800}.mf-table-scroll{width:100%;overflow-x:auto}.mf-order-table{width:100%;min-width:650px;border-collapse:collapse;font-size:12px;color:#0F172A}.mf-order-table th,.mf-order-table td{padding:10px 11px;border-bottom:1px solid #E5E7EB;text-align:left}@media (max-width:480px){.mf-panel{right:0;bottom:0;width:100vw;height:100dvh;max-height:100dvh;border-radius:0}.mf-fab{right:18px!important;bottom:84px!important}}
    `;
    document.head.appendChild(style);
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

  function safeFocus(selector) {
    const el = $(selector);
    if (el && typeof el.focus === 'function') el.focus({ preventScroll: true });
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
