/* MAXTER widget (2 modos: Productos / Mi Pedido)
   Rediseño visual para coincidir con "bot maxter copia.png" y tarjetas "modulo_prod.jpg".
   Backend por defecto: flashbot-backend en Render.
   Puedes override con <script data-backend="https://tu-backend" ...>
*/
(function(){
  const DEFAULT_BACKEND = "https://flashbot-backend-25b6.onrender.com";
  const BACKEND = (function(){
    try{
      const s = document.currentScript;
      return (s && s.dataset && s.dataset.backend) ? s.dataset.backend : DEFAULT_BACKEND;
    }catch(e){ return DEFAULT_BACKEND; }
  })();
  const TITLE = "MAXTER, Tu Asistente Inteligente";

  // ===== Estado global =====
  const chatState = {
    mode: "products",       // "products" | "orders"
    currentQuery: "",
    currentPage: 1,
    pagination: null,
    isLoading: false
  };

  // ===== FAB =====
  const fab = document.createElement('button');
  fab.className = 'mx-fab';
  fab.setAttribute('aria-label','Abrir chat MAXTER, Tu Asesor de Compras');
  fab.setAttribute('title','MAXTER, Tu Asesor de Compras');
  fab.innerHTML = '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M4 19l1.7-3.4A8 8 0 1112 20H6l-2 1z" stroke="currentColor" stroke-width="1.5"/></svg><span>MAXTER · Productos / Mi Pedido</span>';

  // ===== Panel =====
  const panel = document.createElement('section');
  panel.className = 'mx-panel';
  panel.innerHTML = `
    <div class="mx-head">${TITLE} <small style="margin-left:.5rem; font-weight:600;">Asesor de compras</small></div>

    <div class="mx-switch" role="tablist" aria-label="Cambiar modo">
      <button class="mx-tab mx-tab-active" id="mxTabProducts" role="tab" aria-selected="true">Productos</button>
      <button class="mx-tab" id="mxTabOrders" role="tab" aria-selected="false">Mi Pedido</button>
    </div>

    <div class="mx-body" id="mxBody" aria-live="polite" aria-atomic="false"></div>

    <div id="mxPagination" class="mx-pagination" style="display:none;">
      <div class="mx-pagination-controls" id="mxPaginationControls">
        <button class="mx-pagination-btn" id="mxPrevBtn">‹ Anterior</button>
        <span class="mx-pagination-info" id="mxPaginationInfo">Página 1 de 1</span>
        <button class="mx-pagination-btn" id="mxNextBtn">Siguiente ›</button>
      </div>
    </div>

    <form class="mx-form" id="mxForm">
      <input id="mxInput" type="text" placeholder="Ej. sensor agua tinaco, control Sony, soporte 55 pulgadas" aria-label="Escribe tu búsqueda" required />
      <button type="submit" id="mxSubmitBtn">Enviar</button>
    </form>
  `;

  document.addEventListener('DOMContentLoaded', function(){
    document.body.appendChild(fab);
    document.body.appendChild(panel);
    // Mensaje de bienvenida (burbuja bot)
    appendMsg("¡Hola! Soy Maxter, tu asistente de compras de Master Electronics. ¿Qué producto estás buscando? 🔍","bot");
  });

  fab.addEventListener('click', function(){
    panel.classList.toggle('mx-open');
  });

  // ===== Helpers =====
  function bodyEl(){ return document.getElementById('mxBody'); }

  function clearBody(){
    const body = bodyEl();
    if(body) body.innerHTML = '';
  }

  function appendMsg(text, from){
    const body = bodyEl();
    const div = document.createElement('div');
    div.className = 'mx-msg ' + (from === 'user' ? 'mx-user' : 'mx-bot');
    div.textContent = text;
    body.appendChild(div);
    body.scrollTop = body.scrollHeight;
  }

  function showLoading(text){
    const body = bodyEl();
    const div = document.createElement('div');
    div.className = 'mx-loading';
    div.id = 'mxLoadingMsg';
    div.textContent = text || (chatState.mode === 'orders' ? 'Consultando pedido' : 'Buscando productos');
    body.appendChild(div);
    body.scrollTop = body.scrollHeight;
  }

  function hideLoading(){
    const loading = document.getElementById('mxLoadingMsg');
    if(loading) loading.remove();
  }

  // --- Tarjeta de producto (coincide con "modulo_prod.jpg") ---
  function cardHTML(p){
    let inv = '';
    if (Array.isArray(p.inventory) && p.inventory.length){
      const tot = p.inventory.reduce((s,x)=>s + (Number(x.available)||0), 0);
      const parts = p.inventory.map((x, i) => {
        const name = (x.name || '').trim();
        const qty  = Number(x.available)||0;
        return `${name}: ${qty} disponibles`;
      }).join('<span class="mx-dot">·</span> ');
      inv = `<div class="mx-inv"><b>Inventario:</b> ${parts}</div>`;
      if(tot === 0) inv = `<div class="mx-inv"><b>Inventario:</b> Sin existencias</div>`;
    }
    const buyHref = (p.buy_url || '#');
    const viewHref = (p.product_url || '#');

    return `
      <article class="mx-card">
        <img src="${p.image||''}" alt="">
        <div>
          <h4>${p.title||''}</h4>
          ${p.compare_at_price
            ? `<div class="mx-price"><s style="color:#64748b;font-weight:700;margin-right:.35rem">${p.compare_at_price}</s> ${p.price||''}</div>`
            : `<div class="mx-price">${p.price||''}</div>`}
          <div class="mx-actions">
            <a class="mx-btn mx-buy" href="${buyHref}" target="_blank" rel="noopener noreferrer">Comprar ahora</a>
            <a class="mx-link" href="${viewHref}" target="_blank" rel="noopener noreferrer">Ver producto</a>
          </div>
          ${inv}
        </div>
      </article>`;
  }

  function appendProducts(list, isNewSearch){
    if(!Array.isArray(list) || !list.length) return;
    const body = bodyEl();

    if(isNewSearch){
      const oldList = body.querySelector('.mx-list');
      if(oldList) oldList.remove();
    }

    const wrap = document.createElement('div');
    wrap.className = 'mx-list';
    wrap.id = 'mxProductList';
    list.forEach(p => wrap.insertAdjacentHTML('beforeend', cardHTML(p)));
    body.appendChild(wrap);
    body.scrollTop = body.scrollHeight;
  }

  // --- Paginación (solo productos) ---
  function updatePagination(pagination){
    const paginationDiv  = document.getElementById('mxPagination');
    const paginationInfo = document.getElementById('mxPaginationInfo');
    const prevBtn = document.getElementById('mxPrevBtn');
    const nextBtn = document.getElementById('mxNextBtn');

    if(chatState.mode !== 'products' || !pagination || pagination.total_pages <= 1){
      paginationDiv.style.display = 'none';
      chatState.pagination = null;
      return;
    }

    paginationDiv.style.display = 'flex';
    paginationInfo.textContent = `Página ${pagination.page} de ${pagination.total_pages} (${pagination.total} productos)`;
    prevBtn.disabled = !pagination.has_prev;
    nextBtn.disabled = !pagination.has_next;

    chatState.pagination = pagination;
  }

  // =========================
  //  Render Mi Pedido
  // =========================
  const ORDER_FIELDS = [
    "Plataforma","SKU","Pzas","Precio Unitario","Precio Total","Envio",
    "Fecha Inicio","EN PROCESO","Fecha Termino","Almacen","Paqueteria",
    "Guia","Fecha envió","Fecha Entrega"
  ];

  function renderOrders(orderNo, items, fallbackAnswer){
    const body = bodyEl();

    if(orderNo){
      appendMsg(`Resumen del pedido #${orderNo}`, "bot");
    }

    if(!Array.isArray(items) || !items.length){
      if(fallbackAnswer){
        appendMsg(fallbackAnswer, "bot");
      }else{
        appendMsg("No encontramos información con ese número de pedido. Verifica el número tal como aparece en tu comprobante.", "bot");
      }
      return;
    }

    const wrap = document.createElement('div');
    wrap.className='mx-orders';

    items.forEach((r,i)=>{
      const card=document.createElement('article');
      card.className='mx-order';

      const hd=document.createElement('div');
      hd.className='mx-order-hd';
      hd.innerHTML = `
        <div class="mx-order-title">Artículo ${i+1}</div>
        <span class="mx-chip">${(r["EN PROCESO"]||"").toString().trim() || "—"}</span>
      `;
      card.appendChild(hd);

      const bd=document.createElement('div');
      bd.className='mx-order-body';

      let rowsHTML = ORDER_FIELDS.map(label=>{
        const val = (r[label] ?? "—").toString().trim() || "—";
        const isMoney = (label === "Precio Unitario" || label === "Precio Total");
        const isQty = (label === "Pzas");
        const pretty = (isMoney || isQty) ? `<span class="mx-kpi">${val}</span>` : val;
        return `<tr><th scope="row">${label}</th><td>${pretty}</td></tr>`;
      }).join("");

      bd.innerHTML = `
        <table class="mx-order-table" role="table" aria-label="Detalle de artículo del pedido">
          <tbody>
            ${rowsHTML}
          </tbody>
        </table>
      `;

      card.appendChild(bd);
      wrap.appendChild(card);
    });

    body.appendChild(wrap);
    body.scrollTop = body.scrollHeight;
  }

  // ====== SEARCH: Productos ======
  function performSearch(query, page, isNewSearch){
    if(chatState.isLoading) return;
    chatState.isLoading = true;

    const submitBtn = document.getElementById('mxSubmitBtn');

    submitBtn.disabled = true;
    submitBtn.textContent = 'Buscando...';

    if(isNewSearch){
      showLoading('Buscando productos');
      chatState.currentQuery = query;
      chatState.currentPage = 1;
    } else {
      chatState.currentPage = page;
    }

    fetch(BACKEND + "/api/chat", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({
        message: chatState.currentQuery,
        page: page,
        per_page: 10
      })
    })
    .then(r=>r.json())
    .then(res=>{
      hideLoading();
      chatState.isLoading = false;
      submitBtn.disabled = false;
      submitBtn.textContent = (chatState.mode === 'orders' ? 'Consultar' : 'Enviar');

      if(res && Array.isArray(res.products)){
        const n = res.products.length;

        if(isNewSearch){
          if(res.answer){ appendMsg(res.answer, "bot"); }
          else { appendMsg("Aquí tienes lo que encontré:", "bot"); }
        }

        if(n > 0){
          appendProducts(res.products, isNewSearch);
          updatePagination(res.pagination);
        } else {
          appendMsg("No encontré resultados para tu búsqueda. Prueba con: “sensor agua tinaco”, “divisor hdmi 1×4”, “soporte pared 55\"”, “control Samsung”, “cable RCA audio video”.", "bot");
          updatePagination(null);
        }
      } else {
        if(isNewSearch){
          appendMsg(res?.answer || "No encontré productos que coincidan con tu búsqueda.", "bot");
        }
        updatePagination(null);
      }
    })
    .catch(err=>{
      hideLoading();
      chatState.isLoading = false;
      submitBtn.disabled = false;
      submitBtn.textContent = (chatState.mode === 'orders' ? 'Consultar' : 'Enviar');
      console.error('Error en búsqueda:', err);
      appendMsg("Hubo un problema al buscar productos. Intenta de nuevo.", "bot");
      updatePagination(null);
    });
  }

  // ====== SEARCH: Mi Pedido — endpoint /api/orders ======
  function performOrderLookup(query){
    if(chatState.isLoading) return;
    chatState.isLoading = true;

    const submitBtn = document.getElementById('mxSubmitBtn');
    submitBtn.disabled = true;
    submitBtn.textContent = 'Consultando...';
    showLoading('Consultando pedido');

    fetch(BACKEND + "/api/orders", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ order: query })
    })
    .then(r=>r.json())
    .then(res=>{
      hideLoading();
      chatState.isLoading = false;
      submitBtn.disabled = false;
      submitBtn.textContent = 'Consultar';

      const orderNo = res?.order || query;
      if(Array.isArray(res?.items) && res.items.length){
        renderOrders(orderNo, res.items, res?.answer);
      }else{
        renderOrders(orderNo, [], res?.answer);
      }
      updatePagination(null); // Sin paginación en pedidos
    })
    .catch(err=>{
      hideLoading();
      chatState.isLoading = false;
      submitBtn.disabled = false;
      submitBtn.textContent = 'Consultar';
      console.error('Error en consulta de pedido:', err);
      appendMsg("Hubo un problema al consultar tu pedido. Intenta nuevamente en unos segundos.", "bot");
      updatePagination(null);
    });
  }

  // ====== UI: Cambiar modo ======
  function switchMode(mode){
    if(mode === chatState.mode) return;

    chatState.mode = mode;
    chatState.currentQuery = "";
    chatState.currentPage = 1;
    chatState.pagination = null;

    // Tabs
    const tabP = document.getElementById('mxTabProducts');
    const tabO = document.getElementById('mxTabOrders');
    if(mode === 'products'){
      tabP.classList.add('mx-tab-active'); tabP.setAttribute('aria-selected','true');
      tabO.classList.remove('mx-tab-active'); tabO.setAttribute('aria-selected','false');
    }else{
      tabO.classList.add('mx-tab-active'); tabO.setAttribute('aria-selected','true');
      tabP.classList.remove('mx-tab-active'); tabP.setAttribute('aria-selected','false');
    }

    // Placeholder + botón y mensaje de inicio por modo
    const input = document.getElementById('mxInput');
    const btn = document.getElementById('mxSubmitBtn');

    clearBody();
    if(mode === 'products'){
      input.placeholder = "Ej. sensor agua tinaco, control Sony, soporte 55 pulgadas";
      btn.textContent = "Enviar";
      appendMsg("¿Qué producto estás buscando? 🔍","bot");
      updatePagination(null);
    } else {
      input.placeholder = "Ingresa tu número de pedido (ej. 0000 o #0000)";
      btn.textContent = "Consultar";
      appendMsg("Ingresa aquí tu número de pedido para conocer tu estatus.","bot");
      updatePagination(null);
    }
  }

  // ====== Event Listeners ======
  document.addEventListener('DOMContentLoaded', function(){
    const form = document.getElementById('mxForm');
    const input = document.getElementById('mxInput');
    const prevBtn = document.getElementById('mxPrevBtn');
    const nextBtn = document.getElementById('mxNextBtn');

    const tabP = document.getElementById('mxTabProducts');
    const tabO = document.getElementById('mxTabOrders');
    tabP.addEventListener('click', ()=> switchMode('products'));
    tabO.addEventListener('click', ()=> switchMode('orders'));

    form.addEventListener('submit', function(e){
      e.preventDefault();
      const q = input.value.trim();
      if(!q || chatState.isLoading) return;

      // Burbuja del usuario
      appendMsg(q, "user");

      if(chatState.mode === 'products'){
        performSearch(q, 1, true);
      } else {
        performOrderLookup(q);
      }
      input.value="";
    });

    prevBtn.addEventListener('click', function(){
      if(chatState.mode !== 'products') return;
      if(chatState.pagination && chatState.pagination.has_prev && !chatState.isLoading){
        const prevPage = chatState.currentPage - 1;
        performSearch(chatState.currentQuery, prevPage, false);
      }
    });

    nextBtn.addEventListener('click', function(){
      if(chatState.mode !== 'products') return;
      if(chatState.pagination && chatState.pagination.has_next && !chatState.isLoading){
        const nextPage = chatState.currentPage + 1;
        performSearch(chatState.currentQuery, nextPage, false);
      }
    });

    // Enter para enviar (sin salto de línea)
    input.addEventListener('keypress', function(e){
      if(e.key === 'Enter' && !e.shiftKey){
        e.preventDefault();
        form.dispatchEvent(new Event('submit'));
      }
    });
  });
})();
