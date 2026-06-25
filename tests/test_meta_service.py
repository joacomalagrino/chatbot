"""Tests del cliente HTTP de Meta (services/meta_service.py).

Cubren _request_with_retry (reintentos ante 429/5xx y red, raise tras agotar),
los wrappers (_post_with_retry / send_*), _error_summary (no vuelca el body con PII)
y la validación de leadgen_id en get_lead_data ANTES de tocar la red.

No se hace I/O real: se parchea _get_client para devolver un cliente fake con
un .request(...) canned, y asyncio.sleep para no esperar el backoff.
"""
import asyncio

import httpx
import pytest

import services.meta_service as meta_service


class FakeResponse:
    """Imita lo que _request_with_retry consume de httpx.Response."""

    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text

    @property
    def is_success(self):
        return 200 <= self.status_code < 300

    def json(self):
        if isinstance(self._json, Exception):
            raise self._json
        return self._json

    def raise_for_status(self):
        if not self.is_success:
            req = httpx.Request("GET", "https://graph.facebook.com/x")
            raise httpx.HTTPStatusError(
                f"status {self.status_code}", request=req, response=httpx.Response(self.status_code, request=req)
            )


class FakeClient:
    """Cliente fake: devuelve respuestas canned (o lanza excepciones) en orden.

    `outcomes` es una lista de FakeResponse o de Exception. Cada llamada a
    .request consume el siguiente outcome. Registra cuántas veces se llamó.
    """

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0
        self.is_closed = False
        self.last_kwargs = None

    async def request(self, method, url, **kwargs):
        self.calls += 1
        self.last_kwargs = {"method": method, "url": url, **kwargs}
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture()
def no_sleep(monkeypatch):
    """Anula el backoff: asyncio.sleep no espera de verdad y registra los delays."""
    delays = []

    async def fake_sleep(d):
        delays.append(d)

    monkeypatch.setattr(meta_service.asyncio, "sleep", fake_sleep)
    return delays


def _install(monkeypatch, client):
    monkeypatch.setattr(meta_service, "_get_client", lambda: client)
    return client


# ───────────────────────── reintentos ante status transitorio ──────────────

@pytest.mark.parametrize("transient", [429, 500, 502, 503, 504])
def test_retries_on_transient_status_then_returns_json(monkeypatch, no_sleep, transient):
    client = _install(monkeypatch, FakeClient([
        FakeResponse(transient),
        FakeResponse(200, {"ok": True}),
    ]))
    result = asyncio.run(
        meta_service._request_with_retry("POST", "https://x", {}, payload={"a": 1})
    )
    assert result == {"ok": True}
    assert client.calls == 2  # 1 transitorio + 1 exitoso
    assert no_sleep == [1]    # un único backoff (2**0)


def test_returns_immediately_on_first_200(monkeypatch, no_sleep):
    client = _install(monkeypatch, FakeClient([FakeResponse(200, {"id": "123"})]))
    result = asyncio.run(meta_service._request_with_retry("POST", "https://x", {}))
    assert result == {"id": "123"}
    assert client.calls == 1
    assert no_sleep == []  # sin reintentos => sin sleep


def test_exhausts_attempts_then_raises_http_status_error(monkeypatch, no_sleep):
    # Los 3 intentos devuelven 503: en el último NO se reintenta y se hace raise_for_status.
    client = _install(monkeypatch, FakeClient([
        FakeResponse(503),
        FakeResponse(503),
        FakeResponse(503),
    ]))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(meta_service._request_with_retry("POST", "https://x", {}, attempts=3))
    assert client.calls == 3
    # Solo se duerme entre intentos (2 veces para 3 intentos), no después del último.
    assert no_sleep == [1, 2]


def test_non_retryable_4xx_raises_without_retry(monkeypatch, no_sleep):
    # 400 no está en _RETRY_STATUSES: debe fallar al primer intento sin reintentar.
    client = _install(monkeypatch, FakeClient([FakeResponse(400)]))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(meta_service._request_with_retry("POST", "https://x", {}))
    assert client.calls == 1
    assert no_sleep == []


# ───────────────────────── reintentos ante errores de red ──────────────────

def test_retries_on_transport_error_then_succeeds(monkeypatch, no_sleep):
    client = _install(monkeypatch, FakeClient([
        httpx.ConnectError("conn refused"),  # subclase de TransportError
        FakeResponse(200, {"ok": 1}),
    ]))
    result = asyncio.run(meta_service._request_with_retry("GET", "https://x", {}))
    assert result == {"ok": 1}
    assert client.calls == 2
    assert no_sleep == [1]


def test_retries_on_timeout_then_succeeds(monkeypatch, no_sleep):
    client = _install(monkeypatch, FakeClient([
        httpx.ReadTimeout("slow"),  # subclase de TimeoutException
        FakeResponse(200, {"ok": 1}),
    ]))
    result = asyncio.run(meta_service._request_with_retry("GET", "https://x", {}))
    assert result == {"ok": 1}
    assert client.calls == 2


def test_transport_error_persists_reraises(monkeypatch, no_sleep):
    client = _install(monkeypatch, FakeClient([
        httpx.ConnectError("c1"),
        httpx.ConnectError("c2"),
        httpx.ConnectError("c3"),
    ]))
    with pytest.raises(httpx.TransportError):
        asyncio.run(meta_service._request_with_retry("GET", "https://x", {}, attempts=3))
    assert client.calls == 3
    assert no_sleep == [1, 2]


def test_timeout_persists_reraises(monkeypatch, no_sleep):
    client = _install(monkeypatch, FakeClient([
        httpx.ReadTimeout("t1"),
        httpx.ReadTimeout("t2"),
    ]))
    with pytest.raises(httpx.TimeoutException):
        asyncio.run(meta_service._request_with_retry("GET", "https://x", {}, attempts=2))
    assert client.calls == 2


# ───────────────────────── backoff cap (min(2**i, 8)) ──────────────────────

def test_backoff_is_capped_at_8(monkeypatch, no_sleep):
    # 6 intentos transitorios => sleeps en i=0..4: 1,2,4,8,8 (capado en 8).
    client = _install(monkeypatch, FakeClient([FakeResponse(503)] * 6))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(meta_service._request_with_retry("POST", "https://x", {}, attempts=6))
    assert no_sleep == [1, 2, 4, 8, 8]


# ───────────────────────── método: GET usa params, POST usa json ───────────

def test_get_passes_params_not_json(monkeypatch, no_sleep):
    client = _install(monkeypatch, FakeClient([FakeResponse(200, {"ok": 1})]))
    asyncio.run(
        meta_service._request_with_retry("GET", "https://x", {"H": "1"}, params={"fields": "id"})
    )
    assert client.last_kwargs["params"] == {"fields": "id"}
    assert client.last_kwargs["json"] is None


def test_post_passes_json_not_params(monkeypatch, no_sleep):
    client = _install(monkeypatch, FakeClient([FakeResponse(200, {"ok": 1})]))
    asyncio.run(
        meta_service._request_with_retry("POST", "https://x", {}, payload={"body": "hi"}, params={"fields": "id"})
    )
    assert client.last_kwargs["json"] == {"body": "hi"}
    assert client.last_kwargs["params"] is None


# ───────────────────────── _error_summary: no vuelca el body ────────────────

def test_error_summary_extracts_code_and_type_without_body():
    r = FakeResponse(400, {"error": {"code": 190, "type": "OAuthException",
                                     "message": "Juan Pérez tel 1133334444"}})
    summary = meta_service._error_summary(r)
    assert "code=190" in summary
    assert "type=OAuthException" in summary
    # PII del lead NO debe filtrarse en el resumen.
    assert "Juan" not in summary
    assert "1133334444" not in summary


def test_error_summary_handles_non_json_body():
    r = FakeResponse(500, json_data=ValueError("not json"), text="<html>error</html>")
    summary = meta_service._error_summary(r)
    assert summary == "sin detalle"
    assert "html" not in summary


def test_error_summary_handles_missing_error_key():
    r = FakeResponse(400, {"otra_cosa": 1})
    summary = meta_service._error_summary(r)
    assert "code=None" in summary
    assert "type=None" in summary


# ───────────────────────── wrappers de alto nivel ──────────────────────────

def test_post_with_retry_delegates(monkeypatch, no_sleep):
    client = _install(monkeypatch, FakeClient([FakeResponse(200, {"sent": True})]))
    result = asyncio.run(
        meta_service._post_with_retry("https://x", {"body": "hola"}, {"H": "1"})
    )
    assert result == {"sent": True}
    assert client.last_kwargs["method"] == "POST"
    assert client.last_kwargs["json"] == {"body": "hola"}


def test_send_whatsapp_normalizes_phone_and_posts(monkeypatch, no_sleep):
    client = _install(monkeypatch, FakeClient([FakeResponse(200, {"messages": [{"id": "wamid"}]})]))
    result = asyncio.run(meta_service.send_whatsapp_message("+54 9 11 1234 5678", "hola"))
    assert result == {"messages": [{"id": "wamid"}]}
    sent = client.last_kwargs["json"]
    assert sent["messaging_product"] == "whatsapp"
    # 549XXXXXXXXX (13) -> 54XXXXXXXXX (12), sin el 9. Ver normalize_ar_whatsapp.
    assert sent["to"] == "541112345678"
    assert sent["text"] == {"body": "hola"}


def test_send_instagram_posts_recipient(monkeypatch, no_sleep):
    client = _install(monkeypatch, FakeClient([FakeResponse(200, {"message_id": "m1"})]))
    result = asyncio.run(meta_service.send_instagram_message("IGUSER", "hi"))
    assert result == {"message_id": "m1"}
    sent = client.last_kwargs["json"]
    assert sent["messaging_product"] == "instagram"
    assert sent["recipient"] == {"id": "IGUSER"}


# ───────────────────────── get_lead_data: validación de id ──────────────────

@pytest.mark.parametrize("bad", ["../1234", "me", "123abc", "12 34", "", "12/34", "abc", "1.2"])
def test_get_lead_data_rejects_non_numeric_id_before_network(monkeypatch, bad):
    def boom():
        raise AssertionError("no debería crear el cliente con un id inválido")

    monkeypatch.setattr(meta_service, "_get_client", boom)
    with pytest.raises(ValueError):
        asyncio.run(meta_service.get_lead_data(bad))


def test_get_lead_data_accepts_numeric_id_and_uses_get(monkeypatch, no_sleep):
    client = _install(monkeypatch, FakeClient([FakeResponse(200, {"id": "999", "field_data": []})]))
    result = asyncio.run(meta_service.get_lead_data("999888777"))
    assert result == {"id": "999", "field_data": []}
    assert client.last_kwargs["method"] == "GET"
    assert client.last_kwargs["url"].endswith("/999888777")
    # GET => fields por params, sin json.
    assert "fields" in client.last_kwargs["params"]
    assert client.last_kwargs["json"] is None


# ───────────────────────── parse_lead_fields (lógica pura) ──────────────────

def test_parse_lead_fields_flattens_first_value():
    out = meta_service.parse_lead_fields([
        {"name": "full_name", "values": ["Ana", "Maria"]},
        {"name": "email", "values": ["ana@x.com"]},
    ])
    assert out == {"full_name": "Ana", "email": "ana@x.com"}


def test_parse_lead_fields_skips_empty_and_none():
    out = meta_service.parse_lead_fields([
        {"name": "phone", "values": []},
        {"name": None, "values": ["x"]},
        {"values": ["sin name"]},
    ])
    assert out == {}


def test_parse_lead_fields_tolerates_none_input():
    assert meta_service.parse_lead_fields(None) == {}
