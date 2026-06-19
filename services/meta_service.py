import asyncio
import logging

import httpx

from config import get_settings
from services.text_utils import normalize_ar_whatsapp

logger = logging.getLogger(__name__)
settings = get_settings()
META_API_BASE = "https://graph.facebook.com/v21.0"

# Timeout granular: connect corto, read más largo (Graph puede tardar).
_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)
_RETRY_STATUSES = {429, 500, 502, 503, 504}


async def _post_with_retry(url: str, payload: dict, headers: dict, attempts: int = 3) -> dict:
    """POST con reintentos y backoff exponencial para errores transitorios (429/5xx/red)."""
    for i in range(attempts):
        last = i == attempts - 1
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.post(url, json=payload, headers=headers)
        except (httpx.TransportError, httpx.TimeoutException):
            if last:
                logger.exception("Meta POST %s falló por red/timeout", url)
                raise
            await asyncio.sleep(min(2 ** i, 8))
            continue

        if r.status_code in _RETRY_STATUSES and not last:
            logger.warning("Meta %s transitorio (%s), reintento", url, r.status_code)
            await asyncio.sleep(min(2 ** i, 8))
            continue

        if not r.is_success:
            logger.error("Meta API error %s: %s", r.status_code, r.text)
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

    Requiere el permiso `leads_retrieval`. El token va por header Authorization
    (no como query param, para que no quede en logs de acceso/proxies).
    """
    url = f"{META_API_BASE}/{leadgen_id}"
    headers = {"Authorization": f"Bearer {settings.meta_access_token}"}
    params = {"fields": "id,created_time,field_data,form_id,ad_id,campaign_id"}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.get(url, params=params, headers=headers)
        if not r.is_success:
            logger.error("Meta lead fetch error %s: %s", r.status_code, r.text)
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
