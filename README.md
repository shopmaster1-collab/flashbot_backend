# Flashbot Backend + Widget Master / Maxter

Proyecto actualizado para usar un chatbot flotante en `master.com.mx` con tres secciones separadas:

- **Chat**: flujo de texto conectado a `/api/chat` y DeepSeek/catálogo.
- **Voz**: widget oficial de ElevenLabs ConvAI montado dentro del panel, agente `agent_0801k6azj1rxe3arwjrs5y4rsrk4`.
- **Pedidos**: consulta independiente a `/api/orders`, buscando en Google Sheets por la columna **`ORDEN_COMPRA`**.

## Cambios clave de esta versión

- Header del widget actualizado a **Tu Asistente Maxter**.
- Mensaje inicial actualizado a: **¡Hola! Soy tu asistente inteligente Maxter. ¿En qué puedo ayudarte hoy?**
- La pestaña **Pedidos** ahora se muestra como una vista visualmente distinta del Chat.
- El texto de Pedidos es: **¿Necesitas saber el estatus de tu pedido? Escribe tu Número de Pedido.**
- `/api/orders` ya no recorta pedidos con guiones. Conserva completos valores como:
  - `702-7300318-1033843`
  - `2000012817687573`
  - `v44851776ekt-01`
  - `#9188.307766427-A`
- La búsqueda principal se realiza en `ORDEN_COMPRA`.
- Se agregaron alias de compatibilidad como `DE_ORDEN`, `ORDER_ID`, `PEDIDO` y fallback a `FOLIO` si no existe columna de orden.
- La lectura de Google Sheets ahora intenta varias rutas: `pubhtml`, `pub?output=csv` y `gviz/tq?tqx=out:csv`.
- `/api/admin/orders-ping` devuelve `attempts`, `source_url`, `headers`, `rows_count` y `search_columns` para diagnóstico.
- `widget.js` monta `<elevenlabs-convai>` únicamente cuando el usuario entra a la pestaña **Voz**.
- Se mantiene `Cache-Control: no-store` para evitar caché agresiva en Shopify durante correcciones.

## Variables recomendadas en Render

```bash
CHAT_WRITER=deepseek
DEEPSEEK_API_KEY=tu_api_key_deepseek
DEEPSEEK_MODEL=deepseek-chat
ORDERS_PUBHTML_URL=https://docs.google.com/spreadsheets/d/e/2PACX-1vS7MFutb5ikOvAvWsxuc164Txu30GeVkCGZAY3U_fUVmS_0MKMn6ta2hbbNc-hcmFbV0fyAe8A-7PGG/pubhtml?gid=1842193501&single=true
ORDERS_AUTORELOAD=1
ORDERS_TTL_SECONDS=45
ALLOWED_ORIGINS=https://master.com.mx,https://www.master.com.mx,https://master-electronicos.myshopify.com
```

Si `ORDERS_PUBHTML_URL` no se configura, el backend usa por defecto la URL pública indicada arriba.

## Script para embeber en Shopify / master.com.mx

Pegar antes del cierre de `</body>`:

```html
<!-- Master Flashbot Widget -->
<script>
  window.FLASHBOT_BACKEND_URL = 'https://flashbot-backend-25b6.onrender.com';
  window.MASTER_FLASHBOT_OPTIONS = {
    elevenlabsAgentId: 'agent_0801k6azj1rxe3arwjrs5y4rsrk4',
    productPanelId: 'agent-product-panel'
  };
</script>
<script
  defer
  src="https://flashbot-backend-25b6.onrender.com/widget/widget.js?v=20260511_3"
  data-backend="https://flashbot-backend-25b6.onrender.com"
  data-agent-id="agent_0801k6azj1rxe3arwjrs5y4rsrk4">
</script>
<div id="agent-product-panel"></div>
```

No agregues este bloque por separado, porque ahora lo administra `widget.js`:

```html
<elevenlabs-convai agent-id="agent_0801k6azj1rxe3arwjrs5y4rsrk4"></elevenlabs-convai>
<script src="https://unpkg.com/@elevenlabs/convai-widget-embed" async type="text/javascript"></script>
```

## Endpoints principales

- `GET /health`
- `GET /api/health`
- `POST /api/chat`
- `POST /api/orders`
- `GET /api/admin/orders-ping`
- `GET /api/admin/orders-find?order=TU_ORDEN_COMPRA`

## Respuesta de pedidos

`POST /api/orders` acepta:

```json
{ "order": "702-7300318-1033843" }
```

y devuelve `items` con:

- `Orden de compra`
- `SKU de producto`
- `Cantidad`
- `Total`
- `Paquetería`
- `Guía`

## Pruebas recomendadas en PowerShell

```powershell
Invoke-RestMethod `
  -Uri "https://flashbot-backend-25b6.onrender.com/api/admin/orders-ping" `
  -Method Get `
  -Headers @{ "X-Admin-Secret" = "TU_SECRET_REAL" }
```

```powershell
$body = @{ order = "702-7300318-1033843" } | ConvertTo-Json

Invoke-RestMethod `
  -Uri "https://flashbot-backend-25b6.onrender.com/api/orders" `
  -Method Post `
  -ContentType "application/json" `
  -Body $body
```
