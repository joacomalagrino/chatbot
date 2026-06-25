"""Export de leads a CSV: auth, contenido y anti CSV-injection."""
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
        return "ok"

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


def test_export_requires_auth(client):
    assert client.get("/leads/export.csv").status_code == 401


def test_export_returns_csv_with_lead(client):
    body = json.dumps({"entry": [{"changes": [{"field": "messages", "value": {"messages": [
        {"id": "wamid.CSV", "type": "text", "from": "5491100002222",
         "text": {"body": "hola, mi mail es csv@test.com"}},
    ]}}]}]}).encode()
    client.post("/webhook/meta", content=body, headers={"X-Hub-Signature-256": _sign(body)})
    r = client.get("/leads/export.csv", headers=ADMIN)
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert r.text.startswith("fecha,proyecto,nombre")
    assert "csv@test.com" in r.text


def test_csv_safe_neutraliza_formulas():
    from routers.leads import _csv_safe
    assert _csv_safe("=SUM(A1)").startswith("'")
    assert _csv_safe("+1").startswith("'")
    assert _csv_safe("@cmd").startswith("'")
    assert _csv_safe("normal") == "normal"
    assert _csv_safe(None) == ""
