"""Search/sort en /leads, transcript de conversación y /health/ready."""
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

    async def fake_ai(p, c, m, h):
        return "respuesta del bot"

    async def fake_send(*a, **k):
        return {}

    monkeypatch.setattr(convsvc, "get_ai_response", fake_ai)
    monkeypatch.setattr(webhook, "send_whatsapp_message", fake_send)
    monkeypatch.setattr(webhook, "send_instagram_message", fake_send)
    with TestClient(main.app) as c:
        yield c
    models.Base.metadata.drop_all(bind=database.engine)


def _sign(b):
    return "sha256=" + hmac.new(SECRET.encode(), b, hashlib.sha256).hexdigest()


def _wa(wamid, phone, text):
    body = json.dumps({"entry": [{"changes": [{"field": "messages", "value": {"messages": [
        {"id": wamid, "type": "text", "from": phone, "text": {"body": text}},
    ]}}]}]}).encode()
    return body


def _seed_lead(client, wamid, phone, text):
    body = _wa(wamid, phone, text)
    client.post("/webhook/meta", content=body, headers={"X-Hub-Signature-256": _sign(body)})


def test_health_ready_ok(client):
    r = client.get("/health/ready")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"


def test_lead_search_by_email(client):
    _seed_lead(client, "w1", "5491100000001", "hola, mi mail es ana@test.com")
    _seed_lead(client, "w2", "5491100000002", "hola, mi mail es beto@test.com")
    res = client.get("/leads/?q=ana", headers=ADMIN).json()
    assert len(res) == 1
    assert res[0]["email"] == "ana@test.com"


def test_lead_sort_oldest_first(client):
    _seed_lead(client, "w1", "5491100000001", "hola mail a@test.com")
    _seed_lead(client, "w2", "5491100000002", "hola mail b@test.com")
    recent = client.get("/leads/?sort=recent", headers=ADMIN).json()
    oldest = client.get("/leads/?sort=oldest", headers=ADMIN).json()
    assert [l["id"] for l in oldest] == list(reversed([l["id"] for l in recent]))


def test_lead_transcript(client):
    _seed_lead(client, "w1", "5491100000009", "hola, mi mail es trans@test.com")
    lead = client.get("/leads/", headers=ADMIN).json()[0]
    r = client.get(f"/leads/{lead['id']}/messages", headers=ADMIN)
    assert r.status_code == 200
    data = r.json()
    assert data["channel"] == "whatsapp"
    roles = [m["role"] for m in data["messages"]]
    assert roles == ["user", "assistant"]


def test_lead_transcript_requires_auth(client):
    assert client.get("/leads/11111111-1111-1111-1111-111111111111/messages").status_code == 401


def test_lead_transcript_404(client):
    r = client.get("/leads/11111111-1111-1111-1111-111111111111/messages", headers=ADMIN)
    assert r.status_code == 404
