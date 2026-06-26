import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

import httpx

from config import get_settings
from observability import record_error
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


async def _request_with_retry(
    method: str,
    url: str,
    headers: dict,
    attempts: int = 3,
    payload: dict | None = None,
    params: dict | None = None,
) -> dict:
    """Petición HTTP con reintentos y backoff exponencial para errores transitorios (429/5xx/red).

    Soporta GET y POST (y cualquier método que acepte httpx.AsyncClient.request).
    """
    client = _get_client()
    for i in range(attempts):
        last = i == attempts - 1
        try:
            r = await client.request(
                method,
                url,
                headers=headers,
                json=payload if method.upper() != "GET" else None,
                params=params if method.upper() == "GET" else None,
            )
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            if last:
                logger.exception("Meta %s falló por red/timeout en %s", method.upper(), url)
                # Fetch a Graph agotado (red/timeout): registrar para el panel. `url` lleva
                # solo el id de Graph en el path (numérico), sin PII del lead.
                record_error("meta_service.request", exc, method=method.upper(), url=url)
                raise
            await asyncio.sleep(min(2 ** i, 8))
            continue

        if r.status_code in _RETRY_STATUSES and not last:
            logger.warning("Meta %s transitorio (%s), reintento", url, r.status_code)
            await asyncio.sleep(min(2 ** i, 8))
            continue

        if not r.is_success:
            # No logueamos r.text: puede contener datos del destinatario/lead.
            summary = _error_summary(r)
            logger.error("Meta API error %s en %s (%s)", r.status_code, url, summary)
            record_error(
                "meta_service.request",
                method=method.upper(),
                url=url,
                status=r.status_code,
                summary=summary,
            )
        r.raise_for_status()
        return r.json()

    raise RuntimeError("unreachable")  # pragma: no cover


# Alias de compatibilidad para los call-sites existentes.
async def _post_with_retry(url: str, payload: dict, headers: dict, attempts: int = 3) -> dict:
    """POST con reintentos — delega en _request_with_retry."""
    return await _request_with_retry("POST", url, headers, attempts=attempts, payload=payload)


# Ventana de servicio de WhatsApp: Graph solo acepta free-form (type:text) dentro de
# las 24h posteriores al último inbound del usuario. Pasada esa ventana hay que mandar
# una plantilla (type:template) previamente aprobada en Meta.
WHATSAPP_WINDOW = timedelta(hours=24)


def is_within_24h_window(last_inbound_at: datetime | None, now: datetime | None = None) -> bool:
    """¿Sigue abierta la ventana de servicio de 24h?

    `last_inbound_at` es naive UTC (como las columnas DateTime del modelo). None
    (conversación sin inbound registrado, o anterior a esta feature) cuenta como
    ventana CERRADA: fail-safe hacia plantilla, nunca hacia un free-form que Graph
    rechazaría. `now` es inyectable para los tests."""
    if last_inbound_at is None:
        return False
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    return (now - last_inbound_at) < WHATSAPP_WINDOW


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


async def send_whatsapp_template(
    phone: str,
    template_name: str,
    lang_code: str = "es_AR",
    body_params: list[str] | None = None,
) -> dict:
    """Manda un mensaje de PLANTILLA (type:template) por la Graph API.

    Las plantillas se crean y aprueban en Meta (WhatsApp Manager) — eso es tarea del
    usuario, fuera de este código. Acá solo armamos el payload con el `template_name`
    aprobado y, opcionalmente, los parámetros que llenan los placeholders {{1}}, {{2}}…
    del body de la plantilla.

    A diferencia del free-form, una plantilla aprobada SÍ se puede enviar fuera de la
    ventana de 24h: es lo que permite re-enganchar a un lead que se enfrió."""
    url = f"{META_API_BASE}/{settings.meta_whatsapp_phone_id}/messages"
    headers = {"Authorization": f"Bearer {settings.meta_access_token}"}
    template: dict = {
        "name": template_name,
        "language": {"code": lang_code},
    }
    if body_params:
        template["components"] = [{
            "type": "body",
            "parameters": [{"type": "text", "text": str(p)} for p in body_params],
        }]
    payload = {
        "messaging_product": "whatsapp",
        "to": normalize_ar_whatsapp(phone),
        "type": "template",
        "template": template,
    }
    return await _post_with_retry(url, payload, headers)


async def send_whatsapp_reply(
    phone: str, text: str, last_inbound_at: datetime | None
) -> dict | None:
    """Envía la respuesta de WhatsApp eligiendo el tipo según la ventana de 24h.

    - Ventana ABIERTA → free-form (type:text) con el texto generado por el bot.
    - Ventana CERRADA → plantilla de re-engagement (WHATSAPP_REENGAGE_TEMPLATE).

    Si la ventana cerró y NO hay plantilla configurada, NO se manda un free-form (Graph
    lo rechazaría con error y se perdería igual): se loguea y se devuelve None para que
    el caller lo trate como no-entregado. Cuando la plantilla está configurada, el texto
    del bot se pasa como primer parámetro {{1}} por si la plantilla aprobada en Meta tiene
    un placeholder de cuerpo."""
    if is_within_24h_window(last_inbound_at):
        return await send_whatsapp_message(phone, text)

    template = settings.whatsapp_reengage_template
    if not template:
        logger.warning(
            "Ventana de 24h cerrada y WHATSAPP_REENGAGE_TEMPLATE sin configurar: "
            "no se puede reabrir la conversación, mensaje omitido"
        )
        return None
    return await send_whatsapp_template(
        phone,
        template,
        lang_code=settings.whatsapp_reengage_template_lang,
        body_params=[text],
    )


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
    return await _request_with_retry("GET", url, headers, params=params)


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
