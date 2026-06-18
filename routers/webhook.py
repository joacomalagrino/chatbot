from fastapi import APIRouter, Request, Query, HTTPException, Depends
from sqlalchemy.orm import Session
from database import get_db
from services.claude_service import get_ai_response
from services.meta_service import (
    send_whatsapp_message,
    send_instagram_message,
    get_lead_data,
    parse_lead_fields,
)
from services.lead_service import update_lead_from_message
from models import Conversation, Message, Lead
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

# Map Lead Ads form_id → project (update when you create the forms in Meta)
LEAD_FORM_TO_PROJECT: dict[str, str] = {
    # "1234567890": "agencia",
    # "0987654321": "mesa",
}
DEFAULT_LEADFORM_PROJECT = "agencia"


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
            # Instagram / Messenger DMs arrive at the entry level, not under `changes`.
            for event in entry.get("messaging", []):
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

            # WhatsApp messages and Lead Ads arrive under `changes`.
            for change in entry.get("changes", []):
                field = change.get("field")
                value = change.get("value", {})

                if field == "messages":
                    # WhatsApp incoming messages
                    for msg in value.get("messages", []):
                        if msg.get("type") == "text":
                            phone = msg["from"]
                            text = msg["text"]["body"]
                            logger.error("WA incoming from=%s text=%r", phone, text)
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

                elif field == "leadgen":
                    # Lead Ads: a user submitted an instant form
                    await _handle_lead_ad(db, value)
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


async def _handle_lead_ad(db: Session, value: dict):
    """Process a Lead Ads submission: fetch the form data and store a Lead."""
    leadgen_id = value.get("leadgen_id")
    form_id = str(value.get("form_id", ""))
    if not leadgen_id:
        return

    project = LEAD_FORM_TO_PROJECT.get(form_id, DEFAULT_LEADFORM_PROJECT)

    data = await get_lead_data(leadgen_id)
    fields = parse_lead_fields(data.get("field_data", []))

    session_id = f"lead_{leadgen_id}"
    conversation = db.query(Conversation).filter_by(session_id=session_id).first()
    if not conversation:
        conversation = Conversation(
            session_id=session_id,
            project=project,
            channel="lead_ad",
            status="hot",
        )
        db.add(conversation)
        db.commit()
        db.refresh(conversation)

    name = fields.get("full_name") or fields.get("name")
    email = fields.get("email")
    phone = fields.get("phone_number") or fields.get("phone")

    conversation.contact_name = name
    conversation.contact_email = email
    conversation.contact_phone = phone

    lead = conversation.lead
    if not lead:
        lead = Lead(conversation_id=conversation.id, project=project)
        db.add(lead)
    lead.name = name
    lead.email = email
    lead.phone = phone
    lead.interests = fields
    lead.status = "new"
    lead.notes = f"Vino de Lead Ad (form {form_id}, ad {value.get('ad_id', '?')})"
    db.commit()

    logger.info("Lead Ad capturado: %s (%s)", name, project)
