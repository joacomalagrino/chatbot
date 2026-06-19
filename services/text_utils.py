"""Lógica pura de texto: normalización de teléfonos, parseo de JSON del modelo
y extracción de datos de contacto. Sin dependencias de red ni DB — solo stdlib,
para poder testearla de forma aislada."""
import json
import re

# Teléfono: empieza y termina en dígito, con dígitos/espacios/guiones/paréntesis en el medio.
# El rango {6,20} cubre números argentinos completos con separadores (+54 9 11 ....).
PHONE_RE = re.compile(r'(\+?\d[\d\s\-\(\)]{6,20}\d)')
EMAIL_RE = re.compile(r'[\w\.\-]+@[\w\.\-]+\.\w+')
# Handle de IG: el '@' NO puede venir precedido por letra/dígito/punto (así no matchea emails).
IG_HANDLE_RE = re.compile(r'(?<![\w.])@([A-Za-z0-9_.]{2,30})')

# Un teléfono válido para nosotros tiene entre 10 y 15 dígitos (descarta DNIs/años/montos).
MIN_PHONE_DIGITS = 10
MAX_PHONE_DIGITS = 15


def normalize_ar_whatsapp(phone: str) -> str:
    """Normaliza un número para la API de WhatsApp de Meta.

    El webhook entrega los celulares argentinos como 549XXXXXXXXX (13 dígitos),
    pero la API espera 54XXXXXXXXX (12 dígitos, sin el 9). Quita '+' y espacios.
    No toca números de otros países ni los ya normalizados. Es idempotente.
    """
    to = (phone or "").replace("+", "").replace(" ", "")
    if to.startswith("549") and len(to) == 13:
        to = "54" + to[3:]
    return to


def parse_model_json(raw: str) -> dict:
    """Parsea la respuesta JSON de un modelo, tolerando markdown fencing (```).

    Siempre devuelve un dict; ante error devuelve {'error':..., 'raw':...}.
    Nunca lanza excepción.
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        parts = text.split("```", 2)
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        result = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {"error": "No se pudo parsear la respuesta del modelo", "raw": raw}
    if not isinstance(result, dict):
        return {"error": "El modelo no devolvió un objeto JSON", "raw": raw}
    return result


def extract_contact(text: str) -> dict:
    """Extrae teléfono, email e instagram de un mensaje de texto libre.

    Devuelve {'phone': str|None, 'email': str|None, 'instagram': str|None}.
    Primero detecta y remueve los emails para que el '@' del email no se
    confunda con un handle de Instagram. El teléfono se normaliza a solo
    dígitos y se valida el largo.
    """
    text = text or ""
    out = {"phone": None, "email": None, "instagram": None}

    emails = EMAIL_RE.findall(text)
    if emails:
        out["email"] = emails[0]

    # Remover los emails antes de buscar IG y teléfono (evita falsos positivos).
    cleaned = EMAIL_RE.sub(" ", text)

    handles = IG_HANDLE_RE.findall(cleaned)
    if handles:
        out["instagram"] = handles[0]

    for candidate in PHONE_RE.findall(cleaned):
        digits = re.sub(r'\D', '', candidate)
        if MIN_PHONE_DIGITS <= len(digits) <= MAX_PHONE_DIGITS:
            out["phone"] = digits
            break

    return out
