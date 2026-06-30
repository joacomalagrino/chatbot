"""Tests de integración del app (TestClient + SQLite + stubs de Claude/Meta)."""
import asyncio
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

import database
import main
import models
import routers.ads as ads
import routers.webhook as webhook
import services.conversation_service as convsvc
import services.meta_service as meta_service

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
    monkeypatch.setattr(webhook, "send_whatsapp_reply", fake_send)
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


def _lead_payload(leadgen_id: str, form_id: str = "FORM1") -> dict:
    return {
        "entry": [{
            "changes": [{
                "field": "leadgen",
                "value": {"leadgen_id": leadgen_id, "form_id": form_id, "ad_id": "AD1"},
            }],
        }],
    }


# ───────────────────────────────── básicos ───────────────────────────────────

def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_security_headers_present(client):
    h = client.get("/health").headers
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "SAMEORIGIN"
    assert "referrer-policy" in h
    assert "strict-transport-security" in h


def test_widget_is_cacheable(client):
    h = client.get("/widget/chatbot.js").headers
    assert "max-age" in h.get("cache-control", "")


def test_leads_requires_auth(client):
    assert client.get("/leads/").status_code == 401
    r = client.get("/leads/", headers=ADMIN)
    assert r.status_code == 200
    assert r.json() == []


def test_ads_requires_auth(client):
    assert client.post("/ads/generate", json={"project": "agencia", "brief": "x"}).status_code == 401


def test_ads_generate_happy(client, monkeypatch):
    async def fake_generate(project, project_config, brief, channel="ambos"):
        return {"variantes": [{"titular": "Test", "texto_principal": "cuerpo"}]}

    monkeypatch.setattr(ads, "generate_ad", fake_generate)
    r = client.post("/ads/generate", headers=ADMIN,
                    json={"project": "agencia", "brief": "0km financiados"})
    assert r.status_code == 200
    assert r.json()["variantes"][0]["titular"] == "Test"


def test_ads_generate_model_error_is_502(client, monkeypatch):
    async def fake_generate(project, project_config, brief, channel="ambos"):
        return {"error": "Error del modelo (APIError)"}

    monkeypatch.setattr(ads, "generate_ad", fake_generate)
    r = client.post("/ads/generate", headers=ADMIN,
                    json={"project": "agencia", "brief": "x"})
    assert r.status_code == 502


def test_leads_stats_requires_auth(client):
    assert client.get("/leads/stats").status_code == 401


def test_leads_stats_shape(client):
    body = json.dumps(_wa_payload("wamid.ST", "5491100001111", "hola, mi mail es x@y.com")).encode()
    client.post("/webhook/meta", content=body, headers={"X-Hub-Signature-256": _sign(body)})
    d = client.get("/leads/stats", headers=ADMIN).json()
    assert d["total"] == 1
    assert d["by_project"].get("agencia") == 1
    assert "by_status" in d and "by_channel" in d and "last_7d" in d


# ──────────────────────────────── webhook ────────────────────────────────────

def test_webhook_rejects_missing_signature(client):
    body = json.dumps({"entry": []}).encode()
    assert client.post("/webhook/meta", content=body).status_code == 403


def test_webhook_rejects_oversized_body(client):
    big = json.dumps({"entry": [], "x": "z" * 300_000}).encode()
    r = client.post("/webhook/meta", content=big, headers={"X-Hub-Signature-256": _sign(big)})
    assert r.status_code == 413


def test_webhook_rejects_oversized_body_sin_content_length(client):
    """El tope se corta por BYTES LEÍDOS, no por Content-Length: un body chunked
    (sin Content-Length) que supera el tope igual se rechaza con 413, sin que el
    server bufferice todo el payload."""
    big = b"z" * (webhook.MAX_WEBHOOK_BYTES + 5_000)

    def gen():
        # Enviar en chunks fuerza transfer-encoding chunked → sin Content-Length,
        # así el header no puede ser la barrera; lo es el conteo de bytes leídos.
        for _ in range(0, len(big), 16_384):
            yield b"z" * 16_384

    r = client.post(
        "/webhook/meta", content=gen(), headers={"X-Hub-Signature-256": _sign(big)}
    )
    assert r.status_code == 413


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


def test_webhook_whatsapp_stamps_last_inbound_at(client):
    """El inbound de WhatsApp persiste last_inbound_at (reabre la ventana de 24h)."""
    from datetime import datetime, timedelta, timezone

    def _utcnow():
        return datetime.now(timezone.utc).replace(tzinfo=None)

    before = _utcnow() - timedelta(seconds=2)
    body = json.dumps(_wa_payload("wamid.IN", "5491100009999", "hola")).encode()
    r = client.post("/webhook/meta", content=body, headers={"X-Hub-Signature-256": _sign(body)})
    assert r.status_code == 200
    after = _utcnow() + timedelta(seconds=2)

    db = database.SessionLocal()
    try:
        conv = db.query(models.Conversation).filter_by(session_id="wa_5491100009999").first()
        assert conv is not None
        assert conv.last_inbound_at is not None
        # Naive UTC, fresco (entre before/after): la ventana queda abierta.
        assert before <= conv.last_inbound_at <= after
    finally:
        db.close()


def test_webhook_whatsapp_routes_reply_through_window(client, monkeypatch):
    """El webhook delega el envío en send_whatsapp_reply pasándole last_inbound_at, para
    que la decisión free-form/plantilla viva en un solo lugar."""
    captured = {}

    async def capture_send(phone, text, last_inbound_at):
        captured["phone"] = phone
        captured["text"] = text
        captured["last_inbound_at"] = last_inbound_at
        return {"ok": True}

    monkeypatch.setattr(webhook, "send_whatsapp_reply", capture_send)
    body = json.dumps(_wa_payload("wamid.RT", "5491100007777", "hola")).encode()
    r = client.post("/webhook/meta", content=body, headers={"X-Hub-Signature-256": _sign(body)})
    assert r.status_code == 200

    assert captured["phone"] == "5491100007777"
    assert captured["text"] == "respuesta de prueba"
    # Recibe el timestamp recién registrado => is_within_24h_window daría True.
    assert captured["last_inbound_at"] is not None
    assert meta_service.is_within_24h_window(captured["last_inbound_at"]) is True


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


# ──────────────────── regresión: bugs confirmados ────────────────────────────

def test_webhook_signature_fail_closed_without_secret(client, monkeypatch):
    """Bug 1: sin META_APP_SECRET el webhook debe RECHAZAR (fail-closed), no aceptar."""
    monkeypatch.setattr(webhook.settings, "meta_app_secret", "")
    monkeypatch.setattr(webhook.settings, "allow_unsigned_webhooks", False)
    body = json.dumps({"entry": []}).encode()
    # Sin firma y sin secreto configurado -> 403.
    assert client.post("/webhook/meta", content=body).status_code == 403
    # Aun con una "firma" cualquiera, sin secreto no se puede validar -> 403.
    r = client.post("/webhook/meta", content=body,
                    headers={"X-Hub-Signature-256": "sha256=loquesea"})
    assert r.status_code == 403


def test_webhook_unsigned_allowed_only_with_explicit_flag(client, monkeypatch):
    """Bug 1: el modo permisivo solo se habilita con ALLOW_UNSIGNED_WEBHOOKS explícito."""
    monkeypatch.setattr(webhook.settings, "meta_app_secret", "")
    monkeypatch.setattr(webhook.settings, "allow_unsigned_webhooks", True)
    body = json.dumps({"entry": []}).encode()
    assert client.post("/webhook/meta", content=body).status_code == 200


def test_lead_ad_not_claimed_when_graph_fails(client, monkeypatch):
    """Bug 2: si get_lead_data falla, NO se reclama el evento (el lead no se pierde)."""
    async def boom(leadgen_id):
        raise RuntimeError("Graph caído")

    monkeypatch.setattr(webhook, "get_lead_data", boom)
    body = json.dumps(_lead_payload("LEAD_FAIL")).encode()
    # El POST devuelve 200 (proceso en background); el fallo no debe reclamar el evento.
    client.post("/webhook/meta", content=body, headers={"X-Hub-Signature-256": _sign(body)})

    db = database.SessionLocal()
    try:
        assert db.query(models.ProcessedEvent).filter_by(event_id="lead_LEAD_FAIL").first() is None
        assert db.query(models.Lead).count() == 0
    finally:
        db.close()


def test_lead_ad_can_be_reprocessed_after_failure(client, monkeypatch):
    """Bug 2: tras un fallo de Graph, un reintento posterior debe poder persistir el lead."""
    state = {"fail": True}

    async def flaky(leadgen_id):
        if state["fail"]:
            raise RuntimeError("Graph caído")
        return {"field_data": [
            {"name": "full_name", "values": ["Juan Pérez"]},
            {"name": "email", "values": ["juan@lead.com"]},
            {"name": "phone_number", "values": ["+5491100000000"]},
        ]}

    monkeypatch.setattr(webhook, "get_lead_data", flaky)
    body = json.dumps(_lead_payload("LEAD_RETRY")).encode()
    sig = _sign(body)
    client.post("/webhook/meta", content=body, headers={"X-Hub-Signature-256": sig})  # falla

    state["fail"] = False
    client.post("/webhook/meta", content=body, headers={"X-Hub-Signature-256": sig})  # reintento

    db = database.SessionLocal()
    try:
        leads = db.query(models.Lead).all()
        assert len(leads) == 1
        assert leads[0].email == "juan@lead.com"
    finally:
        db.close()


def test_lead_ad_interests_stored_as_list_of_strings(client, monkeypatch):
    """Bug 3: interests se guarda como lista de strings (el panel la renderiza como tags)."""
    async def fake_lead(leadgen_id):
        return {"field_data": [
            {"name": "full_name", "values": ["Ana"]},
            {"name": "email", "values": ["ana@lead.com"]},
            {"name": "modelo_interes", "values": ["SUV"]},
        ]}

    monkeypatch.setattr(webhook, "get_lead_data", fake_lead)
    body = json.dumps(_lead_payload("LEAD_TAGS")).encode()
    client.post("/webhook/meta", content=body, headers={"X-Hub-Signature-256": _sign(body)})

    leads = client.get("/leads/", headers=ADMIN).json()
    assert len(leads) == 1
    interests = leads[0]["interests"]
    assert isinstance(interests, list)
    assert all(isinstance(i, str) for i in interests)
    assert any("modelo_interes: SUV" == i for i in interests)


def test_leads_serialize_tolerates_legacy_dict_interests(client):
    """Bug 3: leads legacy con interests=dict se aplanan a lista al serializar."""
    db = database.SessionLocal()
    try:
        conv = models.Conversation(session_id="legacy_1", project="agencia", channel="lead_ad")
        db.add(conv)
        db.commit()
        db.add(models.Lead(conversation_id=conv.id, project="agencia",
                           interests={"modelo": "SUV", "presupuesto": "20k"}))
        db.commit()
    finally:
        db.close()

    leads = client.get("/leads/", headers=ADMIN).json()
    assert len(leads) == 1
    interests = leads[0]["interests"]
    assert isinstance(interests, list)
    assert "modelo: SUV" in interests


def test_send_failure_does_not_break_processing(client, monkeypatch):
    """Bug 4: si el envío a Meta falla, el turno igual se persiste (visibilidad del fallo)."""
    async def failing_send(*args, **kwargs):
        raise RuntimeError("Meta rechazó el envío")

    monkeypatch.setattr(webhook, "send_whatsapp_reply", failing_send)
    body = json.dumps(_wa_payload("wamid.SENDFAIL", "5491100002222", "hola")).encode()
    r = client.post("/webhook/meta", content=body, headers={"X-Hub-Signature-256": _sign(body)})
    assert r.status_code == 200

    db = database.SessionLocal()
    try:
        # El turno (user + assistant) quedó persistido pese al fallo de envío.
        assert len(db.query(models.Message).all()) == 2
        # El evento quedó reclamado (no se reintenta automáticamente; se loguea para retry).
        assert db.query(models.ProcessedEvent).count() == 1
    finally:
        db.close()


def test_send_failure_does_not_skip_later_messages_in_batch(client, monkeypatch):
    """Bug 4: el fallo de envío del 1er mensaje del batch NO debe abortar el procesamiento
    de los siguientes. Sin _deliver (envío fuera de un try), la excepción del primer
    send rompía el loop de `messages` y el 2do mensaje nunca se procesaba."""
    fail_phone = "5491100003333"

    async def send_first_fails(to, *args, **kwargs):
        if to == fail_phone:
            raise RuntimeError("Meta rechazó el envío al primero")
        return {"ok": True}

    monkeypatch.setattr(webhook, "send_whatsapp_reply", send_first_fails)

    # Un solo webhook con DOS mensajes (distinto remitente): el 1ro falla el envío.
    body = json.dumps({
        "entry": [{
            "changes": [{
                "field": "messages",
                "value": {"messages": [
                    {"id": "wamid.B1", "type": "text", "from": fail_phone,
                     "text": {"body": "hola"}},
                    {"id": "wamid.B2", "type": "text", "from": "5491100004444",
                     "text": {"body": "buenas"}},
                ]},
            }],
        }],
    }).encode()
    r = client.post("/webhook/meta", content=body, headers={"X-Hub-Signature-256": _sign(body)})
    assert r.status_code == 200

    db = database.SessionLocal()
    try:
        # Ambos mensajes se procesaron: 2 conversaciones, cada una con su turno (user+assistant).
        convs = {c.session_id for c in db.query(models.Conversation).all()}
        assert convs == {"wa_5491100003333", "wa_5491100004444"}
        assert len(db.query(models.Message).all()) == 4
        # Ambos eventos reclamados (idempotencia), incluido el que falló el envío.
        assert db.query(models.ProcessedEvent).count() == 2
    finally:
        db.close()


def test_get_lead_data_rejects_non_numeric_id(monkeypatch):
    """Seguridad: get_lead_data valida el leadgen_id (^[0-9]+$) antes de armar la URL.

    Sin la validación, un id como '../foo' o 'me?x=' se interpolaría crudo en la URL
    de Graph. El fetch debe abortar con ValueError sin tocar la red."""
    def boom(*a, **k):  # si se intenta crear el cliente HTTP, falla el test
        raise AssertionError("no debería llegar a la red con un id inválido")

    monkeypatch.setattr(meta_service, "_get_client", boom)
    for bad in ["../1234", "me", "123abc", "12 34", "", "12/34"]:
        with pytest.raises(ValueError):
            asyncio.run(meta_service.get_lead_data(bad))


def test_docs_closed_in_prod(client):
    """Seguridad: con dev=False (default), /docs y /openapi.json no se exponen."""
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/redoc").status_code == 404


def test_hot_conversation_qualifies_lead(client):
    """Bug 5: al volverse 'hot' la conversación (phone + email), el lead pasa a 'qualified'."""
    body = json.dumps(_wa_payload("wamid.HOT", "5491133334444",
                                  "hola, mi tel es 1133334444 y mi mail es hot@test.com")).encode()
    client.post("/webhook/meta", content=body, headers={"X-Hub-Signature-256": _sign(body)})

    db = database.SessionLocal()
    try:
        conv = db.query(models.Conversation).filter_by(session_id="wa_5491133334444").first()
        lead = db.query(models.Lead).first()
        assert conv.status == "hot"
        assert lead.status == "qualified"
    finally:
        db.close()
