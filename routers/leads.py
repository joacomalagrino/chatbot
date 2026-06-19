from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from database import get_db
from models import Lead

router = APIRouter()


@router.get("/")
def list_leads(project: str | None = None, status: str | None = None, db: Session = Depends(get_db)):
    query = db.query(Lead)
    if project:
        query = query.filter_by(project=project)
    if status:
        query = query.filter_by(status=status)
    leads = query.order_by(Lead.created_at.desc()).all()
    return [_serialize(l) for l in leads]


class StatusUpdate(BaseModel):
    status: str


@router.patch("/{lead_id}/status")
def update_status(lead_id: str, body: StatusUpdate, db: Session = Depends(get_db)):
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
        "status": l.status,
        "created_at": l.created_at.isoformat() if l.created_at else None,
    }
