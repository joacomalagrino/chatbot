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


def test_fire_hot_lead_guarda_referencia_fuerte_a_la_task(monkeypatch):
    """C3: con un event loop corriendo y NOTIFY_WEBHOOK_URL seteado, fire_hot_lead crea la
    task de POST y la retiene en _pending_tasks (referencia fuerte) hasta que termina, donde
    el done_callback la descarta. Sin esto el GC podría cancelarla y la alerta no se enviaría."""
    import asyncio

    from services import notify

    monkeypatch.setattr(notify.settings, "notify_webhook_url", "https://hook.example/x")

    posted = []

    async def fake_post(url, payload):
        posted.append((url, payload))

    monkeypatch.setattr(notify, "_post", fake_post)

    async def run():
        notify._pending_tasks.clear()
        notify.fire_hot_lead({"project": "agencia", "channel": "web", "name": "x"})
        # La task quedó retenida con referencia fuerte mientras está en vuelo.
        assert len(notify._pending_tasks) == 1
        # Dejar correr la task: el done_callback la descarta del set.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(notify._pending_tasks) == 0

    asyncio.run(run())
    assert posted == [("https://hook.example/x", posted[0][1])]


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


def test_inbound_wa_adopta_contact_phone_aunque_el_texto_no_traiga_numero(db, monkeypatch):
    """C1: un inbound de WhatsApp setea Lead.phone desde conversation.contact_phone
    (que persiste el webhook) aunque el texto del mensaje no contenga ningún número."""
    monkeypatch.setattr(lead_service, "fire_hot_lead", lambda s: None)

    conv = get_or_create_conversation(
        db, "wa_5491100000000", "agencia", "whatsapp", contact_phone="5491100000000"
    )
    changed = update_lead_from_message(db, conv, "hola, quiero más info")

    assert changed is True
    assert conv.lead is not None
    assert conv.lead.phone == "5491100000000"


def test_inbound_ig_adopta_contact_instagram_aunque_el_texto_no_traiga_handle(db, monkeypatch):
    """C1 (equivalente IG): el handle de IG que persiste el webhook (contact_instagram)
    se adopta en Lead.instagram aunque el texto no traiga un @handle."""
    monkeypatch.setattr(lead_service, "fire_hot_lead", lambda s: None)

    conv = get_or_create_conversation(
        db, "ig_abc123", "agencia", "instagram", contact_instagram="abc123"
    )
    changed = update_lead_from_message(db, conv, "hola, quiero más info")

    assert changed is True
    assert conv.lead is not None
    assert conv.lead.instagram == "abc123"
