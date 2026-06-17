import httpx
from config import get_settings

settings = get_settings()
META_API_BASE = "https://graph.facebook.com/v21.0"


async def send_whatsapp_message(phone: str, text: str) -> dict:
    url = f"{META_API_BASE}/{settings.meta_whatsapp_phone_id}/messages"
    headers = {"Authorization": f"Bearer {settings.meta_access_token}"}
    payload = {
        "messaging_product": "whatsapp",
        "to": phone.replace("+", "").replace(" ", ""),
        "type": "text",
        "text": {"body": text},
    }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url, json=payload, headers=headers)
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
