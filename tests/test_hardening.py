"""Afilados pre-prod del pipeline: purga de processed_events, cota de longitud del mensaje
del usuario, y historial acotado en la query (no cargar toda la conversación)."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import database
import models
import routers.webhook as webhook
import services.conversation_service as convsvc
from services.conversation_service import (
    MAX_HISTORY_MESSAGES,
    MAX_MESSAGE_CHARS,
    get_or_create_conversation,
    record_turn,
)


def _naive_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture()
def db():
    models.Base.metadata.drop_all(bind=database.engine)
    models.Base.metadata.create_all(bind=database.engine)
    sess = database.SessionLocal()
    yield sess
    sess.close()
    models.Base.metadata.drop_all(bind=database.engine)


def test_purge_old_events_borra_viejos_conserva_recientes(db):
    db.add(models.ProcessedEvent(event_id="viejo", created_at=_naive_utc() - timedelta(days=10)))
    db.add(models.ProcessedEvent(event_id="reciente", created_at=_naive_utc()))
    db.commit()
    n = webhook.purge_old_events(db, days=7)
    assert n == 1
    assert [e.event_id for e in db.query(models.ProcessedEvent).all()] == ["reciente"]


def test_record_turn_capa_el_mensaje(db, monkeypatch):
    async def fake_ai(project, cfg, message, history):
        return "ok"

    monkeypatch.setattr(convsvc, "get_ai_response", fake_ai)
    conv = get_or_create_conversation(db, "s1", "agencia", "web")
    asyncio.run(record_turn(db, conv, "x" * (MAX_MESSAGE_CHARS + 500)))
    stored = db.query(models.Message).filter_by(role="user").first()
    assert len(stored.content) == MAX_MESSAGE_CHARS


def test_record_turn_acota_el_historial(db, monkeypatch):
    capturado = {}

    async def fake_ai(project, cfg, message, history):
        capturado["history"] = history
        return "ok"

    monkeypatch.setattr(convsvc, "get_ai_response", fake_ai)
    conv = get_or_create_conversation(db, "s2", "agencia", "web")
    # Sembrar MÁS mensajes que el tope → el historial debe quedar acotado.
    for i in range(MAX_HISTORY_MESSAGES + 20):
        db.add(models.Message(
            conversation_id=conv.id,
            role="user" if i % 2 == 0 else "assistant",
            content="m%d" % i,
        ))
    db.commit()
    asyncio.run(record_turn(db, conv, "nuevo"))
    assert len(capturado["history"]) == MAX_HISTORY_MESSAGES
