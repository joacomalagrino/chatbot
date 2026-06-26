"""Observabilidad: que los fallos SILENCIOSOS dejen rastro.

En este captador de leads un error no visto = lead perdido. Acá se fija que:
- las excepciones en las tareas en BACKGROUND del webhook se LOGUEAN y se registran
  (FastAPI no las reporta, así que sin esto se perderían en silencio),
- los fallos de envío y de fetch a Graph quedan registrados con contexto,
- el registro en memoria respeta el tope (anillo) y el orden (más nuevo primero),
- `configure_logging` aplica el nivel desde LOG_LEVEL,
- el endpoint /leads/errors requiere la auth admin.
"""
import asyncio
import hashlib
import hmac
import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

import database
import main
import models
import observability
import routers.webhook as webhook
import services.conversation_service as convsvc
import services.meta_service as meta_service

SECRET = "test-secret"
ADMIN = {"Authorization": "Bearer test-admin"}


@pytest.fixture()
def client(monkeypatch):
    models.Base.metadata.drop_all(bind=database.engine)
    models.Base.metadata.create_all(bind=database.engine)

    async def fake_ai(p, c, m, h):
        return "respuesta del bot"

    async def fake_send(*a, **k):
        return {}

    monkeypatch.setattr(convsvc, "get_ai_response", fake_ai)
    monkeypatch.setattr(webhook, "send_whatsapp_message", fake_send)
    monkeypatch.setattr(webhook, "send_instagram_message", fake_send)
    # Aislar el registro en memoria entre tests.
    observability.clear_errors()
    with TestClient(main.app) as c:
        yield c
    observability.clear_errors()
    models.Base.metadata.drop_all(bind=database.engine)


def _sign(b: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), b, hashlib.sha256).hexdigest()


def _wa(wamid: str, phone: str, text: str) -> bytes:
    return json.dumps({"entry": [{"changes": [{"field": "messages", "value": {"messages": [
        {"id": wamid, "type": "text", "from": phone, "text": {"body": text}},
    ]}}]}]}).encode()


# ───────────────────── registro de errores (unidad) ─────────────────────────

def test_record_error_y_recent_errors_orden_y_contexto():
    observability.clear_errors()
    observability.record_error("ctx.a", ValueError("boom"), lead="L1")
    observability.record_error("ctx.b", RuntimeError("crash"), lead="L2")

    errs = observability.recent_errors()
    assert observability.error_count() == 2
    # Más nuevo primero.
    assert errs[0]["context"] == "ctx.b"
    assert errs[1]["context"] == "ctx.a"
    assert errs[0]["error_type"] == "RuntimeError"
    assert "crash" in errs[0]["error"]
    assert errs[0]["details"] == {"lead": "L2"}
    assert "ts" in errs[0]


def test_record_error_sin_excepcion():
    observability.clear_errors()
    observability.record_error("ctx.solo", status=503, url="https://x")
    e = observability.recent_errors()[0]
    assert e["error"] is None
    assert e["error_type"] is None
    assert e["details"] == {"status": 503, "url": "https://x"}


def test_registro_es_anillo_acotado():
    observability.clear_errors()
    total = observability.MAX_RECENT_ERRORS + 25
    for i in range(total):
        observability.record_error("ctx", RuntimeError("e%d" % i), n=i)
    # No crece sin límite: se queda en MAX_RECENT_ERRORS.
    assert observability.error_count() == observability.MAX_RECENT_ERRORS
    errs = observability.recent_errors()
    # El más nuevo es el último insertado; los más viejos se descartaron.
    assert errs[0]["details"] == {"n": total - 1}
    assert all(e["details"]["n"] >= total - observability.MAX_RECENT_ERRORS for e in errs)


# ───────────────────── configure_logging: nivel por env ─────────────────────

@pytest.fixture()
def restore_root_level():
    """configure_logging() toca el nivel del root logger (estado global): restaurarlo
    al salir para no contaminar el resto de la suite."""
    prev = logging.getLogger().level
    yield
    logging.getLogger().setLevel(prev)


def test_configure_logging_respeta_log_level(monkeypatch, restore_root_level):
    # Forzar reconfiguración (el módulo es idempotente: marca _logging_configured).
    monkeypatch.setattr(observability, "_logging_configured", False)
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    observability.configure_logging()
    assert logging.getLogger().level == logging.WARNING


def test_configure_logging_invalid_level_cae_a_info(monkeypatch, restore_root_level):
    monkeypatch.setattr(observability, "_logging_configured", False)
    monkeypatch.setenv("LOG_LEVEL", "NO_ES_UN_NIVEL")
    observability.configure_logging()
    assert logging.getLogger().level == logging.INFO


# ───────────────────── background del webhook: no más silencio ──────────────

def test_excepcion_en_background_se_loguea_y_se_registra(client, monkeypatch, caplog):
    """El punto clave: si una tarea en background lanza, antes se perdía en silencio.
    Ahora debe quedar logueada y en el registro de errores recientes."""
    def boom(db, change):
        raise RuntimeError("explotó procesando el change")

    # _handle_change corre DENTRO de _process_event (la tarea en background).
    monkeypatch.setattr(webhook, "_handle_change", boom)

    body = _wa("wamid.BG", "5491100000000", "hola")
    with caplog.at_level(logging.ERROR):
        r = client.post("/webhook/meta", content=body, headers={"X-Hub-Signature-256": _sign(body)})
    # El webhook igual responde 200 (procesa en background); el fallo no debe romper la respuesta.
    assert r.status_code == 200

    # Quedó logueado con contexto (el field del webhook).
    assert any("Error procesando webhook Meta" in rec.message for rec in caplog.records)

    # Y quedó en el registro de errores recientes.
    errs = observability.recent_errors()
    assert any(e["context"] == "webhook._process_event" for e in errs)
    rec = next(e for e in errs if e["context"] == "webhook._process_event")
    assert rec["error_type"] == "RuntimeError"
    assert rec["details"] == {"fields": ["messages"]}


def test_fallo_de_envio_se_registra(client, monkeypatch):
    """Si el envío a Meta falla, el turno se persiste igual PERO el fallo debe registrarse
    (si no, el lead recibió respuesta generada que nunca llegó, en silencio)."""
    async def failing_send(*a, **k):
        raise RuntimeError("Meta rechazó el envío")

    monkeypatch.setattr(webhook, "send_whatsapp_message", failing_send)
    body = _wa("wamid.SF", "5491100002222", "hola")
    r = client.post("/webhook/meta", content=body, headers={"X-Hub-Signature-256": _sign(body)})
    assert r.status_code == 200

    errs = observability.recent_errors()
    deliver = [e for e in errs if e["context"] == "webhook._deliver"]
    assert len(deliver) == 1
    assert deliver[0]["details"]["channel"] == "whatsapp"


# ───────────────────── fetch a Graph: registra el fallo ─────────────────────

def test_graph_fetch_red_agotada_registra_error(monkeypatch):
    """Cuando un fetch a Graph agota los reintentos por red, debe quedar en el registro."""
    observability.clear_errors()

    class FakeClient:
        is_closed = False

        async def request(self, *a, **k):
            raise httpx.ConnectError("conn refused")

    async def fake_sleep(_):
        return None

    monkeypatch.setattr(meta_service, "_get_client", lambda: FakeClient())
    monkeypatch.setattr(meta_service.asyncio, "sleep", fake_sleep)

    with pytest.raises(httpx.TransportError):
        asyncio.run(meta_service._request_with_retry("GET", "https://graph/123", {}, attempts=2))

    errs = observability.recent_errors()
    assert any(e["context"] == "meta_service.request" for e in errs)


# ───────────────────── endpoint /leads/errors: auth admin ───────────────────

def test_errors_endpoint_requires_auth(client):
    # Sin token -> 401 (la auth admin se aplica a todo el router /leads).
    assert client.get("/leads/errors").status_code == 401
    # Con token válido -> 200 y shape esperado.
    r = client.get("/leads/errors", headers=ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert body == {"count": 0, "errors": []}


def test_errors_endpoint_muestra_errores_registrados(client, monkeypatch):
    """End-to-end: un fallo en background aparece luego en /leads/errors."""
    def boom(db, change):
        raise RuntimeError("explotó")

    monkeypatch.setattr(webhook, "_handle_change", boom)
    body = _wa("wamid.E2E", "5491100000000", "hola")
    client.post("/webhook/meta", content=body, headers={"X-Hub-Signature-256": _sign(body)})

    r = client.get("/leads/errors", headers=ADMIN)
    assert r.status_code == 200
    data = r.json()
    assert data["count"] >= 1
    assert any(e["context"] == "webhook._process_event" for e in data["errors"])
