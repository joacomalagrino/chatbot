from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from config import PROJECTS
from database import get_db
from ratelimit import limiter
from services.conversation_service import get_or_create_conversation, record_turn

router = APIRouter()


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

    # Sugerir otros canales tras 3 intercambios completos (6 mensajes).
    suggest = len(conversation.messages) >= 6

    return ChatResponse(
        response=response_text,
        session_id=payload.session_id,
        suggest_channels=suggest,
    )
