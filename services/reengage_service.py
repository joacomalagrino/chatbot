"""Re-engagement proactivo de leads de WhatsApp fuera de la ventana de 24h.

Cuando la ventana de servicio de 24h se cierra, Graph rechaza el free-form: para volver a
escribirle a un lead que se enfrió hay que mandar una PLANTILLA aprobada en Meta. Este
servicio:

1. Selecciona los leads ELEGIBLES (ventana cerrada o por cerrarse, sin re-enganchar antes,
   sin opt-out, con teléfono).
2. Les manda la plantilla configurada vía meta_service.send_whatsapp_template.
3. Marca conversations.reengaged_at para no re-mandarles (idempotencia).

SCAFFOLD apagado por DEFAULT: el doble gate (REENGAGE_ENABLED + plantilla cargada) hace que
run_reengagement sea un NO-OP seguro hasta que el usuario cargue una plantilla aprobada en
Meta y prenda el flag. Nunca se manda nada sin esas dos condiciones.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session

from config import get_settings
from models import Conversation
from observability import record_error
from services.meta_service import is_within_24h_window, send_whatsapp_template

logger = logging.getLogger(__name__)
settings = get_settings()

# Solo el canal de WhatsApp tiene ventana de 24h / plantillas. Instagram y web quedan fuera.
_WHATSAPP = "whatsapp"


def _utcnow() -> datetime:
    """Naive UTC, igual que las columnas DateTime del modelo."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def find_reengageable_conversations(
    db: Session,
    now: datetime | None = None,
    closing_within: timedelta | None = None,
    limit: int = 100,
) -> list[Conversation]:
    """Devuelve las conversaciones de WhatsApp ELEGIBLES para re-engagement.

    Criterios:
      - canal WhatsApp y con teléfono de contacto (sin teléfono no hay a quién mandarle),
      - ventana de 24h ya cerrada — o por cerrarse, si se pasa `closing_within` (margen
        configurable: incluye también las que cierran dentro de ese plazo),
      - NO re-enganchadas antes (reengaged_at IS NULL) → idempotencia,
      - sin opt-out (reengage_opt_out distinto de True).

    `now` es inyectable para los tests. `limit` acota el batch por corrida.
    """
    now = now or _utcnow()

    # Filtros baratos en SQL (canal/teléfono/idempotencia/opt-out). La ventana se evalúa en
    # Python con is_within_24h_window para reusar EXACTAMENTE la misma lógica que el resto
    # (incluido last_inbound_at IS NULL = cerrada).
    candidates = (
        db.query(Conversation)
        .filter(
            Conversation.channel == _WHATSAPP,
            Conversation.contact_phone.isnot(None),
            Conversation.contact_phone != "",
            Conversation.reengaged_at.is_(None),
            or_(
                Conversation.reengage_opt_out.is_(None),
                Conversation.reengage_opt_out.is_(False),
            ),
        )
        .order_by(Conversation.last_inbound_at.asc())
        .all()
    )

    # `closing_within`: además de las ya cerradas, incluir las que cerrarán dentro de ese
    # margen. Se simula adelantando el reloj: si la ventana estará cerrada en (now + margen),
    # ya es elegible. Sin margen, solo cuentan las que están cerradas AHORA.
    horizon = now + closing_within if closing_within else now

    eligible = [
        c for c in candidates if not is_within_24h_window(c.last_inbound_at, now=horizon)
    ]
    return eligible[:limit]


async def run_reengagement(
    db: Session,
    now: datetime | None = None,
    closing_within: timedelta | None = None,
    limit: int = 100,
) -> dict:
    """Corre una pasada de re-engagement: selecciona elegibles, manda la plantilla y marca.

    NO-OP seguro si el re-engagement no está activo (flag apagado o sin plantilla): no toca
    la DB ni la red, devuelve un resumen con skipped="disabled". Devuelve siempre un dict con
    el conteo de selected/sent/failed para que el caller (endpoint admin / cron) lo loguee.
    """
    if not settings.reengage_active():
        logger.info(
            "Re-engagement deshabilitado (REENGAGE_ENABLED=%s, plantilla=%r): no-op",
            settings.reengage_enabled,
            settings.reengage_template(),
        )
        return {"skipped": "disabled", "selected": 0, "sent": 0, "failed": 0}

    template = settings.reengage_template()
    lang = settings.whatsapp_reengage_template_lang
    now = now or _utcnow()

    convs = find_reengageable_conversations(
        db, now=now, closing_within=closing_within, limit=limit
    )
    sent = 0
    failed = 0
    for conv in convs:
        try:
            await send_whatsapp_template(conv.contact_phone, template, lang_code=lang)
        except Exception as exc:
            # Un fallo en un lead NO debe frenar al resto del batch ni quedar silencioso.
            failed += 1
            logger.warning("Re-engagement falló para conversación %s", conv.id)
            record_error("reengage_service.send", exc, conversation_id=str(conv.id))
            continue
        # Marcar reengaged_at SOLO tras el envío exitoso (idempotencia): si falló, queda
        # elegible para el próximo intento. Commit por lead para no perder los ya enviados
        # si el proceso se corta a mitad de batch.
        conv.reengaged_at = now
        db.commit()
        sent += 1

    result = {"skipped": None, "selected": len(convs), "sent": sent, "failed": failed}
    logger.info("Re-engagement: %s", result)
    return result
