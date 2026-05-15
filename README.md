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
