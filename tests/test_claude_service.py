"""Tests de services/claude_service.get_ai_response.

Cubren: extracción del primer bloque de texto, fallback cuando no hay texto,
y fallback (sin propagar) cuando el cliente lanza anthropic.APIError.

Se parchea client.messages.create (AsyncAnthropic => create es corutina) con un
fake async que devuelve un objeto con .content (lista de bloques) o lanza la excepción.
"""
import asyncio

import anthropic
import httpx
import pytest

import services.claude_service as claude_service


class Block:
    """Bloque de respuesta estilo Anthropic: .type y .text."""

    def __init__(self, type_, text=None):
        self.type = type_
        if text is not None:
            self.text = text


class FakeResponse:
    def __init__(self, content):
        self.content = content


def _patch_create(monkeypatch, *, returns=None, raises=None):
    """Parchea client.messages.create con un fake async."""
    captured = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        if raises is not None:
            raise raises
        return returns

    monkeypatch.setattr(claude_service.client.messages, "create", fake_create)
    return captured


PROJECT_CONFIG = {
    "persona": "Sos un asesor de la agencia.",
    "goal": "Captar leads.",
    "questions": ["¿Qué modelo buscás?", "¿Tu presupuesto?"],
}


def _run(message="hola", history=None):
    return asyncio.run(
        claude_service.get_ai_response("agencia", PROJECT_CONFIG, message, history or [])
    )


# ───────────────────────── camino feliz ──────────────────────────

def test_returns_first_text_block(monkeypatch):
    _patch_create(monkeypatch, returns=FakeResponse([Block("text", "Hola, ¿cómo va?")]))
    assert _run() == "Hola, ¿cómo va?"


def test_returns_first_text_block_skipping_non_text(monkeypatch):
    # Un bloque que no es 'text' (ej. tool_use) precede al de texto => se ignora.
    _patch_create(monkeypatch, returns=FakeResponse([
        Block("tool_use"),
        Block("text", "respuesta real"),
    ]))
    assert _run() == "respuesta real"


# ───────────────────────── fallback por contenido vacío ───────────

def test_fallback_when_no_text_block(monkeypatch):
    _patch_create(monkeypatch, returns=FakeResponse([Block("tool_use"), Block("image")]))
    assert _run() == claude_service.FALLBACK


def test_fallback_when_content_empty(monkeypatch):
    _patch_create(monkeypatch, returns=FakeResponse([]))
    assert _run() == claude_service.FALLBACK


def test_fallback_when_text_block_is_empty_string(monkeypatch):
    # text="" es falsy => `text or FALLBACK` cae al fallback.
    _patch_create(monkeypatch, returns=FakeResponse([Block("text", "")]))
    assert _run() == claude_service.FALLBACK


# ───────────────────────── fallback ante error de la API ──────────

def test_fallback_on_api_error_without_propagating(monkeypatch):
    req = httpx.Request("POST", "https://api.anthropic.com")
    err = anthropic.APIConnectionError(message="api caída", request=req)
    assert isinstance(err, anthropic.APIError)
    _patch_create(monkeypatch, raises=err)
    # No debe propagar: devuelve el texto de fallback.
    assert _run() == claude_service.FALLBACK


def test_api_error_se_registra_con_latencia(monkeypatch):
    """Observabilidad: un fallo de Claude queda en el anillo de errores con latency_ms,
    para verlo en /leads/errors (bajo carga, una racha de timeouts deja sin respuesta al lead)."""
    import observability
    observability.clear_errors()
    req = httpx.Request("POST", "https://api.anthropic.com")
    _patch_create(monkeypatch, raises=anthropic.APIConnectionError(message="timeout", request=req))
    _run()
    errs = observability.recent_errors()
    rec = next(e for e in errs if e["context"] == "claude_service.create")
    assert rec["details"]["project"] == "agencia"
    assert "latency_ms" in rec["details"]
    assert isinstance(rec["details"]["latency_ms"], int)
    observability.clear_errors()


def test_non_api_error_propagates(monkeypatch):
    # Una excepción que NO es anthropic.APIError no se atrapa: debe propagar.
    _patch_create(monkeypatch, raises=RuntimeError("bug inesperado"))
    with pytest.raises(RuntimeError):
        _run()


# ───────────────────────── construcción del request ──────────────

def test_system_prompt_includes_persona_goal_and_questions(monkeypatch):
    captured = _patch_create(monkeypatch, returns=FakeResponse([Block("text", "ok")]))
    _run()
    system = captured["system"]
    assert "Sos un asesor de la agencia." in system
    assert "Captar leads." in system
    assert "- ¿Qué modelo buscás?" in system
    assert "- ¿Tu presupuesto?" in system


def test_history_is_capped_to_last_20(monkeypatch):
    captured = _patch_create(monkeypatch, returns=FakeResponse([Block("text", "ok")]))
    # Historial alternado user/assistant (realista): el cap toma los últimos 20.
    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": "m%d" % i}
        for i in range(50)
    ]
    _run(message="nuevo", history=history)
    sent = captured["messages"]
    # history[-20:] (m30..m49, alternados) + el mensaje nuevo => 21.
    assert len(sent) == 21
    assert sent[-1] == {"role": "user", "content": "nuevo"}
    assert sent[0] == {"role": "user", "content": "m30"}


# ───────────────────────── normalización de roles consecutivos ────

def test_collapses_consecutive_same_role_messages(monkeypatch):
    """Dos turnos seguidos del mismo rol (p.ej. doble envío de WhatsApp) se
    colapsan en uno solo antes de mandar a Claude, preservando la alternancia."""
    captured = _patch_create(monkeypatch, returns=FakeResponse([Block("text", "ok")]))
    history = [
        {"role": "user", "content": "hola"},
        {"role": "user", "content": "estás?"},
        {"role": "assistant", "content": "¡Hola!"},
    ]
    _run(message="quiero un auto", history=history)
    sent = captured["messages"]
    # Los dos 'user' iniciales se unen; luego assistant; luego el nuevo user.
    assert sent == [
        {"role": "user", "content": "hola\nestás?"},
        {"role": "assistant", "content": "¡Hola!"},
        {"role": "user", "content": "quiero un auto"},
    ]


def test_collapses_consecutive_assistant_then_user(monkeypatch):
    """El último mensaje (siempre user) se colapsa con un user previo del historial.
    El 'assistant' que quedaba al principio se descarta: la API exige que el primer
    mensaje sea 'user' (ver test_drops_leading_assistant_messages)."""
    captured = _patch_create(monkeypatch, returns=FakeResponse([Block("text", "ok")]))
    history = [
        {"role": "assistant", "content": "Contame qué buscás."},
        {"role": "user", "content": "una SUV"},
    ]
    _run(message="usada", history=history)
    sent = captured["messages"]
    assert sent == [
        {"role": "user", "content": "una SUV\nusada"},
    ]


def test_drops_empty_content_messages(monkeypatch):
    """Mensajes con content vacío se descartan (no rompen la alternancia)."""
    captured = _patch_create(monkeypatch, returns=FakeResponse([Block("text", "ok")]))
    history = [
        {"role": "user", "content": "hola"},
        {"role": "assistant", "content": ""},      # vacío: se descarta
        {"role": "assistant", "content": "buenas"},
    ]
    _run(message="che", history=history)
    sent = captured["messages"]
    assert sent == [
        {"role": "user", "content": "hola"},
        {"role": "assistant", "content": "buenas"},
        {"role": "user", "content": "che"},
    ]


def test_drops_leading_assistant_messages(monkeypatch):
    """La API de Anthropic exige que el PRIMER mensaje sea 'user' (si no, 400). Cuando el
    recorte a los últimos 20 deja un 'assistant' al principio —historial con la alternancia
    rota por el trade-off de duplicación del Message de usuario— esos 'assistant' iniciales
    se descartan. Sin esto, el 400 se tragaba como FALLBACK y el lead recibía "problema
    técnico" turno tras turno hasta que el 'assistant' salía de la ventana. Regresión 2026-07-08."""
    captured = _patch_create(monkeypatch, returns=FakeResponse([Block("text", "ok")]))
    history = [
        {"role": "assistant", "content": "vieja respuesta 1"},
        {"role": "user", "content": "hola"},
        {"role": "assistant", "content": "vieja respuesta 2"},
    ]
    _run(message="che", history=history)
    sent = captured["messages"]
    assert sent[0]["role"] == "user"           # nunca arranca con assistant
    assert sent == [
        {"role": "user", "content": "hola"},
        {"role": "assistant", "content": "vieja respuesta 2"},
        {"role": "user", "content": "che"},
    ]
