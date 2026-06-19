from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session
from pydantic import BaseModel

from database import get_db
from models import Conversation, Lead

router = APIRouter()


@router.get("/")
def list_leads(
    project: str | None = None,
    status: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    query = db.query(Lead)
    if project:
        query = query.filter_by(project=project)
    if status:
        query = query.filter_by(status=status)
    leads = query.order_by(Lead.created_at.desc()).limit(limit).offset(offset).all()
    return [_serialize(l) for l in leads]


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
    week_ago = datetime.utcnow() - timedelta(days=7)
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
        "interests": l.interests,
        "notes": l.notes,
        "status": l.status,
        "created_at": l.created_at.isoformat() if l.created_at else None,
    }
