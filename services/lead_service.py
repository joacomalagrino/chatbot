from sqlalchemy.orm import Session

from models import Conversation, Lead
from services.notify import fire_hot_lead
from services.text_utils import extract_contact


def update_lead_from_message(db: Session, conversation: Conversation, user_message: str) -> bool:
    """Detecta datos de contacto en el mensaje del usuario y actualiza el Lead.

    Devuelve True si hubo cambios persistidos. Hace un único commit y solo
    actualiza el estado de la conversación cuando efectivamente cambió algo.
    """
    lead = conversation.lead
    if not lead:
        lead = Lead(conversation_id=conversation.id, project=conversation.project)
        db.add(lead)

    contact = extract_contact(user_message)
    changed = False

    # Adoptar el contacto que ya viene en la conversación (lo persiste el webhook:
    # contact_phone en WhatsApp, contact_instagram en IG) aunque el texto del mensaje
    # no traiga el dato. Sin esto el teléfono del lead de WhatsApp nunca llegaba a Lead.phone.
    if not lead.phone and conversation.contact_phone:
        lead.phone = conversation.contact_phone
        changed = True

    if not lead.instagram and conversation.contact_instagram:
        lead.instagram = conversation.contact_instagram
        changed = True

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

    was_hot = conversation.status == "hot"

    if conversation.status == "new":
        conversation.status = "warm"
        lead.status = "contacted"

    if lead.phone and (lead.email or lead.instagram):
        # Conversación "caliente" (teléfono + email/instagram): el lead pasa a calificado.
        # Ambas máquinas de estado se mueven juntas; antes el lead se quedaba en "contacted".
        conversation.status = "hot"
        lead.status = "qualified"

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
