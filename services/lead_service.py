import re
from sqlalchemy.orm import Session
from models import Conversation, Lead

PHONE_RE = re.compile(r'(\+?[\d][\d\s\-\(\)]{6,14}[\d])')
EMAIL_RE = re.compile(r'[\w\.\-]+@[\w\.\-]+\.\w+')
IG_HANDLE_RE = re.compile(r'@([\w\.]+)')


def update_lead_from_message(db: Session, conversation: Conversation, user_message: str):
    lead = conversation.lead
    if not lead:
        lead = Lead(conversation_id=conversation.id, project=conversation.project)
        db.add(lead)

    changed = False

    phones = PHONE_RE.findall(user_message)
    if phones and not lead.phone:
        lead.phone = phones[0].strip()
        conversation.contact_phone = lead.phone
        changed = True

    emails = EMAIL_RE.findall(user_message)
    if emails and not lead.email:
        lead.email = emails[0]
        conversation.contact_email = lead.email
        changed = True

    ig_handles = IG_HANDLE_RE.findall(user_message)
    if ig_handles and not lead.instagram:
        lead.instagram = ig_handles[0]
        conversation.contact_instagram = lead.instagram
        changed = True

    if changed and conversation.status == "new":
        conversation.status = "warm"
        lead.status = "contacted"

    if lead.phone and (lead.email or lead.instagram):
        conversation.status = "hot"

    if changed:
        db.commit()
