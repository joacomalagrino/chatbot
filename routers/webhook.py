import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config import PROJECTS, get_settings
from database import SessionLocal
from models import Conversation, Lead, ProcessedEvent
from services.conversation_service import get_or_create_conversation, record_turn
from services.meta_service import (
    get_lead_data,
    parse_lead_fields,
    send_instagram_message,
    send_whatsapp_message,
)

router = APIRouter()
settings = get_settings()
logger = logging.getLogger(__name__)

DEFAULT_WHATSAPP_PROJECT = "agencia"
DEFAULT_INSTAGRAM_PROJECT = "agencia"
DEFAULT_LEADFORM_PROJECT = "agencia"

# Los webhooks de Meta son chicos; cualquier cosa enorme es abuso.
MAX_WEBHOOK_BYTES = 256 * 1024


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
async def receive_meta_event(request: Request, background_tasks: BackgroundTasks):
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > MAX_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail="Payload demasiado grande")
    body_bytes = await request.body()
    if len(body_bytes) > MAX_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail="Payload demasiado grande")
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
    """Procesa el webhook con su propia sesión de DB (la de get_db ya está cerrada)."""
    db = SessionLocal()
    try:
        for entry in body.get("entry", []):
            # Instagram / Messenger DMs llegan a nivel `messaging`.
            for event in entry.get("messaging", []):
                await _handle_ig_event(db, event)
            # WhatsApp y Lead Ads llegan bajo `changes`.
            for change in entry.get("changes", []):
                await _handle_change(db, change)
    except Exception:
        logger.exception("Error procesando webhook Meta")
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
    except Exception:
        logger.exception(
            "ENVÍO NO ENTREGADO (channel=%s conversation=%s): respuesta generada y "
            "persistida pero no enviada; requiere reintento fuera de banda",
            channel,
            conversation_id,
        )
        return False


async def _handle_ig_event(db: Session, event: dict):
    msg = event.get("message", {})
    text = msg.get("text")
    if not text:
        return
    mid = msg.get("mid")
    if not _claim_event(db, f"ig_{mid}" if mid else ""):
        return
    ig_id = event.get("sender", {}).get("id")
    if not ig_id:
        return
    project = _resolve_project("", {}, DEFAULT_INSTAGRAM_PROJECT)
    conversation = get_or_create_conversation(
        db, f"ig_{ig_id}", project, "instagram", contact_instagram=ig_id
    )
    response_text = await record_turn(db, conversation, text)
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
            wamid = msg.get("id")
            if not _claim_event(db, f"wa_{wamid}" if wamid else ""):
                continue
            phone = msg.get("from")
            if not phone:
                continue
            project = _resolve_project(phone, settings.wa_number_map(), DEFAULT_WHATSAPP_PROJECT)
            conversation = get_or_create_conversation(
                db, f"wa_{phone}", project, "whatsapp", contact_phone=phone
            )
            response_text = await record_turn(db, conversation, text)
            await _deliver(send_whatsapp_message(phone, response_text), "whatsapp", conversation.id)

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
    lead.interests = [f"{k}: {v}" for k, v in fields.items()]
    lead.status = "new"
    lead.notes = f"Vino de Lead Ad (form {form_id}, ad {value.get('ad_id', '?')})"

    try:
        db.commit()
    except IntegrityError:
        # Falló la persistencia del Lead: liberar el ProcessedEvent reclamado para que
        # un reintento de Meta pueda volver a procesar (si no, el lead se perdería).
        db.rollback()
        logger.exception("IntegrityError guardando Lead Ad")
        _release_event(db, event_id)
        return

    logger.info("Lead Ad capturado (project=%s)", project)
