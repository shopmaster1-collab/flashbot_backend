# -*- coding: utf-8 -*-
"""
Capa de inteligencia técnica para el catálogo de Master Electronics.

Esta capa NO sustituye al buscador FTS/SQLite. Se ejecuta después de obtener
candidatos y ayuda a ordenar/filtrar productos cuando la consulta contiene
condiciones de compatibilidad: pulgadas de TV, VESA, tipo de soporte,
cisterna/tinaco, alcance, WiFi, display, válvula, etc.
"""

from __future__ import annotations

import copy
import re
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Tuple

NumberPair = Tuple[int, int]


def norm_text(value: Any) -> str:
    """Texto en minúsculas y sin acentos para comparaciones robustas."""
    if value is None:
        return ""
    text = str(value).lower()
    text = "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")
    text = text.replace("×", "x")
    text = text.replace("“", '"').replace("”", '"').replace("″", '"').replace("’", "'")
    return re.sub(r"\s+", " ", text).strip()


def _raw_text(item: Dict[str, Any]) -> str:
    variant = item.get("variant") or {}
    parts = [
        item.get("title") or "",
        item.get("handle") or "",
        item.get("tags") or "",
        item.get("vendor") or "",
        item.get("product_type") or "",
        variant.get("sku") or item.get("sku") or "",
        item.get("body") or "",
    ]
    skus = item.get("skus")
    if isinstance(skus, (list, tuple)):
        parts.extend([x for x in skus if x])
    return " ".join(str(x) for x in parts if x)


def _to_float_number(value: str) -> Optional[float]:
    if not value:
        return None
    clean = str(value).strip().replace(" ", "")
    # 100,000 o 100.000 como miles; 2.5 como decimal.
    if re.search(r"[,.]\d{3}(?:\D|$)", clean):
        clean = clean.replace(",", "").replace(".", "")
    else:
        clean = clean.replace(",", ".")
    try:
        return float(clean)
    except Exception:
        return None


def _unique_keep_order(values: Iterable[str], limit: int = 8) -> List[str]:
    seen = set()
    out: List[str] = []
    for value in values:
        value = str(value or "").strip()
        if not value or value.lower() in seen:
            continue
        seen.add(value.lower())
        out.append(value)
        if len(out) >= limit:
            break
    return out


def analyze_query(query: str) -> Dict[str, Any]:
    """Extrae intención y parámetros técnicos de la pregunta del usuario."""
    q = query or ""
    qn = norm_text(q)

    support_words = ["soporte", "base", "bracket", "mount", "montaje", "pared", "techo", "piso"]
    tv_words = ["pantalla", "tv", "televisor", "television", "monitor", "vesa"]
    water_words = ["agua", "tinaco", "cisterna", "tanque de agua", "nivel", "water", "fuga", "inundacion"]
    gas_words = ["gas", "lp", "propano", "butano", "estacionario", "gassensor", "tanque estacionario"]

    is_support = any(w in qn for w in support_words) and (any(w in qn for w in tv_words) or "vesa" in qn)
    is_water = any(w in qn for w in water_words)
    is_gas = any(w in qn for w in gas_words)

    intent = "general"
    if is_gas and not is_water:
        intent = "gas"
    if is_water:
        intent = "water"
    if is_support:
        intent = "support"

    # Pulgadas: preferimos números con unidad explícita. Si no hay unidad, sólo tomamos
    # el número cuando la consulta claramente es de soporte/pantalla.
    tv_inches: Optional[int] = None
    inch_patterns = [
        r"\b(1[9]|[2-9]\d|10\d|11\d|120)\s*(?:\"|''|pulgadas?|pulg\.?|plg\.?|inch(?:es)?)\b",
        r"(?:pantalla|tv|televisor|television|monitor)\D{0,24}\b(1[9]|[2-9]\d|10\d|11\d|120)\b",
    ]
    for pat in inch_patterns:
        m = re.search(pat, qn)
        if m:
            try:
                tv_inches = int(m.group(1))
                break
            except Exception:
                pass
    if tv_inches is None and is_support:
        # Caso: "soporte para 80". Evitamos litros/metros y números absurdos.
        for m in re.finditer(r"\b(1[9]|[2-9]\d|10\d|11\d|120)\b", qn):
            tail = qn[m.end():m.end() + 12]
            head = qn[max(0, m.start() - 12):m.start()]
            if any(u in tail for u in [" litro", " litros", " l", "lt", "metro", " metros", "m2", "m²"]):
                continue
            if any(u in head for u in ["vesa", "x"]):
                continue
            tv_inches = int(m.group(1))
            break

    # VESA: requiere palabra VESA o contexto claro de soporte/pantalla y dos medidas >= 50.
    vesa: Optional[NumberPair] = None
    for m in re.finditer(r"\b(\d{2,4})\s*x\s*(\d{2,4})\b", qn):
        a, b = int(m.group(1)), int(m.group(2))
        around = qn[max(0, m.start() - 18):m.end() + 18]
        if a >= 50 and b >= 50 and ("vesa" in around or is_support):
            vesa = (a, b)
            break

    # Litros de tinaco/cisterna.
    tank_liters: Optional[int] = None
    m_l = re.search(r"\b(\d{1,3}(?:[,.]\d{3})+|\d+(?:[,.]\d+)?)\s*(?:l|lt|lts|litros?|litro)\b", qn)
    if m_l:
        val = _to_float_number(m_l.group(1))
        if val is not None:
            tank_liters = int(round(val))

    tank_type: Optional[str] = None
    if "cisterna" in qn:
        tank_type = "cisterna"
    elif "tinaco" in qn:
        tank_type = "tinaco"
    elif "tanque de agua" in qn:
        tank_type = "tanque de agua"

    height_m: Optional[float] = None
    if intent == "water":
        for m in re.finditer(r"\b(\d+(?:[,.]\d+)?)\s*(?:m|metro|metros)\b", qn):
            val = _to_float_number(m.group(1))
            if val and 0.2 <= val <= 50:
                height_m = val
                break

    support_mount_type = None
    for key, aliases in {
        "techo": ["techo", "ceiling"],
        "pared": ["pared", "muro", "wall"],
        "piso": ["piso", "movil", "movil", "mobile", "floor"],
        "brazo": ["brazo", "articulado", "articulable", "extendible"],
        "esquina": ["esquina", "corner"],
        "fijo": ["fijo", "fixed"],
        "inclinable": ["inclinable", "tilt", "tilting"],
    }.items():
        if any(a in qn for a in aliases):
            support_mount_type = key
            break

    wants = {
        "wifi": any(w in qn for w in ["wifi", "wi-fi", "app", "celular", "remoto", "alexa", "google home"]),
        "without_wifi": any(w in qn for w in ["sin wifi", "sin wi-fi", "no wifi", "no requiere wifi"]),
        "display": any(w in qn for w in ["pantalla", "display", "lcd"]),
        "alarm": any(w in qn for w in ["alarma", "alerta", "notificacion", "notificaciones"]),
        "valve": any(w in qn for w in ["valvula", "electrovalvula", "cierre"]),
        "ultrasonic": any(w in qn for w in ["ultra", "ultrasonico", "ultrasonica"]),
        "pressure": any(w in qn for w in ["presion"]),
        "bluetooth": "bluetooth" in qn,
        "wireless": any(w in qn for w in ["inalambrico", "inalambrica", "radiofrecuencia", "rf"]),
    }

    return {
        "intent": intent,
        "tv_inches": tv_inches,
        "vesa": vesa,
        "tank_liters": tank_liters,
        "tank_type": tank_type,
        "height_m": height_m,
        "support_mount_type": support_mount_type,
        "wants": wants,
        "normalized": qn,
        "has_technical_filters": bool(tv_inches or vesa or tank_liters or tank_type or height_m or any(wants.values()) or support_mount_type),
    }


def build_search_queries(query: str, analysis: Optional[Dict[str, Any]] = None) -> List[str]:
    """Genera consultas adicionales para traer candidatos suficientes del índice."""
    analysis = analysis or analyze_query(query)
    q = (query or "").strip()
    queries: List[str] = [q] if q else []
    intent = analysis.get("intent")

    if intent == "support":
        base = "soporte pantalla tv televisor monitor vesa"
        if analysis.get("tv_inches"):
            queries.append(f"{base} {analysis['tv_inches']} pulgadas")
        if analysis.get("vesa"):
            a, b = analysis["vesa"]
            queries.append(f"{base} VESA {a}x{b}")
        if analysis.get("support_mount_type"):
            queries.append(f"{base} {analysis['support_mount_type']}")
        queries.extend(["soporte pantalla tv", "soporte televisor vesa"])
    elif intent == "water":
        queries.extend([
            "sensor nivel agua cisterna tinaco iot water connect-water easy-water",
            "medidor nivel agua cisterna tinaco",
            "sensor agua app alarma wifi display valvula ultrasonico",
        ])
    elif intent == "gas":
        queries.extend([
            "sensor nivel gas tanque estacionario iot gassensor easy-gas connect-gas",
            "medidor gas app alarma display valvula",
        ])

    return _unique_keep_order(queries, limit=6)


def extract_product_specs(item: Dict[str, Any]) -> Dict[str, Any]:
    """Extrae especificaciones probables de un producto usando título, tags, handle y body."""
    raw = _raw_text(item)
    text = norm_text(raw)
    title = norm_text(item.get("title") or "")
    handle = norm_text(item.get("handle") or "")

    specs: Dict[str, Any] = {
        "text": text,
        "category": "general",
        "tv_ranges": [],
        "tv_exact_inches": [],
        "vesa_pairs": [],
        "max_weight_kg": None,
        "support_mount_types": [],
        "water_tank_types": [],
        "wireless_range_m": None,
        "water_depth_m": None,
        "features": {},
    }

    is_support = any(w in text for w in ["soporte", "bracket", "mount", "montaje"]) and any(w in text for w in ["pantalla", "tv", "televisor", "television", "monitor", "vesa"])
    is_water = any(w in text for w in ["agua", "water", "tinaco", "cisterna", "nivel de liquido", "nivel liquido"])
    is_gas = any(w in text for w in ["gas", "gassensor", "tanque estacionario", "lp"])

    if is_support:
        specs["category"] = "support"
    elif is_water and not is_gas:
        specs["category"] = "water"
    elif is_gas and not is_water:
        specs["category"] = "gas"

    # Rango de pulgadas para soportes. Se limita a 13-120 para evitar capacidades/metros.
    ranges: List[Tuple[Optional[int], int]] = []
    for pat in [
        r"\b(?:de\s*)?(1[3-9]|[2-9]\d|10\d|11\d|120)\s*(?:\"|pulgadas?|pulg\.?|plg\.?)?\s*(?:-|–|—|a|hasta)\s*(1[3-9]|[2-9]\d|10\d|11\d|120)\s*(?:\"|pulgadas?|pulg\.?|plg\.?)",
        r"\b(?:pantallas?|tv|televisores?|monitores?)\D{0,18}(1[3-9]|[2-9]\d|10\d|11\d|120)\s*(?:-|–|—|a|hasta)\s*(1[3-9]|[2-9]\d|10\d|11\d|120)\b",
    ]:
        for m in re.finditer(pat, text):
            a, b = int(m.group(1)), int(m.group(2))
            lo, hi = min(a, b), max(a, b)
            if 13 <= lo <= 120 and 13 <= hi <= 120 and hi - lo <= 100:
                ranges.append((lo, hi))
    for m in re.finditer(r"\b(?:hasta|up to)\s*(1[3-9]|[2-9]\d|10\d|11\d|120)\s*(?:\"|pulgadas?|pulg\.?|plg\.?)", text):
        ranges.append((None, int(m.group(1))))
    exact_inches = []
    if not ranges and specs["category"] == "support":
        for m in re.finditer(r"\b(1[3-9]|[2-9]\d|10\d|11\d|120)\s*(?:\"|pulgadas?|pulg\.?|plg\.?)\b", text):
            exact_inches.append(int(m.group(1)))
    specs["tv_ranges"] = _dedupe_ranges(ranges)
    specs["tv_exact_inches"] = sorted(set(exact_inches))[:8]

    # VESA: medidas típicas >= 50. Sólo tiene sentido en productos de soporte.
    if specs["category"] == "support":
        pairs: List[NumberPair] = []
        for m in re.finditer(r"\b(\d{2,4})\s*x\s*(\d{2,4})\b", text):
            a, b = int(m.group(1)), int(m.group(2))
            around = text[max(0, m.start() - 24):m.end() + 24]
            if 50 <= a <= 1000 and 50 <= b <= 1000:
                if "vesa" in around or any(w in text for w in ["soporte", "pantalla", "tv"]):
                    pairs.append((a, b))
        specs["vesa_pairs"] = _dedupe_pairs(pairs)

    # Peso máximo.
    kg_values: List[float] = []
    for m in re.finditer(r"\b(\d{1,3}(?:[,.]\d+)?)\s*(?:kg|kilogramos?)\b", text):
        val = _to_float_number(m.group(1))
        if val and 1 <= val <= 300:
            kg_values.append(val)
    if kg_values:
        specs["max_weight_kg"] = max(kg_values)

    mount_types = []
    for key, aliases in {
        "techo": ["techo", "ceiling"],
        "pared": ["pared", "muro", "wall"],
        "piso": ["piso", "movil", "mobile", "floor"],
        "brazo": ["brazo", "articulado", "articulable", "extendible"],
        "esquina": ["esquina", "corner"],
        "fijo": ["fijo", "fixed"],
        "inclinable": ["inclinable", "tilt", "tilting"],
    }.items():
        if any(a in text for a in aliases):
            mount_types.append(key)
    specs["support_mount_types"] = _unique_keep_order(mount_types)

    tank_types = []
    if "cisterna" in text or "cisternas" in text:
        tank_types.append("cisterna")
    if "tinaco" in text or "tinacos" in text:
        tank_types.append("tinaco")
    if "tanque de agua" in text:
        tank_types.append("tanque de agua")
    specs["water_tank_types"] = _unique_keep_order(tank_types)

    # Alcance inalámbrico / distancia. Tomamos el máximo mencionado en metros si el producto es agua/gas.
    meters: List[float] = []
    for m in re.finditer(r"\b(\d{1,4}(?:[,.]\d+)?)\s*(?:m|metros?)\b", text):
        val = _to_float_number(m.group(1))
        if val and 1 <= val <= 2000:
            meters.append(val)
    if meters:
        specs["wireless_range_m"] = max(meters)

    specs["features"] = {
        "wifi": any(w in text for w in ["wifi", "wi-fi", "app", "master iot", "alexa", "google home"]),
        "bluetooth": "bluetooth" in text or "ble" in text,
        # En soportes, "pantalla" describe la TV, no un display del producto.
        "display": specs["category"] != "support" and any(w in text for w in ["pantalla", "display", "lcd"]),
        "alarm": any(w in text for w in ["alarma", "alerta", "notificacion", "notificaciones"]),
        "valve": any(w in text for w in ["valvula", "electrovalvula", "cierre"]),
        "ultrasonic": any(w in text for w in ["ultrasonico", "ultrasonica", "ultra"]),
        "pressure": "presion" in text,
        "wireless": any(w in text for w in ["inalambrico", "inalambrica", "radiofrecuencia", "rf"]),
    }

    # Overrides ligeros por familias/SKU cuando la ficha no trae todas las palabras.
    family = f"{handle} {title}"
    if "easy-water" in family:
        specs["features"].update({"bluetooth": True, "display": True})
        if "cisterna" not in specs["water_tank_types"]:
            specs["water_tank_types"].append("cisterna")
        if "tinaco" not in specs["water_tank_types"]:
            specs["water_tank_types"].append("tinaco")
    if "connect-water" in family or "iot-water" in family:
        specs["features"].update({"wifi": True, "alarm": True})
    if "waterultra" in family:
        specs["features"]["ultrasonic"] = True
    if "waterv" in family:
        specs["features"]["valve"] = True
    if "easy-gas" in family:
        specs["features"].update({"display": True})
    if "connect-gas" in family or "gassensor" in family:
        specs["features"].update({"wifi": True, "alarm": True})

    return specs


def _dedupe_ranges(ranges: Iterable[Tuple[Optional[int], int]]) -> List[Tuple[Optional[int], int]]:
    seen = set()
    out: List[Tuple[Optional[int], int]] = []
    for lo, hi in ranges:
        key = (lo, hi)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out[:8]


def _dedupe_pairs(pairs: Iterable[NumberPair]) -> List[NumberPair]:
    seen = set()
    out: List[NumberPair] = []
    for a, b in pairs:
        key = (int(a), int(b))
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out[:12]


def _range_contains(range_pair: Tuple[Optional[int], int], inches: int) -> bool:
    lo, hi = range_pair
    if lo is None:
        return inches <= hi
    return lo <= inches <= hi


def _range_label(range_pair: Tuple[Optional[int], int]) -> str:
    lo, hi = range_pair
    return f"hasta {hi}\"" if lo is None else f"{lo}\" a {hi}\""


def _vesa_pair_supports(product_pair: NumberPair, requested: NumberPair) -> bool:
    pa, pb = product_pair
    ra, rb = requested
    return (pa >= ra and pb >= rb) or (pa >= rb and pb >= ra)


def apply_catalog_intelligence(query: str, items: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Ordena/filtra candidatos y agrega razones de compatibilidad."""
    analysis = analyze_query(query)
    if not items:
        return [], {"analysis": analysis, "filtered_out": 0, "notes": []}

    intent = analysis.get("intent")
    scored: List[Tuple[int, int, Dict[str, Any]]] = []
    filtered_out = 0
    notes: List[str] = []

    for idx, original in enumerate(items):
        item = copy.deepcopy(original)
        specs = extract_product_specs(item)
        score = max(0, 50 - idx)  # conserva parte del ranking original
        reasons: List[str] = []
        warnings: List[str] = []
        hard_incompatible = False

        if intent == "support":
            if specs["category"] != "support":
                score -= 350
                hard_incompatible = True
            else:
                score += 120

            inches = analysis.get("tv_inches")
            if inches:
                ranges = specs.get("tv_ranges") or []
                exacts = specs.get("tv_exact_inches") or []
                compatible_ranges = [r for r in ranges if _range_contains(r, int(inches))]
                if compatible_ranges:
                    score += 520
                    reasons.append(f"Compatible con pantalla de {inches}\" ({_range_label(compatible_ranges[0])})")
                elif ranges:
                    max_hi = max((r[1] for r in ranges), default=0)
                    min_lo_values = [r[0] for r in ranges if r[0] is not None]
                    min_lo = min(min_lo_values) if min_lo_values else None
                    if max_hi < inches:
                        score -= 900
                        hard_incompatible = True
                        warnings.append(f"Rango menor al solicitado: máximo {max_hi}\"")
                    elif min_lo and min_lo > inches:
                        score -= 320
                        warnings.append(f"Rango inicia en {min_lo}\", mayor a {inches}\"")
                elif inches in exacts:
                    score += 260
                    reasons.append(f"Menciona {inches}\" en sus especificaciones")
                else:
                    warnings.append("No encontré rango de pulgadas en la ficha; confirma medidas antes de comprar")
                    score += 10

            requested_vesa = analysis.get("vesa")
            if requested_vesa:
                pairs = specs.get("vesa_pairs") or []
                vesa_label = f"{requested_vesa[0]}x{requested_vesa[1]}"
                if requested_vesa in pairs or (requested_vesa[1], requested_vesa[0]) in pairs:
                    score += 520
                    reasons.append(f"VESA {vesa_label} compatible")
                elif pairs and any(_vesa_pair_supports(pair, requested_vesa) for pair in pairs):
                    score += 250
                    best = next(pair for pair in pairs if _vesa_pair_supports(pair, requested_vesa))
                    reasons.append(f"VESA hasta {best[0]}x{best[1]}; probable compatibilidad con {vesa_label}")
                elif pairs:
                    score -= 850
                    hard_incompatible = True
                    warnings.append(f"No coincide con VESA {vesa_label}")
                else:
                    warnings.append(f"No encontré VESA {vesa_label} en la ficha; confirmar antes de comprar")
                    score += 8

            mount = analysis.get("support_mount_type")
            if mount:
                if mount in specs.get("support_mount_types", []):
                    score += 170
                    reasons.append(f"Tipo de instalación: {mount}")
                elif specs.get("support_mount_types"):
                    score -= 80

        elif intent == "water":
            if specs["category"] == "water":
                score += 180
            elif specs["category"] == "gas":
                score -= 700
                hard_incompatible = True

            tank_type = analysis.get("tank_type")
            if tank_type:
                product_tanks = specs.get("water_tank_types") or []
                if tank_type in product_tanks:
                    score += 250
                    reasons.append(f"Apto para {tank_type}")
                elif product_tanks:
                    score += 60
                    warnings.append(f"La ficha menciona {', '.join(product_tanks)}, confirma uso en {tank_type}")
                else:
                    score += 30
                    warnings.append(f"Confirmar instalación en {tank_type}")

            if analysis.get("tank_liters"):
                liters = int(analysis["tank_liters"])
                reasons.append(f"Consulta para depósito de {liters:,} L".replace(",", ","))
                notes.append("Para cisternas o tinacos, la compatibilidad depende más de la altura/profundidad y del tipo de sensor que de los litros totales.")

            wants = analysis.get("wants") or {}
            feature_labels = {
                "wifi": "monitoreo por WiFi/app",
                "without_wifi": "operación sin WiFi",
                "display": "pantalla/display",
                "alarm": "alertas/alarma",
                "valve": "válvula/cierre automático",
                "ultrasonic": "medición ultrasónica",
                "pressure": "medición por presión",
                "bluetooth": "Bluetooth",
                "wireless": "comunicación inalámbrica",
            }
            features = specs.get("features") or {}
            for key, label in feature_labels.items():
                if not wants.get(key):
                    continue
                if key == "without_wifi":
                    if not features.get("wifi"):
                        score += 120
                        reasons.append(label)
                    else:
                        score -= 80
                    continue
                if features.get(key):
                    score += 120
                    reasons.append(label)

            if specs.get("wireless_range_m") and specs["wireless_range_m"] >= 100:
                reasons.append(f"Alcance mencionado: {int(specs['wireless_range_m'])} m")
                score += 60

        elif intent == "gas":
            if specs["category"] == "gas":
                score += 160
            elif specs["category"] == "water":
                score -= 600
                hard_incompatible = True

            wants = analysis.get("wants") or {}
            features = specs.get("features") or {}
            for key, label in {
                "wifi": "monitoreo por WiFi/app",
                "display": "pantalla/display",
                "alarm": "alertas/alarma",
                "valve": "válvula/cierre automático",
                "bluetooth": "Bluetooth",
            }.items():
                if wants.get(key) and features.get(key):
                    score += 120
                    reasons.append(label)

        # Metadatos para el frontend y endpoints admin.
        item["compatibility"] = {
            "score": int(score),
            "reasons": _unique_keep_order(reasons, limit=5),
            "warnings": _unique_keep_order(warnings, limit=3),
            "specs_summary": _build_specs_summary(specs),
        }
        item["catalog_specs"] = {k: v for k, v in specs.items() if k not in {"text"}}

        if hard_incompatible and intent in {"support", "water", "gas"}:
            filtered_out += 1
            # No lo eliminamos de inmediato si el puntaje quedó aceptable por alguna coincidencia;
            # sí lo hundimos para que no aparezca salvo que no haya alternativas.
            score -= 500
        scored.append((score, idx, item))

    scored.sort(key=lambda x: (x[0], -x[1]), reverse=True)

    # Si hay filtros técnicos fuertes, ocultamos claramente incompatibles cuando existen suficientes opciones.
    technically_specific = analysis.get("tv_inches") or analysis.get("vesa") or analysis.get("tank_type") or analysis.get("tank_liters")
    if technically_specific:
        positive = [it for score, _, it in scored if score > 0]
        if positive:
            out = positive
        else:
            out = [it for _, _, it in scored]
    else:
        out = [it for _, _, it in scored]

    context = {
        "analysis": analysis,
        "filtered_out": filtered_out,
        "notes": _unique_keep_order(notes, limit=3),
        "technical_filter_applied": bool(technically_specific),
    }
    return out, context


def _build_specs_summary(specs: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    if specs.get("tv_ranges"):
        out.append("Pantallas " + ", ".join(_range_label(r) for r in specs["tv_ranges"][:2]))
    if specs.get("vesa_pairs"):
        pairs = specs["vesa_pairs"][:3]
        out.append("VESA " + ", ".join(f"{a}x{b}" for a, b in pairs))
    if specs.get("max_weight_kg"):
        out.append(f"Hasta {specs['max_weight_kg']:g} kg")
    if specs.get("support_mount_types"):
        out.append("Instalación " + ", ".join(specs["support_mount_types"][:2]))
    if specs.get("water_tank_types"):
        out.append("Uso en " + ", ".join(specs["water_tank_types"][:2]))
    if specs.get("wireless_range_m"):
        out.append(f"Alcance {int(specs['wireless_range_m'])} m")
    features = specs.get("features") or {}
    labels = []
    for key, label in [
        ("wifi", "WiFi/app"),
        ("bluetooth", "Bluetooth"),
        ("display", "display"),
        ("alarm", "alertas"),
        ("valve", "válvula"),
        ("ultrasonic", "ultrasónico"),
        ("pressure", "presión"),
        ("wireless", "inalámbrico"),
    ]:
        if features.get(key):
            labels.append(label)
    if labels:
        out.append(", ".join(labels[:4]))
    return _unique_keep_order(out, limit=5)


def build_catalog_answer(query: str, items: List[Dict[str, Any]], total_count: int, page: int, per_page: int, context: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Respuesta textual basada en la inteligencia técnica aplicada."""
    context = context or {}
    analysis = context.get("analysis") or analyze_query(query)
    intent = analysis.get("intent")
    if intent not in {"support", "water", "gas"}:
        return None

    top_reasons: List[str] = []
    for it in items[:3]:
        comp = it.get("compatibility") or {}
        top_reasons.extend(comp.get("reasons") or [])
    top_reasons = _unique_keep_order(top_reasons, limit=4)

    parts: List[str] = []
    if intent == "support":
        detail = []
        if analysis.get("tv_inches"):
            detail.append(f"pantalla de {analysis['tv_inches']}\"")
        if analysis.get("vesa"):
            a, b = analysis["vesa"]
            detail.append(f"VESA {a}x{b}")
        if analysis.get("support_mount_type"):
            detail.append(f"instalación de {analysis['support_mount_type']}")
        if detail:
            parts.append("Revisé la compatibilidad técnica para " + ", ".join(detail) + ".")
        else:
            parts.append("Revisé las opciones de soportes tomando en cuenta compatibilidad de pantalla y montaje.")
        if top_reasons:
            parts.append("Prioricé productos que indican: " + "; ".join(top_reasons[:3]) + ".")
        parts.append("Antes de comprar, confirma también el peso de tu pantalla y que el patrón VESA coincida con la ficha del producto.")

    elif intent == "water":
        if analysis.get("tank_type") or analysis.get("tank_liters"):
            tank = analysis.get("tank_type") or "depósito"
            if analysis.get("tank_liters"):
                parts.append(f"Para una {tank} de {int(analysis['tank_liters']):,} L, filtré sensores relacionados con nivel de agua, tinaco/cisterna y monitoreo.".replace(",", ","))
            else:
                parts.append(f"Para {tank}, filtré sensores relacionados con nivel de agua y monitoreo.")
            parts.append("En estos casos los litros ayudan como referencia, pero el dato decisivo suele ser la altura/profundidad de medición y el tipo de instalación.")
        else:
            parts.append("Filtré opciones de sensores de agua según la aplicación solicitada.")
        if top_reasons:
            parts.append("Prioricé coincidencias como: " + "; ".join(top_reasons[:3]) + ".")
        parts.append("Si me confirmas profundidad, distancia entre sensor y receptor, y si necesitas app/WiFi o display local, puedo afinar más la recomendación.")

    elif intent == "gas":
        parts.append("Filtré opciones de sensores de gas evitando mezclarlas con productos de agua u otros sensores.")
        if top_reasons:
            parts.append("Prioricé coincidencias como: " + "; ".join(top_reasons[:3]) + ".")
        parts.append("Confirma si buscas sólo medición, alertas por app o cierre automático con válvula para elegir el modelo adecuado.")

    if total_count > per_page:
        shown = min(len(items), per_page)
        parts.append(f"Te muestro {shown} de {total_count} opciones ordenadas por coincidencia técnica.")
    elif total_count:
        parts.append(f"Encontré {total_count} opción(es) relevantes ordenadas por compatibilidad.")

    return " ".join(parts)
