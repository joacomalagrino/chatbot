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
import services.lead_service as lead_service
from services.conversation_service import get_or_create_conversation
from services.lead_service import update_lead_from_message


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


def test_whatsapp_transient_failure_releases_claim_so_retry_recovers(fresh_db, monkeypatch):
    """Si record_turn falla en la 1ª entrega (timeout de Claude, hipo de DB —más probable
    bajo ráfaga), el evento reclamado se LIBERA: el reintento de Meta (mismo wamid) reprocesa
    y el lead recibe respuesta. Sin el _release_event, el claim quedaba quemado y el inbound
    se PERDÍA (medio turno persistido, usuario nunca contestado)."""
    calls = {"n": 0}

    async def flaky_ai(project, project_config, message, history):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Claude timeout (transitorio)")
        return "respuesta ok"

    monkeypatch.setattr(convsvc, "get_ai_response", flaky_ai)

    body = {
        "entry": [{
            "changes": [{
                "field": "messages",
                "value": {"messages": [{
                    "id": "wamid_retry", "type": "text",
                    "from": "5491100000000", "text": {"body": "me interesa"},
                }]},
            }],
        }],
    }

    asyncio.run(webhook._process_event(body))   # 1ª entrega: Claude falla
    asyncio.run(webhook._process_event(body))   # Meta REINTENTA el mismo wamid

    db = database.SessionLocal()
    try:
        # El evento quedó reclamado tras el retry exitoso (idempotencia futura intacta).
        assert db.query(models.ProcessedEvent).count() == 1
        # Y el lead efectivamente recibió respuesta: existe el Message del asistente.
        assert (
            db.query(models.Message)
            .filter(models.Message.role == "assistant", models.Message.content == "respuesta ok")
            .count()
            == 1
        )
    finally:
        db.close()


def test_instagram_transient_failure_releases_claim_so_retry_recovers(fresh_db, monkeypatch):
    """Mismo contrato que WhatsApp pero por el canal de Instagram (_handle_ig_event)."""
    calls = {"n": 0}

    async def flaky_ai(project, project_config, message, history):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Claude timeout (transitorio)")
        return "respuesta ok"

    monkeypatch.setattr(convsvc, "get_ai_response", flaky_ai)

    body = {
        "entry": [{
            "messaging": [{
                "sender": {"id": "ig_user_1"},
                "message": {"mid": "mid_retry", "text": "hola"},
            }],
        }],
    }

    asyncio.run(webhook._process_event(body))   # 1ª entrega: Claude falla
    asyncio.run(webhook._process_event(body))   # Meta REINTENTA el mismo mid

    db = database.SessionLocal()
    try:
        assert db.query(models.ProcessedEvent).count() == 1
        assert (
            db.query(models.Message)
            .filter(models.Message.role == "assistant", models.Message.content == "respuesta ok")
            .count()
            == 1
        )
    finally:
        db.close()


def test_whatsapp_transient_failure_duplicates_user_message(fresh_db, monkeypatch):
    """REGRESIÓN del trade-off documentado en _handle_change: cuando record_turn falla en la
    1ª entrega, el Message del usuario YA fue commiteado (record_turn lo persiste ANTES del
    await a Claude) y el db.rollback() del except NO lo revierte. El reintento de Meta vuelve
    a insertar un Message de usuario idéntico → queda DUPLICADO.

    Se acepta a conciencia (mejor duplicar el inbound que perder el lead). Este test fija el
    comportamiento ACTUAL: tras fallo transitorio + reintento hay UN solo turno respondido
    (1 Message de asistente, lead recuperado) pero DOS Messages de usuario idénticos. Si algún
    día se deduplica (id externo en Message), este test debe actualizarse junto con el cambio."""
    calls = {"n": 0}

    async def flaky_ai(project, project_config, message, history):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Claude timeout (transitorio)")
        return "respuesta ok"

    monkeypatch.setattr(convsvc, "get_ai_response", flaky_ai)

    body = {
        "entry": [{
            "changes": [{
                "field": "messages",
                "value": {"messages": [{
                    "id": "wamid_dup_retry", "type": "text",
                    "from": "5491100000000", "text": {"body": "me interesa"},
                }]},
            }],
        }],
    }

    asyncio.run(webhook._process_event(body))   # 1ª entrega: Claude falla
    asyncio.run(webhook._process_event(body))   # Meta REINTENTA el mismo wamid

    db = database.SessionLocal()
    try:
        # El lead se recupera: el evento quedó reclamado y hay respuesta del asistente (1 sola).
        assert db.query(models.ProcessedEvent).count() == 1
        assert (
            db.query(models.Message)
            .filter(models.Message.role == "assistant")
            .count()
            == 1
        )
        # Pero el Message del usuario quedó DUPLICADO (el commit previo al fallo no se revierte).
        user_msgs = (
            db.query(models.Message).filter(models.Message.role == "user").all()
        )
        assert len(user_msgs) == 2
        assert [m.content for m in user_msgs] == ["me interesa", "me interesa"]
    finally:
        db.close()


def test_instagram_transient_failure_duplicates_user_message(fresh_db, monkeypatch):
    """Mismo trade-off de duplicación que WhatsApp pero por Instagram (_handle_ig_event)."""
    calls = {"n": 0}

    async def flaky_ai(project, project_config, message, history):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Claude timeout (transitorio)")
        return "respuesta ok"

    monkeypatch.setattr(convsvc, "get_ai_response", flaky_ai)

    body = {
        "entry": [{
            "messaging": [{
                "sender": {"id": "ig_user_dup"},
                "message": {"mid": "mid_dup_retry", "text": "hola"},
            }],
        }],
    }

    asyncio.run(webhook._process_event(body))   # 1ª entrega: Claude falla
    asyncio.run(webhook._process_event(body))   # Meta REINTENTA el mismo mid

    db = database.SessionLocal()
    try:
        assert db.query(models.ProcessedEvent).count() == 1
        assert (
            db.query(models.Message)
            .filter(models.Message.role == "assistant")
            .count()
            == 1
        )
        user_msgs = (
            db.query(models.Message).filter(models.Message.role == "user").all()
        )
        assert len(user_msgs) == 2
        assert [m.content for m in user_msgs] == ["hola", "hola"]
    finally:
        db.close()


def test_concurrent_lead_creation_same_conversation_is_handled(fresh_db, monkeypatch):
    """Dos turnos casi simultáneos de la MISMA conversación, cada uno detectando un dato de
    contacto distinto: ambos ven conversation.lead is None y crean Lead(conversation_id=...).
    leads.conversation_id es UNIQUE → el commit perdedor choca con IntegrityError. El handler
    debe reconciliar (releer el Lead ganador y reaplicar) en vez de tirar el merge / 500.

    Verifica: sin errores, exactamente 1 Lead, y AMBOS datos de contacto mergeados (no se
    pierde el del turno perdedor)."""
    monkeypatch.setattr(lead_service, "fire_hot_lead", lambda s: None)

    # Conversación creada una sola vez (la race que probamos es la del Lead, no la conv).
    setup = database.SessionLocal()
    conv = get_or_create_conversation(setup, "wa_leadrace", "agencia", "whatsapp", contact_phone=None)
    conv_id = conv.id
    setup.close()

    messages = {
        0: "mi mail es persona@ejemplo.com",
        1: "mi tel es 1123456780",
    }

    def worker(i, barrier):
        db = database.SessionLocal()
        try:
            c = db.get(models.Conversation, conv_id)
            barrier.wait()
            update_lead_from_message(db, c, messages[i])
        finally:
            db.close()

    errors = _run_threads(worker, 2)
    assert errors == [], errors

    db = database.SessionLocal()
    try:
        leads = db.query(models.Lead).filter_by(conversation_id=conv_id).all()
        assert len(leads) == 1, "la UNIQUE constraint debe dejar una sola fila"
        lead = leads[0]
        # El merge de ambos turnos sobrevive: ni el email ni el teléfono se perdieron.
        assert lead.email == "persona@ejemplo.com"
        assert lead.phone == "1123456780"
    finally:
        db.close()


def test_record_turn_releases_connection_during_claude_await(fresh_db, monkeypatch):
    """RECURSO (bug "pool exhaustion autoinfligida"): durante el await largo a Claude,
    record_turn NO debe retener la conexión de la Session.

    Con pool 10+20, si cada turno sostiene su conexión checked-out e idle-in-transaction
    durante los ~segundos que tarda Claude, una ráfaga de ~30 turnos concurrentes agota el
    pool y el resto muere con "QueuePool limit ... reached" (señal #1, reproducida por el
    loadtest). El fix soltó la conexión (db.commit()) tras armar el history y antes del await.

    Este test fija el contrato de forma DETERMINISTA (sin timing real): mockeamos get_ai_response
    y, MIENTRAS corre el await, verificamos que:
      (1) la Session que llamó a record_turn ya cerró su transacción -> db.in_transaction() es
          False (soltó la conexión). Antes del fix, la query de `history` dejaba una transacción
          de lectura abierta y esto era True durante todo el await;
      (2) se puede sacar y USAR otra conexión del pool en paralelo (prueba de humo del recurso).
    Y que el turno se persiste igual (respuesta + Message del asistente), o sea que soltar la
    conexión no cambió el comportamiento observable.
    """
    db = database.SessionLocal()
    conv = get_or_create_conversation(db, "web_await", "agencia", "web")
    observed = {}

    async def probe_ai(project, project_config, message, history):
        # Durante el await: la Session de record_turn NO debe estar en transacción.
        observed["in_transaction"] = db.in_transaction()
        # Y el pool debe poder entregar OTRA conexión para trabajar en paralelo.
        other = database.SessionLocal()
        try:
            other.query(models.Conversation).count()
            observed["other_ok"] = True
        finally:
            other.close()
        return "respuesta ok"

    monkeypatch.setattr(convsvc, "get_ai_response", probe_ai)

    resp = asyncio.run(convsvc.record_turn(db, conv, "hola"))
    db.close()

    assert observed.get("in_transaction") is False, (
        "record_turn retuvo la conexión (transacción abierta) durante el await a Claude"
    )
    assert observed.get("other_ok") is True
    assert resp == "respuesta ok"

    check = database.SessionLocal()
    try:
        # El turno se persistió igual: mensaje del usuario + respuesta del asistente.
        assert (
            check.query(models.Message)
            .filter(models.Message.role == "assistant", models.Message.content == "respuesta ok")
            .count()
            == 1
        )
        assert check.query(models.Message).filter(models.Message.role == "user").count() == 1
    finally:
        check.close()


def test_stream_turn_releases_connection_during_claude_await(fresh_db, monkeypatch):
    """Mismo contrato de recurso que record_turn pero para stream_turn: la conexión se suelta
    ANTES de empezar a iterar el stream (el commit va antes del async for). Verifica que durante
    el stream la Session no está en transacción y que otra conexión del pool queda disponible,
    y que el Message del asistente se persiste con el texto acumulado en el finally."""
    db = database.SessionLocal()
    conv = get_or_create_conversation(db, "web_stream_await", "agencia", "web")
    observed = {}

    async def probe_stream(project, project_config, message, history):
        observed["in_transaction"] = db.in_transaction()
        other = database.SessionLocal()
        try:
            other.query(models.Conversation).count()
            observed["other_ok"] = True
        finally:
            other.close()
        yield "respuesta ok"

    monkeypatch.setattr(convsvc, "stream_ai_response", probe_stream)

    async def drain():
        return [delta async for delta in convsvc.stream_turn(db, conv, "hola")]

    chunks = asyncio.run(drain())
    db.close()

    assert observed.get("in_transaction") is False, (
        "stream_turn retuvo la conexión (transacción abierta) durante el await al stream"
    )
    assert observed.get("other_ok") is True
    assert chunks == ["respuesta ok"]

    check = database.SessionLocal()
    try:
        assert (
            check.query(models.Message)
            .filter(models.Message.role == "assistant", models.Message.content == "respuesta ok")
            .count()
            == 1
        )
    finally:
        check.close()
