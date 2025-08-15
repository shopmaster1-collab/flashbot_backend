document.addEventListener("DOMContentLoaded", function () {
  const API_URL = "https://flashbot-backend-25b6.onrender.com/chat";
  const ORIGIN = window.location.origin;

  // ✅ Asegura que logo/loader salgan del mismo host que el backend
  const ASSETS_BASE = new URL(API_URL).origin + "/static/img/";
  const LOGO_URL = ASSETS_BASE + "img_m.png";
  const LOADER_URL = ASSETS_BASE + "barra.gif";

  console.log("✅ Widget cargado correctamente desde:", ORIGIN);

  const style = document.createElement("style");
  style.textContent = `
    #chatbot-bubble {
      position: fixed;
      bottom: 20px;
      left: 20px;
      background-color: #007bff;
      color: white;
      padding: 10px 14px;
      border-radius: 28px;
      cursor: pointer;
      z-index: 2147483647;
      display: flex;
      align-items: center;
      font-family: sans-serif;
      font-size: 14px;
      box-shadow: 0 4px 8px rgba(0, 0, 0, 0.3);
      transition: background 0.3s;
    }
    #chatbot-bubble:hover { background-color: #005fcc; }
    #chatbot-bubble img { width: 28px; height: 28px; margin-right: 8px; }
    #chatbot-window {
      position: fixed;
      bottom: 90px;
      left: 20px;
      width: 360px;
      height: 480px;
      background: white;
      border-radius: 12px;
      box-shadow: 0 8px 16px rgba(0, 0, 0, 0.25);
      z-index: 2147483647;
      display: none;
      flex-direction: column;
      overflow: hidden;
      font-family: sans-serif;
    }
    #chatbot-header {
      background: #007bff;
      color: white;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 10px;
    }
    #chatbot-header img { height: 28px; margin-right: 10px; }
    #chatbot-title { flex: 1; font-weight: bold; font-size: 14px; }
    #chatbot-close { cursor: pointer; font-size: 18px; }
    #chatbot-messages { flex: 1; padding: 12px; overflow-y: auto; }
    #chatbot-input { display: flex; border-top: 1px solid #ccc; }
    #chatbot-input input {
      flex: 1; padding: 10px; border: none; outline: none;
    }
    #chatbot-input button {
      background: #007bff; color: white; border: none; padding: 0 16px; cursor: pointer;
    }
    .chatbot-loading { text-align: center; margin: 10px 0; }
    .chatbot-loading img { height: 20px; }
  `;
  document.head.appendChild(style);

  const bubble = document.createElement("div");
  bubble.id = "chatbot-bubble";
  bubble.innerHTML = `<img src="${LOGO_URL}" alt="Chat"><span>MAXTER, Tu Asistente Inteligente</span>`;
  document.body.appendChild(bubble);

  const windowDiv = document.createElement("div");
  windowDiv.id = "chatbot-window";
  windowDiv.innerHTML = `
    <div id="chatbot-header">
      <img src="${LOGO_URL}" alt="Logo">
      <div id="chatbot-title">MAXTER, Tu Asistente Inteligente</div>
      <div id="chatbot-close">✖️</div>
    </div>
    <div id="chatbot-messages"></div>
    <div id="chatbot-input">
      <input type="text" placeholder="Escribe tu mensaje..." />
      <button>Enviar</button>
    </div>
  `;
  document.body.appendChild(windowDiv);

  const input = windowDiv.querySelector("input");
  const button = windowDiv.querySelector("button");
  const messages = windowDiv.querySelector("#chatbot-messages");
  const closeBtn = windowDiv.querySelector("#chatbot-close");

  bubble.addEventListener("click", () => { windowDiv.style.display = "flex"; });
  closeBtn.addEventListener("click", () => { windowDiv.style.display = "none"; });

  function appendMessage(text, from = "user") {
    const div = document.createElement("div");
    div.style.marginBottom = "10px";
    div.style.textAlign = from === "user" ? "right" : "left";
    if (from === "user") {
      div.innerHTML = `<span style="background:#007bff;color:white;padding:8px;border-radius:8px;display:inline-block;max-width:80%">${text}</span>`;
    } else {
      div.innerHTML = text;
    }
    messages.appendChild(div);
    messages.scrollTop = messages.scrollHeight;
  }

  function showLoading() {
    const loading = document.createElement("div");
    loading.className = "chatbot-loading";
    loading.id = "chatbot-loading";
    loading.innerHTML = `<img src="${LOADER_URL}" alt="Cargando...">`;
    messages.appendChild(loading);
    messages.scrollTop = messages.scrollHeight;
  }

  function hideLoading() {
    const loading = document.getElementById("chatbot-loading");
    if (loading) loading.remove();
  }

  async function sendMessage() {
    const question = input.value.trim();
    if (!question) return;
    appendMessage(question, "user");
    input.value = "";
    showLoading();

    try {
      const res = await fetch(API_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: question,
          origin: ORIGIN // ✅ se envía explícitamente
        })
      });

      const data = await res.json();
      hideLoading();
      if (data.success) {
        appendMessage(data.response, "bot");
      } else {
        appendMessage("Hubo un error al procesar tu mensaje.", "bot");
      }
    } catch (err) {
      hideLoading();
      appendMessage("Error de conexión con el servidor.", "bot");
    }
  }

  button.addEventListener("click", sendMessage);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") sendMessage();
  });
});
