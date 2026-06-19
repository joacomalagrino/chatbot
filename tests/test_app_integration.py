"""Tests de integración del app (TestClient + SQLite + stubs de Claude/Meta)."""
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

    # Evitar llamadas reales a Claude y a Meta.
    monkeypatch.setattr(convsvc, "get_ai_response", fake_ai)
    monkeypatch.setattr(webhook, "send_whatsapp_message", fake_send)
    monkeypatch.setattr(webhook, "send_instagram_message", fake_send)

    with TestClient(main.app) as c:
        yield c

    models.Base.metadata.drop_all(bind=database.engine)


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def _wa_payload(wamid: str, phone: str, text: str) -> dict:
    return {
        "entry": [{
            "changes": [{
                "field": "messages",
                "value": {"messages": [{
                    "id": wamid, "type": "text", "from": phone, "text": {"body": text},
                }]},
            }],
        }],
    }


# ───────────────────────────────── básicos ───────────────────────────────────

def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_leads_requires_auth(client):
    assert client.get("/leads/").status_code == 401
    r = client.get("/leads/", headers=ADMIN)
    assert r.status_code == 200
    assert r.json() == []


def test_ads_requires_auth(client):
    assert client.post("/ads/generate", json={"project": "agencia", "brief": "x"}).status_code == 401


# ──────────────────────────────── webhook ────────────────────────────────────

def test_webhook_rejects_missing_signature(client):
    body = json.dumps({"entry": []}).encode()
    assert client.post("/webhook/meta", content=body).status_code == 403


def test_webhook_rejects_bad_signature(client):
    body = json.dumps({"entry": []}).encode()
    r = client.post("/webhook/meta", content=body,
                    headers={"X-Hub-Signature-256": "sha256=deadbeef"})
    assert r.status_code == 403


def test_webhook_handshake_echoes_challenge(client):
    r = client.get("/webhook/meta", params={
        "hub.mode": "subscribe", "hub.challenge": "00ab12", "hub.verify_token": "test-verify",
    })
    assert r.status_code == 200
    assert r.text == "00ab12"  # verbatim, sin int()


def test_webhook_whatsapp_creates_conversation_messages_and_lead(client):
    body = json.dumps(_wa_payload("wamid.ABC", "5491165613300",
                                  "hola, mi mail es juan@test.com")).encode()
    r = client.post("/webhook/meta", content=body, headers={"X-Hub-Signature-256": _sign(body)})
    assert r.status_code == 200

    db = database.SessionLocal()
    try:
        convs = db.query(models.Conversation).all()
        assert len(convs) == 1 and convs[0].channel == "whatsapp"
        assert len(db.query(models.Message).all()) == 2  # user + assistant
        leads = db.query(models.Lead).all()
        assert len(leads) == 1
        assert leads[0].email == "juan@test.com"
        assert leads[0].instagram is None  # el @ del email NO es handle de IG
    finally:
        db.close()


def test_webhook_idempotent_on_retry(client):
    body = json.dumps(_wa_payload("wamid.SAME", "5491100000000", "hola")).encode()
    sig = _sign(body)
    client.post("/webhook/meta", content=body, headers={"X-Hub-Signature-256": sig})
    client.post("/webhook/meta", content=body, headers={"X-Hub-Signature-256": sig})  # reintento

    db = database.SessionLocal()
    try:
        # Pese a los 2 POST idénticos, el mensaje se procesó una sola vez.
        assert len(db.query(models.Message).all()) == 2
        assert len(db.query(models.ProcessedEvent).all()) == 1
    finally:
        db.close()


# ────────────────────────────────── leads ────────────────────────────────────

def test_lead_status_update_flow(client):
    # Crear un lead vía webhook (con dato de contacto) y cambiarle el estado por la API.
    body = json.dumps(_wa_payload("wamid.X", "5491155556666", "hola, mi mail es ana@test.com")).encode()
    client.post("/webhook/meta", content=body, headers={"X-Hub-Signature-256": _sign(body)})

    leads = client.get("/leads/", headers=ADMIN).json()
    assert len(leads) == 1
    lead_id = leads[0]["id"]

    r = client.patch(f"/leads/{lead_id}/status", headers=ADMIN, json={"status": "contacted"})
    assert r.status_code == 200
    assert r.json()["status"] == "contacted"


def test_lead_status_update_invalid_id_is_404(client):
    r = client.patch("/leads/11111111-1111-1111-1111-111111111111/status",
                     headers=ADMIN, json={"status": "contacted"})
    assert r.status_code == 404


def test_lead_status_update_rejects_invalid_status(client):
    r = client.patch("/leads/11111111-1111-1111-1111-111111111111/status",
                     headers=ADMIN, json={"status": "loquesea"})
    assert r.status_code == 422
