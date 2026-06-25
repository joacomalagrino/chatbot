from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session
from pydantic import BaseModel

from database import get_db
from models import Conversation, Lead

router = APIRouter()


@router.get("/")
def list_leads(
    project: str | None = None,
    status: str | None = None,
    q: str | None = None,
    sort: str = Query("recent", pattern="^(recent|oldest)$"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    query = db.query(Lead)
    if project:
        query = query.filter_by(project=project)
    if status:
        query = query.filter_by(status=status)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(
            Lead.name.ilike(like), Lead.email.ilike(like),
            Lead.phone.ilike(like), Lead.instagram.ilike(like),
        ))
    order = Lead.created_at.asc() if sort == "oldest" else Lead.created_at.desc()
    leads = query.order_by(order).limit(limit).offset(offset).all()
    return [_serialize(l) for l in leads]


@router.get("/{lead_id}/messages")
def lead_messages(lead_id: UUID, db: Session = Depends(get_db)):
    """Transcript de la conversación asociada a un lead (para verlo en el panel)."""
    lead = db.query(Lead).filter_by(id=lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead no encontrado")
    conv = lead.conversation
    if not conv:
        return {"channel": None, "messages": []}
    return {
        "channel": conv.channel,
        "messages": [
            {
                "role": m.role,
                "content": m.content,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in conv.messages
        ],
    }


def _csv_safe(val) -> str:
    """Anti CSV-injection: si la celda empieza con un caracter de fórmula, la prefija
    con comilla simple para que Excel/Sheets no la interpreten como fórmula."""
    s = "" if val is None else str(val)
    if s[:1] in ("=", "+", "-", "@", "\t", "\r"):
        s = "'" + s
    return s


@router.get("/export.csv")
def export_leads(
    project: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
):
    """Exporta los leads (mismos filtros que el listado) a CSV descargable."""
    query = db.query(Lead)
    if project:
        query = query.filter_by(project=project)
    if status:
        query = query.filter_by(status=status)
    leads = query.order_by(Lead.created_at.desc()).all()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["fecha", "proyecto", "nombre", "telefono", "email",
                "instagram", "estado", "intereses", "notas"])
    for l in leads:
        w.writerow([_csv_safe(x) for x in (
            l.created_at.isoformat() if l.created_at else "",
            l.project, l.name, l.phone, l.email, l.instagram, l.status,
            "; ".join(_interests_list(l.interests)),
            (l.notes or "").replace("\n", " "),
        )])
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=leads.csv"},
    )


@router.get("/stats")
def lead_stats(db: Session = Depends(get_db)):
    """Métricas para el dashboard del panel: totales por estado, proyecto y canal."""
    by_status = dict(db.query(Lead.status, func.count(Lead.id)).group_by(Lead.status).all())
    by_project = dict(db.query(Lead.project, func.count(Lead.id)).group_by(Lead.project).all())
    by_channel = dict(
        db.query(Conversation.channel, func.count(Conversation.id))
        .group_by(Conversation.channel).all()
    )
    total = db.query(func.count(Lead.id)).scalar() or 0
    week_ago = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)
    last_7d = db.query(func.count(Lead.id)).filter(Lead.created_at >= week_ago).scalar() or 0
    return {
        "total": total,
        "last_7d": last_7d,
        "by_status": by_status,
        "by_project": by_project,
        "by_channel": by_channel,
    }


class StatusUpdate(BaseModel):
    status: Literal["new", "contacted", "qualified", "lost"]


@router.patch("/{lead_id}/status")
def update_status(lead_id: UUID, body: StatusUpdate, db: Session = Depends(get_db)):
    # lead_id: UUID -> FastAPI valida el formato y pasa un objeto UUID al query
    # (la columna es Uuid; pasarle un str crudo rompería con StatementError).
    lead = db.query(Lead).filter_by(id=lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead no encontrado")
    lead.status = body.status
    db.commit()
    return _serialize(lead)


def _serialize(l: Lead) -> dict:
    return {
        "id": str(l.id),
        "project": l.project,
        "name": l.name,
        "phone": l.phone,
        "email": l.email,
        "instagram": l.instagram,
        # interests: lista de strings (el panel espera .length/.map). Toleramos un dict
        # legacy (leads guardados antes del fix) aplanándolo a "clave: valor".
        "interests": _interests_list(l.interests),
        "notes": l.notes,
        "status": l.status,
        "created_at": l.created_at.isoformat() if l.created_at else None,
    }


def _interests_list(interests) -> list[str]:
    """Normaliza `interests` a lista de strings para el panel.

    Acepta el formato nuevo (lista) y el legacy (dict {campo: valor})."""
    if isinstance(interests, dict):
        return [f"{k}: {v}" for k, v in interests.items()]
    if isinstance(interests, list):
        return [str(i) for i in interests]
    return []
