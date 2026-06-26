"""Tests de concurrencia: eventos de webhook simultáneos.

Meta puede entregar eventos concurrentes del mismo lead/conversación (reintentos,
ráfagas de mensajes). Estos tests verifican LOCALMENTE (threads + SQLite) que:

- la idempotencia por id de evento (ProcessedEvent) deja pasar exactamente uno
  bajo inserción concurrente (PK + manejo de IntegrityError);
- la creación concurrente de la misma Conversation no duplica filas
  (get_or_create_conversation tolera la race);
- procesar varios eventos distintos de la MISMA conversación en paralelo no
  duplica la conversación ni pierde mensajes.

No llaman a Meta ni a Claude reales (se stubean). Corren contra la DB de test.
"""
import asyncio
import threading

import pytest

import database
import models
import routers.webhook as webhook
import services.conversation_service as convsvc
from services.conversation_service import get_or_create_conversation


@pytest.fixture()
def fresh_db(monkeypatch):
    models.Base.metadata.drop_all(bind=database.engine)
    models.Base.metadata.create_all(bind=database.engine)

    async def fake_ai(project, project_config, message, history):
        return "ok"

    async def fake_send(*args, **kwargs):
        return {}

    monkeypatch.setattr(convsvc, "get_ai_response", fake_ai)
    monkeypatch.setattr(webhook, "send_whatsapp_reply", fake_send)
    monkeypatch.setattr(webhook, "send_instagram_message", fake_send)
    yield
    models.Base.metadata.drop_all(bind=database.engine)


def _run_threads(target, n):
    barrier = threading.Barrier(n)
    errors = []

    def wrapped(i):
        try:
            target(i, barrier)
        except Exception as e:  # pragma: no cover - lo reportamos como fallo
            errors.append(repr(e))

    threads = [threading.Thread(target=wrapped, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def test_concurrent_claim_event_lets_exactly_one_win(fresh_db):
    """10 threads reclaman el MISMO event_id: solo uno gana, sin errores, 1 fila."""
    claimed = []

    def worker(i, barrier):
        db = database.SessionLocal()
        try:
            barrier.wait()
            claimed.append(webhook._claim_event(db, "evt_same"))
        finally:
            db.close()

    errors = _run_threads(worker, 10)
    assert errors == []
    assert claimed.count(True) == 1, claimed
    assert claimed.count(False) == 9

    db = database.SessionLocal()
    try:
        assert db.query(models.ProcessedEvent).count() == 1
    finally:
        db.close()


def test_concurrent_create_same_conversation_is_not_duplicated(fresh_db):
    """8 threads crean la misma session_id en paralelo: una sola conversación."""
    ids = []

    def worker(i, barrier):
        db = database.SessionLocal()
        try:
            barrier.wait()
            conv = get_or_create_conversation(db, "race_session", "agencia", "web")
            assert conv is not None
            ids.append(str(conv.id))
        finally:
            db.close()

    errors = _run_threads(worker, 8)
    assert errors == []
    assert len(set(ids)) == 1, f"se crearon conversaciones distintas: {set(ids)}"

    db = database.SessionLocal()
    try:
        assert db.query(models.Conversation).filter_by(session_id="race_session").count() == 1
    finally:
        db.close()


def test_concurrent_distinct_events_same_conversation(fresh_db):
    """6 eventos distintos (mismos remitente) procesados en paralelo, cada uno con
    su propia sesión (como en _process_event): 1 conversación, todos los mensajes."""

    def make_body(wamid, text):
        return {
            "entry": [{
                "changes": [{
                    "field": "messages",
                    "value": {"messages": [{
                        "id": wamid, "type": "text",
                        "from": "5491100000000", "text": {"body": text},
                    }]},
                }],
            }],
        }

    def worker(i, barrier):
        barrier.wait()
        asyncio.run(webhook._process_event(make_body(f"wamid_{i}", f"msg {i}")))

    errors = _run_threads(worker, 6)
    assert errors == []

    db = database.SessionLocal()
    try:
        assert db.query(models.Conversation).count() == 1
        # 6 mensajes de usuario + 6 respuestas del asistente.
        assert db.query(models.Message).count() == 12
        # 6 eventos distintos reclamados.
        assert db.query(models.ProcessedEvent).count() == 6
    finally:
        db.close()


def test_concurrent_same_event_processed_once(fresh_db):
    """El MISMO evento (mismo wamid) procesado en paralelo: idempotente.
    Solo un par user+assistant se persiste pese a N entregas simultáneas."""

    body = {
        "entry": [{
            "changes": [{
                "field": "messages",
                "value": {"messages": [{
                    "id": "wamid_dup", "type": "text",
                    "from": "5491100000000", "text": {"body": "hola"},
                }]},
            }],
        }],
    }

    def worker(i, barrier):
        barrier.wait()
        asyncio.run(webhook._process_event(body))

    errors = _run_threads(worker, 8)
    assert errors == []

    db = database.SessionLocal()
    try:
        assert db.query(models.Conversation).count() == 1
        assert db.query(models.Message).count() == 2  # un solo turno
        assert db.query(models.ProcessedEvent).count() == 1
    finally:
        db.close()
