@app.post("/api/chat")
def chat():
    data = request.get_json(force=True)
    query = (data.get("message") or "").strip()
    if not query:
        return jsonify({"answer": "¿Qué estás buscando? Puedo ayudarte a encontrar productos del catálogo.", "products": []})

    items = indexer.search(query, k=5)

    # Tarjetas ya listas
    cards = []
    for it in items:
        v = it["variant"]
        cards.append({
            "title": it["title"],
            "image": it["image"],
            "price": money(v["price"]),
            "compare_at_price": money(v["compare_at_price"]) if v["compare_at_price"] else None,
            "buy_url": it["buy_url"],
            "product_url": it["product_url"],
            "inventory": it["variant"]["inventory"],
        })

    # Si no hay ítems, respuesta corta
    if not items:
        return jsonify({
            "answer": "No encontré resultados exactos para tu consulta. Prueba con palabras clave como marca, modelo o categoría. 😉",
            "products": []
        })

    # Intento con Deepseek (contexto limitado)
    context = indexer.mini_catalog_json(items)
    user_msg = USER_TEMPLATE.format(query=query, catalog_json=context)
    answer = ""
    try:
        answer = deeps.chat(SYSTEM_PROMPT, user_msg) or ""
    except Exception as e:
        print(f"[WARN] Deepseek chat error: {e}", flush=True)
        answer = ""

    # Si el LLM dio una negación o vacío, generamos respuesta basada en catálogo
    neg_tokens = ["no dispongo", "no tengo información", "no cuento", "lo siento"]
    if (not answer) or any(tok in answer.lower() for tok in neg_tokens):
        # arma una frase útil con 3-5 sugerencias
        tops = [f"- {c['title']} — {c['price']}  \n  {c['product_url']}" for c in cards[:5]]
        answer = (
            f"Encontré estas opciones relacionadas con “{query}”:  \n" +
            "\n".join(tops) +
            "\n\n¿Quieres que filtre por precio, marca o disponibilidad?"
        )

    return jsonify({"answer": answer, "products": cards})
