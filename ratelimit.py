"""Rate limiter compartido (slowapi). La clave es la IP del cliente.

De DÓNDE se saca esa IP depende de la cadena de proxies, y elegir mal rompe algo:

- `request.client.host` (lo que resuelve uvicorn desde X-Forwarded-For según
  `--forwarded-allow-ips`): con el default `*` uvicorn toma el LEFTMOST del XFF, que el
  cliente controla → SPOOFEABLE (se evade el rate-limit cambiando el header).
- RIGHTMOST del XFF: el edge de Railway APPENDEA el IP real del cliente al final del XFF
  (un `X-Forwarded-For: 1.2.3.4` spoofeado llega al contenedor como `1.2.3.4, <ip_real>`),
  así que para un dominio `*.up.railway.app` SIN un CDN propio delante, el rightmost es el
  cliente y NO se puede spoofear. Es la garantía documentada de Railway (el edge appendea).

  CUIDADO: si hubiera un CDN propio (p. ej. Fastly en un dominio custom) DELANTE del edge,
  el rightmost pasaría a ser el edge COMPARTIDO del CDN → TODOS los clientes caerían en una
  sola key = rate-limit colapsado a un bucket global (outage de /chat). Por eso el rightmost
  NO es el default: se habilita explícitamente con `RATE_LIMIT_IP_SOURCE=xff_rightmost`
  DESPUÉS de confirmar que no hay CDN propio delante. Hoy la prod es
  `chatbot-production-de88.up.railway.app` (sin dominio custom → sin CDN).

DEFAULT `peer` (= comportamiento actual, request.client.host): mergear esto NO cambia nada
en prod. Es un fix OPT-IN por env, seguro por default.

Cómo VERIFICAR antes de habilitar rightmost: mandá >20 req/min a `POST /chat` con un
`X-Forwarded-For` FIJO y FALSO (ej. `1.2.3.4`). Con rightmost, la key es tu IP real (que el
edge appendea a la derecha), así que igual empezás a recibir 429 — el spoof NO evade. Si en
cambio TODOS los clientes comparten el mismo límite (429 con muy poco tráfico agregado), hay
un CDN delante y NO conviene habilitarlo.
"""
import os

from fastapi import Request
from slowapi import Limiter


def _client_ip(request: Request) -> str:
    """Key del rate-limit. Ver el docstring del módulo por qué la fuente es configurable.

    `RATE_LIMIT_IP_SOURCE`:
      - "peer" (default): la IP que resolvió uvicorn (request.client.host). No colapsa a un
        bucket global; spoofeable si --forwarded-allow-ips queda en '*'.
      - "xff_rightmost": el ÚLTIMO valor de X-Forwarded-For (el que el edge de Railway
        appendea). No spoofeable en *.up.railway.app; requiere que NO haya CDN propio delante.
    """
    peer = request.client.host if request.client else "127.0.0.1"
    # Leído por request (no al importar) para que sea configurable/testeable sin recargar.
    if os.getenv("RATE_LIMIT_IP_SOURCE", "peer").strip().lower() == "xff_rightmost":
        xff = request.headers.get("x-forwarded-for", "")
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            return parts[-1]
        # Sin XFF (local / peer directo): caemos al peer, no a un default espúreo.
    return peer


limiter = Limiter(key_func=_client_ip)
