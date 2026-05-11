# Flashbot Backend + Widget Master

Proyecto actualizado para usar un chatbot flotante en `master.com.mx` con tres secciones separadas:

- **Chat**: flujo de texto conectado a `/api/chat` y DeepSeek/catálogo.
- **Voz**: widget oficial de ElevenLabs ConvAI, agente `agent_0801k6azj1rxe3arwjrs5y4rsrk4`.
- **Pedidos**: consulta independiente a `/api/orders`, buscando en Google Sheets por la columna `FOLIO`.

## Variables recomendadas en Render

```bash
CHAT_WRITER=deepseek
DEEPSEEK_API_KEY=tu_api_key_deepseek
DEEPSEEK_MODEL=deepseek-chat
ORDERS_PUBHTML_URL=https://docs.google.com/spreadsheets/d/e/2PACX-1vS7MFutb5ikOvAvWsxuc164Txu30GeVkCGZAY3U_fUVmS_0MKMn6ta2hbbNc-hcmFbV0fyAe8A-7PGG/pubhtml?gid=1842193501&single=true
ORDERS_AUTORELOAD=1
ORDERS_TTL_SECONDS=45
ALLOWED_ORIGINS=https://master.com.mx,https://www.master.com.mx
```

Si `ORDERS_PUBHTML_URL` no se configura, el backend usa por defecto la URL pública indicada arriba.

## Script para embeber en Shopify / master.com.mx

Pegar antes del cierre de `</body>`:

```html
<script>
  window.FLASHBOT_BACKEND_URL = 'https://flashbot-backend-25b6.onrender.com';
</script>
<script defer src="https://flashbot-backend-25b6.onrender.com/widget/widget.js"></script>
```

Alternativa con `data-backend`:

```html
<script defer
  src="https://flashbot-backend-25b6.onrender.com/widget/widget.js"
  data-backend="https://flashbot-backend-25b6.onrender.com">
</script>
```

## Endpoints principales

- `GET /health`
- `POST /api/chat`
- `POST /api/orders`
- `GET /api/admin/orders-ping`
- `GET /api/admin/orders-find?folio=TU_FOLIO`

## Respuesta de pedidos

`POST /api/orders` acepta:

```json
{ "folio": "A1BC3" }
```

y devuelve `items` con:

- `Folio`
- `Orden de compra`
- `SKU de producto`
- `Cantidad`
- `Total`
- `Paquetería`
- `Guía`
