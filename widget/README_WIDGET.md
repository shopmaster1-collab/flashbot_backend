# Widget flotante Master / Flashbot

Este widget crea una ventana flotante con tres secciones independientes:

1. **Chat**: usa el backend `/api/chat`, conectado al flujo de catálogo/DeepSeek.
2. **Voz**: carga exclusivamente el widget oficial de ElevenLabs ConvAI con el agente `agent_0801k6azj1rxe3arwjrs5y4rsrk4`.
3. **Pedidos**: usa el backend `/api/orders`, consulta la hoja pública de Google Sheets por columna `FOLIO` y devuelve una tabla ordenada.

## Código para embeber en master.com.mx

Pega este bloque antes de `</body>` en el theme de Shopify:

```html
<script>
  window.FLASHBOT_BACKEND_URL = 'https://flashbot-backend-25b6.onrender.com';
</script>
<script defer src="https://flashbot-backend-25b6.onrender.com/widget/widget.js"></script>
```

También puedes usar `data-backend` directamente:

```html
<script defer
  src="https://flashbot-backend-25b6.onrender.com/widget/widget.js"
  data-backend="https://flashbot-backend-25b6.onrender.com">
</script>
```

`widget.js` carga automáticamente `widget.css` desde la misma carpeta.

## ElevenLabs usado

```html
<elevenlabs-convai agent-id="agent_0801k6azj1rxe3arwjrs5y4rsrk4"></elevenlabs-convai>
<script src="https://unpkg.com/@elevenlabs/convai-widget-embed" async type="text/javascript"></script>
```

El script se inyecta automáticamente al abrir la pestaña **Voz**.
