"""Notificación de leads calientes al equipo (best-effort).

Siempre loguea cuando entra un lead caliente. Si `NOTIFY_WEBHOOK_URL` está configurado
(Slack/Discord/Make/etc.), además le hace POST. Nunca bloquea el flujo ni propaga errores:
si la notificación falla, el lead ya quedó guardado igual."""
import asyncio
import logging

import httpx

from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Referencias fuertes a las tareas fire-and-forget en vuelo. asyncio.create_task solo
# guarda una referencia DÉBIL: sin esto el GC puede recolectar (y cancelar) la tarea
# antes de que termine, y la alerta de lead caliente nunca se enviaría.
_pending_tasks: set = set()


def _format(s: dict) -> str:
    contacto = s.get("phone") or s.get("email") or s.get("instagram") or "s/contacto"
    return f"🔥 Lead caliente ({s.get('project')}): {s.get('name') or 's/nombre'} · {contacto}"


async def _post(url: str, payload: dict) -> None:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(url, json=payload)
    except Exception:
        logger.exception("No se pudo enviar la notificación de lead caliente")


def fire_hot_lead(summary: dict) -> None:
    """Dispara la notificación. Loguea sin volcar PII de más; el detalle va al webhook."""
    logger.info("LEAD CALIENTE project=%s canal=%s", summary.get("project"), summary.get("channel"))
    url = settings.notify_webhook_url
    if not url:
        return
    payload = {"text": _format(summary), "lead": summary}
    try:
        # Si hay un event loop corriendo (camino async del webhook/chat), fire-and-forget.
        # Guardar referencia fuerte hasta que termine (ver _pending_tasks) para que el GC
        # no la cancele en medio.
        task = asyncio.get_running_loop().create_task(_post(url, payload))
        _pending_tasks.add(task)
        task.add_done_callback(_pending_tasks.discard)
    except RuntimeError:
        # Sin loop (camino sync/tests): POST inline best-effort.
        try:
            httpx.post(url, json=payload, timeout=5)
        except Exception:
            logger.exception("No se pudo enviar la notificación de lead caliente (sync)")
