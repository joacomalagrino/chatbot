"""La clave del rate-limit usa el ÚLTIMO valor de X-Forwarded-For (no spoofeable)."""
from ratelimit import _client_ip


class _Req:
    def __init__(self, xff=None, peer="5.5.5.5"):
        self.headers = {"x-forwarded-for": xff} if xff else {}
        self.client = type("C", (), {"host": peer})() if peer else None


def test_client_ip_toma_el_ultimo_xff():
    # El cliente forjea los primeros valores; el proxy de confianza agrega el real al final.
    assert _client_ip(_Req("1.1.1.1, 2.2.2.2, 9.9.9.9")) == "9.9.9.9"


def test_client_ip_un_solo_valor():
    assert _client_ip(_Req("8.8.8.8")) == "8.8.8.8"


def test_client_ip_sin_xff_usa_peer():
    assert _client_ip(_Req(xff=None, peer="7.7.7.7")) == "7.7.7.7"


def test_client_ip_sin_client_fallback():
    assert _client_ip(_Req(xff=None, peer=None)) == "127.0.0.1"
