"""Lógica compartida de conversación, usada por el chat web y el webhook de Meta.

Centraliza: creación de conversación a prueba de race conditions, persistencia
del turno y armado del historial sin depender de timestamps."""
import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config import PROJECTS
from models import Conversation, Message
from services.claude_service import get_ai_response, stream_ai_response
from services.lead_service import update_lead_from_message

logger = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 4000     # cota del mensaje del usuario (el widget ya lo hace a 2000; el webhook no)
MAX_HISTORY_MESSAGES = 40    # cuántos mensajes traer para el historial (claude_service recorta a 20)


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
        # Otro request creó la misma session_id en paralelo: releemos la suya.
        db.rollback()
        conversation = db.query(Conversation).filter_by(session_id=session_id).first()
        if conversation is None:
            # No debería pasar (el IntegrityError implica que existe), pero bajo
            # ciertos niveles de aislamiento la fila podría no ser visible aún.
            # Mejor fallar ruidoso que devolver None y reventar aguas abajo.
            raise RuntimeError(
                f"Conversación {session_id} no encontrada tras IntegrityError"
            )
    else:
        db.refresh(conversation)
    return conversation


async def record_turn(db: Session, conversation: Conversation, text: str) -> str:
    """Persiste el mensaje del usuario, llama a Claude y persiste la respuesta.

    Devuelve el texto de la respuesta. El historial se arma filtrando por el id
    del mensaje recién creado (no por `[:-1]`), así no depende del orden por
    timestamp cuando dos mensajes comparten el mismo `created_at`.
    """
    text = (text or "")[:MAX_MESSAGE_CHARS]   # cota en el chokepoint: cubre el webhook (que no capaba)
    user_msg = Message(conversation_id=conversation.id, role="user", content=text)
    db.add(user_msg)
    db.commit()
    db.refresh(conversation)

    # Traer SOLO los últimos N mensajes (claude_service recorta a 20): evita cargar TODA la
    # conversación —que en WhatsApp persiste indefinidamente— para descartar casi todo.
    recent = (
        db.query(Message)
        .filter(Message.conversation_id == conversation.id, Message.id != user_msg.id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(MAX_HISTORY_MESSAGES)
        .all()
    )
    recent.reverse()
    history = [{"role": m.role, "content": m.content} for m in recent]

    # Capturar lo que necesita el await en locales y SOLTAR la conexión del pool antes de
    # llamar a Claude: sin esto la conexión quedaba checked-out e idle-in-transaction durante
    # todo el await (hasta ~60s: timeout 20s × 2 reintentos), así que ~30 turnos concurrentes
    # en una ráfaga agotaban el pool (10+20) y el resto moría con "QueuePool limit reached"
    # —la señal #1 que la app instrumenta, y que además quemaba claims del webhook—. El
    # trabajo de DB del turno es de milisegundos; retener la conexión durante la latencia de
    # Claude era una saturación autoinfligida. db.commit() cierra la transacción de lectura y
    # devuelve la conexión al pool; el write posterior re-checkoutea por unos ms.
    project = conversation.project
    conversation_id = conversation.id
    db.commit()

    response_text = await get_ai_response(project, PROJECTS[project], text, history)

    db.add(Message(conversation_id=conversation_id, role="assistant", content=response_text))
    db.commit()

    update_lead_from_message(db, conversation, text)
    return response_text


async def stream_turn(db: Session, conversation: Conversation, text: str):
    """Variante streaming de record_turn: async generator que yieldea cada delta de la
    respuesta a medida que Claude la genera.

    Persiste el mensaje del usuario igual que record_turn (cap de chars + history filtrado
    por id), itera stream_ai_response acumulando el texto completo, y recién cuando el stream
    termina persiste el Message del asistente con el texto completo y actualiza el lead. NO
    modifica record_turn (que sigue para el webhook y el /chat no-streaming)."""
    text = (text or "")[:MAX_MESSAGE_CHARS]   # mismo cap en el chokepoint que record_turn
    user_msg = Message(conversation_id=conversation.id, role="user", content=text)
    db.add(user_msg)
    db.commit()
    db.refresh(conversation)

    recent = (
        db.query(Message)
        .filter(Message.conversation_id == conversation.id, Message.id != user_msg.id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(MAX_HISTORY_MESSAGES)
        .all()
    )
    recent.reverse()
    history = [{"role": m.role, "content": m.content} for m in recent]

    # Igual que record_turn: soltar la conexión del pool antes de streamear (puede durar
    # incluso más que un turno normal). Ver el comentario allá.
    project = conversation.project
    conversation_id = conversation.id
    db.commit()

    parts = []
    try:
        async for delta in stream_ai_response(project, PROJECTS[project], text, history):
            parts.append(delta)
            yield delta
    finally:
        # Persistir en finally para que sobreviva una desconexión del cliente
        # (GeneratorExit lanzado en el `yield`): de lo contrario se perdían el Message
        # del asistente y el update del lead, aunque Claude ya hubiera generado texto.
        response_text = "".join(parts)
        if response_text:
            db.add(
                Message(conversation_id=conversation_id, role="assistant", content=response_text)
            )
            db.commit()
            update_lead_from_message(db, conversation, text)
