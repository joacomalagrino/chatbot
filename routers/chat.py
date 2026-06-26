import json
import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from config import PROJECTS
from database import SessionLocal, get_db
from models import Message
from ratelimit import limiter
from services.conversation_service import (
    get_or_create_conversation,
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


@router.post("/", response_model=ChatResponse)
@limiter.limit("20/minute")
async def chat(request: Request, payload: ChatRequest, db: Session = Depends(get_db)):
    if payload.project not in PROJECTS:
        raise HTTPException(status_code=400, detail=f"Proyecto inválido: {payload.project}")

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
        except Exception:
            logger.exception("Error en /chat/stream")
            yield f"data: {json.dumps({'error': True})}\n\n"
        finally:
            db.close()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
