"""PROBE QA — tests adversariales para descubrir huecos/bugs. Borrar tras la corrida."""
import asyncio
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

import database
import main
import models
import routers.webhook as webhook
import services.conversation_service as convsvc
import services.meta_service as meta_service
from services.text_utils import extract_contact, parse_model_json

SECRET = "test-secret"
ADMIN = {"Authorization": "Bearer test-admin"}


@pytest.fixture()
def client(monkeypatch):
    models.Base.metadata.drop_all(bind=database.engine)
    models.Base.metadata.create_all(bind=database.engine)

    async def fake_ai(project, project_config, message, history):
        return "respuesta de prueba"

    async def fake_send(*args, **kwargs):
        return {"ok": True}

    monkeypatch.setattr(convsvc, "get_ai_response", fake_ai)
    monkeypatch.setattr(webhook, "send_whatsapp_message", fake_send)
    monkeypatch.setattr(webhook, "send_instagram_message", fake_send)
    with TestClient(main.app) as c:
        yield c
    models.Base.metadata.drop_all(bind=database.engine)


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def _ig_payload(mid, sender_id, text):
    return {"entry": [{"messaging": [{
        "sender": {"id": sender_id}, "message": {"mid": mid, "text": text},
    }]}]}


# ─────────────── 1. Instagram path: SIN test de integración hoy ───────────────

def test_instagram_dm_creates_conversation_and_lead(client):
    body = json.dumps(_ig_payload("ig.M1", "IGUSER1", "hola, mi mail es ig@test.com")).encode()
    r = client.post("/webhook/meta", content=body, headers={"X-Hub-Signature-256": _sign(body)})
    assert r.status_code == 200
    db = database.SessionLocal()
    try:
        conv = db.query(models.Conversation).filter_by(session_id="ig_IGUSER1").first()
        assert conv is not None and conv.channel == "instagram"
        assert conv.contact_instagram == "IGUSER1"
        assert db.query(models.Lead).first().email == "ig@test.com"
    finally:
        db.close()


def test_instagram_echo_message_ignored(client):
    # Meta reenvía nuestros propios mensajes salientes como echo. ¿Se filtran?
    body = json.dumps({"entry": [{"messaging": [{
        "sender": {"id": "PAGE"}, "message": {"mid": "ig.echo", "text": "hola", "is_echo": True},
    }]}]}).encode()
    client.post("/webhook/meta", content=body, headers={"X-Hub-Signature-256": _sign(body)})
    db = database.SessionLocal()
    try:
        # Si NO se filtra el echo, se crea una conversación con session_id ig_PAGE.
        n = db.query(models.Conversation).count()
        print("ECHO -> conversaciones creadas:", n)
        assert n == 0, "echo (is_echo) NO se filtra -> el bot se contesta a sí mismo"
    finally:
        db.close()


# ─────────────── 2. Lead Ad SIN contacto (campos vacíos) ───────────────

def test_lead_ad_with_empty_fields(client, monkeypatch):
    async def fake_lead(leadgen_id):
        return {"field_data": []}  # formulario sin datos
    monkeypatch.setattr(webhook, "get_lead_data", fake_lead)
    body = json.dumps({"entry": [{"changes": [{
        "field": "leadgen", "value": {"leadgen_id": "999", "form_id": "F"},
    }]}]}).encode()
    client.post("/webhook/meta", content=body, headers={"X-Hub-Signature-256": _sign(body)})
    leads = client.get("/leads/", headers=ADMIN).json()
    print("LEAD vacio ->", leads)
    assert len(leads) == 1  # se crea igual, status hot


# ─────────────── 3. WhatsApp: tipos no-texto (imagen, status, reaction) ───────────────

def test_whatsapp_status_update_ignored(client):
    # WhatsApp manda 'statuses' (delivered/read) bajo el mismo field 'messages'.
    body = json.dumps({"entry": [{"changes": [{
        "field": "messages",
        "value": {"statuses": [{"id": "wamid.S", "status": "delivered"}]},
    }]}]}).encode()
    r = client.post("/webhook/meta", content=body, headers={"X-Hub-Signature-256": _sign(body)})
    assert r.status_code == 200
    db = database.SessionLocal()
    try:
        assert db.query(models.Conversation).count() == 0
    finally:
        db.close()


def test_whatsapp_image_type_ignored(client):
    body = json.dumps({"entry": [{"changes": [{
        "field": "messages",
        "value": {"messages": [{"id": "wamid.IMG", "type": "image", "from": "549110"}]},
    }]}]}).encode()
    client.post("/webhook/meta", content=body, headers={"X-Hub-Signature-256": _sign(body)})
    db = database.SessionLocal()
    try:
        assert db.query(models.Conversation).count() == 0
    finally:
        db.close()


# ─────────────── 4. chat web: project inválido y suggest_channels ───────────────

def test_chat_invalid_project_400(client):
    r = client.post("/chat/", json={
        "session_id": "s1", "project": "noexiste", "message": "hola"})
    assert r.status_code == 400


def test_chat_suggest_channels_after_3_exchanges(client):
    for i in range(3):
        r = client.post("/chat/", json={
            "session_id": "sx", "project": "agencia", "message": f"m{i}"})
        assert r.status_code == 200
    print("suggest_channels en 3er turno:", r.json()["suggest_channels"])
    assert r.json()["suggest_channels"] is True


# ─────────────── 5. handshake GET con mode != subscribe ───────────────

def test_webhook_handshake_wrong_mode_403(client):
    r = client.get("/webhook/meta", params={
        "hub.mode": "unsubscribe", "hub.challenge": "x", "hub.verify_token": "test-verify"})
    assert r.status_code == 403


def test_webhook_handshake_wrong_token_403(client):
    r = client.get("/webhook/meta", params={
        "hub.mode": "subscribe", "hub.challenge": "x", "hub.verify_token": "MALO"})
    assert r.status_code == 403


# ─────────────── 6. empty body / no entry ───────────────

def test_webhook_empty_body_ok(client):
    body = b""
    r = client.post("/webhook/meta", content=body, headers={"X-Hub-Signature-256": _sign(body)})
    assert r.status_code == 200


# ─────────────── 7. extract_contact edge: teléfono dentro de un email ───────────────

def test_extract_contact_phone_in_email_not_picked(client):
    out = extract_contact("escribime a juan1123456789@gmail.com")
    print("phone-in-email ->", out)
    # El email se remueve antes de buscar teléfono, así que NO debería sacar phone del local-part.
    assert out["email"] == "juan1123456789@gmail.com"


def test_extract_contact_multiple_phones_takes_first_valid(client):
    out = extract_contact("viejo 12345 nuevo 1123456789")
    print("multi-phone ->", out)


# ─────────────── 8. normalize teléfono de WhatsApp con '+' que llega del 'from' ───────────────

def test_wa_from_with_plus_normalizes(client):
    # WhatsApp 'from' normalmente viene sin '+', pero probamos robustez de session_id.
    body = json.dumps({"entry": [{"changes": [{
        "field": "messages",
        "value": {"messages": [{
            "id": "wamid.PL", "type": "text", "from": "+5491100009999", "text": {"body": "hola"}}]},
    }]}]}).encode()
    client.post("/webhook/meta", content=body, headers={"X-Hub-Signature-256": _sign(body)})
    db = database.SessionLocal()
    try:
        sids = [c.session_id for c in db.query(models.Conversation).all()]
        print("session_ids con '+':", sids)
    finally:
        db.close()


# ─────────────── 9. parse_model_json: doble fence / texto antes ───────────────

def test_parse_model_json_text_before_fence():
    out = parse_model_json("Acá está el JSON:\n```json\n{\"a\":1}\n```")
    print("texto-antes-de-fence ->", out)


# ─────────────── 10. rate limit del chat (20/min) ───────────────

def test_chat_rate_limit_429(client):
    last = None
    for i in range(25):
        last = client.post("/chat/", json={
            "session_id": "rl", "project": "agencia", "message": "hola"})
    print("ultimo status tras 25 posts:", last.status_code)
    assert last.status_code == 429
