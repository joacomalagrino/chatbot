"""Tests del endpoint de streaming SSE /chat/stream (TestClient + SQLite + stub de Claude).

El streaming es ADITIVO: no toca /chat ni el webhook. Acá verificamos que los deltas
yieldeados por el (stub del) cliente se emiten como frames SSE, que su concatenación
reconstruye la respuesta, y que el Message del asistente + el Lead quedan persistidos
DESPUÉS de que el stream termina."""
import json

import pytest
from fastapi.testclient import TestClient

import database
import main
import models
import services.conversation_service as convsvc


@pytest.fixture()
def client(monkeypatch):
    models.Base.metadata.drop_all(bind=database.engine)
    models.Base.metadata.create_all(bind=database.engine)

    # Stub del streaming de Claude: yieldea deltas conocidos (async generator), sin red.
    async def fake_stream(project, project_config, message, history):
        for delta in DELTAS:
            yield delta

    monkeypatch.setattr(convsvc, "stream_ai_response", fake_stream)

    with TestClient(main.app) as c:
        yield c

    models.Base.metadata.drop_all(bind=database.engine)


DELTAS = ["Hola", " ", "mundo", "!"]


def _read_frames(client, payload):
    """POSTea a /chat/stream y devuelve (status, content_type, lista de dicts de los frames)."""
    with client.stream("POST", "/chat/stream", json=payload) as r:
        status = r.status_code
        ctype = r.headers.get("content-type", "")
        body = "".join(r.iter_text())
    frames = []
    for chunk in body.split("\n\n"):
        chunk = chunk.strip()
        if not chunk.startswith("data:"):
            continue
        frames.append(json.loads(chunk[len("data:"):].strip()))
    return status, ctype, frames


def test_stream_happy_reconstructs_response(client):
    status, ctype, frames = _read_frames(
        client, {"session_id": "s1", "project": "agencia", "message": "hola"}
    )
    assert status == 200
    assert "text/event-stream" in ctype

    deltas = [f["delta"] for f in frames if "delta" in f]
    assert deltas == DELTAS
    assert "".join(deltas) == "Hola mundo!"

    done = [f for f in frames if f.get("done")]
    assert len(done) == 1
    assert done[0]["suggest_channels"] is False  # solo 2 mensajes (user + assistant)


def test_stream_persists_assistant_message_and_lead_after(client):
    _read_frames(
        client,
        {"session_id": "s2", "project": "agencia",
         "message": "hola, mi mail es juan@test.com"},
    )

    db = database.SessionLocal()
    try:
        conv = db.query(models.Conversation).filter_by(session_id="s2").first()
        assert conv is not None
        msgs = db.query(models.Message).filter_by(conversation_id=conv.id).all()
        roles = sorted(m.role for m in msgs)
        assert roles == ["assistant", "user"]
        # El Message del asistente quedó con el texto COMPLETO (todos los deltas concatenados).
        asst = next(m for m in msgs if m.role == "assistant")
        assert asst.content == "Hola mundo!"
        # El Lead se actualizó con el contacto detectado en el mensaje del usuario.
        lead = db.query(models.Lead).filter_by(conversation_id=conv.id).first()
        assert lead is not None
        assert lead.email == "juan@test.com"
    finally:
        db.close()


def test_stream_invalid_project_is_400(client):
    r = client.post("/chat/stream", json={
        "session_id": "s3", "project": "noexiste", "message": "hola",
    })
    assert r.status_code == 400


def test_stream_suggest_channels_after_three_exchanges(client):
    payload = {"session_id": "s4", "project": "agencia", "message": "hola"}
    # 3 intercambios completos => 6 mensajes => suggest_channels True en el 3ro.
    _read_frames(client, payload)
    _read_frames(client, payload)
    status, _, frames = _read_frames(client, payload)
    assert status == 200
    done = next(f for f in frames if f.get("done"))
    assert done["suggest_channels"] is True


def test_stream_emits_error_frame_on_failure(client, monkeypatch):
    """Si el turno falla a mitad, el endpoint emite un frame {"error": true} y cierra
    (no propaga la excepción ni rompe la respuesta HTTP)."""
    async def boom(project, project_config, message, history):
        raise RuntimeError("Claude caído")
        yield  # pragma: no cover — marca la función como async generator

    monkeypatch.setattr(convsvc, "stream_ai_response", boom)
    status, ctype, frames = _read_frames(
        client, {"session_id": "err1", "project": "agencia", "message": "hola"}
    )
    assert status == 200
    assert any(f.get("error") for f in frames)
    assert not any("delta" in f for f in frames)


def test_stream_logs_pool_exhaustion_on_failure(client, monkeypatch):
    """Si el turno de /chat/stream muere por saturación del pool, ahora la contabilizamos IGUAL
    que el path no-streaming (mismo helper/firma): antes el `except` la tragaba y, como el cuerpo
    del stream corre DESPUÉS del endpoint, el pool_exhaustion_observer de main.py nunca la veía —
    monitoreo ciego en el camino por defecto del widget. La respuesta al cliente NO cambia
    (sigue el 200 + frame {"error": true})."""
    import observability
    from sqlalchemy.exc import TimeoutError as PoolTimeoutError

    observability.reset_pool_exhaustion_count()
    observability.clear_errors()

    async def boom(project, project_config, message, history):
        raise PoolTimeoutError("QueuePool limit of size 10 overflow 20 reached")
        yield  # pragma: no cover — marca la función como async generator

    monkeypatch.setattr(convsvc, "stream_ai_response", boom)
    status, _, frames = _read_frames(
        client, {"session_id": "pool1", "project": "agencia", "message": "hola"}
    )

    # Respuesta al cliente sin cambios: 200 + frame de error, sin deltas.
    assert status == 200
    assert any(f.get("error") for f in frames)
    assert not any("delta" in f for f in frames)

    # Y ahora SÍ queda registrada para el monitoreo, con el path del stream.
    assert observability.pool_exhaustion_count() == 1
    pool_errs = [e for e in observability.recent_errors() if e["context"] == "db.pool_exhausted"]
    assert len(pool_errs) == 1
    assert pool_errs[0]["details"]["path"] == "/chat/stream"

    observability.reset_pool_exhaustion_count()
    observability.clear_errors()


def test_stream_persists_when_client_disconnects_midway(monkeypatch):
    """C2: si el cliente se desconecta a mitad del stream (GeneratorExit lanzado en el
    yield), el Message del asistente con lo generado hasta ese punto y el update del lead
    igual se persisten (la persistencia vive en un finally)."""
    import asyncio

    models.Base.metadata.drop_all(bind=database.engine)
    models.Base.metadata.create_all(bind=database.engine)

    async def fake_stream(project, project_config, message, history):
        for delta in ["parte1 ", "parte2 ", "parte3"]:
            yield delta

    monkeypatch.setattr(convsvc, "stream_ai_response", fake_stream)

    async def run():
        db = database.SessionLocal()
        try:
            conv = convsvc.get_or_create_conversation(db, "disc1", "agencia", "web")
            agen = convsvc.stream_turn(db, conv, "hola, mi mail es ana@test.com")
            # Consumir solo el primer delta y luego cerrar el generador: simula la
            # desconexión del cliente (Starlette hace aclose() sobre el async gen).
            first = await agen.__anext__()
            assert first == "parte1 "
            await agen.aclose()
        finally:
            db.close()

    asyncio.run(run())

    db = database.SessionLocal()
    try:
        conv = db.query(models.Conversation).filter_by(session_id="disc1").first()
        assert conv is not None
        asst = db.query(models.Message).filter_by(
            conversation_id=conv.id, role="assistant"
        ).first()
        # Persistió el Message del asistente con lo generado antes del corte.
        assert asst is not None
        assert asst.content == "parte1 "
        # Y el lead se actualizó pese a la desconexión.
        lead = db.query(models.Lead).filter_by(conversation_id=conv.id).first()
        assert lead is not None
        assert lead.email == "ana@test.com"
    finally:
        db.close()

    models.Base.metadata.drop_all(bind=database.engine)


def test_stream_does_not_break_plain_chat(client, monkeypatch):
    """El /chat no-streaming (fallback) sigue intacto: el streaming es aditivo. /chat usa
    get_ai_response (no stream_ai_response), así que lo stubeamos aparte para no tocar la red."""
    async def fake_ai(project, project_config, message, history):
        return "respuesta no-streaming"

    monkeypatch.setattr(convsvc, "get_ai_response", fake_ai)
    r = client.post("/chat/", json={
        "session_id": "plain1", "project": "agencia", "message": "hola",
    })
    assert r.status_code == 200
    assert r.json()["response"] == "respuesta no-streaming"
