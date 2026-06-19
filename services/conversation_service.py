"""Lógica compartida de conversación, usada por el chat web y el webhook de Meta.

Centraliza: creación de conversación a prueba de race conditions, persistencia
del turno y armado del historial sin depender de timestamps."""
import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config import PROJECTS
from models import Conversation, Message
from services.claude_service import get_ai_response
from services.lead_service import update_lead_from_message

logger = logging.getLogger(__name__)


def get_or_create_conversation(
    db: Session, session_id: str, project: str, channel: str, **contacts
) -> Conversation:
    """Devuelve la conversación de `session_id`, creándola si no existe.

    Tolera race conditions: si dos requests crean la misma session_id en
    paralelo, el segundo captura el IntegrityError y relee la existente.
    """
    conversation = db.query(Conversation).filter_by(session_id=session_id).first()
    if conversation:
        return conversation

    conversation = Conversation(
        session_id=session_id, project=project, channel=channel, **contacts
    )
    db.add(conversation)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        conversation = db.query(Conversation).filter_by(session_id=session_id).first()
    else:
        db.refresh(conversation)
    return conversation


async def record_turn(db: Session, conversation: Conversation, text: str) -> str:
    """Persiste el mensaje del usuario, llama a Claude y persiste la respuesta.

    Devuelve el texto de la respuesta. El historial se arma filtrando por el id
    del mensaje recién creado (no por `[:-1]`), así no depende del orden por
    timestamp cuando dos mensajes comparten el mismo `created_at`.
    """
    user_msg = Message(conversation_id=conversation.id, role="user", content=text)
    db.add(user_msg)
    db.commit()
    db.refresh(conversation)

    history = [
        {"role": m.role, "content": m.content}
        for m in conversation.messages
        if m.id != user_msg.id
    ]

    response_text = await get_ai_response(
        conversation.project, PROJECTS[conversation.project], text, history
    )

    db.add(Message(conversation_id=conversation.id, role="assistant", content=response_text))
    db.commit()

    update_lead_from_message(db, conversation, text)
    return response_text
