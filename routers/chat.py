import json
import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from config import PROJECTS
from database import SessionLocal, get_db, is_pool_exhaustion
from models import Message
from observability import log_pool_exhaustion
from ratelimit import limiter
from services.conversation_service import (
    get_or_create_conversation,
    is_reserved_session_id,
    record_turn,
    stream_turn,
)

router = APIRouter()
logger = logging.getLogger(__name__)

# Sugerir otros canales tras 3 intercambios completos (6 mensajes).
SUGGEST_AFTER_MESSAGES = 6


def _should_suggest_channels(db: Session, conversation_id) -> bool:
    """¿La conversación ya llegó al umbral de mensajes para sugerir otros canales?

    Cuenta a nivel query con .limit(SUGGEST_AFTER_MESSAGES) en vez de `len(conversation.messages)`,
    que materializaría TODA la colección lazy en cada turno (en WhatsApp la conversación persiste
    indefinidamente). Solo necesitamos saber si hay >= umbral, así que basta con contar hasta ahí.
    """
    count = (
        db.query(Message.id)
        .filter(Message.conversation_id == conversation_id)
        .limit(SUGGEST_AFTER_MESSAGES)
        .count()
    )
    return count >= SUGGEST_AFTER_MESSAGES


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=200)
    project: str = Field(min_length=1, max_length=50)
    message: str = Field(min_length=1, max_length=2000)
    channel: Literal["web", "whatsapp", "instagram"] = "web"


class ChatResponse(BaseModel):
    response: str
    session_id: str
    suggest_channels: bool = False


def _reject_reserved_session(session_id: str) -> None:
    """El chat web es PÚBLICO (sin auth) y el cliente elige su session_id. Los
    canales entrantes de Meta usan session_id enumerables (wa_<telefono>, ig_<id>,
    lead_<id>); sin este guard un cliente web podría pasar session_id="wa_<telefono>"
    y adjuntarse a la conversación real de ese lead, leyendo su historial (PII) o
    envenenándola: IDOR. Rechazamos uniformemente cualquier prefijo reservado (mismo
    400 exista o no la conversación → sin oráculo de enumeración)."""
    if is_reserved_session_id(session_id):
        raise HTTPException(status_code=400, detail="session_id inválido")


@router.post("/", response_model=ChatResponse)
@limiter.limit("20/minute")
async def chat(request: Request, payload: ChatRequest, db: Session = Depends(get_db)):
    if payload.project not in PROJECTS:
        raise HTTPException(status_code=400, detail=f"Proyecto inválido: {payload.project}")
    _reject_reserved_session(payload.session_id)

    conversation = get_or_create_conversation(
        db, payload.session_id, payload.project, payload.channel
    )
    response_text = await record_turn(db, conversation, payload.message)

    suggest = _should_suggest_channels(db, conversation.id)

    return ChatResponse(
        response=response_text,
        session_id=payload.session_id,
        suggest_channels=suggest,
    )


@router.post("/stream")
@limiter.limit("20/minute")
async def chat_stream(request: Request, payload: ChatRequest):
    """Igual que /chat pero con streaming SSE: emite cada delta a medida que Claude
    responde, para acelerar la velocidad percibida. El widget cae a /chat (no-streaming)
    si esto falla. NO reemplaza a /chat.

    El cuerpo del stream corre DESPUÉS de que el endpoint retornó, cuando la sesión de
    Depends(get_db) ya estaría cerrada. Por eso el generador abre su PROPIA sesión
    (SessionLocal) y la cierra en finally —mismo patrón que _process_event en webhook.py."""
    if payload.project not in PROJECTS:
        raise HTTPException(status_code=400, detail=f"Proyecto inválido: {payload.project}")
    _reject_reserved_session(payload.session_id)

    async def gen():
        db = SessionLocal()
        try:
            conversation = get_or_create_conversation(
                db, payload.session_id, payload.project, payload.channel
            )
            async for delta in stream_turn(db, conversation, payload.message):
                yield f"data: {json.dumps({'delta': delta})}\n\n"

            suggest = _should_suggest_channels(db, conversation.id)
            yield f"data: {json.dumps({'done': True, 'suggest_channels': suggest})}\n\n"
        except Exception as exc:
            logger.exception("Error en /chat/stream")
            # El cuerpo del stream corre DESPUÉS de que el endpoint retornó, así que la
            # saturación del pool que se lanza acá NO pasa por el pool_exhaustion_observer
            # (main.py) —el except la traga antes—. Y /chat/stream es el path por DEFECTO del
            # widget: sin este log el monitoreo quedaría ciego justo en el camino más usado.
            # Contabilizamos IGUAL que el path no-streaming (mismo helper/firma) sin cambiar la
            # respuesta al cliente.
            if is_pool_exhaustion(exc):
                log_pool_exhaustion(exc, path=request.url.path, method=request.method)
            yield f"data: {json.dumps({'error': True})}\n\n"
        finally:
            db.close()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
