"""Tests del ruteo interno del webhook de Meta (routers/webhook.py).

A diferencia de test_app_integration (que entra por HTTP) y test_concurrency (que
estresa la idempotencia con threads), acá ejercitamos las funciones de ruteo
DIRECTAMENTE para fijar su contrato:

- _handle_change: despacha messages -> WhatsApp y leadgen -> _handle_lead_ad, y
  FILTRA los mensajes con type != "text" (audios, imágenes, etc.) sin crear nada.
- _handle_lead_ad: trae el lead de Graph, lo reclama (idempotencia) y persiste el Lead.
- _deliver: devuelve True si el envío anda, y False capturando el fallo sin propagarlo.
- la interacción claim/_release_event: si la persistencia del Lead falla tras reclamar
  el evento, se libera el ProcessedEvent para permitir el reintento de Meta.

Se stubea Claude y Meta (no hay red). Cada test usa su propia sesión contra la DB de test.
"""
import asyncio

import pytest
from sqlalchemy.exc import IntegrityError

import database
import models
import routers.webhook as webhook
import services.conversation_service as convsvc


@pytest.fixture()
def fresh_db(monkeypatch):
    models.Base.metadata.drop_all(bind=database.engine)
    models.Base.metadata.create_all(bind=database.engine)

    async def fake_ai(project, project_config, message, history):
        return "respuesta"

    async def fake_send(*args, **kwargs):
        return {"ok": True}

    monkeypatch.setattr(convsvc, "get_ai_response", fake_ai)
    monkeypatch.setattr(webhook, "send_whatsapp_reply", fake_send)
    monkeypatch.setattr(webhook, "send_instagram_message", fake_send)
    yield
    models.Base.metadata.drop_all(bind=database.engine)


def _counts():
    db = database.SessionLocal()
    try:
        return {
            "conversations": db.query(models.Conversation).count(),
            "messages": db.query(models.Message).count(),
            "leads": db.query(models.Lead).count(),
            "events": db.query(models.ProcessedEvent).count(),
        }
    finally:
        db.close()


# ───────────────────────── _handle_change: ruteo por field ──────────────────

def test_handle_change_routes_text_message_to_whatsapp(fresh_db, monkeypatch):
    sent = []

    async def capture(phone, text, *args, **kwargs):
        sent.append((phone, text))
        return {"ok": True}

    monkeypatch.setattr(webhook, "send_whatsapp_reply", capture)

    change = {
        "field": "messages",
        "value": {"messages": [{
            "id": "wamid_1", "type": "text",
            "from": "5491100000000", "text": {"body": "hola"},
        }]},
    }
    db = database.SessionLocal()
    try:
        asyncio.run(webhook._handle_change(db, change))
    finally:
        db.close()

    assert sent == [("5491100000000", "respuesta")]
    c = _counts()
    assert c["conversations"] == 1
    assert c["messages"] == 2          # user + assistant
    assert c["events"] == 1            # wamid reclamado


def test_handle_change_skips_non_text_messages(fresh_db, monkeypatch):
    """Un mensaje con type != "text" (audio, imagen, sticker) se ignora: ni
    conversación, ni mensajes, ni claim del evento."""
    sent = []

    async def capture(phone, text, *args, **kwargs):
        sent.append((phone, text))

    monkeypatch.setattr(webhook, "send_whatsapp_reply", capture)

    change = {
        "field": "messages",
        "value": {"messages": [{
            "id": "wamid_audio", "type": "audio",
            "from": "5491100000000", "audio": {"id": "media123"},
        }]},
    }
    db = database.SessionLocal()
    try:
        asyncio.run(webhook._handle_change(db, change))
    finally:
        db.close()

    assert sent == []
    assert _counts() == {"conversations": 0, "messages": 0, "leads": 0, "events": 0}


def test_handle_change_processes_text_and_skips_non_text_in_same_batch(fresh_db, monkeypatch):
    """Batch mixto: solo el mensaje de texto se procesa; el de tipo no-texto se saltea."""
    sent = []

    async def capture(phone, text, *args, **kwargs):
        sent.append((phone, text))

    monkeypatch.setattr(webhook, "send_whatsapp_reply", capture)

    change = {
        "field": "messages",
        "value": {"messages": [
            {"id": "m_img", "type": "image", "from": "5491100000000",
             "image": {"id": "x"}},
            {"id": "m_txt", "type": "text", "from": "5491100000000",
             "text": {"body": "consulta"}},
        ]},
    }
    db = database.SessionLocal()
    try:
        asyncio.run(webhook._handle_change(db, change))
    finally:
        db.close()

    assert sent == [("5491100000000", "respuesta")]
    c = _counts()
    assert c["messages"] == 2
    assert c["events"] == 1            # solo el de texto reclamó evento


def test_handle_change_skips_text_message_without_from(fresh_db, monkeypatch):
    """Sin 'from' no se puede responder ni reclamar: se ignora sin tocar nada."""
    async def capture(phone, text, *args, **kwargs):
        raise AssertionError("no debería enviarse sin remitente")

    monkeypatch.setattr(webhook, "send_whatsapp_reply", capture)

    change = {
        "field": "messages",
        "value": {"messages": [{
            "id": "wamid_noremit", "type": "text", "text": {"body": "hola"},
        }]},
    }
    db = database.SessionLocal()
    try:
        asyncio.run(webhook._handle_change(db, change))
    finally:
        db.close()

    assert _counts() == {"conversations": 0, "messages": 0, "leads": 0, "events": 0}


def test_handle_change_routes_leadgen_to_lead_handler(fresh_db, monkeypatch):
    """field == "leadgen" delega en _handle_lead_ad con el value."""
    seen = []

    async def fake_handler(db, value):
        seen.append(value)

    monkeypatch.setattr(webhook, "_handle_lead_ad", fake_handler)

    change = {"field": "leadgen", "value": {"leadgen_id": "555", "form_id": "F1"}}
    db = database.SessionLocal()
    try:
        asyncio.run(webhook._handle_change(db, change))
    finally:
        db.close()

    assert seen == [{"leadgen_id": "555", "form_id": "F1"}]


def test_handle_change_ignores_unknown_field(fresh_db):
    """Un field desconocido (ej. "feed") no rompe ni persiste nada."""
    db = database.SessionLocal()
    try:
        asyncio.run(webhook._handle_change(db, {"field": "feed", "value": {}}))
    finally:
        db.close()
    assert _counts() == {"conversations": 0, "messages": 0, "leads": 0, "events": 0}


# ───────────────────────── _handle_lead_ad ─────────────────────────────────

def test_handle_lead_ad_fetches_claims_and_persists_lead(fresh_db, monkeypatch):
    async def fake_lead(leadgen_id):
        assert leadgen_id == "9876"
        return {"field_data": [
            {"name": "full_name", "values": ["Ana Pérez"]},
            {"name": "email", "values": ["ana@test.com"]},
            {"name": "phone_number", "values": ["+5491133334444"]},
        ]}

    notified = []
    monkeypatch.setattr(webhook, "get_lead_data", fake_lead)
    monkeypatch.setattr(webhook, "fire_hot_lead", lambda payload: notified.append(payload))

    db = database.SessionLocal()
    try:
        asyncio.run(webhook._handle_lead_ad(db, {"leadgen_id": "9876", "form_id": "F1"}))
    finally:
        db.close()

    db = database.SessionLocal()
    try:
        lead = db.query(models.Lead).first()
        assert lead is not None
        assert lead.name == "Ana Pérez"
        assert lead.email == "ana@test.com"
        assert lead.phone == "+5491133334444"
        # interests como lista de strings "campo: valor" (el panel renderiza tags).
        assert all(isinstance(i, str) for i in lead.interests)
        conv = db.query(models.Conversation).filter_by(session_id="lead_9876").first()
        assert conv is not None and conv.status == "hot"
        assert db.query(models.ProcessedEvent).filter_by(event_id="lead_9876").count() == 1
    finally:
        db.close()

    # Un Lead Ad entra como caliente: se avisa al equipo.
    assert len(notified) == 1
    assert notified[0]["channel"] == "lead_ad"


def test_handle_lead_ad_without_leadgen_id_is_noop(fresh_db, monkeypatch):
    async def boom(leadgen_id):
        raise AssertionError("no debería pegarle a Graph sin leadgen_id")

    monkeypatch.setattr(webhook, "get_lead_data", boom)

    db = database.SessionLocal()
    try:
        asyncio.run(webhook._handle_lead_ad(db, {"form_id": "F1"}))
    finally:
        db.close()
    assert _counts() == {"conversations": 0, "messages": 0, "leads": 0, "events": 0}


def test_handle_lead_ad_idempotent_on_duplicate_leadgen(fresh_db, monkeypatch):
    """El mismo leadgen_id entregado dos veces persiste un solo Lead."""
    calls = []

    async def fake_lead(leadgen_id):
        calls.append(leadgen_id)
        return {"field_data": [{"name": "full_name", "values": ["Dup"]}]}

    monkeypatch.setattr(webhook, "get_lead_data", fake_lead)
    monkeypatch.setattr(webhook, "fire_hot_lead", lambda payload: None)

    value = {"leadgen_id": "111", "form_id": "F1"}
    for _ in range(2):
        db = database.SessionLocal()
        try:
            asyncio.run(webhook._handle_lead_ad(db, value))
        finally:
            db.close()

    c = _counts()
    assert c["leads"] == 1
    assert c["events"] == 1


def test_handle_lead_ad_releases_event_when_persist_fails(fresh_db, monkeypatch):
    """Si el commit del Lead falla DESPUÉS de reclamar el evento, se libera el
    ProcessedEvent (vía _release_event) para que Meta pueda reintentar — si no, el
    lead quedaría marcado como procesado y se perdería para siempre."""
    async def fake_lead(leadgen_id):
        return {"field_data": [{"name": "full_name", "values": ["Falla"]}]}

    monkeypatch.setattr(webhook, "get_lead_data", fake_lead)
    monkeypatch.setattr(webhook, "fire_hot_lead", lambda payload: None)

    released = []
    real_release = webhook._release_event

    def spy_release(db, event_id):
        released.append(event_id)
        return real_release(db, event_id)

    monkeypatch.setattr(webhook, "_release_event", spy_release)

    # Forzar el fallo de persistencia SOLO en el commit del Lead. Identificamos ese
    # commit por la presencia de un Lead entre los objetos pendientes (db.new); los
    # commits previos (_claim_event, get_or_create_conversation) pasan normalmente.
    db = database.SessionLocal()
    original_commit = db.commit

    def flaky_commit():
        if any(isinstance(obj, models.Lead) for obj in db.new):
            raise IntegrityError("forced", None, Exception("boom"))
        return original_commit()

    db.commit = flaky_commit
    try:
        asyncio.run(webhook._handle_lead_ad(db, {"leadgen_id": "222", "form_id": "F1"}))
    finally:
        db.commit = original_commit
        db.close()

    # Se intentó liberar el evento reclamado.
    assert released == ["lead_222"]
    # Y efectivamente quedó liberado: ni Lead ni ProcessedEvent persistidos.
    c = _counts()
    assert c["leads"] == 0
    assert c["events"] == 0


# ───────────────────────── _claim_event: referencia fuerte a la purga ───────

def test_claim_event_retiene_referencia_fuerte_a_la_task_de_purga(fresh_db, monkeypatch):
    """C3: al reclamar un evento con un loop corriendo se lanza la purga fire-and-forget;
    debe quedar retenida en _pending_tasks (referencia fuerte) hasta terminar, donde el
    done_callback la descarta. Sin esto el GC podría cancelarla."""
    async def fake_purge():
        return None

    monkeypatch.setattr(webhook, "_maybe_purge_events", fake_purge)

    async def run():
        webhook._pending_tasks.clear()
        db = database.SessionLocal()
        try:
            assert webhook._claim_event(db, "ev_strongref") is True
            # Quedó retenida con referencia fuerte mientras está en vuelo.
            assert len(webhook._pending_tasks) == 1
            # Dejar correr la task: el done_callback la descarta.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert len(webhook._pending_tasks) == 0
        finally:
            db.close()

    asyncio.run(run())


# ───────────────────────── _deliver ────────────────────────────────────────

def test_deliver_returns_true_on_success(fresh_db):
    async def ok_send():
        return {"sent": True}

    result = asyncio.run(webhook._deliver(ok_send(), "whatsapp", "conv-1"))
    assert result is True


def test_deliver_returns_false_and_captures_on_failure(fresh_db, monkeypatch):
    """Un fallo de envío NO se propaga: _deliver lo captura, lo registra y devuelve False
    (el turno ya quedó persistido; el envío requiere reintento fuera de banda)."""
    captured = []
    monkeypatch.setattr(
        webhook, "record_error",
        lambda where, exc=None, **ctx: captured.append((where, ctx)),
    )

    async def failing_send():
        raise RuntimeError("Meta rechazó el envío")

    result = asyncio.run(webhook._deliver(failing_send(), "instagram", "conv-9"))
    assert result is False
    # Quedó rastro explícito (no se traga en el except global de _process_event).
    assert len(captured) == 1
    where, ctx = captured[0]
    assert where == "webhook._deliver"
    assert ctx["channel"] == "instagram"
    assert ctx["conversation_id"] == "conv-9"
