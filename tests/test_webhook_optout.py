"""Tests del opt-out de re-engagement disparado desde el inbound de WhatsApp.

Cuando un lead manda una palabra de baja (BAJA/STOP/CANCELAR…), el handler del webhook
marca reengage_opt_out=True en esa conversación, ADEMÁS de procesar el turno normalmente
(el ruteo y la respuesta no cambian). El selector proactivo (reengage_service) ya respeta
ese flag, así que acá también verificamos —de forma liviana— que un lead marcado por esta
vía queda excluido de find_reengageable_conversations.

Claude y Meta van stubeados (sin red), igual que en test_webhook_routing.
"""
import asyncio

import pytest

import database
import models
import routers.webhook as webhook
import services.conversation_service as convsvc
import services.reengage_service as reengage


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


def _inbound(text, *, phone="5491100000000", wamid="wamid_1"):
    """Despacha un inbound de texto de WhatsApp por el ruteo real del webhook."""
    change = {
        "field": "messages",
        "value": {"messages": [{
            "id": wamid, "type": "text",
            "from": phone, "text": {"body": text},
        }]},
    }
    db = database.SessionLocal()
    try:
        asyncio.run(webhook._handle_change(db, change))
    finally:
        db.close()


def _conv(phone="5491100000000"):
    db = database.SessionLocal()
    try:
        return db.query(models.Conversation).filter_by(session_id=f"wa_{phone}").first()
    finally:
        db.close()


# ───────────────────────── marca opt-out ────────────────────────────────────

def test_baja_marks_opt_out(fresh_db):
    """Un inbound "BAJA" marca reengage_opt_out=True en esa conversación."""
    _inbound("BAJA")
    conv = _conv()
    assert conv is not None
    assert conv.reengage_opt_out is True


@pytest.mark.parametrize("text", ["baja", " BAJA ", "Baja", "  stop", "Cancelar"])
def test_opt_out_is_case_insensitive_and_trimmed(fresh_db, text):
    """Variantes de mayúsculas/espacios de las keywords igual marcan el opt-out."""
    _inbound(text)
    conv = _conv()
    assert conv is not None
    assert conv.reengage_opt_out is True


def test_opt_out_still_processes_the_turn(fresh_db, monkeypatch):
    """El opt-out es ADITIVO: el turno se procesa y se responde igual que un inbound normal."""
    sent = []

    async def capture(phone, text, *args, **kwargs):
        sent.append((phone, text))
        return {"ok": True}

    monkeypatch.setattr(webhook, "send_whatsapp_reply", capture)

    _inbound("BAJA")

    # Se respondió el turno (no se cortó el flujo).
    assert sent == [("5491100000000", "respuesta")]
    db = database.SessionLocal()
    try:
        # user + assistant persistidos, evento reclamado: el ruteo normal no cambió.
        assert db.query(models.Message).count() == 2
        assert db.query(models.ProcessedEvent).count() == 1
    finally:
        db.close()


# ───────────────────────── NO marca en inbound normal ───────────────────────

def test_normal_message_does_not_mark_opt_out(fresh_db):
    """Un inbound normal no toca el flag (queda en False/None)."""
    _inbound("hola, busco un auto")
    conv = _conv()
    assert conv is not None
    assert conv.reengage_opt_out in (False, None)


def test_keyword_as_substring_does_not_mark(fresh_db):
    """Mencionar la palabra dentro de una frase NO marca baja (match exacto, no substring)."""
    _inbound("no me des de baja todavía")
    conv = _conv()
    assert conv is not None
    assert conv.reengage_opt_out in (False, None)


# ───────────────────────── integración liviana con el selector ──────────────

def test_selector_excludes_conversation_opted_out_via_webhook(fresh_db):
    """Un lead que pidió la baja por el inbound queda fuera del selector de re-engagement.

    Cierra la ventana de 24h (last_inbound_at viejo) para que, de no estar el opt-out,
    sería elegible; con el opt-out marcado debe quedar excluido.
    """
    from datetime import datetime, timedelta, timezone

    _inbound("BAJA", phone="5491155556666", wamid="wamid_optout")

    db = database.SessionLocal()
    try:
        conv = db.query(models.Conversation).filter_by(session_id="wa_5491155556666").first()
        assert conv.reengage_opt_out is True
        # Forzar ventana cerrada (inbound viejo) para que el único filtro restante sea el opt-out.
        # Naive UTC, igual que las columnas DateTime del modelo.
        conv.last_inbound_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=48)
        db.commit()

        eligible = reengage.find_reengageable_conversations(db)
        assert conv.id not in [c.id for c in eligible]
    finally:
        db.close()
