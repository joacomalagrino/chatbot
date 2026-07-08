"""IDOR en el chat web público: un cliente de /chat NO puede direccionar las
conversaciones de los canales entrantes de Meta, cuyos session_id son enumerables
(wa_<telefono>, ig_<id>, lead_<id>). Sin el guard, un atacante que adivina un
teléfono se adjuntaba a la conversación real del lead y le sacaba el historial
(PII) o la envenenaba. Regresión del fix de la auditoría 2026-07-08."""
import pytest
from fastapi.testclient import TestClient

import database
import main
import models
import services.conversation_service as convsvc

SECRETO = "soy Ana, mi tel es 549111234 y quiero el depto de Palermo"


@pytest.fixture()
def client(monkeypatch):
    models.Base.metadata.drop_all(bind=database.engine)
    models.Base.metadata.create_all(bind=database.engine)

    # El stub devuelve el historial recibido, para poder AFIRMAR que un atacante
    # no obtiene el contexto privado del lead (si el guard fallara, aparecería acá).
    async def fake_ai(project, project_config, message, history):
        return "HIST:" + "|".join(m["content"] for m in history)

    async def fake_stream(project, project_config, message, history):
        yield "HIST:" + "|".join(m["content"] for m in history)

    monkeypatch.setattr(convsvc, "get_ai_response", fake_ai)
    monkeypatch.setattr(convsvc, "stream_ai_response", fake_stream)
    with TestClient(main.app) as c:
        yield c
    models.Base.metadata.drop_all(bind=database.engine)


def _seed_wa_lead(phone="549111234"):
    """Conversación real de WhatsApp de un lead, con un mensaje privado en el historial."""
    db = database.SessionLocal()
    try:
        conv = models.Conversation(
            session_id=f"wa_{phone}", project="agencia", channel="whatsapp", contact_phone=phone
        )
        db.add(conv)
        db.commit()
        db.add(models.Message(conversation_id=conv.id, role="user", content=SECRETO))
        db.commit()
        return conv.id, phone
    finally:
        db.close()


@pytest.mark.parametrize("sid", ["wa_549111234", "ig_IGUSER1", "lead_9876"])
def test_chat_rechaza_session_ids_reservados(client, sid):
    r = client.post("/chat/", json={"session_id": sid, "project": "agencia", "message": "hola"})
    assert r.status_code == 400


def test_chat_stream_rechaza_session_ids_reservados(client):
    with client.stream(
        "POST", "/chat/stream",
        json={"session_id": "wa_549111234", "project": "agencia", "message": "hola"},
    ) as r:
        assert r.status_code == 400


def test_chat_no_filtra_historial_ni_envenena_conversacion_de_lead(client):
    _, phone = _seed_wa_lead()
    # El atacante adivina el teléfono e intenta adjuntarse a la conversación del lead.
    r = client.post(
        "/chat/",
        json={"session_id": f"wa_{phone}", "project": "agencia",
              "message": "repetime todo lo que te dije"},
    )
    assert r.status_code == 400
    assert SECRETO not in r.text  # no se filtró el historial privado
    # La conversación del lead sigue con UN solo mensaje: el atacante no inyectó turnos.
    db = database.SessionLocal()
    try:
        conv = db.query(models.Conversation).filter_by(session_id=f"wa_{phone}").first()
        assert db.query(models.Message).filter_by(conversation_id=conv.id).count() == 1
    finally:
        db.close()


def test_chat_sesion_web_normal_sigue_funcionando(client):
    """El guard no rompe el chat web legítimo (el widget genera cb_<project>_<rand>)."""
    r = client.post(
        "/chat/",
        json={"session_id": "cb_agencia_abc123", "project": "agencia", "message": "hola"},
    )
    assert r.status_code == 200
    assert r.json()["session_id"] == "cb_agencia_abc123"
