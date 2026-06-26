"""El POST /webhook/meta NO se rate-limitea por IP: los webhooks de Meta llegan TODOS desde
el pool compartido de Facebook (misma IP para todos los leads), así que un tope por IP sería
un tope GLOBAL y dropearía leads legítimos en una ráfaga de campaña. La firma HMAC y la dedup
por event_id ya cubren abuso/replay. Este test fija ese contrato: muchos requests válidos
consecutivos NO deben dar 429."""
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
    # Resetear el bucket en memoria por las dudas, para no heredar estado de otros tests.
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


def test_webhook_no_se_rate_limitea(client):
    # 130 requests válidos consecutivos (misma IP, como vendrían del pool de Facebook):
    # ninguno debe devolver 429. Si hubiera un tope por IP, los leads se perderían.
    for i in range(130):
        body = _event(f"wamid.{i}")
        r = client.post(
            "/webhook/meta", content=body, headers={"X-Hub-Signature-256": _sign(body)}
        )
        assert r.status_code == 200, f"request {i} devolvió {r.status_code}"
