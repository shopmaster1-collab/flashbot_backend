/* MAXTER widget.js — UI y lógica de chat
   Requiere widget.css cargado en el theme. */
(function () {
  // Lee atributos del <script> que carga este archivo
  const ATTR = (name, def) =>
    (document.currentScript && document.currentScript.getAttribute(name)) || def;

  const BACKEND = (ATTR("data-backend", "https://flashbot-backend-25b6.onrender.com") || "").replace(/\/+$/,'');
  const TITLE    = ATTR("data-title", "MAXTER, Tu Asistente Inteligente");
  const POS      = ATTR("data-position", "left");   // left | right
  const PRIMARY  = ATTR("data-primary", "#0b73ff"); // color barra/botón principal

  // Helpers
  const moneyToNumber = (m) => {
    if (typeof m === "number") return m;
    if (!m) return null;
    const n = m.replace(/[^\d,.\-]/g, "").replace(/\.(?=\d{3,})/g, "").replace(",", ".");
    const f = parseFloat(n);
    return Number.isFinite(f) ? f : null;
  };
  const fmt = (n) =>
    new Intl.NumberFormat("es-MX", { style: "currency", currency: "MXN", maximumFractionDigits: 2 }).format(n || 0);

  // Crear contenedores
  const root = document.createElement("div");
  document.body.appendChild(root);

  // Botón flotante
  const fab = document.createElement("button");
  fab.className = "mx-fab";
  fab.innerHTML = `
    <span class="mx-fab__logo" aria-hidden="true">
      <svg viewBox="0 0 24 24"><path d="M4 18l4-12 4 8 4-8 4 12h-3l-1-3-4 3-4-3-1 3H4z"/></svg>
    </span>
    <span>${TITLE}</span>
  `;
  root.appendChild(fab);

  // Panel
  const panel = document.createElement("section");
  panel.className = "mx-panel";
  panel.innerHTML = `
    <header class="mx-header" style="background:${PRIMARY}">
      <div class="mx-title">
        <span class="mx-hlogo" aria-hidden="true">
          <svg viewBox="0 0 24 24"><path d="M4 18l4-12 4 8 4-8 4 12h-3l-1-3-4 3-4-3-1 3H4z"/></svg>
        </span>
        <span>${TITLE}</span>
      </div>
      <button class="mx-close" aria-label="Cerrar">✕</button>
    </header>
    <main class="mx-body"></main>
    <div class="mx-input">
      <input class="mx-text" type="text" placeholder="Escribe tu mensaje..." autocomplete="off"/>
      <button class="mx-send" style="background:${PRIMARY}">Enviar</button>
    </div>
  `;
  root.appendChild(panel);

  // Posición izquierda/derecha
  if (POS === "right") {
    fab.style.left = "auto"; fab.style.right = "16px";
    panel.style.left = "auto"; panel.style.right = "16px";
  }

  // Referencias
  const body = panel.querySelector(".mx-body");
  const txt  = panel.querySelector(".mx-text");
  const send = panel.querySelector(".mx-send");
  const closeBtn = panel.querySelector(".mx-close");

  // UX básico
  const open = () => { panel.classList.add("mx-open"); setTimeout(() => txt.focus(), 50); };
  const close = () => panel.classList.remove("mx-open");

  fab.addEventListener("click", () => {
    if (panel.classList.contains("mx-open")) close(); else open();
  });
  closeBtn.addEventListener("click", close);

  // Render de mensajes
  const addMsg = (text, who="bot") => {
    const div = document.createElement("div");
    div.className = "mx-msg" + (who === "bot" ? " mx-msg--bot" : "");
    div.textContent = text;
    body.appendChild(div);
    body.scrollTop = body.scrollHeight;
  };

  // Render de productos
  const renderProducts = (products = []) => {
    if (!products || !products.length) return;
    const wrap = document.createElement("div");
    wrap.className = "mx-products";

    products.forEach(p => {
      const price = moneyToNumber(p.price) ?? p.price;
      const old   = moneyToNumber(p.compare_at_price);
      const hasOff = old && price && old > price;
      const inv = Array.isArray(p.inventory) ? p.inventory : [];

      const card = document.createElement("article");
      card.className = "mx-card";
      card.innerHTML = `
        <img class="mx-img" src="${p.image || ''}" alt="">
        <div>
          <h4 class="mx-ttl">${p.title || ''}</h4>
          <div class="mx-price">
            <span class="mx-cur">${typeof price === 'number' ? fmt(price) : (p.price || '')}</span>
            ${hasOff ? `<span class="mx-old">${fmt(old)}</span>` : ``}
          </div>
          <div class="mx-row">
            <a class="mx-btn mx-btn--buy" href="${p.buy_url}" target="_self" rel="nofollow noopener">Comprar ahora</a>
            <a class="mx-link" href="${p.product_url}" target="_blank" rel="noopener">Ver producto</a>
          </div>
          <div class="mx-inv">
            <b>Inventario:</b><br>
            ${
              inv.length
                ? inv.map(i => `${i.name}: ${i.available} disponibles`).join('<br>')
                : 'Consultar disponibilidad'
            }
          </div>
        </div>
      `;
      wrap.appendChild(card);
    });

    body.appendChild(wrap);
    body.scrollTop = body.scrollHeight;
  };

  // Enviar consulta
  const sendQuery = async () => {
    const q = (txt.value || "").trim();
    if (!q) return;
    addMsg(q, "user");
    txt.value = "";
    send.disabled = true;

    try {
      const r = await fetch(`${BACKEND}/api/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: q })
      });
      const data = await r.json();
      const ans = data && data.answer ? String(data.answer) : "Lo siento, no tengo esa info por ahora.";
      addMsg(ans, "bot");
      if (Array.isArray(data.products)) renderProducts(data.products);
    } catch (e) {
      addMsg("Hubo un problema al consultar. Intenta de nuevo.", "bot");
      console.error("[MAXTER] error", e);
    } finally {
      send.disabled = false;
    }
  };

  // Listeners
  send.addEventListener("click", sendQuery);
  txt.addEventListener("keydown", (e) => {
    if (e.key === "Enter") sendQuery();
  });

  // Mensaje inicial sutil al abrir por primera vez
  let greeted = false;
  fab.addEventListener("click", () => {
    if (!greeted && panel.classList.contains("mx-open")) {
      addMsg("¡Hola! Soy MAXTER. Pregúntame por productos (p. ej. “divisor HDMI 1×4”, “antena UHF exterior”, “control remoto Samsung”).");
      greeted = true;
    }
  });

  // Ajustes de accesibilidad / color
  fab.style.background = PRIMARY;
  panel.querySelector(".mx-header").style.background = PRIMARY;
  send.style.background = PRIMARY;
})();
