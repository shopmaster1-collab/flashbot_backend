/* MAXTER widget (dos modos: Productos / Mi pedido)
   - Panel se muestra oculto y aparece mediante un botón flotante (launcher)
   - Posicionado en el extremo izquierdo para no chocar con WhatsApp
   - Flujo Productos (POST `${BACKEND}/api/chat`) + Pedidos (POST `${BACKEND}/api/orders/status`)
   - Puedes override backend con: <script src=".../widget.js" data-backend="https://tu-backend">
*/
(function(){
  const DEFAULT_BACKEND = "https://flashbot-backend-25b6.onrender.com";

  function detectBackend(){
    try{
      const s = document.currentScript;
      if (s && s.dataset && s.dataset.backend) return s.dataset.backend;
      const scripts = document.getElementsByTagName('script');
      for (let i=0;i<scripts.length;i++){
        const el = scripts[i];
        if (el.dataset && el.dataset.backend) return el.dataset.backend;
        const src = el.getAttribute('src') || '';
        if (src.indexOf('widget.js') !== -1 && el.dataset && el.dataset.backend) return el.dataset.backend;
      }
    }catch(e){}
    return DEFAULT_BACKEND;
  }
  const BACKEND = detectBackend();
  const TITLE = "MAXTER, Tu Asistente Inteligente";

  // ======= Estado global =======
  const state = {
    isOpen: false,
  };
  const chatState = {
    isLoading: false,
    currentQuery: "",
    currentPage: 1,
    pagination: null,  // { has_next, has_prev, total_pages }
    lastResponse: null
  };

  // ======= Helpers =======
  function isNode(x){
    return x && typeof x === 'object' && typeof x.nodeType === 'number' && typeof x.nodeName === 'string';
  }
  function el(tag, attrs={}, children=[]){
    const n = document.createElement(tag);
    for (const [k,v] of Object.entries(attrs || {})){
      if (k === "className") n.className = v;
      else if (k === "html") n.innerHTML = v;
      else if (k === "style") n.setAttribute("style", v);
      else n.setAttribute(k, v);
    }
    const arr = Array.isArray(children) ? children : [children];
    arr
      .filter(c => c !== null && c !== undefined && c !== false)
      .forEach(c => {
        if (isNode(c)) n.appendChild(c);
        else n.appendChild(document.createTextNode(String(c)));
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
  function htmlEscape(s){
    return String(s).replace(/[&<>"']/g, (m)=>({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[m]));
  }

  // ======= App =======
  function initWidget(){
    // Evitar doble-montaje
    if (document.querySelector(".mx-root")) return;

    // --- Contenedor raíz a la izquierda ---
    const root = el("div", {className: "mx-root mx-left"});

    // --- Botón flotante (launcher) ---
    const launcher = el("button", {className: "mx-launcher", title: "Chatea con Maxter"}, [
      el("span", {className:"mx-launcher-icon", html:"🤖"}),
      el("span", {className:"mx-launcher-label"}, "Maxter")
    ]);

    // --- Panel principal (inicia oculto) ---
    const btnClose = el("button", {className:"mx-close", title:"Cerrar"}, "×");
    const head = el("div", {className: "mx-head"}, [
      el("div", {className:"mx-title"}, [
        document.createTextNode(TITLE),
        el("small", {html: "&nbsp;Asesor de compras"})
      ]),
      btnClose
    ]);
    const tabs = el("div", {className: "mx-tabs"}, [
      el("button", {className: "mx-tab active", "data-tab":"products"}, "Productos"),
      el("button", {className: "mx-tab", "data-tab":"orders"}, "Mi pedido"),
    ]);

    // ======= Vista Productos =======
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

    // ======= Vista Pedidos =======
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

    // --- Contenedor del panel (para poder ocultar/mostrar) ---
    const shell = el("div", {className:"mx-shell", style:"display:none;"}, [
      panelProducts, panelOrders
    ]);

    // --- Montaje en DOM ---
    root.appendChild(shell);
    root.appendChild(launcher);
    document.body.appendChild(root);

    // ======= Abrir / Cerrar =======
    function openPanel(){
      state.isOpen = true;
      shell.style.display = "block";
      launcher.style.display = "none";
      // foco en el input de productos
      const tx = shell.querySelector("#mxInput");
      if (tx) setTimeout(()=>tx.focus(), 50);
    }
    function closePanel(){
      state.isOpen = false;
      shell.style.display = "none";
      launcher.style.display = "inline-flex";
    }
    launcher.addEventListener("click", openPanel);
    document.addEventListener("keydown", function(ev){
      if (ev.key === "Escape" && state.isOpen) closePanel();
    });
    // Cierre con la X (ojo: hay dos heads; el primero tiene el botón real)
    btnClose.addEventListener("click", closePanel);

    // ======= Tabs =======
    function switchTab(name){
      const isProducts = name === "products";
      panelProducts.style.display = isProducts ? "block" : "none";
      panelOrders.style.display   = isProducts ? "none"  : "block";
      document.querySelectorAll(".mx-tab").forEach(b=>{
        b.classList.toggle("active", b.dataset.tab === name);
      });
    }
    document.addEventListener("click", function(ev){
      const b = ev.target.closest(".mx-tab");
      if(!b) return;
      switchTab(b.dataset.tab);
    });

    // ======= Flujo Productos =======
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
      if (ans.answer_html)      appendMsg(bodyProducts, ans.answer_html, "bot");
      else if (ans.answer)      appendMsg(bodyProducts, htmlEscape(ans.answer), "bot");
      if (ans.cards_html)       appendMsg(bodyProducts, ans.cards_html, "bot");
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
        if(!resp.ok){
          appendMsg(bodyProducts, "Hubo un error realizando la búsqueda.", "bot");
          return;
        }
        chatState.currentQuery = q; chatState.currentPage = page;
        paintProductsAnswer(data);
      }catch(e){
        appendMsg(bodyProducts, "Error de red consultando el backend.", "bot");
      }finally{
        setLoading(formProducts, false);
      }
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

    // ======= Flujo Pedidos =======
    const orderInput = formOrders.querySelector("#mxOrderInput");

    function paintOrderAnswer(md){
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
  }

  // Montaje seguro
  if (document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', initWidget, { once: true });
  } else {
    initWidget();
  }
})();
