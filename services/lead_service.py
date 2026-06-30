from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import Conversation, Lead
from services.notify import fire_hot_lead
from services.text_utils import extract_contact


def _apply_contact(conversation: Conversation, lead: Lead, contact: dict) -> bool:
    """Vuelca los datos de contacto detectados sobre el Lead/Conversation, sin pisar lo
    ya cargado (solo rellena lo vacío). Devuelve True si cambió algo. No commitea."""
    changed = False

    if contact["phone"] and not lead.phone:
        lead.phone = contact["phone"]
        conversation.contact_phone = lead.phone
        changed = True

    if contact["email"] and not lead.email:
        lead.email = contact["email"]
        conversation.contact_email = lead.email
        changed = True

    if contact["instagram"] and not lead.instagram:
        lead.instagram = contact["instagram"]
        conversation.contact_instagram = lead.instagram
        changed = True

    if not changed:
        return False

    if conversation.status == "new":
        conversation.status = "warm"
        lead.status = "contacted"

    if lead.phone and (lead.email or lead.instagram):
        # Conversación "caliente" (teléfono + email/instagram): el lead pasa a calificado.
        # Ambas máquinas de estado se mueven juntas; antes el lead se quedaba en "contacted".
        conversation.status = "hot"
        lead.status = "qualified"

    return True


def update_lead_from_message(db: Session, conversation: Conversation, user_message: str) -> bool:
    """Detecta datos de contacto en el mensaje del usuario y actualiza el Lead.

    Devuelve True si hubo cambios persistidos. Hace un único commit y solo
    actualiza el estado de la conversación cuando efectivamente cambió algo.

    Tolera la race de creación concurrente del Lead: dos turnos casi simultáneos de la
    MISMA conversación (p.ej. dos mensajes de WhatsApp seguidos, cada uno en su propia
    sesión de _process_event) pueden ver `conversation.lead is None` a la vez y ambos
    crear `Lead(conversation_id=...)`. leads.conversation_id es UNIQUE, así que el segundo
    commit reventaba con un IntegrityError NO manejado: el turno perdedor tiraba su merge
    de contacto (y en el chat web, un 500). Acá, ante ese choque, releemos el Lead que ganó
    y reaplicamos el contacto sobre él, espejando a get_or_create_conversation.
    """
    contact = extract_contact(user_message)
    was_hot = conversation.status == "hot"

    lead = conversation.lead
    created = lead is None
    if created:
        lead = Lead(conversation_id=conversation.id, project=conversation.project)
        db.add(lead)

    if not _apply_contact(conversation, lead, contact):
        return False

    try:
        db.commit()
    except IntegrityError:
        if not created:
            # No fue la race del create (el Lead ya existía): no sabemos reconciliarlo,
            # re-lanzamos para que el caller/observabilidad lo vea.
            db.rollback()
            raise
        # Otro turno creó el Lead de esta conversación en paralelo: descartamos nuestro
        # INSERT, releemos el Lead ganador y reaplicamos el contacto sobre él.
        db.rollback()
        lead = db.query(Lead).filter_by(conversation_id=conversation.id).first()
        if lead is None:
            # No debería pasar (el IntegrityError implica que existe), pero si bajo cierto
            # aislamiento aún no es visible, mejor fallar ruidoso que tragarlo en silencio.
            raise
        if not _apply_contact(conversation, lead, contact):
            # El Lead ganador ya tenía estos datos: nada que persistir, pero sí persiste
            # cualquier cambio de status de la conversación que _apply_contact dejó dirty.
            db.commit()
            return False
        db.commit()

    # Notificar al equipo SOLO en la transición a caliente (no en cada mensaje posterior).
    if not was_hot and conversation.status == "hot":
        fire_hot_lead({
            "project": conversation.project,
            "channel": conversation.channel,
            "name": lead.name,
            "phone": lead.phone,
            "email": lead.email,
            "instagram": lead.instagram,
        })

    return True
