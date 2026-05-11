# Widget flotante Master / Flashbot

Este widget crea una ventana flotante con tres secciones independientes:

1. **Chat**: usa el backend `/api/chat`, conectado al flujo de catálogo/DeepSeek.
2. **Voz**: monta dentro del widget el componente oficial de ElevenLabs ConvAI con el agente `agent_0801k6azj1rxe3arwjrs5y4rsrk4`.
3. **Pedidos**: usa el backend `/api/orders`, consulta la hoja pública de Google Sheets por columna `FOLIO` y devuelve una tabla ordenada.

## Corrección importante para Shopify

No pegues el script externo de ElevenLabs por separado en el theme. El archivo `widget.js` ya lo carga cuando el usuario abre la pestaña **Voz**. Esto evita que el script de ElevenLabs busque un elemento `<elevenlabs-convai>` antes de que el widget lo monte.

## Código recomendado para embeber en master.com.mx

Pega este bloque antes de `</body>` en el theme de Shopify:

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

`widget.js` carga automáticamente `widget.css` desde la misma carpeta. También incluye CSS crítico de emergencia para que el botón siga funcionando aunque Shopify tarde en cargar la hoja de estilos.

## Prueba rápida en consola

Después de publicar el theme y redeplegar Render, abre la consola del navegador y ejecuta:

```js
window.MasterFlashbotWidget.open()
```

También puedes validar el backend desde el navegador:

```text
https://flashbot-backend-25b6.onrender.com/health
https://flashbot-backend-25b6.onrender.com/widget/widget.js?v=20260511_2
```

## ElevenLabs usado internamente

```html
<elevenlabs-convai agent-id="agent_0801k6azj1rxe3arwjrs5y4rsrk4"></elevenlabs-convai>
<script src="https://unpkg.com/@elevenlabs/convai-widget-embed" async type="text/javascript"></script>
```

El script se inyecta automáticamente al abrir la pestaña **Voz**.
