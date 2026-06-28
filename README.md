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
| `WHATSAPP_REENGAGE_TEMPLATE` | Nombre EXACTO de una plantilla aprobada en Meta, para re-enganchar a un lead fuera de la ventana de 24h. Vacío = no se reabre la conversación fuera de ventana. Ver "Ventana de 24h de WhatsApp". |
| `WHATSAPP_REENGAGE_TEMPLATE_LANG` | Código de idioma de esa plantilla (default `es_AR`). Debe coincidir con el aprobado en Meta. |
| `REENGAGE_ENABLED` | **Default `0` (apagado)**: habilita el re-engagement *proactivo* (`POST /reengage/run`). Ver "Re-engagement proactivo". |
| `REENGAGE_TEMPLATE_NAME` | Plantilla del re-engagement proactivo. Vacío (default) = cae a `WHATSAPP_REENGAGE_TEMPLATE`; si ambas vacías, no manda nada. |
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

## Ventana de 24h de WhatsApp

WhatsApp solo deja mandar mensajes **free-form** (`type:text`) dentro de las **24h**
posteriores al último mensaje del usuario (la "ventana de servicio"). Pasada esa ventana,
la Graph API rechaza el free-form: para volver a escribirle al lead hay que usar una
**plantilla** (`type:template`) previamente aprobada por Meta.

Cómo lo maneja el código (`services/meta_service.py`):

- Cada inbound de WhatsApp guarda `conversations.last_inbound_at` (reabre la ventana).
- `is_within_24h_window(last_inbound_at)` decide si la ventana sigue abierta
  (`None` = cerrada, fail-safe hacia plantilla).
- `send_whatsapp_reply(phone, text, last_inbound_at)` rutea el envío:
  **ventana abierta → free-form**; **cerrada → plantilla** `WHATSAPP_REENGAGE_TEMPLATE`.
- `send_whatsapp_template(phone, name, lang_code, body_params)` arma y manda la plantilla.

### Qué tenés que hacer vos en Meta (fuera del código)

Las plantillas **se crean y se aprueban en Meta**; el código no las puede crear. Para
habilitar el re-engagement fuera de ventana:

1. En **WhatsApp Manager → Plantillas de mensajes**, creá una plantilla de categoría
   *Marketing* (o *Utility*, según el caso) y mandala a aprobación. Si querés insertar el
   texto generado por el bot, usá un placeholder de cuerpo `{{1}}` (se completa con el
   primer `body_param`).
2. Esperá a que Meta la **apruebe**.
3. Seteá el **nombre exacto** de la plantilla aprobada en `WHATSAPP_REENGAGE_TEMPLATE`
   (y `WHATSAPP_REENGAGE_TEMPLATE_LANG` si el idioma no es `es_AR`).

Sin `WHATSAPP_REENGAGE_TEMPLATE` configurada, los envíos fuera de ventana se **omiten**
(se loguea un warning) en vez de mandar un free-form que Graph rechazaría igual.

### Re-engagement proactivo (fuera de ventana)

Lo anterior reabre la ventana *cuando el bot ya iba a responder*. El **re-engagement
proactivo** (`services/reengage_service.py`) va más allá: barre los leads cuya ventana de
24h **ya cerró** y les manda la plantilla para reactivarlos, sin que haya un mensaje
disparador. Está **SCAFFOLDEADO y apagado por default** — no manda nada hasta que lo
prendas explícitamente.

Cómo funciona:

- `find_reengageable_conversations(db)` selecciona los **elegibles**: canal WhatsApp con
  teléfono, ventana de 24h **cerrada** (o por cerrarse, con `closing_within`), **sin
  re-enganchar antes** (`reengaged_at IS NULL`, idempotencia) y **sin opt-out**
  (`reengage_opt_out`).
- `run_reengagement(db)` manda la plantilla a cada uno y marca `reengaged_at` **solo tras
  el envío OK**. Una segunda corrida no re-manda a los ya marcados.
- **Doble gate (NO-OP seguro)**: si `REENGAGE_ENABLED` está en `0` **o** no hay plantilla
  (`REENGAGE_TEMPLATE_NAME`, con fallback a `WHATSAPP_REENGAGE_TEMPLATE`), el servicio
  **no manda nada** y devuelve `{"skipped": "disabled", ...}`.

**Para activarlo** (en este orden): 1) creá y **aprobá** la plantilla en Meta (igual que
arriba); 2) seteá `REENGAGE_TEMPLATE_NAME` (o reusá `WHATSAPP_REENGAGE_TEMPLATE`); 3) poné
`REENGAGE_ENABLED=1`.

**Trigger (cron externo).** El repo no tiene scheduler propio: la corrida periódica se
dispara pegándole al endpoint admin, con la misma auth que `/leads`:

```bash
curl -X POST https://<tu-app>/reengage/run \
  -H "Authorization: Bearer $ADMIN_API_KEY"
```

Cableá eso a un cron externo (Railway cron, GitHub Actions con `schedule`, o
cron-job.org). Cadencia sugerida: **1 vez por hora** (acota el lag entre que la ventana
cierra y el re-engagement; cada lead se manda **una sola vez** por la idempotencia, así
que correr seguido no duplica envíos). Ejemplo de cron: `0 * * * *`.

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
