"""Tests de services/ads_service.generate_ad.

Cubren: corte por max_tokens (no parsea), parseo de JSON válido del texto,
error de la API => {"error": ...}, y manejo del JSON con markdown fencing /
inválido (vía parse_model_json).

NOTA: el código real usa AsyncAnthropic y `await client.messages.create(...)`
(generate_ad es `async def`), así que el fake de create es una corutina.
Se invoca con asyncio.run.
"""
import asyncio

import anthropic
import httpx
import pytest

import services.ads_service as ads_service


class Block:
    def __init__(self, type_, text=None):
        self.type = type_
        if text is not None:
            self.text = text


class FakeResponse:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason


def _patch_create(monkeypatch, *, returns=None, raises=None):
    captured = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        if raises is not None:
            raise raises
        return returns

    monkeypatch.setattr(ads_service.client.messages, "create", fake_create)
    return captured


PROJECT_CONFIG = {"persona": "Agencia de autos.", "goal": "Vender 0km."}


def _run(brief="0km financiados", channel="ambos"):
    return asyncio.run(
        ads_service.generate_ad("agencia", PROJECT_CONFIG, brief, channel)
    )


VALID_JSON = (
    '{"variantes": [{"titular": "0km ya", "texto_principal": "cuerpo", '
    '"descripcion": "d", "cta": "Más información", "concepto_visual": "auto"}], '
    '"publico_sugerido": {"edad": "25-55", "intereses": ["autos"], "ubicacion": "AMBA"}, '
    '"presupuesto_sugerido_ars_dia": "5000"}'
)


# ───────────────────────── max_tokens => error sin parsear ────────

def test_max_tokens_returns_error_without_parsing(monkeypatch):
    # Aunque el texto fuera JSON válido, con stop_reason=max_tokens se aborta antes.
    _patch_create(monkeypatch, returns=FakeResponse(
        [Block("text", VALID_JSON)], stop_reason="max_tokens"))
    result = _run()
    assert "error" in result
    assert "max_tokens" in result["error"]
    # No debe traer las variantes (no se parseó).
    assert "variantes" not in result


# ───────────────────────── JSON válido => dict parseado ───────────

def test_valid_json_is_parsed(monkeypatch):
    _patch_create(monkeypatch, returns=FakeResponse([Block("text", VALID_JSON)]))
    result = _run()
    assert "error" not in result
    assert result["variantes"][0]["titular"] == "0km ya"
    assert result["publico_sugerido"]["edad"] == "25-55"


def test_valid_json_with_markdown_fence_is_parsed(monkeypatch):
    fenced = "```json\n" + VALID_JSON + "\n```"
    _patch_create(monkeypatch, returns=FakeResponse([Block("text", fenced)]))
    result = _run()
    assert "error" not in result
    assert result["variantes"][0]["cta"] == "Más información"


def test_uses_first_text_block(monkeypatch):
    _patch_create(monkeypatch, returns=FakeResponse([
        Block("tool_use"),
        Block("text", VALID_JSON),
    ]))
    result = _run()
    assert result["variantes"][0]["titular"] == "0km ya"


# ───────────────────────── JSON inválido / vacío => error ─────────

def test_invalid_json_returns_error_with_raw(monkeypatch):
    _patch_create(monkeypatch, returns=FakeResponse([Block("text", "esto no es json")]))
    result = _run()
    assert "error" in result
    assert result["raw"] == "esto no es json"


def test_no_text_block_returns_error(monkeypatch):
    # Sin bloque de texto, raw="" => parse_model_json devuelve error.
    _patch_create(monkeypatch, returns=FakeResponse([Block("tool_use")]))
    result = _run()
    assert "error" in result


def test_json_array_not_object_returns_error(monkeypatch):
    # parse_model_json exige un objeto JSON; un array => error.
    _patch_create(monkeypatch, returns=FakeResponse([Block("text", '[1, 2, 3]')]))
    result = _run()
    assert "error" in result


# ───────────────────────── error de la API => {"error": ...} ──────

def test_api_error_returns_error_dict(monkeypatch):
    req = httpx.Request("POST", "https://api.anthropic.com")
    err = anthropic.APIConnectionError(message="api caída", request=req)
    assert isinstance(err, anthropic.APIError)
    _patch_create(monkeypatch, raises=err)
    result = _run()
    assert "error" in result
    # El mensaje incluye el nombre de la clase de error (no propaga la excepción).
    assert "APIConnectionError" in result["error"]


def test_non_api_error_propagates(monkeypatch):
    # Solo se atrapa anthropic.APIError; otras excepciones deben propagar.
    _patch_create(monkeypatch, raises=RuntimeError("bug"))
    with pytest.raises(RuntimeError):
        _run()


# ───────────────────────── construcción del request ──────────────

def test_system_and_user_prompt_carry_context(monkeypatch):
    captured = _patch_create(monkeypatch, returns=FakeResponse([Block("text", VALID_JSON)]))
    _run(brief="plan de financiación", channel="instagram")
    assert "Agencia de autos." in captured["system"]
    assert "Vender 0km." in captured["system"]
    user_msg = captured["messages"][0]["content"]
    assert "plan de financiación" in user_msg
    assert "instagram" in user_msg
