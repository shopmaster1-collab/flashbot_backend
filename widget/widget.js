/* MAXTER widget (dos modos: Productos / Mi pedido)
   - Conserva tu flujo actual de Productos (POST /api/chat)
   - Añade flujo de Pedidos (POST /api/orders/status)
   - Puedes override backend con: <script data-backend="https://tu-backend" ...>
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

  // ======= Estado global (Productos) =======
  const chatState = {
    isLoading: false,
    currentQuery: "",
    currentPage: 1,
    pagination: null,  // { has_next, has_prev, total_pages }
    lastResponse: null
  };

  // ======= Helpers =======
  function el(tag, attrs={}, children=[]) {
  const n = document.createElement(tag);

  // Atributos
  Object.entries(attrs).forEach(([k, v]) => {
    if (k === "className") n.className = v;
    else if (k === "html") n.innerHTML = v;
    else n.setAttribute(k, v);
  });

  // Hijos (acepta Node, string, number, arrays)
  const arr = Array.isArray(children) ? children : [children];
  arr
    .filter(c => c !== null && c !== undefined && c !== false)
    .forEach(c => {
      if (c instanceof Node) {
        n.appendChild(c);
      } else {
        n.appendChild(document.createTextNode(String(c)));
      }
    });

  return n;
}

  function appendMsg(container, htmlStr, role="bot"){
    const b = el("div", {className: role === "bot" ? "mx-msg" : "mx-msg user", html: htmlStr});
    container.appendChild(b);
    container.scrollTop = container.scrollHeight;
  }
  function setLoading(form, on){
    chatState.isLoading = !!on;
    const btn = form.querySelector("button[type=submit]");
    if(btn){ btn.disabled = !!on; btn.innerText = on ? "Consultando..." : "Enviar"; }
  }
  function setLoadingOrders(form, on){
    const btn = form.querySelector("button[type=submit]");
    if(btn){ btn.disabled = !!on; btn.innerText = on ? "Consultando..." : "Consultar"; }
  }
  function asSafeHTML(s){ return s; } // backend devuelve markdown->HTML ya saneado en tu flujo

  // ======= Render principal =======
  document.addEventListener("DOMContentLoaded", function(){
    const root = el("div", {className: "mx-root"});

    // Head / Tabs
    const head = el("div", {className: "mx-head"}, [
      document.createTextNode(TITLE),
      el("small", {html: "&nbsp;Asesor de compras"})
    ]);
    const tabs = el("div", {className: "mx-tabs"}, [
      el("button", {className: "mx-tab active", "data-tab":"products"}, "Productos"),
      el("button", {className: "mx-tab", "data-tab":"orders"}, "Mi pedido"),
    ]);

    // ======= Vista Productos (lo existente) =======
    const bodyProducts = el("div", {className:"mx-body", id:"mxBody"}, [
      el("div", {className:"mx-msg", html:"¡Hola! Soy Maxter, tu asistente de compras de Master Electronics. ¿Qué producto estás buscando? 🔍"})
    ]);
    const pagi = el("div", {className:"mx-pagination", id:"mxPagination", style:"display:none;"}, [
      el("div", {className:"mx-pagination-controls", id:"mxPaginationControls"}, [
        el("button", {className:"mx-pagination-btn", id:"mxPrevBtn"}, "‹ Anterior"),
        el("span", {className:"mx-pagination-info", id:"mxPaginationInfo"}, "Página 1 de 1"),
        el("button", {className:"mx-pagination-btn", id:"mxNextBtn"}, "Siguiente ›"),
      ])
    ]);
    const formProducts = el("form", {className:"mx-form", id:"mxForm"}, [
      el("textarea", {id:"mxInput", rows:"1", placeholder:"Escribe tu búsqueda (ej. 'soporte de monitor 22 pulgadas')"}),
      el("button", {type:"submit"}, "Enviar")
    ]);
    const panelProducts = el("div", {className:"mx-panel", id:"mxPanelProducts"}, [
      head, tabs, bodyProducts, pagi, formProducts
    ]);

    // ======= Vista Pedidos (nueva) =======
    const bodyOrders = el("div", {className:"mx-body", id:"mxOrderBody"}, [
      el("div", {className:"mx-msg", html:"Consulta el <b>estatus</b> de tu compra. Ingresa tu número de orden (4–15 dígitos)."})
    ]);
    const formOrders = el("form", {className:"mx-form", id:"mxOrderForm"}, [
      el("input", {id:"mxOrderInput", type:"text", placeholder:"Ejemplo: 12345678 o #12345678"}),
      el("button", {type:"submit"}, "Consultar")
    ]);
    const panelOrders = el("div", {className:"mx-panel", id:"mxPanelOrders", style:"display:none;"}, [
      head.cloneNode(true), tabs.cloneNode(true), bodyOrders, formOrders
    ]);

    // Contenedor raíz
    root.appendChild(panelProducts);
    root.appendChild(panelOrders);
    document.body.appendChild(root);

    // ======= Lógica de Tabs =======
    function switchTab(name){
      const isProducts = name === "products";
      panelProducts.style.display = isProducts ? "block" : "none";
      panelOrders.style.display   = isProducts ? "none"  : "block";
      // Marca activa
      [...document.querySelectorAll(".mx-tab")].forEach(b=>{
        b.classList.toggle("active", b.dataset.tab === name);
      });
    }
    document.addEventListener("click", function(ev){
      const b = ev.target.closest(".mx-tab");
      if(!b) return;
      switchTab(b.dataset.tab);
    });

    // ======= Flujo Productos (conservado) =======
    const input = formProducts.querySelector("#mxInput");
    const mxPrevBtn = pagi.querySelector("#mxPrevBtn");
    const mxNextBtn = pagi.querySelector("#mxNextBtn");
    const mxPaginationInfo = pagi.querySelector("#mxPaginationInfo");

    function updatePaginationUI(){
      if(!chatState.pagination){ pagi.style.display = "none"; return; }
      const p = chatState.pagination;
      pagi.style.display = "block";
      mxPrevBtn.disabled = !p.has_prev || chatState.isLoading;
      mxNextBtn.disabled = !p.has_next || chatState.isLoading;
      mxPaginationInfo.textContent = `Página ${chatState.currentPage} de ${p.total_pages}`;
    }

    function paintProductsAnswer(ans){
      // ans: { answer_html, cards_html, pagination }
      if(ans.answer_html) appendMsg(bodyProducts, ans.answer_html, "bot");
      if(ans.cards_html)  appendMsg(bodyProducts, ans.cards_html, "bot");
      chatState.pagination = ans.pagination || null;
      updatePaginationUI();
    }

    async function performSearch(q, page=1, fromUser=true){
      if(!q || !q.trim()) return;
      if(fromUser) appendMsg(bodyProducts, htmlEscape(q), "user");
      setLoading(formProducts, true);
      try{
        const resp = await fetch(`${BACKEND}/api/chat`, {
          method: "POST",
          headers: {"Content-Type":"application/json"},
          body: JSON.stringify({ query: q, page })
        });
        const data = await resp.json();
        if(!resp.ok){ appendMsg(bodyProducts, "Hubo un error realizando la búsqueda.", "bot"); return; }
        chatState.currentQuery = q; chatState.currentPage = page;
        paintProductsAnswer(data);
      }catch(e){
        appendMsg(bodyProducts, "Error de red consultando el backend.", "bot");
      }finally{
        setLoading(formProducts, false);
      }
    }

    function htmlEscape(s){
      return s.replace(/[&<>"']/g, (m)=>({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[m]));
    }

    formProducts.addEventListener("submit", function(e){
      e.preventDefault();
      performSearch(input.value, 1, true);
      input.value = "";
    });

    mxPrevBtn.addEventListener("click", function(){
      if(chatState.pagination && chatState.pagination.has_prev && !chatState.isLoading){
        performSearch(chatState.currentQuery, chatState.currentPage - 1, false);
      }
    });
    mxNextBtn.addEventListener("click", function(){
      if(chatState.pagination && chatState.pagination.has_next && !chatState.isLoading){
        performSearch(chatState.currentQuery, chatState.currentPage + 1, false);
      }
    });

    input.addEventListener("keypress", function(e){
      if(e.key === 'Enter' && !e.shiftKey){
        e.preventDefault();
        formProducts.dispatchEvent(new Event('submit'));
      }
    });

    // ======= Flujo Pedidos (nuevo) =======
    const orderInput = formOrders.querySelector("#mxOrderInput");

    function paintOrderAnswer(md){
      // Render simple: tu backend devuelve markdown; aquí lo inyectamos como texto preformateado simple.
      // Si lo prefieres con algún mini markdown renderer del lado del widget, lo integramos luego.
      appendMsg(bodyOrders, md, "bot");
    }

    async function performOrderLookup(orderRaw){
      if(!orderRaw || !orderRaw.trim()) return;
      appendMsg(bodyOrders, `Consultar pedido: ${htmlEscape(orderRaw)}`, "user");
      setLoadingOrders(formOrders, true);
      try{
        const resp = await fetch(`${BACKEND}/api/orders/status`, {
          method: "POST",
          headers: {"Content-Type":"application/json"},
          body: JSON.stringify({ order_no: orderRaw })
        });
        const data = await resp.json();
        if(!resp.ok || !data.ok){
          paintOrderAnswer("No fue posible consultar el estatus en este momento.");
          return;
        }
        paintOrderAnswer(data.answer || "Sin datos.");
      }catch(e){
        paintOrderAnswer("Error de red consultando el estatus.");
      }finally{
        setLoadingOrders(formOrders, false);
      }
    }

    formOrders.addEventListener("submit", function(e){
      e.preventDefault();
      performOrderLookup(orderInput.value);
      orderInput.value = "";
    });
  });
})();
