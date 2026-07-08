import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config import PROJECTS, get_settings
from database import SessionLocal, is_pool_exhaustion
from models import Conversation, Lead, ProcessedEvent
from observability import log_pool_exhaustion, record_error
from services.conversation_service import get_or_create_conversation, record_turn
from services.meta_service import (
    get_lead_data,
    parse_lead_fields,
    send_instagram_message,
    send_whatsapp_reply,
)
from services.notify import fire_hot_lead

router = APIRouter()
settings = get_settings()
logger = logging.getLogger(__name__)

DEFAULT_WHATSAPP_PROJECT = "agencia"
DEFAULT_INSTAGRAM_PROJECT = "agencia"
DEFAULT_LEADFORM_PROJECT = "agencia"

# Los webhooks de Meta son chicos; cualquier cosa enorme es abuso.
MAX_WEBHOOK_BYTES = 256 * 1024

# Campos de contacto del Lead Ad que ya van a name/email/phone: se excluyen de `interests`
# para no duplicarlos como tags en el panel.
_LEAD_CONTACT_FIELDS = frozenset({"full_name", "name", "email", "phone_number", "phone"})


def _is_optout_message(text: str) -> bool:
    """¿El inbound es un pedido de baja del re-engagement? Match exacto contra las palabras
    configuradas (REENGAGE_OPTOUT_KEYWORDS), tras trim + case-insensitive. Exacto (no
    substring) para no marcar baja por mensajes que solo mencionan la palabra
    (ej. "no me des de baja todavía")."""
    return (text or "").strip().casefold() in settings.optout_keywords()


def _resolve_project(value: str, mapping: dict, default: str) -> str:
    """Resuelve el proyecto desde un mapping; cae al default y garantiza que exista."""
    project = mapping.get(value, default)
    if project not in PROJECTS:
        project = default if default in PROJECTS else next(iter(PROJECTS))
    return project


def _valid_signature(body: bytes, header: str) -> bool:
    """Valida X-Hub-Signature-256 (HMAC-SHA256 del body crudo con el App Secret)."""
    secret = settings.meta_app_secret
    if not secret:
        # Fail-closed: sin App Secret no se puede validar nada, así que se rechaza.
        # Para dev se puede abrir explícitamente con ALLOW_UNSIGNED_WEBHOOKS=1; nunca por default.
        if settings.allow_unsigned_webhooks:
            logger.warning(
                "META_APP_SECRET no configurado y ALLOW_UNSIGNED_WEBHOOKS activo: "
                "webhook SIN validación de firma (modo dev)"
            )
            return True
        logger.error("META_APP_SECRET no configurado: webhook rechazado (fail-closed)")
        return False
    if not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    received = header.split("=", 1)[1]
    return hmac.compare_digest(expected, received)


@router.get("/meta")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
):
    if not settings.meta_verify_token:
        # Fail-closed: sin token configurado no se valida nada.
        raise HTTPException(status_code=503, detail="Webhook no configurado (falta META_VERIFY_TOKEN)")
    if (
        hub_mode == "subscribe"
        and hub_challenge is not None
        and hub_verify_token is not None
        and hmac.compare_digest(hub_verify_token, settings.meta_verify_token)
    ):
        # Meta espera el challenge devuelto verbatim como texto plano.
        return PlainTextResponse(content=hub_challenge)
    raise HTTPException(status_code=403, detail="Token de verificación inválido")


@router.post("/meta")
# NO se rate-limitea por IP: todos los webhooks de Meta llegan desde el pool compartido de
# Facebook (la misma IP para TODOS los leads), así que un tope por IP es en realidad un tope
# GLOBAL — en una ráfaga de campaña el lead nº 121 recibiría 429 y se PERDERÍA. El abuso y el
# replay ya están cubiertos sin dropear tráfico legítimo: la firma HMAC (_valid_signature)
# rechaza payloads no firmados y la dedup por event_id (_claim_event) descarta reintentos.
async def receive_meta_event(request: Request, background_tasks: BackgroundTasks):
    # Cortar por BYTES LEÍDOS, no por Content-Length: el header puede faltar
    # (transfer-encoding: chunked) o mentir, y `await request.body()` bufferiza
    # TODO en memoria antes de poder chequear el tamaño. Acá leemos por chunks y
    # abortamos apenas superamos el tope, sin bufferizar el resto. El header sirve
    # solo como fast-fail temprano.
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > MAX_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail="Payload demasiado grande")
    chunks = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_WEBHOOK_BYTES:
            raise HTTPException(status_code=413, detail="Payload demasiado grande")
        chunks.append(chunk)
    body_bytes = b"".join(chunks)
    if not _valid_signature(body_bytes, request.headers.get("X-Hub-Signature-256", "")):
        raise HTTPException(status_code=403, detail="Firma inválida")

    try:
        body = json.loads(body_bytes or b"{}")
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="JSON inválido")

    # Responder 200 al instante y procesar en segundo plano: así Meta no reintenta
    # por timeout mientras esperamos a Claude / a la Graph API.
    background_tasks.add_task(_process_event, body)
    return {"status": "ok"}


async def _process_event(body: dict):
    """Procesa el webhook con su propia sesión de DB (la de get_db ya está cerrada).

    Esta tarea corre en BACKGROUND (background_tasks.add_task): FastAPI NO reporta sus
    excepciones, así que cualquier fallo acá sería silencioso = lead perdido. El except
    global LOGUEA con contexto y además lo deja en el registro de errores recientes
    (observability.record_error) para verlo desde el panel sin entrar a los logs de Railway.
    """
    db = SessionLocal()
    try:
        for entry in body.get("entry", []):
            # Instagram / Messenger DMs llegan a nivel `messaging`.
            for event in entry.get("messaging", []):
                await _handle_ig_event(db, event)
            # WhatsApp y Lead Ads llegan bajo `changes`.
            for change in entry.get("changes", []):
                await _handle_change(db, change)
    except Exception as exc:
        # Qué campos trae el webhook ayuda a saber qué se perdió, sin volcar PII del lead.
        fields = sorted({
            change.get("field")
            for entry in body.get("entry", [])
            for change in entry.get("changes", [])
            if change.get("field")
        })
        # Saturación del pool de DB: es la señal #1 de que la app está al techo bajo la
        # ráfaga de webhooks. La marcamos aparte (WARN + contador) para que no se pierda
        # entre los errores genéricos del registro; igual la dejamos en _process_event para
        # saber qué evento se perdió.
        if is_pool_exhaustion(exc):
            log_pool_exhaustion(exc, where="webhook._process_event", fields=fields)
        logger.exception("Error procesando webhook Meta (fields=%s)", fields)
        record_error("webhook._process_event", exc, fields=fields)
    finally:
        db.close()


EVENT_TTL_DAYS = 7
_PURGE_INTERVAL_S = 3600
_last_purge = 0.0
_purge_lock = asyncio.Lock()

# Referencias fuertes a las tareas de purga fire-and-forget en vuelo. create_task solo
# guarda una referencia débil: sin esto el GC podría recolectarlas (y cancelarlas) antes
# de que terminen.
_pending_tasks: set = set()


def purge_old_events(db: Session, days: int = EVENT_TTL_DAYS) -> int:
    """Borra los processed_events más viejos que `days`. Meta no reintenta un webhook tras
    horas, así que la idempotencia solo necesita una ventana corta; sin esto la tabla de
    dedup crecía monótona para siempre. Devuelve cuántas filas borró."""
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    n = db.query(ProcessedEvent).filter(ProcessedEvent.created_at < cutoff).delete()
    db.commit()
    return n


async def _maybe_purge_events() -> None:
    """Dispara la purga a lo sumo 1x/hora por worker (throttle en memoria) para no correr el
    DELETE en cada webhook. Idempotente entre workers (cada uno purga lo que ya purgó otro).

    Abre su PROPIA sesión: NO hereda la del request, que es fire-and-forget y podría
    ya estar cerrada cuando esta tarea corre (use-after-close), o estar siendo usada por
    otra query (la Session de SQLAlchemy no es segura para uso concurrente).

    El chequeo-y-actualización de _last_purge está protegido por _purge_lock para que
    dos BackgroundTasks concurrentes no lancen la purga al mismo tiempo."""
    global _last_purge
    now = time.monotonic()
    # Chequeo rápido sin lock para el camino caliente (evita contención en cada webhook).
    if now - _last_purge < _PURGE_INTERVAL_S:
        return
    async with _purge_lock:
        # Re-chequear dentro del lock: otro task pudo haber actualizado _last_purge
        # justo antes de que adquiriéramos el lock.
        if time.monotonic() - _last_purge < _PURGE_INTERVAL_S:
            return
        _last_purge = time.monotonic()
    db = SessionLocal()
    try:
        purge_old_events(db)
    except Exception:
        db.rollback()
        logger.exception("No se pudo purgar processed_events")
    finally:
        db.close()


def _claim_event(db: Session, event_id: str) -> bool:
    """Registra el id del evento. Devuelve True si es nuevo, False si ya se procesó."""
    if not event_id:
        return True
    if db.query(ProcessedEvent).filter_by(event_id=event_id).first():
        return False
    db.add(ProcessedEvent(event_id=event_id))
    try:
        db.commit()
        # Purga lazy: la lanzamos como tarea fire-and-forget si hay un event loop
        # corriendo (i.e. cuando se llama desde dentro de _process_event). Si no hay
        # loop (tests síncronos directos) get_running_loop lanza RuntimeError y se
        # omite — la purga es best-effort y no afecta la idempotencia. La tarea abre
        # su propia sesión (no le pasamos `db`, que es del request).
        try:
            task = asyncio.get_running_loop().create_task(_maybe_purge_events())
            _pending_tasks.add(task)
            task.add_done_callback(_pending_tasks.discard)
        except RuntimeError:
            pass
        return True
    except IntegrityError:
        db.rollback()
        return False


def _release_event(db: Session, event_id: str) -> None:
    """Libera un evento ya reclamado (revierte _claim_event) para permitir reintentos.

    Se usa cuando, tras reclamar el evento, la persistencia posterior falla: sin esto
    el evento quedaría marcado como procesado y el reintento de Meta se descartaría."""
    if not event_id:
        return
    try:
        db.query(ProcessedEvent).filter_by(event_id=event_id).delete()
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("No se pudo liberar el ProcessedEvent %s", event_id)


async def _deliver(send_coro, channel: str, conversation_id) -> bool:
    """Envía la respuesta capturando el fallo de envío en vez de tragarlo silenciosamente.

    El claim del evento (idempotencia) ocurre antes del envío, así que si Meta nos
    rechaza el envío el turno ya quedó persistido pero el usuario no recibió respuesta.
    Como mínimo dejamos rastro EXPLÍCITO (no se pierde en el except global de
    _process_event) para poder reintentar fuera de banda. Devuelve True si se entregó."""
    try:
        await send_coro
        return True
    except Exception as exc:
        logger.exception(
            "ENVÍO NO ENTREGADO (channel=%s conversation=%s): respuesta generada y "
            "persistida pero no enviada; requiere reintento fuera de banda",
            channel,
            conversation_id,
        )
        record_error(
            "webhook._deliver", exc, channel=channel, conversation_id=str(conversation_id)
        )
        return False


async def _handle_ig_event(db: Session, event: dict):
    msg = event.get("message", {})
    # Echo: Meta reenvía NUESTROS propios mensajes salientes como evento. Si no se
    # filtran, el bot se contestaría a sí mismo en loop. Descartar antes de todo.
    if msg.get("is_echo"):
        return
    text = msg.get("text")
    if not text:
        return
    ig_id = event.get("sender", {}).get("id")
    if not ig_id:
        return
    # Reclamar el evento DESPUÉS de validar que es procesable: si lo reclamáramos antes
    # y faltara el sender, un reintento válido de Meta quedaría descartado.
    mid = msg.get("mid")
    event_id = f"ig_{mid}" if mid else ""
    if not _claim_event(db, event_id):
        return
    # Todo lo que sigue al claim va DENTRO del try que libera el evento. Si algo entre el
    # get_or_create y el commit del turno falla por CUALQUIER motivo (checkout del pool /
    # OperationalError bajo ráfaga, timeout de Claude, hipo de DB), liberamos el evento
    # reclamado para que el reintento de Meta (mismo mid) lo reprocese. Sin esto el claim
    # queda quemado sin liberar y el inbound se PIERDE en silencio: el bot nunca contesta.
    # El _deliver posterior queda FUERA: ahí el turno YA está persistido y reintentar lo
    # duplicaría; el fallo de envío se trata aparte (reintento fuera de banda).
    #
    # TRADE-OFF CONOCIDO (duplicación del Message de usuario): igual que en WhatsApp
    # (_handle_change). record_turn commitea el Message del usuario ANTES del await a Claude,
    # así que el db.rollback() de acá no lo revierte y el reintento de Meta inserta un Message
    # de usuario idéntico → el inbound queda DUPLICADO (la respuesta del asistente se persiste
    # una sola vez). Se acepta: mejor duplicar el mensaje del usuario que PERDER el lead.
    # test_concurrency lo documenta (test_instagram_transient_failure_duplicates_user_message).
    try:
        # Ruteo por cuenta: recipient.id es la cuenta de IG que recibió el DM. Si hay
        # config (INSTAGRAM_ACCOUNT_TO_PROJECT), mapeamos a su proyecto; si no, cae al
        # default (mismo comportamiento previo, pero con el hook ya cableado).
        recipient_id = event.get("recipient", {}).get("id", "")
        project = _resolve_project(
            recipient_id, settings.ig_account_map(), DEFAULT_INSTAGRAM_PROJECT
        )
        conversation = get_or_create_conversation(
            db, f"ig_{ig_id}", project, "instagram", contact_instagram=ig_id
        )
        response_text = await record_turn(db, conversation, text)
    except Exception as exc:
        db.rollback()
        record_error("webhook._handle_ig_event", exc, channel="instagram", event_id=event_id)
        _release_event(db, event_id)
        return
    await _deliver(send_instagram_message(ig_id, response_text), "instagram", conversation.id)


async def _handle_change(db: Session, change: dict):
    field = change.get("field")
    value = change.get("value", {})

    if field == "messages":
        for msg in value.get("messages", []):
            if msg.get("type") != "text":
                continue
            text = (msg.get("text") or {}).get("body")
            if not text:
                continue
            phone = msg.get("from")
            if not phone:
                continue
            # Reclamar DESPUÉS de validar 'from': si reclamáramos antes y faltara,
            # un reintento válido de Meta (mismo wamid) quedaría descartado.
            wamid = msg.get("id")
            event_id = f"wa_{wamid}" if wamid else ""
            if not _claim_event(db, event_id):
                continue
            # Todo lo que sigue al claim va DENTRO del try que libera el evento. Si algo entre
            # el get_or_create y el commit del turno falla por CUALQUIER motivo (checkout del
            # pool / OperationalError bajo ráfaga, timeout de Claude, hipo de DB), liberamos el
            # evento reclamado para que el reintento de Meta (mismo wamid) lo reprocese. Sin
            # esto el claim queda quemado y el inbound se PIERDE: medio turno persistido (user
            # sin reply) y el lead nunca contestado. El _deliver posterior NO libera (ahí el
            # turno ya está persistido; reintentar lo duplicaría).
            #
            # TRADE-OFF CONOCIDO (duplicación del Message de usuario): record_turn commitea el
            # Message del usuario ANTES del await a Claude (conversation_service.record_turn),
            # así que cuando Claude falla ese mensaje YA está persistido y el db.rollback() de
            # acá no lo revierte (fue otra transacción). El reintento de Meta vuelve a insertar
            # un Message de usuario idéntico → el inbound queda DUPLICADO (la respuesta del
            # asistente, en cambio, se persiste una sola vez, en el intento exitoso). Se acepta:
            # mejor duplicar el mensaje del usuario que PERDER el lead. Deduplicarlo prolijamente
            # exigiría un id externo (wamid) en Message —columna + migración + cambio de firma de
            # record_turn—, fuera del alcance de este fix. test_concurrency lo documenta como
            # comportamiento esperado (test_whatsapp_transient_failure_duplicates_user_message).
            try:
                project = _resolve_project(phone, settings.wa_number_map(), DEFAULT_WHATSAPP_PROJECT)
                conversation = get_or_create_conversation(
                    db, f"wa_{phone}", project, "whatsapp", contact_phone=phone
                )
                # Marcar el inbound: reabre la ventana de servicio de 24h. Naive UTC para
                # igualar las columnas DateTime del modelo. Se persiste con el turno (record_turn
                # commitea) para que un envío proactivo posterior sepa si la ventana sigue abierta.
                conversation.last_inbound_at = datetime.now(timezone.utc).replace(tzinfo=None)
                # Opt-out de re-engagement: si el lead pide la baja (BAJA/STOP/CANCELAR…), marcarlo
                # para que el selector proactivo no le vuelva a escribir. Additivo: NO corta el flujo
                # —se sigue respondiendo este turno normalmente— y se persiste con el commit de
                # record_turn (mismo turno, sin commit extra en el hot path).
                if not conversation.reengage_opt_out and _is_optout_message(text):
                    conversation.reengage_opt_out = True
                    logger.info("Re-engagement opt-out registrado (conversation=%s)", conversation.id)
                response_text = await record_turn(db, conversation, text)
            except Exception as exc:
                db.rollback()
                record_error("webhook._handle_change", exc, channel="whatsapp", event_id=event_id)
                _release_event(db, event_id)
                return
            # Ruteo por ventana: con el inbound recién registrado la ventana está abierta,
            # así que esta respuesta sale free-form. send_whatsapp_reply centraliza la
            # decisión (abierta → free-form / cerrada → plantilla) para los envíos proactivos.
            await _deliver(
                send_whatsapp_reply(phone, response_text, conversation.last_inbound_at),
                "whatsapp",
                conversation.id,
            )

    elif field == "leadgen":
        await _handle_lead_ad(db, value)


async def _handle_lead_ad(db: Session, value: dict):
    """Procesa una submission de Lead Ads: trae los datos del formulario y guarda un Lead."""
    leadgen_id = value.get("leadgen_id")
    if not leadgen_id:
        return

    form_id = str(value.get("form_id", ""))
    project = _resolve_project(form_id, settings.lead_form_map(), DEFAULT_LEADFORM_PROJECT)

    # Traer y parsear los datos del lead ANTES de reclamar el evento: si Graph falla
    # acá, no se reclamó nada y Meta puede reintentar (no se pierde el lead).
    data = await get_lead_data(leadgen_id)
    fields = parse_lead_fields(data.get("field_data", []))

    # Reclamar el evento recién ahora (idempotencia). Si ya estaba, salir.
    event_id = f"lead_{leadgen_id}"
    if not _claim_event(db, event_id):
        return

    # Todo lo que sigue al claim va DENTRO del try que libera el evento. Si get_or_create,
    # el parseo o el commit fallan por CUALQUIER motivo —no solo IntegrityError: también
    # checkout del pool / OperationalError bajo ráfaga de campaña— liberamos el ProcessedEvent
    # reclamado para que Meta pueda reintentar. Sin esto el lead PAGO quedaría marcado como
    # procesado y el reintento de Meta lo descartaría → lead perdido para siempre.
    try:
        conversation = get_or_create_conversation(db, f"lead_{leadgen_id}", project, "lead_ad")
        conversation.status = "hot"

        name = fields.get("full_name") or fields.get("name")
        email = fields.get("email")
        phone = fields.get("phone_number") or fields.get("phone")

        conversation.contact_name = name
        conversation.contact_email = email
        conversation.contact_phone = phone

        lead = conversation.lead
        if not lead:
            lead = Lead(conversation_id=conversation.id, project=project)
            db.add(lead)
        lead.name = name
        lead.email = email
        lead.phone = phone
        # interests como lista de strings "campo: valor": el panel lo renderiza como tags
        # (.length/.map). Guardar el dict crudo hacía que nunca aparecieran las tags.
        # Excluir los campos de contacto (ya van en name/email/phone): si no, se duplicaban
        # como tags (full_name, name, email, phone_number, phone).
        lead.interests = [
            f"{k}: {v}"
            for k, v in fields.items()
            if k not in _LEAD_CONTACT_FIELDS
        ]
        lead.status = "new"
        lead.notes = f"Vino de Lead Ad (form {form_id}, ad {value.get('ad_id', '?')})"

        db.commit()
    except Exception as exc:
        # Falló la persistencia del Lead (o el get_or_create/parseo previo): liberar el
        # ProcessedEvent reclamado para que un reintento de Meta pueda volver a procesar
        # (si no, el lead se perdería). _release_event hace su propio commit, así que el
        # db.rollback() previo deja la sesión limpia para el DELETE.
        db.rollback()
        logger.exception("Error guardando Lead Ad (project=%s)", project)
        record_error(
            "webhook._handle_lead_ad", exc, project=project, leadgen_id=str(leadgen_id)
        )
        _release_event(db, event_id)
        return

    logger.info("Lead Ad capturado (project=%s)", project)
    # Un Lead Ad entra directo como caliente → avisar al equipo.
    fire_hot_lead({
        "project": project, "channel": "lead_ad",
        "name": name, "phone": phone, "email": email,
    })
