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
    monkeypatch.setattr(webhook, "send_whatsapp_reply", fake_send)
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

    monkeypatch.setattr(webhook, "send_whatsapp_reply", failing_send)
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


# ───────────────────── saturación del pool de DB (señal #1) ──────────────────

from sqlalchemy.exc import OperationalError, TimeoutError as PoolTimeoutError

import database


def test_is_pool_exhaustion_detecta_queuepool_timeout():
    """La TimeoutError de SQLAlchemy ("QueuePool limit ... reached") es saturación."""
    exc = PoolTimeoutError("QueuePool limit of size 10 overflow 20 reached, connection timed out")
    assert database.is_pool_exhaustion(exc) is True


def test_is_pool_exhaustion_detecta_operationalerror_de_postgres():
    """Postgres reporta el agotamiento del lado servidor por mensaje, no como pool timeout."""
    exc = OperationalError("SELECT 1", {}, Exception("FATAL: remaining connection slots are reserved"))
    assert database.is_pool_exhaustion(exc) is True
    exc2 = OperationalError("SELECT 1", {}, Exception("FATAL: sorry, too many connections"))
    assert database.is_pool_exhaustion(exc2) is True


def test_is_pool_exhaustion_ignora_otros_errores():
    """No confundir un error de negocio con saturación: cualquier otra cosa es False."""
    assert database.is_pool_exhaustion(ValueError("nada que ver")) is False
    assert database.is_pool_exhaustion(OperationalError("x", {}, Exception("syntax error"))) is False


def test_log_pool_exhaustion_cuenta_loguea_y_registra(caplog):
    """Contabiliza, loguea WARN y deja el evento en el anillo de errores recientes."""
    observability.reset_pool_exhaustion_count()
    observability.clear_errors()
    exc = PoolTimeoutError("QueuePool limit reached")
    with caplog.at_level(logging.WARNING):
        n1 = observability.log_pool_exhaustion(exc, where="test")
        n2 = observability.log_pool_exhaustion(exc, where="test")

    assert (n1, n2) == (1, 2)
    assert observability.pool_exhaustion_count() == 2
    assert any("DB pool agotado" in rec.message for rec in caplog.records)
    errs = observability.recent_errors()
    pool_errs = [e for e in errs if e["context"] == "db.pool_exhausted"]
    assert len(pool_errs) == 2
    assert pool_errs[0]["details"]["count"] == 2  # más nuevo primero
    observability.reset_pool_exhaustion_count()


def test_middleware_registra_saturacion_del_pool(monkeypatch):
    """Si un endpoint muere por saturación del pool, el middleware lo deja en /leads/errors
    SIN cambiar la respuesta (sigue siendo el 5xx de siempre)."""
    observability.reset_pool_exhaustion_count()
    observability.clear_errors()

    # Forzar que la query de /leads/stats lance el pool timeout.
    import routers.leads as leads_router
    monkeypatch.setattr(leads_router, "func", _BoomFunc())

    # raise_server_exceptions=False para observar la respuesta 500 real (en vez de que el
    # TestClient re-lance la excepción del server, que es su default).
    with TestClient(main.app, raise_server_exceptions=False) as c:
        r = c.get("/leads/stats", headers=ADMIN)

    # Cero cambio de comportamiento: la respuesta sigue siendo 500 (no la tragamos).
    assert r.status_code == 500
    assert observability.pool_exhaustion_count() == 1
    assert any(e["context"] == "db.pool_exhausted" for e in observability.recent_errors())
    observability.reset_pool_exhaustion_count()


class _BoomFunc:
    """Stub de sqlalchemy.func: cualquier atributo lanza el pool timeout al invocarse,
    simulando que la query de /leads/stats muere por saturación del pool."""

    def __getattr__(self, _name):
        def _raise(*a, **k):
            raise PoolTimeoutError("QueuePool limit of size 10 overflow 20 reached")
        return _raise


# ───────────────────── log de config efectiva al arranque ───────────────────

def test_log_startup_config_emite_una_linea_estructurada(caplog):
    with caplog.at_level(logging.INFO, logger="observability"):
        observability.log_startup_config(pool_size=10, max_overflow=20, db_io="sync")
    line = next(r.message for r in caplog.records if "startup config" in r.message)
    # Claves ordenadas y presentes (línea estable/diff-eable entre deploys).
    assert "db_io=sync" in line
    assert "max_overflow=20" in line
    assert "pool_size=10" in line
    assert line.index("db_io") < line.index("max_overflow") < line.index("pool_size")
