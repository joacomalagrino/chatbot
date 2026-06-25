"""Notificación de lead caliente: se dispara solo en la transición a hot."""
import pytest

import database
import models
import services.lead_service as lead_service
from services.conversation_service import get_or_create_conversation
from services.lead_service import update_lead_from_message


@pytest.fixture()
def db():
    models.Base.metadata.drop_all(bind=database.engine)
    models.Base.metadata.create_all(bind=database.engine)
    s = database.SessionLocal()
    yield s
    s.close()
    models.Base.metadata.drop_all(bind=database.engine)


def test_fire_hot_lead_sin_webhook_no_falla():
    from services.notify import fire_hot_lead
    # Sin NOTIFY_WEBHOOK_URL (default ""), solo loguea y no lanza.
    fire_hot_lead({"project": "agencia", "channel": "web", "name": "x"})


def test_notifica_en_transicion_a_hot(db, monkeypatch):
    calls = []
    monkeypatch.setattr(lead_service, "fire_hot_lead", lambda s: calls.append(s))

    conv = get_or_create_conversation(db, "s1", "agencia", "web")
    # phone + email → caliente: notifica una vez.
    update_lead_from_message(db, conv, "soy juan, tel 1123456789 0 y mail juan@x.com")
    assert len(calls) == 1
    assert calls[0]["project"] == "agencia"

    # Otro cambio (agrega instagram) pero ya estaba hot → NO vuelve a notificar.
    update_lead_from_message(db, conv, "mi ig es @juani")
    assert len(calls) == 1
