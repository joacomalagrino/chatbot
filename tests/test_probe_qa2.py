"""PROBE QA 2 — ordering bugs en el claim del webhook. Borrar tras la corrida."""
import json, hashlib, hmac
import pytest
from fastapi.testclient import TestClient
import database, main, models
import routers.webhook as webhook
import services.conversation_service as convsvc

SECRET = "test-secret"


@pytest.fixture()
def client(monkeypatch):
    models.Base.metadata.drop_all(bind=database.engine)
    models.Base.metadata.create_all(bind=database.engine)
    async def fake_ai(p, c, m, h): return "ok"
    async def fake_send(*a, **k): return {"ok": True}
    monkeypatch.setattr(convsvc, "get_ai_response", fake_ai)
    monkeypatch.setattr(webhook, "send_whatsapp_message", fake_send)
    monkeypatch.setattr(webhook, "send_instagram_message", fake_send)
    with TestClient(main.app) as c:
        yield c
    models.Base.metadata.drop_all(bind=database.engine)


def _sign(b): return "sha256=" + hmac.new(SECRET.encode(), b, hashlib.sha256).hexdigest()


def test_wa_claim_before_phone_check_strands_event(client):
    """WhatsApp: _claim_event corre ANTES de chequear 'from'. Si 'from' falta (payload raro),
    el evento queda reclamado pero sin procesar -> un reintento CORRECTO se descarta."""
    body = json.dumps({"entry": [{"changes": [{
        "field": "messages",
        "value": {"messages": [{"id": "wamid.NOFROM", "type": "text", "text": {"body": "hola"}}]},
    }]}]}).encode()
    client.post("/webhook/meta", content=body, headers={"X-Hub-Signature-256": _sign(body)})
    db = database.SessionLocal()
    try:
        claimed = db.query(models.ProcessedEvent).filter_by(event_id="wa_wamid.NOFROM").first()
        print("evento reclamado pese a faltar 'from':", claimed is not None)
        # Si quedó reclamado, un reintento con 'from' presente NUNCA se procesará.
        assert claimed is None, "BUG: evento reclamado sin 'from' -> reintento válido descartado"
    finally:
        db.close()


def test_ig_claim_before_sender_check_strands_event(client):
    """IG: _claim_event corre ANTES de chequear sender.id. Mismo patrón."""
    body = json.dumps({"entry": [{"messaging": [{
        "message": {"mid": "ig.NOSENDER", "text": "hola"},  # sin 'sender'
    }]}]}).encode()
    client.post("/webhook/meta", content=body, headers={"X-Hub-Signature-256": _sign(body)})
    db = database.SessionLocal()
    try:
        claimed = db.query(models.ProcessedEvent).filter_by(event_id="ig_ig.NOSENDER").first()
        print("IG evento reclamado pese a faltar sender:", claimed is not None)
        assert claimed is None, "BUG: IG evento reclamado sin sender -> reintento válido descartado"
    finally:
        db.close()
