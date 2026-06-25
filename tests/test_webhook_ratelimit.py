"""Rate-limit del POST /webhook/meta (120/min): un payload firmado capturado no se puede
reproducir sin tope (acota la amplificación de fetches a Graph)."""
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
from ratelimit import limiter

SECRET = "test-secret"


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
    # Aislar el bucket en memoria: este test agota el límite a propósito y no debe
    # filtrar el estado a otros tests (ni heredar el de ellos).
    limiter.reset()
    with TestClient(main.app) as c:
        yield c
    limiter.reset()
    models.Base.metadata.drop_all(bind=database.engine)


def _sign(b):
    return "sha256=" + hmac.new(SECRET.encode(), b, hashlib.sha256).hexdigest()


def _event(wamid):
    return json.dumps({"entry": [{"changes": [{"field": "messages", "value": {"messages": [
        {"id": wamid, "type": "text", "from": "5491100000000", "text": {"body": "hola"}},
    ]}}]}]}).encode()


def test_webhook_rate_limit_429(client):
    last = None
    # 120/minute: el request 121 (con firma válida) debe ser rechazado por el limiter.
    for i in range(121):
        body = _event(f"wamid.{i}")
        last = client.post(
            "/webhook/meta", content=body, headers={"X-Hub-Signature-256": _sign(body)}
        )
    assert last.status_code == 429
