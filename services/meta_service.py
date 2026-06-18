import httpx
from config import get_settings

settings = get_settings()
META_API_BASE = "https://graph.facebook.com/v21.0"


async def send_whatsapp_message(phone: str, text: str) -> dict:
    url = f"{META_API_BASE}/{settings.meta_whatsapp_phone_id}/messages"
    headers = {"Authorization": f"Bearer {settings.meta_access_token}"}
    to = phone.replace("+", "").replace(" ", "")
    # Argentina: webhook sends 549XXXXXXXX but API list stores 5415XXXXXXXX
    if to.startswith("549") and len(to) == 13:
        to = "5415" + to[4:]
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url, json=payload, headers=headers)
        if not r.is_success:
            import logging
            logging.getLogger(__name__).error("WA send error %s: %s", r.status_code, r.text)
        r.raise_for_status()
        return r.json()


async def send_instagram_message(instagram_user_id: str, text: str) -> dict:
    url = f"{META_API_BASE}/{settings.meta_instagram_account_id}/messages"
    headers = {"Authorization": f"Bearer {settings.meta_access_token}"}
    payload = {
        "recipient": {"id": instagram_user_id},
        "message": {"text": text},
    }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url, json=payload, headers=headers)
        r.raise_for_status()
        return r.json()


async def get_lead_data(leadgen_id: str) -> dict:
    """Fetch a Lead Ads submission by its leadgen_id.

    Requires the `leads_retrieval` permission. Returns the raw Graph payload
    including `field_data` (list of {name, values}).
    """
    url = f"{META_API_BASE}/{leadgen_id}"
    params = {
        "access_token": settings.meta_access_token,
        "fields": "id,created_time,field_data,form_id,ad_id,campaign_id",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        return r.json()


def parse_lead_fields(field_data: list) -> dict:
    """Flatten Graph `field_data` into {name: first_value}.

    Field names depend on the form: full_name, email, phone_number, etc.
    """
    out = {}
    for field in field_data or []:
        name = field.get("name")
        values = field.get("values") or []
        if name and values:
            out[name] = values[0]
    return out
