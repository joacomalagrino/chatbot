from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from database import get_db
from services.claude_service import get_ai_response
from services.lead_service import update_lead_from_message
from models import Conversation, Message
from config import PROJECTS

router = APIRouter()


class ChatRequest(BaseModel):
    session_id: str
    project: str
    message: str
    channel: str = "web"


class ChatResponse(BaseModel):
    response: str
    session_id: str
    suggest_channels: bool = False


@router.post("/", response_model=ChatResponse)
async def chat(payload: ChatRequest, db: Session = Depends(get_db)):
    if payload.project not in PROJECTS:
        raise HTTPException(status_code=400, detail=f"Proyecto inválido: {payload.project}")

    conversation = db.query(Conversation).filter_by(session_id=payload.session_id).first()
    if not conversation:
        conversation = Conversation(
            session_id=payload.session_id,
            project=payload.project,
            channel=payload.channel,
        )
        db.add(conversation)
        db.commit()
        db.refresh(conversation)

    user_msg = Message(conversation_id=conversation.id, role="user", content=payload.message)
    db.add(user_msg)
    db.commit()
    db.refresh(conversation)

    history = [{"role": m.role, "content": m.content} for m in conversation.messages[:-1]]

    project_config = PROJECTS[payload.project]
    response_text = await get_ai_response(
        project=payload.project,
        project_config=project_config,
        message=payload.message,
        history=history,
    )

    assistant_msg = Message(conversation_id=conversation.id, role="assistant", content=response_text)
    db.add(assistant_msg)
    db.commit()

    update_lead_from_message(db, conversation, payload.message)

    # Suggest WhatsApp/Instagram after 3 full exchanges (6 messages)
    suggest = len(conversation.messages) >= 6

    return ChatResponse(
        response=response_text,
        session_id=payload.session_id,
        suggest_channels=suggest,
    )
