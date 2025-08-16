(function () {
  if (window.__MAXTER_WIDGET_LOADED__) { return; }
  window.__MAXTER_WIDGET_LOADED__ = true;

  const API_URL = "https://flashbot-backend-25b6.onrender.com/chat";
  const ORIGIN = window.location.origin;

  function init() {
    if (document.getElementById("chatbot-bubble") || document.getElementById("chatbot-window")) return;

    // —— Estilos base del widget ——
    const style = document.createElement("style");
    style.id = "maxter-widget-style";
    style.textContent = `
      #chatbot-bubble {
        position: fixed;
        left: 18px;
        bottom: 18px;
        z-index: 2147483646;
        display: inline-flex;
        align-items: center;
        gap: 8px;
        background: #0d6efd;
        color: #fff;
        border-radius: 999px;
        padding: 10px 14px;
        cursor: pointer;
        box-shadow: 0 10px 30px rgba(0,0,0,.25);
        font-family: system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;
      }
      #chatbot-bubble img{width:26px;height:26px;border-radius:50%}
      #chatbot-bubble span{font-weight:600}

      #chatbot-window {
        position: fixed;
        left: 18px;
        bottom: 78px;
        width: 380px;
        max-height: 70vh;
        background: #fff;
        color: #111827;
        border: 1px solid #e5e7eb;
        border-radius: 12px;
        display: none;
        flex-direction: column;
        overflow: hidden;
        z-index: 2147483647;
        box-shadow: 0 20px 40px rgba(0,0,0,.25);
        font-family: system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;
      }
      #chatbot-header{display:flex;align-items:center;gap:10px;padding:10px 12px;background:#0d6efd;color:#fff}
      #chatbot-header img{width:28px;height:28px;border-radius:50%}
      #chatbot-title{font-weight:700;font-size:14px;flex:1}
      #chatbot-close{cursor:pointer;opacity:.85}
      #chatbot-messages{padding:10px;overflow-y:auto;background:#f7f7f9;display:flex;flex-direction:column;gap:8px}

      .msg{display:inline-block;max-width:85%;padding:8px 10px;border-radius:10px;line-height:1.25;font-size:13px;word-break:break-word}
      .msg.user{align-self:flex-end;background:#e6f0ff}
      .msg.bot{align-self:flex-start;background:#fff;border:1px solid #e5e7eb}
      .msg.bot .rich{font-size:13px}

      #chatbot-input{display:flex;gap:8px;align-items:center;padding:10px;border-top:1px solid #e9ecef;background:#fff}
      #chatbot-input input{flex:1;padding:8px 10px;border-radius:8px;border:1px solid #cfd4da;outline:none;font-size:14px}
      #chatbot-input button{background:#198754;color:#fff;border:none;border-radius:8px;padding:8px 12px;cursor:pointer;font-weight:600}

      .loading{display:inline-flex;align-items:center;gap:6px;color:#6c757d;font-size:12px}
      .loading .dot{width:6px;height:6px;border-radius:50%;background:#6c757d;animation:blink 1.2s infinite}
      .loading .dot:nth-child(2){animation-delay:.2s}
      .loading .dot:nth-child(3){animation-delay:.4s}
      @keyframes blink{0%,80%,100%{opacity:.2}40%{opacity:1}}

      @media (max-width: 480px){
        #chatbot-window{left:10px;width:92vw;max-height:70vh}
        #chatbot-bubble{left:10px}
      }
    `;
    document.head.appendChild(style);

    // —— Sobrescrituras de layout para tarjetas de producto ——
    (function injectLayoutOverrides(){
      if (document.getElementById("maxter-overrides-20250816")) return;
      const s = document.createElement("style");
      s.id = "maxter-overrides-20250816";
      s.textContent = `
        /* 1) El mensaje del bot ocupa todo el ancho del chat cuando trae contenido rico */
        .msg.bot{ max-width: 100% !important; width: 100% !important; }
        .msg.bot .rich{ max-width: 100% !important; width: 100% !important; }

        /* 2) Tarjeta de producto a ancho completo (independiente del HTML del backend) */
        .msg.bot .rich > div[style*="border-bottom"]{
          /* El backend actual envía cada producto como un <div> con border-bottom */
          display: grid !important;
          grid-template-columns: 90px 1fr !important;
          gap: 12px !important;
          align-items: start !important;
          width: 100% !important;

          border: 1px solid #e5e7eb !important;
          border-radius: 12px !important;
          background: #fff !important;
          padding: 10px !important;
          margin: 10px 0 12px 0 !important;
        }
        .msg.bot .rich > div[style*="border-bottom"] img{
          width: 90px !important;
          height: 90px !important;
          object-fit: contain !important;
          border-radius: 8px !important;
          margin-right: 0 !important;
        }
        .msg.bot .rich > div[style*="border-bottom"] > div[style*="flex:1"]{
          grid-column: 2 / -1 !important;
          width: 100% !important;
        }

        /* 3) Título en bloque completo */
        .msg.bot .rich > div[style*="border-bottom"] > div[style*='flex:1'] > div:first-child{
          font-weight: 700 !important;
          line-height: 1.25 !important;
          margin: 0 0 6px 0 !important;
          white-space: normal !important;
        }

        /* 4) Fila exclusiva para PRECIO + BOTÓN "Comprar ahora" */
        .msg.bot .rich > div[style*="border-bottom"] > div[style*='flex:1'] > div:nth-child(2){
          display: flex !important;
          align-items: center !important;
          justify-content: flex-start !important;
          gap: 12px !important;
          margin: 6px 0 6px 0 !important;
          flex-wrap: nowrap !important; /* botón en la misma línea */
          width: 100% !important;
        }
        /* Precio destacado */
        .msg.bot .rich > div[style*="border-bottom"] > div[style*='flex:1'] > div:nth-child(2) div{
          color: #0d6efd !important;
          font-weight: 800 !important;
          font-size: 16px !important;
          letter-spacing: .2px !important;
        }
        /* Botón comprar ahora ancho (texto completo en una línea) */
        .msg.bot .rich > div[style*="border-bottom"] > div[style*='flex:1'] > div:nth-child(2) a{
          display: inline-flex !important;
          align-items: center !important;
          gap: 6px !important;
          background: #198754 !important;
          color: #fff !important;
          border-radius: 8px !important;
          padding: 8px 12px !important;
          text-decoration: none !important;
          font-weight: 700 !important;
          white-space: nowrap !important; /* evitar salto dentro del botón */
        }

        /* 5) Debajo: enlaces "Ver producto" y "Manual de producto" con texto completo */
        .msg.bot .rich > div[style*="border-bottom"] > div[style*='flex:1'] > div:nth-child(3){
          display: grid !important;
          grid-auto-flow: row !important;
          gap: 6px !important;
          margin-top: 6px !important;
          width: 100% !important;
        }
        .msg.bot .rich > div[style*="border-bottom"] > div[style*='flex:1'] > div:nth-child(3) a{
          color: #0d6efd !important;
          text-decoration: underline !important;
          font-size: 13px !important;
          white-space: normal !important;      /* NO cortar el texto */
          overflow-wrap: anywhere !important;   /* Romper si es muy largo */
          display: inline !important;
        }

        /* 6) Inmediatamente debajo: Inventario por tienda con mejor legibilidad */
        .msg.bot .rich > div[style*="border-bottom"] ul{
          margin: 6px 0 0 18px !important;
          padding: 0 !important;
        }
        .msg.bot .rich > div[style*="border-bottom"] li{
          list-style: disc !important;
          margin: 2px 0 !important;
          white-space: normal !important;
        }
      `;
      document.head.appendChild(s);
    })();

    // —— Burbuja ——
    const bubble = document.createElement("div");
    bubble.id = "chatbot-bubble";
    bubble.innerHTML = `
      <img src="https://flashbot-backend-25b6.onrender.com/static/img/img_m.png" alt="Maxter">
      <span>Maxter te ayuda</span>
    `;
    document.body.appendChild(bubble);

    // —— Ventana ——
    const win = document.createElement("div");
    win.id = "chatbot-window";
    win.innerHTML = `
      <div id="chatbot-header">
        <img src="https://flashbot-backend-25b6.onrender.com/static/img/img_m.png" alt="Logo">
        <div id="chatbot-title">MAXTER, Tu Asistente Inteligente</div>
        <div id="chatbot-close">✖️</div>
      </div>
      <div id="chatbot-messages"></div>
      <div id="chatbot-input">
        <input id="chatbot-text" placeholder="Escribe tu mensaje...">
        <button id="chatbot-send">Enviar</button>
      </div>
    `;
    document.body.appendChild(win);

    // Refs
    const messages = document.getElementById("chatbot-messages");
    const input = document.getElementById("chatbot-text");
    const sendBtn = document.getElementById("chatbot-send");
    const closeBtn = document.getElementById("chatbot-close");

    let greeted = false;

    function appendMessage(text, who="bot", isHtml=false){
      const div = document.createElement("div");
      div.className = `msg ${who}`;
      if(isHtml){
        const wrapper = document.createElement("div");
        wrapper.className = "rich";
        wrapper.innerHTML = text;
        div.appendChild(wrapper);
      }else{
        div.textContent = text;
      }
      messages.appendChild(div);
      messages.scrollTop = messages.scrollHeight;
    }

    let loadingEl = null;
    function showLoading(){
      hideLoading();
      loadingEl = document.createElement("div");
      loadingEl.className = "msg bot";
      loadingEl.innerHTML = `<span class="loading"><span class="dot"></span><span class="dot"></span><span class="dot"></span> Buscando…</span>`;
      messages.appendChild(loadingEl);
      messages.scrollTop = messages.scrollHeight;
    }
    function hideLoading(){
      if(loadingEl && loadingEl.parentNode){ loadingEl.parentNode.removeChild(loadingEl); }
      loadingEl = null;
    }

    function openChat(){
      win.style.display = "flex";
      if(!greeted){
        appendMessage("¡Hola! Soy Maxter y estoy para ayudarte.", "bot");
        greeted = true;
      }
    }
    function closeChat(){ win.style.display = "none"; }

    bubble.addEventListener("click", openChat);
    closeBtn.addEventListener("click", closeChat);

    async function sendMessage(){
      const text = (input.value || "").trim();
      if(!text){ return; }
      appendMessage(text, "user");
      input.value = "";

      showLoading();
      try{
        const res = await fetch(API_URL, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message: text, origin: ORIGIN })
        });
        const data = await res.json();
        hideLoading();
        if(data && data.success){
          appendMessage(data.response || "No tengo respuesta.", "bot", true);
        }else{
          appendMessage("Ups, no pude procesar tu solicitud.", "bot");
        }
      }catch(e){
        hideLoading();
        appendMessage("Ocurrió un error de red. Inténtalo de nuevo.", "bot");
      }
    }

    sendBtn.addEventListener("click", sendMessage);
    input.addEventListener("keydown", (ev) => { if(ev.key === "Enter") sendMessage(); });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
