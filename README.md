# Actualización: Candado estricto de catálogo para Flashbot

## Objetivo

Esta actualización corrige el comportamiento del chatbot para evitar que muestre productos falsos, irrelevantes o fuera del catálogo de `master.com.mx`.

A partir de esta versión, si el usuario solicita un artículo que no se vende en el sitio, por ejemplo:

- ventiladores
- alimentos
- animales
- lavadoras
- refrigeradores
- celulares
- laptops
- juguetes
- ropa
- cualquier producto no publicado en la tienda

el bot debe responder únicamente:

```text
lo siento, no vendemos el artículo que solicitas en este sitio.
```

Y debe regresar el arreglo de productos vacío:

```json
"products": []
```

El objetivo principal es que el bot **no mienta, no improvise y no recomiende productos parecidos cuando no existe una coincidencia real en el catálogo**.

---

## Archivos actualizados

Esta actualización modifica únicamente los siguientes archivos del backend:

```text
backend/app.py
backend/indexer.py
backend/catalog_intelligence.py
backend/prompts.py
```

No se modificó el frontend ni el widget visual.

---

## Cambios principales

### 1. `backend/app.py`

Se agregó un candado estricto de catálogo antes de entregar la respuesta final al usuario.

Nuevas constantes y funciones agregadas:

```python
CATALOG_NO_MATCH_MESSAGE
_catalog_tokens()
_detect_catalog_families()
_item_matches_family()
_has_direct_product_match()
_specific_query_tokens_for_families()
_item_matches_specific_tokens()
_apply_strict_catalog_guard()
_catalog_no_match_payload()
```

El flujo ahora hace lo siguiente:

1. Recibe la consulta del usuario.
2. Busca candidatos en el catálogo.
3. Aplica inteligencia de compatibilidad técnica.
4. Aplica el candado estricto de catálogo.
5. Si no hay coincidencia real, bloquea la respuesta y devuelve:

```json
{
  "answer": "lo siento, no vendemos el artículo que solicitas en este sitio.",
  "products": [],
  "pagination": {
    "page": 1,
    "per_page": 10,
    "total": 0,
    "total_pages": 0,
    "has_next": false,
    "has_prev": false
  }
}
```

También se agregó registro en consola para detectar cuándo el candado bloquea una consulta:

```text
[CHAT][CATALOG_GUARD] rejected query='...' reason=...
```

Esto ayuda a depurar por qué una búsqueda fue rechazada.

---

### 2. `backend/indexer.py`

Se reforzó la limpieza de términos de búsqueda para evitar coincidencias falsas generadas por palabras genéricas.

Ahora se ignoran términos como:

```text
busco, buscando, quiero, necesito, estamos, producto, productos, artículo,
precio, costo, recomiendas, cotizar, tienda, página, web, etc.
```

También se agregó el campo interno:

```python
"_search_score"
```

Este campo permite conocer qué tan fuerte fue la coincidencia de búsqueda. No está pensado para mostrarse al usuario final, sino como dato técnico para diagnóstico y depuración.

---

### 3. `backend/catalog_intelligence.py`

Se ajustó la inteligencia de catálogo para evitar confusiones entre familias de productos, especialmente entre sensores de agua y sensores de gas.

Ejemplo del problema que se evita:

```text
sensor de nivel de gas
```

Antes podía interpretarse de forma ambigua por contener la palabra `nivel`. Ahora, si la consulta contiene intención de gas, no debe arrastrar resultados de agua sólo por palabras genéricas.

---

### 4. `backend/prompts.py`

Se reforzó el prompt de DeepSeek para que respete el resultado del backend.

Reglas agregadas:

- No inventar productos.
- No inventar categorías.
- No inventar marcas, precios, existencias ni características.
- Si el catálogo relevante está vacío, responder exactamente:

```text
lo siento, no vendemos el artículo que solicitas en este sitio.
```

- No sugerir alternativas cuando el producto solicitado no exista.
- No convertir una negativa del backend en una recomendación comercial.

---

## Familias de productos permitidas

El candado trabaja con familias reales del catálogo. Actualmente se contemplan las siguientes familias:

```text
support
antenna
remote
decoder
cable_connector
sensor_water
sensor_gas
sensor_general
camera_security
audio
power_energy
network
```

Estas familias permiten que el bot siga respondiendo consultas válidas como:

```text
busco un soporte para pantalla de 80 pulgadas
```

```text
tengo una pantalla VESA 400x400, qué soporte me recomiendas
```

```text
necesito un sensor para cisterna
```

```text
busco un sensor para tanque estacionario de gas
```

```text
quiero una antena para televisión
```

Pero bloquean consultas fuera del catálogo como:

```text
busco un ventilador
```

```text
venden alimento para perro
```

```text
tienen lavadoras
```

---

## Respuesta esperada para productos inexistentes

### Entrada

```text
busco un ventilador
```

### Salida esperada

```json
{
  "answer": "lo siento, no vendemos el artículo que solicitas en este sitio.",
  "products": []
}
```

---

## Respuesta esperada para productos válidos

### Entrada

```text
busco un soporte para pantalla de 80 pulgadas
```

### Salida esperada

El bot debe devolver únicamente productos reales del catálogo relacionados con soportes para pantalla, respetando compatibilidad técnica cuando los datos estén disponibles.

Ejemplo conceptual:

```json
{
  "answer": "Encontré soportes compatibles para pantallas grandes...",
  "products": [
    {
      "title": "...",
      "sku": "...",
      "product_url": "..."
    }
  ]
}
```

La respuesta exacta dependerá del catálogo indexado.

---

## Cómo instalar la actualización

1. Respaldar los archivos actuales del backend.
2. Reemplazar los siguientes archivos por los actualizados:

```text
backend/app.py
backend/indexer.py
backend/catalog_intelligence.py
backend/prompts.py
```

3. Subir los cambios al repositorio o servidor correspondiente.
4. Reiniciar el servicio del backend.

En Render, normalmente basta con subir los cambios al repositorio conectado para que se dispare un nuevo deploy, o hacer un redeploy manual desde el panel de Render.

---

## Pruebas recomendadas

Después de instalar la actualización, probar estas consultas en el chat:

### Consultas que deben ser rechazadas

```text
busco un ventilador
```

```text
venden alimentos para perro
```

```text
tienen animales
```

```text
quiero una lavadora
```

```text
venden celulares samsung
```

Todas deben responder:

```text
lo siento, no vendemos el artículo que solicitas en este sitio.
```

Y deben devolver:

```json
"products": []
```

### Consultas que deben seguir funcionando

```text
busco un soporte para pantalla
```

```text
busco un soporte para pantalla de 80 pulgadas
```

```text
tengo una pantalla VESA 400x400
```

```text
necesito un sensor de agua para cisterna
```

```text
busco un sensor para gas estacionario
```

```text
quiero una antena para TV
```

```text
busco un control remoto
```

```text
necesito un cable HDMI
```

Estas consultas deben mostrar productos reales del catálogo, siempre que existan productos indexados relacionados.

---

## Validación técnica realizada

Se validó que los archivos actualizados compilan correctamente con `py_compile`.

Comando usado:

```bash
python -m py_compile backend/app.py backend/indexer.py backend/catalog_intelligence.py backend/prompts.py
```

---

## Notas importantes

Esta actualización no elimina la búsqueda inteligente. La hace más estricta.

El bot seguirá recomendando productos cuando la consulta coincida con familias reales de productos de Master, pero bloqueará cualquier respuesta cuando:

- la consulta pertenece claramente a un producto que no se vende;
- la consulta no coincide con ninguna familia válida;
- el índice devuelve resultados débiles o no relacionados;
- los productos encontrados no contienen señales suficientes de coincidencia real.

Esto reduce el riesgo de que el bot muestre productos incorrectos sólo porque encontró palabras genéricas en el catálogo.

---

## Recomendación futura

Para una mejora posterior, se recomienda crear un archivo configurable, por ejemplo:

```text
backend/catalog_families.py
```

O bien:

```text
backend/catalog_rules.json
```

Esto permitiría administrar familias, sinónimos, términos bloqueados y reglas de coincidencia sin editar directamente `app.py`.

También se recomienda que el proceso de indexación extraiga automáticamente categorías reales desde Shopify, usando campos como:

```text
product_type
tags
vendor
handle
sku
collections
```

De esa forma, el candado de catálogo podría mantenerse sincronizado automáticamente con la tienda.

---

# [MAXTER CHAT STORAGE - START: HISTORIAL DE CHAT Y PEDIDOS]

## Objetivo de esta actualización

Esta versión agrega almacenamiento para las consultas escritas realizadas en las pestañas:

- **Chat**: pregunta del visitante, respuesta de Maxter y productos presentados.
- **Pedidos**: número consultado, si fue encontrado, respuesta y artículos devueltos.

La lógica existente del catálogo, DeepSeek, Shopify, paginación y consulta de pedidos no fue reemplazada. El guardado ocurre después de construir la respuesta y se envía a un ejecutor independiente. Si el almacenamiento no está disponible, el endpoint original sigue respondiendo.

## Archivos agregados

```text
backend/chat_storage.py
backend/chat_admin_endpoints.py
backend/.env.example
tests/test_chat_storage.py
.gitignore
```

## Archivos actualizados

```text
backend/app.py
backend/requirements.txt
widget/widget.js
README.md
```

## Cómo se identifica una conversación

El widget genera identificadores anónimos:

```text
visitor_id
session_id
request_id
```

- `visitor_id`: permanece en `localStorage` para reconocer el mismo navegador sin conocer la identidad de la persona.
- `session_id`: permanece en `sessionStorage` y agrupa los mensajes de la pestaña actual.
- `request_id`: identifica cada envío y evita registros duplicados si una solicitud se repite.

No se almacena la dirección IP desde este código.

## La paginación no se duplica como conversación

Cuando el usuario pulsa **Anterior** o **Siguiente**, el widget envía:

```json
{
  "is_new_message": false,
  "event_type": "pagination"
}
```

El backend no registra esa llamada como una nueva pregunta. Sólo se almacena el mensaje que realmente escribió el visitante.

## Bases y tablas nuevas

El almacenamiento es completamente independiente del índice del catálogo.

```text
maxter_chat_sessions
maxter_chat_exchanges
maxter_order_queries
```

La reindexación de productos no elimina estas tablas.

## Configuración recomendada para producción en Render

### 1. Crear PostgreSQL

Crear una instancia PostgreSQL en la misma cuenta y región que el backend de Maxter.

### 2. Configurar variables del servicio web

Agregar en el servicio del backend:

```text
CHAT_STORAGE_ENABLED=true
DATABASE_URL=<INTERNAL_DATABASE_URL_DE_POSTGRESQL>
```

Debe utilizarse la URL interna cuando PostgreSQL y el servicio web se encuentran en la misma región.

No escribir la URL real dentro del repositorio. Debe guardarse como variable de entorno de Render.

### 3. Mantener el secreto administrativo

Los endpoints de consulta utilizan el mismo secreto existente:

```text
ADMIN_REINDEX_SECRET
```

Debe enviarse mediante el encabezado:

```text
X-Admin-Secret
```

### 4. Desplegar

Al desplegar, `backend/requirements.txt` instala el adaptador PostgreSQL. Al iniciar el backend, las tablas se crean automáticamente si todavía no existen.

## Fallback SQLite

Cuando no existe `DATABASE_URL`, se crea una base separada en:

```text
backend/data/maxter_chat_history.sqlite3
```

También puede definirse otra ruta:

```text
CHAT_DB_PATH=/ruta/maxter_chat_history.sqlite3
```

Este fallback es útil para desarrollo y pruebas locales. En un servicio sin disco persistente no debe considerarse almacenamiento permanente.

## Endpoints administrativos

Todos requieren:

```text
X-Admin-Secret: valor_de_ADMIN_REINDEX_SECRET
```

### Estado y contadores

```http
GET /api/admin/chat-storage/status
```

Ejemplo:

```bash
curl \
  -H "X-Admin-Secret: TU_SECRETO" \
  "https://flashbot-backend-25b6.onrender.com/api/admin/chat-storage/status"
```

### Consultar chats

```http
GET /api/admin/conversations?section=chat&limit=100&offset=0
```

Filtros disponibles:

```text
section=chat|orders
session_id=<id>
date_from=<fecha ISO>
date_to=<fecha ISO>
limit=1..1000
offset=<número>
```

Ejemplo:

```bash
curl \
  -H "X-Admin-Secret: TU_SECRETO" \
  "https://flashbot-backend-25b6.onrender.com/api/admin/conversations?section=chat&limit=100"
```

### Consultar pedidos

```bash
curl \
  -H "X-Admin-Secret: TU_SECRETO" \
  "https://flashbot-backend-25b6.onrender.com/api/admin/conversations?section=orders&limit=100"
```

### Exportar CSV de Chat

```bash
curl \
  -H "X-Admin-Secret: TU_SECRETO" \
  -o maxter_chat.csv \
  "https://flashbot-backend-25b6.onrender.com/api/admin/conversations/export.csv?section=chat"
```

### Exportar CSV de Pedidos

```bash
curl \
  -H "X-Admin-Secret: TU_SECRETO" \
  -o maxter_pedidos.csv \
  "https://flashbot-backend-25b6.onrender.com/api/admin/conversations/export.csv?section=orders"
```

El máximo predeterminado de exportación es 50,000 registros. Puede ajustarse con:

```text
CHAT_EXPORT_MAX_ROWS=50000
```

## Datos guardados en Chat

```text
fecha y hora UTC
session_id
visitor_id
request_id
mensaje escrito
respuesta de Maxter
consulta efectiva interpretada
productos mostrados: título, SKU, precio, variante y URL
URL y título de la página
referrer
user-agent
```

## Datos guardados en Pedidos

```text
fecha y hora UTC
session_id
visitor_id
request_id
número consultado
pedido encontrado o no encontrado
cantidad de artículos
respuesta
artículos devueltos
URL y título de la página
referrer
user-agent
```

## Etiquetas permanentes en el código

Todos los cambios están delimitados con comentarios como:

```text
[MAXTER CHAT STORAGE - START: ...]
[MAXTER CHAT STORAGE - END: ...]
```

Estas etiquetas no deben eliminarse en futuras actualizaciones porque permiten identificar rápidamente cada parte de la implementación.

## Pruebas

Validación de sintaxis:

```bash
python -m py_compile \
  backend/app.py \
  backend/chat_storage.py \
  backend/chat_admin_endpoints.py

node --check widget/widget.js
```

Prueba automatizada del almacenamiento:

```bash
python -m unittest tests/test_chat_storage.py -v
```

La prueba comprueba:

- creación del esquema;
- guardado de Chat;
- guardado de Pedidos;
- deduplicación por `request_id`;
- lectura de registros;
- contadores administrativos.

# [MAXTER CHAT STORAGE - END: HISTORIAL DE CHAT Y PEDIDOS]
