"""Trigger del re-engagement proactivo (endpoint admin).

El repo NO tiene scheduler propio (ni APScheduler ni Celery): la corrida periódica se
dispara desde afuera con un cron (Railway cron / GitHub Actions / cron-job.org) que pega a
este endpoint. Va montado bajo /reengage con la MISMA auth admin que /leads y /ads
(require_admin → ADMIN_API_KEY, fail-closed).

Es un NO-OP seguro mientras el re-engagement esté apagado (REENGAGE_ENABLED=0) o sin
plantilla cargada: en ese caso devuelve skipped="disabled" y no manda nada.
"""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from database import get_db
from ratelimit import limiter
from services.reengage_service import run_reengagement

router = APIRouter()


@router.post("/run")
@limiter.limit("6/minute")
async def trigger_reengagement(request: Request, db: Session = Depends(get_db)):
    """Dispara una corrida de re-engagement y devuelve el resumen (selected/sent/failed).

    Pensado para cablearse a un cron externo (ver README). Idempotente entre corridas:
    los leads ya re-enganchados (reengaged_at no nulo) quedan excluidos del selector, así
    que reintentar la corrida no duplica envíos."""
    return await run_reengagement(db)
