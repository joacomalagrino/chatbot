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
- `routers/leads.py` — listado (con búsqueda `q` y `sort`), métricas (incluida tasa de
  conversión), export CSV, transcript de conversación y cambio de estado de leads (internos).
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
| `META_APP_SECRET` | **Fail-closed**: valida la firma del webhook. Si falta, el webhook se rechaza con 403 (no se puede verificar el origen). Para desarrollo se puede abrir explícitamente con `ALLOW_UNSIGNED_WEBHOOKS=1`. |
| `ALLOW_UNSIGNED_WEBHOOKS` | **Solo dev** (NO en prod): si vale `1` y falta `META_APP_SECRET`, acepta webhooks sin validar la firma. Por default `0` (fail-closed). |
| `META_ACCESS_TOKEN` | Token de la Graph API para responder mensajes y traer datos de Lead Ads. |
| `META_WHATSAPP_PHONE_ID`, `META_INSTAGRAM_ACCOUNT_ID` | IDs de los canales de Meta. |
| `ALLOWED_ORIGINS` | CORS: dominios del widget separados por coma, o `*`. |
| `WHATSAPP_NUMBER_TO_PROJECT`, `LEAD_FORM_TO_PROJECT` | Ruteo opcional (JSON) de número/formulario a proyecto. Si vacío, todo cae en `agencia`. |
| `NOTIFY_WEBHOOK_URL` | Opcional: webhook (Slack/Discord/Make/etc.) para avisar cuando entra un lead caliente. Vacío = solo log. |

## Correr en local

```bash
pip install -r requirements-dev.txt
# en local: recarga con CHATBOT_DEV=1
CHATBOT_DEV=1 python main.py        # http://localhost:8000
# o:
uvicorn main:app --reload
```

- Health check (liveness): `GET /health` · readiness con chequeo de DB: `GET /health/ready`
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

## Migraciones (Alembic)

El schema se maneja con **Alembic** (`migrations/`). En el arranque, `database.init_db()`
aplica las migraciones automáticamente:

- **DB fresca** (dev/tests/entorno nuevo): corre todas las migraciones desde cero.
- **DB de prod creada por `create_all`** antes de Alembic (tiene tablas pero no
  `alembic_version`): se **adopta** con `stamp` al baseline `0001_baseline` —sin recrear
  ni tocar datos— y luego aplica lo que hubiera pendiente.
- **DB ya bajo Alembic**: solo aplica lo pendiente (no-op si está al día).

Si el tooling de Alembic falla, cae a `create_all` para que la app igual arranque.

Crear una migración nueva tras cambiar un modelo (`models.py`):

```bash
# La URL la toma de DATABASE_URL (config.py). En local podés apuntar a una sqlite.
DATABASE_URL="sqlite:///./dev.db" alembic revision --autogenerate -m "descripcion del cambio"
# revisar el archivo generado en migrations/versions/ y luego:
DATABASE_URL="sqlite:///./dev.db" alembic upgrade head
```

El test `tests/test_migrations.py::test_no_drift_baseline_matches_models` falla si se
cambia un modelo sin generar la migración correspondiente.

## Deploy (Railway)

`railway.toml` define el build con NIXPACKS y arranca con
`uvicorn main:app --host 0.0.0.0 --port $PORT`, con health check en `/health`.
Las variables de entorno se cargan desde el panel de Railway. Las migraciones corren
solas en el arranque (`init_db`), no hace falta un paso de release aparte.

## Notas de seguridad

- `/leads` y `/ads` son fail-closed: sin `ADMIN_API_KEY` quedan cerrados.
- El webhook es fail-closed: valida la firma HMAC del body crudo con `META_APP_SECRET`.
  Si falta el secreto, rechaza con 403 salvo que se setee `ALLOW_UNSIGNED_WEBHOOKS=1`
  (solo dev, nunca en prod).
- Los errores de la Graph API se loguean sin volcar el body (puede traer PII del lead).
- Headers de seguridad (nosniff, X-Frame-Options, Referrer-Policy, HSTS) y CSP estricta
  en `/admin`. Respuestas comprimidas con gzip.
