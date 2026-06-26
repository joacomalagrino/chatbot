"""Rate limiter compartido (slowapi). La clave es la IP REAL del cliente.

Detrás del edge de Railway (con CDN Fastly delante), `X-Forwarded-For` es
"cliente, ...intermedios, edge". OJO: tomar el ÚLTIMO valor NO es seguro acá — es la IP
del edge compartido de Fastly, la misma para todos los clientes, así que la clave colapsaría
a un único bucket global. Tomar el primero tampoco sirve: lo controla el cliente (spoofeable).

uvicorn ya corre con `--proxy-headers --forwarded-allow-ips='*'` (ver railway.toml), así que
RESUELVE el cliente real desde X-Forwarded-For y lo deja en `request.client.host`. Usamos eso
directamente: es lo más simple y robusto. Sin proxy (local/tests) es el peer directo.
"""
from fastapi import Request
from slowapi import Limiter


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "127.0.0.1"


limiter = Limiter(key_func=_client_ip)
