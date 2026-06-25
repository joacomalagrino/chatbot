"""Rate limiter compartido (slowapi). La clave es la IP REAL del cliente.

Detrás del edge de Railway, `X-Forwarded-For` es "cliente, ...intermedios, edge".
El PRIMER valor lo controla el cliente (spoofeable: mandando un XFF distinto en cada
request se evade el rate-limit); el ÚLTIMO lo agrega el proxy de confianza. Tomamos el
último para que la clave no se pueda falsificar. Sin XFF (local/tests) caemos al peer.
"""
from fastapi import Request
from slowapi import Limiter


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[-1].strip()
    return request.client.host if request.client else "127.0.0.1"


limiter = Limiter(key_func=_client_ip)
