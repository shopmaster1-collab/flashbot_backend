# Flashbot Backend + Widget Master

Proyecto actualizado para usar un chatbot flotante en `master.com.mx` con tres secciones separadas:

- **Chat**: flujo de texto conectado a `/api/chat` y DeepSeek/catálogo.
- **Voz**: widget oficial de ElevenLabs ConvAI montado dentro del panel, agente `agent_0801k6azj1rxe3arwjrs5y4rsrk4`.
- **Pedidos**: consulta independiente a `/api/orders`, buscando en Google Sheets por la columna `FOLIO`.

## Cambios clave de esta versión

- El widget ya no depende de que el theme de Shopify cargue ElevenLabs por separado.
- `widget.js` monta `<elevenlabs-convai>` únicamente cuando el usuario entra a la pestaña **Voz**.
- Se agregaron `clientTools` de ElevenLabs dentro del widget para mostrar tarjetas de producto sin usar scripts externos en Shopify.
- Se agregaron timeouts y mensajes claros si Render tarda o el backend no responde.
- Se agregó CSS crítico de respaldo si `widget.css` no carga.
- Se cambió la caché de archivos `/widget/*` a `no-store` para evitar que Shopify o el navegador usen una versión antigua durante correcciones.
- Se agregó `/api/health` además de `/health`.

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
  src="https://flashbot-backend-25b6.onrender.com/widget/widget.js?v=20260511_2"
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
