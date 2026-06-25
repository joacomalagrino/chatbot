# Changelog

Todos los cambios notables de este proyecto se documentan acá.

El formato sigue [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/)
y el proyecto adhiere (con criterio flexible) a [Versionado Semántico](https://semver.org/lang/es/).

> Nota: el servicio está deployado en Railway (auto-deploy desde `origin/main`).
> Las versiones se listan en orden cronológico de desarrollo.

## [Unreleased]

### Added

- **Migraciones con Alembic.** El schema deja de depender de `create_all` y pasa a
  manejarse con migraciones versionadas (`migrations/`, baseline `0001_baseline` que
  reproduce exactamente el schema previo). En el arranque, `database.init_db()` hace
  un **auto-bootstrap**: una DB fresca corre todas las migraciones; la DB de prod
  (creada por `create_all` antes de Alembic) se **adopta** con `alembic stamp` al
  baseline —sin recrear ni tocar datos— y de ahí en más aplica lo pendiente. Si el
  tooling de Alembic falla, cae a `create_all` para que la app siempre arranque.
  Cómo crear una migración nueva: ver README → "Migraciones". +4 tests (DB fresca,
  adopción sin pérdida de datos, idempotencia, no-drift modelos vs migraciones).

## [0.5.0] — Auditoría profunda + features de captación (sin publicar)

Pasada multi-agente sobre el código ya maduro: bugs reales encontrados por tests
adversariales, hardening adicional, features para el panel y mucha más cobertura.

### Fixed

- **Echo de Instagram**: Meta reenvía nuestros propios mensajes salientes como
  evento; sin filtrar `is_echo` el bot se contestaba a sí mismo en loop. Se descartan.
- **Reclamo de evento prematuro**: el webhook reclamaba el id del evento ANTES de
  validar `from` (WhatsApp) / `sender` (Instagram); si faltaban, un reintento válido
  de Meta quedaba descartado. Ahora se valida primero y recién se reclama.
- Purga lazy de `processed_events`: la tarea fire-and-forget reusaba la sesión del
  request (posible use-after-close); ahora abre la suya. `get_running_loop` en vez del
  deprecado `get_event_loop`.

### Security

- Rate-limit no spoofeable: la clave usa el ÚLTIMO `X-Forwarded-For` (el del proxy de
  confianza), no el primero (controlable por el cliente) → no se evade el límite de `/chat`.
- CSP del panel `/admin` endurecida a `style-src 'self'` (sin `'unsafe-inline'`): se
  eliminaron todos los estilos inline (a clases CSS).
- CSV export con protección anti CSV-injection.

### Added

- Export de leads a CSV (`GET /leads/export.csv`) + botón en el panel.
- Búsqueda (`q`) y orden (`sort`) en el listado de leads, con UI (debounce).
- Transcript de la conversación de cada lead (`GET /leads/{id}/messages`) + vista
  expandible en el panel.
- Notificación al equipo cuando entra un lead caliente (log + `NOTIFY_WEBHOOK_URL`).
- `GET /health/ready`: readiness que verifica la DB (503 si Postgres cae); `/health`
  queda como liveness.
- Tasa de conversión y promedio de mensajes en `/leads/stats`.
- Accesibilidad del panel: live region en toasts, manejo de foco, navegación por
  flechas en las tabs (roving tabindex), affordance de notas. Widget: scroll trapping,
  linkify seguro de los mensajes del bot.

### Performance

- Índice en `processed_events.created_at` (la purga filtra por ahí) y pool de
  conexiones dimensionado para ráfagas.

### Tests

- +~110 tests: bugs del webhook, rate-limit, export, transcript, notificación,
  readiness, retry de Meta, fallback de Claude y parsing de anuncios.

## [0.4.0] — Endurecimiento pre-prod (sin publicar)

Pasada de seguridad, robustez y UX dejando el servicio listo para deployar.

### Added

- README y `.env.example` documentando todas las variables y el comportamiento
  fail-closed del webhook y de los endpoints internos.
- Quick replies por proyecto en la bienvenida del widget, para bajar la fricción
  de arranque (se ocultan al primer envío).
- Badge de notificación en el launcher del widget que invita a abrir el chat.
- Animación de aparición de mensajes y scroll suave al fondo, respetando
  `prefers-reduced-motion`.
- Tests de concurrencia del webhook (claim idempotente, creación concurrente de
  conversación, varios eventos en paralelo) y de los defaults de CORS.
- Reintentos con backoff en `get_lead_data` de la Graph API (antes una sola llamada).

### Changed

- El widget guarda el `SESSION_ID` en `sessionStorage`, así no se pierde el lead
  al recargar la página.
- El cliente de Claude para anuncios pasó a `AsyncAnthropic` (no bloquea el threadpool).
- `leadgen_id` se valida (`^[0-9]+$`) antes de armar la URL de Graph.
- El historial de conversación se trae acotado (últimos 40 mensajes vía índice) en
  vez de cargar toda la conversación, que en WhatsApp puede crecer sin límite.
- El mensaje del usuario que llega por webhook se capa a 4000 caracteres antes de
  ir a la API paga (el widget ya capaba a 2000).
- Mejoras de accesibilidad y de detalle visual del widget (focus-trap, `aria-modal`
  válido, swap del launcher a ícono de cerrar, alineación de input/botón) y del
  panel admin (tabla scrolleable en mobile, tabs con ARIA, números del embudo legibles).

### Fixed

- El webhook de Meta ahora es **fail-closed**: sin `META_APP_SECRET` rechaza con
  403 (antes aceptaba todo), salvo que se setee `ALLOW_UNSIGNED_WEBHOOKS=1` en dev.
- Los datos de un Lead Ad se traen **antes** de reclamar el evento: si Graph falla,
  el lead no se pierde y el claim se libera.
- Los intereses del lead se guardan como lista de strings, así el panel los muestra
  como tags.
- Los fallos de entrega de WhatsApp/Instagram se capturan y loguean (visibilidad
  para reintentar fuera de banda).
- El lead pasa a estado `qualified` cuando la conversación se vuelve "caliente".
- Fechas naive en UTC no-deprecadas, compatibles con las columnas `DateTime` de Postgres.
- `get_or_create_conversation` falla ruidoso si el re-read tras `IntegrityError`
  vuelve vacío, en vez de propagar un `None` que reventaba más abajo.

### Security

- CORS deja de defaultear a `"*"`: en prod queda restringido a los dominios propios
  (agencia/mesa/ticketera u `ALLOWED_ORIGINS`); en dev se abre para probar local.
- Rate limit correcto detrás del proxy de Railway (`--proxy-headers` /
  `--forwarded-allow-ips`, aísla por IP real vía `X-Forwarded-For`).
- `/docs`, `/redoc` y `/openapi.json` quedan cerrados en prod (solo con `CHATBOT_DEV=1`).
- Purga automática de `processed_events` con TTL de 7 días (la tabla crecía para
  siempre); throttleada y con `asyncio.Lock` para evitar doble purga.
- `anthropic` bumpeado a `>=0.55`.

## [0.3.0] — Panel admin, dashboard y CI (sin publicar)

Fase 2: panel de administración, rediseño del widget y suite de tests de integración.

### Added

- Panel de administración en `/admin`: tabla de leads con filtros por proyecto y
  estado, cambio de estado inline y generador de anuncios con IA (3 variantes +
  público + presupuesto). Dark mode y responsive.
- Pestaña "Resumen" del panel: stat-cards (total, últimos 7 días, calificados,
  perdidos), embudo de conversión y barras por proyecto, todo en HTML/CSS puro.
- Skeletons de carga, estado de error con botón "Reintentar", cambio de estado
  optimista con toast, y botón "Copiar" por variante de anuncio.
- Endpoint `GET /leads/stats` (total, últimos 7 días, por estado/proyecto/canal)
  para alimentar el dashboard.
- Tests de integración (`tests/test_app_integration.py`) sobre SQLite con stubs de
  Claude y Meta, y CI en GitHub Actions que corre `pytest` en cada push (Python 3.12).

### Changed

- Rediseño del widget: dark mode (`prefers-color-scheme`), accent por proyecto,
  accesibilidad (role `dialog`/`log`, `aria-live`, focus management, Escape cierra),
  responsive full-width en mobile y anti doble-envío.
- Los UUID pasan al tipo `Uuid` genérico de SQLAlchemy 2.0: nativo en Postgres,
  `CHAR(32)` en SQLite, lo que habilita tests y dev local sin Postgres.
- `/leads/` paginado (limit/offset); el serializado incluye notas e intereses como chips.
- Cliente `httpx` reutilizable en `meta_service` (reusa TLS hacia Graph), cerrado en
  el shutdown.
- `uvicorn` arranca sin reload por defecto (solo con `CHATBOT_DEV=1`).

### Fixed

- `PATCH /leads/{id}/status` tipa `lead_id` como UUID (antes pasar un str a la
  columna `Uuid` tiraba 500) y restringe el estado a valores válidos.

### Security

- CSP estricta en `/admin`: assets externalizados (`styles.css` / `app.js`),
  `script-src 'self'` sin inline (bloquea XSS), `frame-ancestors 'none'`.
- Token de admin en `sessionStorage` (antes `localStorage`), con purga automática
  ante un 401.
- Headers de seguridad en todas las respuestas (`X-Content-Type-Options`,
  `X-Frame-Options`, `Referrer-Policy`, `Strict-Transport-Security`).
- `META_VERIFY_TOKEN` sin default público; el handshake del webhook responde 503
  si falta.
- Límite de tamaño del body del webhook (256 KB → 413) como defensa anti-DoS.
- Los errores de la Graph API se loguean sin volcar el body (puede traer PII del lead).

### Performance

- Índices de DB en `messages.conversation_id`, `leads` (project/status/created_at)
  y `conversations` (project/status).
- `GZipMiddleware` para comprimir la API, el panel y el widget.
- `Cache-Control public max-age=3600` en `/widget`.

## [0.2.0] — Hardening de seguridad y robustez (sin publicar)

Primera pasada seria de seguridad y robustez sobre el backend, con la lógica pura
extraída para poder testearla sin red ni DB.

### Added

- 26 tests de la lógica pura de texto (`services/text_utils.py`).
- Servicio compartido `conversation_service.py` para deduplicar el flujo de chat y webhook.
- Idempotencia del webhook por id de evento (tabla `ProcessedEvent`): descarta los
  reintentos de Meta para no duplicar respuestas.

### Changed

- Cliente `AsyncAnthropic` con timeout, `max_retries` y fallback, para no bloquear
  el event loop.
- El webhook responde 200 al instante y procesa en `BackgroundTasks` con su propia
  sesión de DB (Meta no reintenta por timeout).
- Retry con backoff y timeout granular en las llamadas a la Graph API.
- `pool_pre_ping` y `pool_recycle` en el engine para descartar conexiones stale de Railway.

### Fixed

- Race condition al crear una `Conversation`: captura `IntegrityError` y relee.
- `extract_contact`: el `@` de un email ya no se guarda como handle de Instagram.
- Detección de teléfono endurecida (10-15 dígitos) y arreglo de un bug que truncaba
  el último dígito de números argentinos con espacios.
- El historial deja de depender del orden por timestamp (filtra por id del mensaje).
- `update_lead_from_message` hace un solo commit, dejando el estado coherente.
- `send_instagram_message` manda `messaging_product=instagram`.
- El handshake del webhook devuelve el `hub.challenge` verbatim (no rompe la verificación).

### Security

- Validación de firma HMAC (`X-Hub-Signature-256`) del webhook de Meta.
- Auth por Bearer token (`ADMIN_API_KEY`) en `/leads` y `/ads`, **fail-closed**
  (antes `/leads` exponía toda la PII de los clientes sin auth).
- Rate limiting (`slowapi`) en `/chat` (20/min) y `/ads` (10/min).
- Validación de tamaño y tipo en `ChatRequest` y `AdRequest`.
- CORS configurable por `ALLOWED_ORIGINS` (antes `"*"` fijo).
- El token de Meta viaja por header en `get_lead_data` (no en query param).
- Se elimina el log de PII (teléfono + texto) del deploy.

## [0.1.0] — Backend inicial (sin publicar)

Primer servicio funcional: un backend que atiende el widget web y los canales de Meta,
conversa con Claude y captura leads.

### Added

- Servicio FastAPI inicial: chat con Claude (Haiku), webhook de Meta y widget web
  embebible (`widget/chatbot.js`).
- Generador de anuncios con IA y soporte de Lead Ads en el webhook
  (`services/ads_service.py`, `routers/ads.py`).
- Página de demo del widget (`widget/demo.html`).
- Modelos de conversaciones, mensajes, leads y eventos, sobre PostgreSQL.
- Deploy en Railway vía NIXPACKS (`railway.toml`) con health check en `/health`.

### Fixed

- Las variables de entorno de Meta son opcionales, así la app bootea sin la config
  completa.
- `postgres://` se normaliza a `postgresql://` para compatibilidad con SQLAlchemy.
- `/health` responde aunque falle la inicialización de la DB (se captura el error
  en el lifespan).
- Python fijado en 3.12 para el build de Nixpacks en Railway.
- Ajuste del formato de número de WhatsApp para Argentina (`549…` → `54…`) en la
  Graph API.
