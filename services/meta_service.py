import asyncio
import logging
import re

import httpx

from config import get_settings
from services.text_utils import normalize_ar_whatsapp

logger = logging.getLogger(__name__)
settings = get_settings()
META_API_BASE = "https://graph.facebook.com/v21.0"

# Los ids de Graph (leadgen_id, etc.) son numéricos. Validarlos antes de
# interpolarlos en la URL evita path traversal/inyección si la firma del
# webhook se relajara alguna vez (defensa en profundidad).
_GRAPH_ID_RE = re.compile(r"^[0-9]+$")

# Timeout granular: connect corto, read más largo (Graph puede tardar).
_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)
_RETRY_STATUSES = {429, 500, 502, 503, 504}

# Cliente reutilizable: evita rehacer el handshake TLS a graph.facebook.com en
# cada mensaje. Se cierra en el shutdown del app (main.lifespan).
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=_TIMEOUT)
    return _client


async def close_client():
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
        _client = None


def _error_summary(r: httpx.Response) -> str:
    """Resumen del error de Graph SIN volcar el body (puede traer PII del lead)."""
    try:
        err = r.json().get("error", {})
        return f"code={err.get('code')} type={err.get('type')}"
    except Exception:
        return "sin detalle"


async def _post_with_retry(url: str, payload: dict, headers: dict, attempts: int = 3) -> dict:
    """POST con reintentos y backoff exponencial para errores transitorios (429/5xx/red)."""
    client = _get_client()
    for i in range(attempts):
        last = i == attempts - 1
        try:
            r = await client.post(url, json=payload, headers=headers)
        except (httpx.TransportError, httpx.TimeoutException):
            if last:
                logger.exception("Meta POST falló por red/timeout en %s", url)
                raise
            await asyncio.sleep(min(2 ** i, 8))
            continue

        if r.status_code in _RETRY_STATUSES and not last:
            logger.warning("Meta %s transitorio (%s), reintento", url, r.status_code)
            await asyncio.sleep(min(2 ** i, 8))
            continue

        if not r.is_success:
            # No logueamos r.text: puede contener datos del destinatario/lead.
            logger.error("Meta API error %s en %s (%s)", r.status_code, url, _error_summary(r))
        r.raise_for_status()
        return r.json()

    raise RuntimeError("unreachable")  # pragma: no cover


async def send_whatsapp_message(phone: str, text: str) -> dict:
    url = f"{META_API_BASE}/{settings.meta_whatsapp_phone_id}/messages"
    headers = {"Authorization": f"Bearer {settings.meta_access_token}"}
    payload = {
        "messaging_product": "whatsapp",
        "to": normalize_ar_whatsapp(phone),
        "type": "text",
        "text": {"body": text},
    }
    return await _post_with_retry(url, payload, headers)


async def send_instagram_message(instagram_user_id: str, text: str) -> dict:
    url = f"{META_API_BASE}/{settings.meta_instagram_account_id}/messages"
    headers = {"Authorization": f"Bearer {settings.meta_access_token}"}
    payload = {
        "messaging_product": "instagram",
        "recipient": {"id": instagram_user_id},
        "message": {"text": text},
    }
    return await _post_with_retry(url, payload, headers)


async def get_lead_data(leadgen_id: str) -> dict:
    """Trae una submission de Lead Ads por su leadgen_id.

    Requiere el permiso `leads_retrieval`. El token va por header Authorization.
    Ante error NO logueamos el body (trae field_data = nombre/teléfono/email del lead).
    """
    if not _GRAPH_ID_RE.match(str(leadgen_id)):
        logger.error("leadgen_id inválido (no numérico), abortando fetch")
        raise ValueError("leadgen_id inválido")
    url = f"{META_API_BASE}/{leadgen_id}"
    headers = {"Authorization": f"Bearer {settings.meta_access_token}"}
    params = {"fields": "id,created_time,field_data,form_id,ad_id,campaign_id"}
    client = _get_client()
    r = await client.get(url, params=params, headers=headers)
    if not r.is_success:
        logger.error("Meta lead fetch error %s (%s)", r.status_code, _error_summary(r))
    r.raise_for_status()
    return r.json()


def parse_lead_fields(field_data: list) -> dict:
    """Aplana el `field_data` de Graph en {name: primer_valor}.

    Los nombres dependen del formulario: full_name, email, phone_number, etc.
    """
    out = {}
    for field in field_data or []:
        name = field.get("name")
        values = field.get("values") or []
        if name and values:
            out[name] = values[0]
    return out
