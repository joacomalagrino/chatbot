"""La clave del rate-limit usa la IP resuelta por uvicorn (`request.client.host`).

uvicorn corre con --proxy-headers --forwarded-allow-ips='*', así que ya deja en
request.client.host la IP real del cliente (no se parsea X-Forwarded-For a mano:
tomar el último valor sería el edge compartido de Fastly y el primero sería spoofeable)."""
from ratelimit import _client_ip


class _Req:
    def __init__(self, peer="5.5.5.5"):
        self.client = type("C", (), {"host": peer})() if peer else None


def test_client_ip_usa_request_client_host():
    # uvicorn (--proxy-headers) ya resolvió el cliente real y lo dejó en client.host.
    assert _client_ip(_Req("9.9.9.9")) == "9.9.9.9"


def test_client_ip_sin_client_fallback():
    assert _client_ip(_Req(peer=None)) == "127.0.0.1"
