(function () {
  // --- Evita doble inyección ---
  if (window.__MAXTER_WIDGET_LOADED__) {
    console.info("[Maxter] widget ya estaba cargado, omito segunda inicialización.");
    return;
  }
  window.__MAXTER_WIDGET_LOADED__ = true;

  const API_URL = "https://flashbot-backend-25b6.onrender.com/chat";
  const ORIGIN = window.location.origin;

  function init() {
    try {
      if (document.getElementById("chatbot-bubble") || document.getElementById("chatbot-window")) {
        console.info("[Maxter] UI ya existe, retorno.");
        return;
      }

      // ---- Estilos básicos ----
      const style = document.createElement("style");
      style.id = "maxter-widget-style";
      style.textContent = `
        #chatbot-bubble {
          position: fixed;
          bottom: 20px;
          right: 20px;
          background-color: #007bff;
          color: white;
          padding: 10px 14px;
          border-radius: 24px;
          display: flex;
          align-items: center;
          gap: 8px;
          font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
          font-weight: 600;
          cursor: pointer;
          z-index: 999999;
          box-shadow: 0 8px 20px rgba(0,0,0,.2);
        }
        #chatbot-bubble img { width: 28px; height: 28px; border-radius: 50%; }
        #chatbot-window {
          position: fixed;
          bottom: 88px;
          right: 20px;
          width: 360px;
          max-height: 70vh;
          border-radius: 12px;
          background: #fff;
          box-shadow: 0 10px 30px rgba(0,0,0,.25);
          display: none;
          flex-direction: column;
          overflow: hidden;
          z-index: 999999;
          font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
        }
        #chatbot-header {
          display: flex;
          align-items: center;
          gap: 10px;
          padding: 10px 12px;
          background: #0d6efd;
          color: #fff;
        }
        #chatbot-header img { width: 28px; height: 28px; border-radius: 50%; }
        #chatbot-title { font-weight: 700; font-size: 14px; flex: 1; }
        #chatbot-close { cursor: pointer; opacity: .85; }
        #chatbot-messages {
          padding: 10px;
          overflow-y: auto;
          background: #f7f7f9;
          display: flex;
          flex-direction: column;
          gap: 8px;
        }
        .msg {
          display: inline-block;
          max-width: 85%;
          padding: 8px 10px;
          border-radius: 10px;
          line-height: 1.25;
          font-size: 13px;
          word-break: break-word;
        }
        .msg.user { align-self: flex-end; background: #e6f0ff; }
        .msg.bot  { align-self: flex-start; background: #ffffff; border: 1px solid #e5e7eb; }
        .msg.bot .rich { font-size: 13px; }
        #chatbot-input {
          display: flex;
          gap: 8px;
          align-items: center;
          padding: 10px;
          border-top: 1px solid #e9ecef;
          background: #fff;
        }
        #chatbot-input input {
          flex: 1;
          padding: 8px 10px;
          border-radius: 8px;
          border: 1px solid #cfd4da;
          outline: none;
          font-size: 14px;
        }
        #chatbot-input button {
          background: #198754;
          color: #fff;
          border: 0;
          border-radius: 8px;
          padding: 8px 12px;
          cursor: pointer;
          font-weight: 600;
        }
        .loading {
          display: inline-flex;
          align-items: center;
          gap: 6px;
          color: #6c757d;
          font-size: 12px;
        }
        .loading .dot {
          width: 6px; height: 6px; border-radius: 50%; background: #6c757d;
          animation: blink 1.2s infinite;
        }
        .loading .dot:nth-child(2){ animation-delay: .2s; }
        .loading .dot:nth-child(3){ animation-delay: .4s; }
        @keyframes blink { 0%, 80%, 100% { opacity: .2 } 40% { opacity: 1 } }
      `;
      document.head.appendChild(style);

      // ---- Bubble ----
      const bubble = document.createElement("div");
      bubble.id = "chatbot-bubble";
      bubble.innerHTML = `
        <img src="https://flashbot-backend-25b6.onrender.com/static/img/img_m.png" alt="Maxter">
        <span>Maxter te ayuda</span>
      `;
      document.body.appendChild(bubble);

      // ---- Window ----
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
          <input type="text" placeholder="Escribe tu mensaje..." />
          <button>Enviar</button>
        </div>
      `;
      document.body.appendChild(win);

      const messages = win.querySelector("#chatbot-messages");
      const input = win.querySelector("#chatbot-input input");
      const sendBtn = win.querySelector("#chatbot-input button");
      const closeBtn = win.querySelector("#chatbot-close");

      let greeted = false;

      function appendMessage(text, who = "bot", isHtml = false) {
        const div = document.createElement("div");
        div.className = `msg ${who}`;
        if (isHtml) {
          const wrapper = document.createElement("div");
          wrapper.className = "rich";
          wrapper.innerHTML = text;
          div.appendChild(wrapper);
        } else {
          div.textContent = text;
        }
        messages.appendChild(div);
        messages.scrollTop = messages.scrollHeight;
      }

      let loadingEl = null;
      function showLoading() {
        loadingEl = document.createElement("div");
        loadingEl.className = "msg bot";
        loadingEl.innerHTML = `<span class="loading">Pensando<span class="dot"></span><span class="dot"></span><span class="dot"></span></span>`;
        messages.appendChild(loadingEl);
        messages.scrollTop = messages.scrollHeight;
      }
      function hideLoading() {
        if (loadingEl && loadingEl.parentNode) loadingEl.parentNode.removeChild(loadingEl);
        loadingEl = null;
      }

      function openChat() {
        win.style.display = "flex";
        if (!greeted) {
          appendMessage("¡Hola! Soy Maxter y estoy para ayudarte.", "bot");
          greeted = true;
        }
      }
      function closeChat() { win.style.display = "none"; }

      bubble.addEventListener("click", openChat);
      closeBtn.addEventListener("click", closeChat);

      async function sendMessage() {
        const text = (input.value || "").trim();
        if (!text) return;
        appendMessage(text, "user");
        input.value = "";

        showLoading();
        try {
          const res = await fetch(API_URL, {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
            },
            body: JSON.stringify({ message: text, origin: ORIGIN })
          });
          hideLoading();
          if (!res.ok) {
            appendMessage("Hubo un problema procesando tu mensaje.", "bot");
            return;
          }
          const data = await res.json();
          if (data && data.success) {
            appendMessage(data.response || "Sin respuesta.", "bot", true);
          } else {
            appendMessage("Hubo un error al procesar tu mensaje.", "bot");
          }
        } catch (e) {
          hideLoading();
          appendMessage("Error de conexión con el servidor.", "bot");
        }
      }

      sendBtn.addEventListener("click", sendMessage);
      input.addEventListener("keydown", (e) => { if (e.key === "Enter") sendMessage(); });

      console.info("[Maxter] widget cargado.");
    } catch (err) {
      console.error("[Maxter] error inicializando widget:", err);
    }
  }

  // --- Arranque robusto: si el DOM ya está listo, inicia; si no, espera ---
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }
})();
