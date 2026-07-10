"""La key del rate-limit (`_client_ip`) es configurable por `RATE_LIMIT_IP_SOURCE`.

- default "peer": usa la IP resuelta por uvicorn (request.client.host).
- "xff_rightmost": el ÚLTIMO valor de X-Forwarded-For (el que el edge de Railway appendea
  = el cliente real, no spoofeable en *.up.railway.app). Ver el docstring de ratelimit.py.
"""
import pytest

from ratelimit import _client_ip


class _Req:
    def __init__(self, peer="5.5.5.5", xff=None):
        self.client = type("C", (), {"host": peer})() if peer else None
        self.headers = {"x-forwarded-for": xff} if xff is not None else {}


# ───────────────────────── default: peer (request.client.host) ──────────────

def test_default_usa_request_client_host(monkeypatch):
    monkeypatch.delenv("RATE_LIMIT_IP_SOURCE", raising=False)
    # uvicorn (--proxy-headers) ya resolvió el cliente real y lo dejó en client.host.
    assert _client_ip(_Req("9.9.9.9")) == "9.9.9.9"


def test_default_ignora_el_xff(monkeypatch):
    # En modo peer NO parseamos el header a mano (evita el bucket-global si hay CDN delante).
    monkeypatch.delenv("RATE_LIMIT_IP_SOURCE", raising=False)
    assert _client_ip(_Req("9.9.9.9", xff="1.2.3.4, 9.9.9.9")) == "9.9.9.9"


def test_default_sin_client_fallback(monkeypatch):
    monkeypatch.delenv("RATE_LIMIT_IP_SOURCE", raising=False)
    assert _client_ip(_Req(peer=None)) == "127.0.0.1"


# ───────────────────────── xff_rightmost (opt-in) ───────────────────────────

def test_rightmost_toma_el_ultimo_valor_del_xff(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_IP_SOURCE", "xff_rightmost")
    # El edge appendea el IP real a la DERECHA → esa es la key.
    assert _client_ip(_Req("100.64.0.1", xff="9.9.9.9")) == "9.9.9.9"


def test_rightmost_ignora_el_spoof_del_cliente(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_IP_SOURCE", "xff_rightmost")
    # El cliente manda `X-Forwarded-For: 1.2.3.4` (spoof); el edge appendea su IP real 9.9.9.9.
    # La key debe ser el rightmost (9.9.9.9), NO el spoof → no se evade el límite.
    assert _client_ip(_Req("100.64.0.1", xff="1.2.3.4, 9.9.9.9")) == "9.9.9.9"


def test_rightmost_sin_xff_cae_al_peer(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_IP_SOURCE", "xff_rightmost")
    # Sin header (local / peer directo): usamos el peer, no un default espúreo.
    assert _client_ip(_Req("7.7.7.7")) == "7.7.7.7"


def test_rightmost_xff_vacio_o_solo_comas_cae_al_peer(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_IP_SOURCE", "xff_rightmost")
    assert _client_ip(_Req("7.7.7.7", xff="  ")) == "7.7.7.7"
    assert _client_ip(_Req("7.7.7.7", xff=" , ")) == "7.7.7.7"


def test_rightmost_sin_client_ni_xff_fallback(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_IP_SOURCE", "xff_rightmost")
    assert _client_ip(_Req(peer=None)) == "127.0.0.1"
