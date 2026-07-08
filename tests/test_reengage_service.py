"""Tests del re-engagement proactivo (services/reengage_service.py + POST /reengage/run).

El Meta service va MOCKEADO: send_whatsapp_template se reemplaza por un fake que registra
los envíos, así no se hace I/O real. Cubren:

- gating: flag apagado O sin plantilla => NO-OP (no manda, no marca reengaged_at),
- happy path: con flag + plantilla selecciona los elegibles, manda y marca reengaged_at,
- idempotencia: una segunda corrida NO re-manda al mismo lead,
- opt-out: respeta reengage_opt_out,
- ventana de 24h: no toca a los que están en ventana; toma los cerrados; respeta closing_within,
- endpoint /reengage/run: auth admin (fail-closed) + devuelve el resumen.
"""
import asyncio
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import database
import main
import models
import services.reengage_service as reengage
from config import get_settings

ADMIN = {"Authorization": "Bearer test-admin"}

# Reloj fijo para los tests: la ventana de 24h se evalúa contra este "now".
NOW = datetime(2026, 6, 28, 12, 0, 0)


@pytest.fixture()
def db():
    models.Base.metadata.drop_all(bind=database.engine)
    models.Base.metadata.create_all(bind=database.engine)
    session = database.SessionLocal()
    try:
        yield session
    finally:
        session.close()
        models.Base.metadata.drop_all(bind=database.engine)


@pytest.fixture()
def sends(monkeypatch):
    """Mockea el envío de plantilla de Meta. Registra (phone, template, lang) por llamada."""
    calls = []

    async def fake_send(phone, template_name, lang_code="es_AR", body_params=None):
        calls.append({"phone": phone, "template": template_name, "lang": lang_code})
        return {"messages": [{"id": "wamid_fake"}]}

    monkeypatch.setattr(reengage, "send_whatsapp_template", fake_send)
    return calls


@pytest.fixture()
def enable(monkeypatch):
    """Activa el re-engagement (flag + plantilla) en el settings que usa el servicio."""
    monkeypatch.setattr(reengage.settings, "reengage_enabled", True)
    monkeypatch.setattr(reengage.settings, "reengage_template_name", "reengage_v1")
    monkeypatch.setattr(reengage.settings, "whatsapp_reengage_template", "")
    monkeypatch.setattr(reengage.settings, "whatsapp_reengage_template_lang", "es_AR")
    # reengage_active() / reengage_template() leen los attrs de arriba: quedan activos.
    return reengage.settings


def _mk_conv(db, *, phone="5491111111111", channel="whatsapp", hours_ago=30,
             reengaged_at=None, opt_out=None, session_id=None):
    """Crea una conversación con last_inbound_at a `hours_ago` horas de NOW."""
    conv = models.Conversation(
        project="agencia",
        session_id=session_id or f"sess-{phone}-{hours_ago}-{reengaged_at}-{opt_out}",
        channel=channel,
        contact_phone=phone,
        last_inbound_at=(NOW - timedelta(hours=hours_ago)) if hours_ago is not None else None,
        reengaged_at=reengaged_at,
        reengage_opt_out=opt_out,
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


# ───────────────────────── gating: NO-OP seguro ────────────────────────────

def test_disabled_flag_is_noop(db, sends, monkeypatch):
    # Flag apagado (default) aunque haya plantilla: no manda nada.
    monkeypatch.setattr(reengage.settings, "reengage_enabled", False)
    monkeypatch.setattr(reengage.settings, "reengage_template_name", "reengage_v1")
    monkeypatch.setattr(reengage.settings, "whatsapp_reengage_template", "")
    conv = _mk_conv(db, hours_ago=30)

    result = asyncio.run(reengage.run_reengagement(db, now=NOW))

    assert result == {"skipped": "disabled", "selected": 0, "sent": 0, "failed": 0}
    assert sends == []
    db.refresh(conv)
    assert conv.reengaged_at is None


def test_enabled_but_no_template_is_noop(db, sends, monkeypatch):
    # Flag prendido pero sin plantilla (ni la nueva ni la legacy): no manda nada.
    monkeypatch.setattr(reengage.settings, "reengage_enabled", True)
    monkeypatch.setattr(reengage.settings, "reengage_template_name", "")
    monkeypatch.setattr(reengage.settings, "whatsapp_reengage_template", "")
    conv = _mk_conv(db, hours_ago=30)

    result = asyncio.run(reengage.run_reengagement(db, now=NOW))

    assert result["skipped"] == "disabled"
    assert sends == []
    db.refresh(conv)
    assert conv.reengaged_at is None


# ───────────────────────── happy path + idempotencia ───────────────────────

def test_sends_to_eligible_marks_and_is_idempotent(db, sends, enable):
    conv = _mk_conv(db, phone="5491111111111", hours_ago=30)

    result = asyncio.run(reengage.run_reengagement(db, now=NOW))

    assert result == {"skipped": None, "selected": 1, "sent": 1, "failed": 0}
    assert len(sends) == 1
    assert sends[0]["phone"] == "5491111111111"
    assert sends[0]["template"] == "reengage_v1"
    assert sends[0]["lang"] == "es_AR"
    db.refresh(conv)
    assert conv.reengaged_at == NOW

    # Segunda corrida: el lead ya tiene reengaged_at => NO se re-manda (idempotencia).
    later = NOW + timedelta(hours=1)
    result2 = asyncio.run(reengage.run_reengagement(db, now=later))
    assert result2 == {"skipped": None, "selected": 0, "sent": 0, "failed": 0}
    assert len(sends) == 1  # sigue siendo 1: no hubo segundo envío


def test_template_name_falls_back_to_whatsapp_reengage_template(db, sends, monkeypatch):
    # REENGAGE_TEMPLATE_NAME vacío pero WHATSAPP_REENGAGE_TEMPLATE seteada => se reusa esa.
    monkeypatch.setattr(reengage.settings, "reengage_enabled", True)
    monkeypatch.setattr(reengage.settings, "reengage_template_name", "")
    monkeypatch.setattr(reengage.settings, "whatsapp_reengage_template", "legacy_tpl")
    _mk_conv(db, hours_ago=30)

    asyncio.run(reengage.run_reengagement(db, now=NOW))

    assert len(sends) == 1
    assert sends[0]["template"] == "legacy_tpl"


# ───────────────────────── opt-out ──────────────────────────────────────────

def test_opt_out_is_skipped(db, sends, enable):
    opted_in = _mk_conv(db, phone="5491111111111", hours_ago=30, opt_out=False)
    opted_out = _mk_conv(db, phone="5492222222222", hours_ago=30, opt_out=True)

    result = asyncio.run(reengage.run_reengagement(db, now=NOW))

    assert result["sent"] == 1
    phones = [s["phone"] for s in sends]
    assert "5491111111111" in phones
    assert "5492222222222" not in phones  # opt-out excluido
    db.refresh(opted_out)
    assert opted_out.reengaged_at is None
    db.refresh(opted_in)
    assert opted_in.reengaged_at == NOW


# ───────────────────────── ventana de 24h ──────────────────────────────────

def test_in_window_is_not_selected(db, sends, enable):
    # Inbound hace 1h: ventana ABIERTA => no es elegible (Graph aún acepta free-form).
    conv = _mk_conv(db, hours_ago=1)
    eligible = reengage.find_reengageable_conversations(db, now=NOW)
    assert eligible == []

    result = asyncio.run(reengage.run_reengagement(db, now=NOW))
    assert result["sent"] == 0
    assert sends == []
    db.refresh(conv)
    assert conv.reengaged_at is None


def test_closed_window_is_selected(db, sends, enable):
    # Inbound hace 30h: ventana CERRADA => elegible.
    _mk_conv(db, hours_ago=30)
    eligible = reengage.find_reengageable_conversations(db, now=NOW)
    assert len(eligible) == 1


def test_none_inbound_counts_as_closed(db, sends, enable):
    # last_inbound_at NULL = ventana cerrada (fail-safe), elegible si tiene teléfono.
    _mk_conv(db, hours_ago=None)
    eligible = reengage.find_reengageable_conversations(db, now=NOW)
    assert len(eligible) == 1


def test_closing_within_margin_includes_soon_to_close(db, sends, enable):
    # Inbound hace 23h: cierra en 1h. Sin margen NO es elegible; con closing_within=2h SÍ.
    _mk_conv(db, hours_ago=23)
    assert reengage.find_reengageable_conversations(db, now=NOW) == []
    soon = reengage.find_reengageable_conversations(
        db, now=NOW, closing_within=timedelta(hours=2)
    )
    assert len(soon) == 1


def test_non_whatsapp_and_no_phone_excluded(db, sends, enable):
    _mk_conv(db, channel="instagram", phone="iguser", hours_ago=30, session_id="ig")
    _mk_conv(db, channel="whatsapp", phone=None, hours_ago=30, session_id="nophone")
    eligible = reengage.find_reengageable_conversations(db, now=NOW)
    assert eligible == []


def test_failed_send_does_not_mark_reengaged(db, enable, monkeypatch):
    # Si el envío explota, reengaged_at queda NULL (reintenta en la próxima corrida).
    async def boom(*a, **k):
        raise RuntimeError("graph down")

    monkeypatch.setattr(reengage, "send_whatsapp_template", boom)
    conv = _mk_conv(db, hours_ago=30)

    result = asyncio.run(reengage.run_reengagement(db, now=NOW))

    assert result == {"skipped": None, "selected": 1, "sent": 0, "failed": 1}
    db.refresh(conv)
    assert conv.reengaged_at is None


def test_atomic_claim_skips_lead_reengaged_concurrently(db, sends, enable, monkeypatch):
    # Carrera (TOCTOU): find_reengageable seleccionó el lead cuando reengaged_at era NULL,
    # pero OTRO runner lo reclamó justo antes de que este mande. Simulamos devolviendo desde
    # find un conv que YA tiene reengaged_at seteado: el UPDATE ... WHERE reengaged_at IS NULL
    # debe dar rowcount 0 → NO se manda la plantilla (nada de doble envío) y sent=0.
    already = _mk_conv(db, hours_ago=30, reengaged_at=NOW - timedelta(hours=1))
    monkeypatch.setattr(
        reengage, "find_reengageable_conversations", lambda *a, **k: [already]
    )

    result = asyncio.run(reengage.run_reengagement(db, now=NOW))

    assert result == {"skipped": None, "selected": 1, "sent": 0, "failed": 0}
    assert sends == []  # el claim atómico bloqueó el envío
    db.refresh(already)
    assert already.reengaged_at == NOW - timedelta(hours=1)  # no lo pisamos


# ───────────────────────── endpoint /reengage/run (auth + trigger) ──────────

@pytest.fixture()
def client():
    models.Base.metadata.drop_all(bind=database.engine)
    models.Base.metadata.create_all(bind=database.engine)
    with TestClient(main.app) as c:
        yield c
    models.Base.metadata.drop_all(bind=database.engine)


def test_endpoint_requires_admin(client):
    # Sin token => fail-closed (no autorizado), igual que /leads y /ads.
    r = client.post("/reengage/run")
    assert r.status_code in (401, 403, 503)


def test_endpoint_disabled_returns_noop_summary(client):
    # Con auth pero re-engagement apagado (default): NO-OP, devuelve skipped="disabled".
    get_settings.cache_clear()
    r = client.post("/reengage/run", headers=ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert body["skipped"] == "disabled"
    assert body["sent"] == 0
