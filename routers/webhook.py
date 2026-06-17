from fastapi import APIRouter, Request, Query, HTTPException, Depends
from sqlalchemy.orm import Session
from database import get_db
from services.claude_service import get_ai_response
from services.meta_service import send_whatsapp_message, send_instagram_message
from services.lead_service import update_lead_from_message
from models import Conversation, Message
from config import get_settings, PROJECTS
import logging

router = APIRouter()
settings = get_settings()
logger = logging.getLogger(__name__)

# Map WhatsApp phone numbers → project (update when you have real numbers)
WHATSAPP_NUMBER_TO_PROJECT: dict[str, str] = {
    # "5491100000000": "agencia",
    # "5491100000001": "mesa",
}

# Default project when the number isn't mapped
DEFAULT_WHATSAPP_PROJECT = "agencia"
DEFAULT_INSTAGRAM_PROJECT = "agencia"


@router.get("/meta")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
):
    if hub_mode == "subscribe" and hub_verify_token == settings.meta_verify_token:
        return int(hub_challenge)
    raise HTTPException(status_code=403, detail="Token de verificación inválido")


@router.post("/meta")
async def receive_meta_event(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    try:
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                field = change.get("field")
                value = change.get("value", {})

                if field == "messages":
                    # WhatsApp incoming messages
                    for msg in value.get("messages", []):
                        if msg.get("type") == "text":
                            phone = msg["from"]
                            text = msg["text"]["body"]
                            project = WHATSAPP_NUMBER_TO_PROJECT.get(phone, DEFAULT_WHATSAPP_PROJECT)
                            await _handle_incoming(
                                db=db,
                                session_id=f"wa_{phone}",
                                project=project,
                                channel="whatsapp",
                                text=text,
                                contact_phone=phone,
                                send_fn=lambda t, p=phone: send_whatsapp_message(p, t),
                            )

                    # Instagram DMs
                    for event in value.get("messaging", []):
                        msg = event.get("message", {})
                        if msg.get("text"):
                            ig_id = event["sender"]["id"]
                            await _handle_incoming(
                                db=db,
                                session_id=f"ig_{ig_id}",
                                project=DEFAULT_INSTAGRAM_PROJECT,
                                channel="instagram",
                                text=msg["text"],
                                contact_instagram=ig_id,
                                send_fn=lambda t, i=ig_id: send_instagram_message(i, t),
                            )
    except Exception:
        logger.exception("Error procesando webhook Meta")

    # Meta requires a 200 response even on errors
    return {"status": "ok"}


async def _handle_incoming(
    db: Session,
    session_id: str,
    project: str,
    channel: str,
    text: str,
    send_fn,
    contact_phone: str = None,
    contact_instagram: str = None,
):
    conversation = db.query(Conversation).filter_by(session_id=session_id).first()
    if not conversation:
        conversation = Conversation(
            session_id=session_id,
            project=project,
            channel=channel,
            contact_phone=contact_phone,
            contact_instagram=contact_instagram,
        )
        db.add(conversation)
        db.commit()
        db.refresh(conversation)

    db.add(Message(conversation_id=conversation.id, role="user", content=text))
    db.commit()
    db.refresh(conversation)

    history = [{"role": m.role, "content": m.content} for m in conversation.messages[:-1]]
    response_text = await get_ai_response(project, PROJECTS[project], text, history)

    db.add(Message(conversation_id=conversation.id, role="assistant", content=response_text))
    db.commit()

    update_lead_from_message(db, conversation, text)

    await send_fn(response_text)
