# Chatbot

Servicio de captación de clientes (leads) para varios proyectos vía un único backend.
Atiende un widget web, WhatsApp e Instagram (Meta directa) y Lead Ads, conversa con
Claude, detecta datos de contacto y los guarda como leads. Incluye un panel de
administración para ver leads, métricas y generar variantes de anuncios.

Backend en **FastAPI**. Aún no está deployado.

## Proyectos soportados

El mismo backend sirve a varios negocios; cada uno tiene su propia persona y objetivo
(ver `config.py` → `PROJECTS`):

- `agencia` — Gonzalo Ferraro Automóviles
- `mesa` — Mesa (helpdesk)
- `ticketera` — Soporte Dedalus

## Arquitectura

```
widget web ─┐
WhatsApp  ──┤
Instagram ──┼─▶ FastAPI ─▶ Claude (Haiku) ─▶ respuesta
Lead Ads  ──┘     │
                  └─▶ Postgres (conversaciones, mensajes, leads, eventos)

panel /admin ─▶ /leads, /ads (requieren ADMIN_API_KEY)
```

- `routers/chat.py` — chat del widget web (`POST /chat/`), con rate limit por IP.
- `routers/webhook.py` — webhook de Meta (`GET/POST /webhook/meta`): verifica el
  handshake, valida la firma `X-Hub-Signature-256`, descarta reintentos (idempotencia
  por id de evento) y procesa WhatsApp / Instagram / Lead Ads en segundo plano.
- `routers/leads.py` — listado, métricas y cambio de estado de leads (internos).
- `routers/ads.py` — generación de anuncios con Claude (interno).
- `services/` — Claude (chat y anuncios), Meta Graph API, lógica de conversación y
  lead, y utilidades de texto puras (`text_utils.py`).

## Requisitos

- Python 3.12 (ver `.python-version`)
- PostgreSQL (en local/test se puede usar SQLite; los modelos usan el tipo `Uuid`
  genérico y corren en ambos)

## Configuración

Copiá `.env.example` a `.env` y completá las variables. Las más importantes:

| Variable | Para qué |
|---|---|
| `DATABASE_URL` | Conexión a Postgres (`postgres://` se reescribe a `postgresql://`). |
| `ANTHROPIC_API_KEY` | Llamadas a Claude (chat y anuncios). |
| `ADMIN_API_KEY` | **Fail-closed**: sin esto, `/leads` y `/ads` devuelven 503. Se manda como `Authorization: Bearer <token>`. |
| `META_VERIFY_TOKEN` | **Fail-closed**: sin esto el handshake del webhook devuelve 503. |
| `META_APP_SECRET` | Habilita la validación de firma del webhook. Si falta, se acepta el webhook pero se loguea una advertencia. |
| `META_ACCESS_TOKEN` | Token de la Graph API para responder mensajes y traer datos de Lead Ads. |
| `META_WHATSAPP_PHONE_ID`, `META_INSTAGRAM_ACCOUNT_ID` | IDs de los canales de Meta. |
| `ALLOWED_ORIGINS` | CORS: dominios del widget separados por coma, o `*`. |
| `WHATSAPP_NUMBER_TO_PROJECT`, `LEAD_FORM_TO_PROJECT` | Ruteo opcional (JSON) de número/formulario a proyecto. Si vacío, todo cae en `agencia`. |

## Correr en local

```bash
pip install -r requirements-dev.txt
# en local: recarga con CHATBOT_DEV=1
CHATBOT_DEV=1 python main.py        # http://localhost:8000
# o:
uvicorn main:app --reload
```

- Health check: `GET /health`
- Docs interactivas: `GET /docs`
- Widget de demo: `GET /widget/demo.html`
- Panel admin: `GET /admin/` (pide el `ADMIN_API_KEY`)

## Tests

```bash
pytest -q
```

Cubren la lógica pura de texto (`tests/test_text_utils.py`) y la integración del app
con stubs de Claude y Meta sobre SQLite (`tests/test_app_integration.py`): firma del
webhook, idempotencia, creación de conversación/mensaje/lead y la API de leads.

## Deploy (Railway)

`railway.toml` define el build con NIXPACKS y arranca con
`uvicorn main:app --host 0.0.0.0 --port $PORT`, con health check en `/health`.
Las variables de entorno se cargan desde el panel de Railway.

## Notas de seguridad

- `/leads` y `/ads` son fail-closed: sin `ADMIN_API_KEY` quedan cerrados.
- El webhook valida la firma HMAC del body crudo cuando `META_APP_SECRET` está seteado.
- Los errores de la Graph API se loguean sin volcar el body (puede traer PII del lead).
- Headers de seguridad (nosniff, X-Frame-Options, Referrer-Policy, HSTS) y CSP estricta
  en `/admin`. Respuestas comprimidas con gzip.
