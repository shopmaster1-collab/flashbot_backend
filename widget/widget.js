/* MAXTER widget (1 columna + mensaje corto + paginación)
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

  // Estado global del chat
  const chatState = {
    currentQuery: "",
    currentPage: 1,
    pagination: null,
    isLoading: false
  };

  // ====== FAB ======
  const fab = document.createElement('button');
  fab.className = 'mx-fab';
  fab.setAttribute('aria-label','Abrir chat MAXTER, Tu Asesor de Compras');
  fab.setAttribute('title','MAXTER, Tu Asesor de Compras');
  fab.innerHTML = '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M4 19l1.7-3.4A8 8 0 1112 20H6l-2 1z" stroke="currentColor" stroke-width="1.5"/></svg><span>MAXTER, Tu Asesor de Compras</span>';

  // ====== Panel ======
  const panel = document.createElement('section');
  panel.className = 'mx-panel';
  panel.innerHTML = `
    <div class="mx-head">${TITLE} <small style="margin-left:.5rem; font-weight:500;">Asesor de compras</small></div>
    <div class="mx-body" id="mxBody">
      <div class="mx-msg">¡Hola! Soy Maxter, tu asistente de compras de Master Electronics. ¿Qué producto estás buscando? 🔍</div>
    </div>
    <div id="mxPagination" class="mx-pagination" style="display:none;">
      <div class="mx-pagination-controls" id="mxPaginationControls">
        <button class="mx-pagination-btn" id="mxPrevBtn">‹ Anterior</button>
        <span class="mx-pagination-info" id="mxPaginationInfo">Página 1 de 1</span>
        <button class="mx-pagination-btn" id="mxNextBtn">Siguiente ›</button>
      </div>
    </div>
    <form class="mx-form" id="mxForm">
      <input id="mxInput" type="text" placeholder="Ej. sensor agua tinaco, control Sony, soporte 55 pulgadas" required />
      <button type="submit" id="mxSubmitBtn">Enviar</button>
    </form>
  `;

  document.addEventListener('DOMContentLoaded', function(){
    document.body.appendChild(fab);
    document.body.appendChild(panel);
  });

  fab.addEventListener('click', function(){
    panel.classList.toggle('mx-open');
  });

  // ====== Helpers ======
  function money(val){
    try{
      if(val===null||val===undefined) return null;
      const n = Number(val);
      return n.toLocaleString('es-MX',{style:'currency', currency:'MXN'});
    }catch(e){ return val; }
  }

  function cardHTML(p){
    let inv = '';
    if (Array.isArray(p.inventory) && p.inventory.length){
      const tot = p.inventory.reduce((s,x)=>s + (Number(x.available)||0), 0);
      const branches = p.inventory.map(x=>`${x.name}: ${x.available}`).join(' · ');
      inv = `<div class="mx-inv">Existencias (${tot}): ${branches}</div>`;
    }
    return `
      <article class="mx-card">
        <img src="${p.image||''}" alt="">
        <div>
          <h4>${p.title||''}</h4>
          ${p.compare_at_price
            ? `<div class="mx-price"><s style="color:#64748b;font-weight:600;margin-right:.35rem">${p.compare_at_price}</s> ${p.price||''}</div>`
            : `<div class="mx-price">${p.price||''}</div>`}
          <div class="mx-actions">
            <a class="mx-btn mx-buy" href="${p.buy_url||'#'}">Comprar ahora</a>
            <a class="mx-link" href="${p.product_url||'#'}">Ver producto</a>
          </div>
          ${inv}
        </div>
      </article>`;
  }

  function appendMsg(text){
    const body=document.getElementById('mxBody');
    const div=document.createElement('div'); div.className='mx-msg'; div.textContent=text; body.appendChild(div);
    body.scrollTop = body.scrollHeight;
  }

  function showLoading(){
    const body=document.getElementById('mxBody');
    const div=document.createElement('div'); div.className='mx-loading'; div.id='mxLoadingMsg'; div.textContent='Buscando productos'; body.appendChild(div);
    body.scrollTop = body.scrollHeight;
  }

  function hideLoading(){
    const loading = document.getElementById('mxLoadingMsg');
    if(loading) loading.remove();
  }

  function appendProducts(list, isNewSearch){
    if(!Array.isArray(list) || !list.length) return;
    const body=document.getElementById('mxBody');

    if(isNewSearch){
      const oldList = body.querySelector('.mx-list');
      if(oldList) oldList.remove();
    }

    const wrap=document.createElement('div'); wrap.className='mx-list'; wrap.id='mxProductList';
    list.forEach(p=> wrap.insertAdjacentHTML('beforeend', cardHTML(p)));
    body.appendChild(wrap);
    body.scrollTop = body.scrollHeight;
  }

  function updatePagination(pagination){
    const paginationDiv = document.getElementById('mxPagination');
    const paginationInfo = document.getElementById('mxPaginationInfo');
    const prevBtn = document.getElementById('mxPrevBtn');
    const nextBtn = document.getElementById('mxNextBtn');

    if(!pagination || pagination.total_pages <= 1){
      paginationDiv.style.display = 'none';
      return;
    }

    paginationDiv.style.display = 'flex';
    paginationInfo.textContent = `Página ${pagination.page} de ${pagination.total_pages} (${pagination.total} productos)`;

    prevBtn.disabled = !pagination.has_prev;
    nextBtn.disabled = !pagination.has_next;

    chatState.pagination = pagination;
  }

  function performSearch(query, page, isNewSearch){
    if(chatState.isLoading) return;

    chatState.isLoading = true;
    const submitBtn = document.getElementById('mxSubmitBtn');
    const input = document.getElementById('mxInput');

    submitBtn.disabled = true;
    submitBtn.textContent = 'Buscando...';

    if(isNewSearch){
      showLoading();
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
      submitBtn.textContent = 'Enviar';

      if(res && Array.isArray(res.products)){
        const n = res.products.length;

        if(n > 0){
          if(isNewSearch && res.answer){ appendMsg(res.answer); }
          appendProducts(res.products, isNewSearch);
          updatePagination(res.pagination);
        } else {
          if(isNewSearch){
            appendMsg("No encontré resultados para tu búsqueda. Intenta con palabras clave más específicas como 'sensor agua tinaco', 'divisor hdmi 1×4', 'soporte pared 55\"', 'control Samsung' o 'cable RCA audio video'.");
          }
          updatePagination(null);
        }
      } else {
        if(isNewSearch){
          appendMsg(res?.answer || "No encontré productos que coincidan con tu búsqueda.");
        }
        updatePagination(null);
      }
    })
    .catch(err=>{
      hideLoading();
      chatState.isLoading = false;
      submitBtn.disabled = false;
      submitBtn.textContent = 'Enviar';
      console.error('Error en búsqueda:', err);
      appendMsg("Hubo un problema al buscar productos. Intenta de nuevo.");
      updatePagination(null);
    });
  }

  // ====== Event Listeners ======
  document.addEventListener('DOMContentLoaded', function(){
    const form = document.getElementById('mxForm');
    const input = document.getElementById('mxInput');
    const prevBtn = document.getElementById('mxPrevBtn');
    const nextBtn = document.getElementById('mxNextBtn');

    form.addEventListener('submit', function(e){
      e.preventDefault();
      const q = input.value.trim();
      if(!q || chatState.isLoading) return;
      performSearch(q, 1, true);
      input.value="";
    });

    prevBtn.addEventListener('click', function(){
      if(chatState.pagination && chatState.pagination.has_prev && !chatState.isLoading){
        const prevPage = chatState.currentPage - 1;
        performSearch(chatState.currentQuery, prevPage, false);
      }
    });

    nextBtn.addEventListener('click', function(){
      if(chatState.pagination && chatState.pagination.has_next && !chatState.isLoading){
        const nextPage = chatState.currentPage + 1;
        performSearch(chatState.currentQuery, nextPage, false);
      }
    });

    input.addEventListener('keypress', function(e){
      if(e.key === 'Enter' && !e.shiftKey){
        e.preventDefault();
        form.dispatchEvent(new Event('submit'));
      }
    });
  });
})();
