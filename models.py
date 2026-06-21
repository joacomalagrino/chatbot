from sqlalchemy import Column, String, Text, DateTime, ForeignKey, JSON, Uuid
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
import uuid
from database import Base

# Uuid genérico de SQLAlchemy 2.0: usa el tipo UUID nativo en Postgres y CHAR(32)
# en SQLite, así los tests/dev local corren sin Postgres y prod queda igual.


def _utcnow():
    """Timestamp UTC naive. Reemplaza a datetime.utcnow (deprecado en 3.12)
    sin volverse aware: las columnas DateTime son naive (sin timezone=True),
    y en Postgres comparar naive vs aware rompe."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    project = Column(String(50), nullable=False, index=True)   # agencia | mesa | ticketera
    session_id = Column(String(200), nullable=False, unique=True)
    channel = Column(String(20), default="web")            # web | whatsapp | instagram
    contact_name = Column(String(200))
    contact_phone = Column(String(50))
    contact_email = Column(String(200))
    contact_instagram = Column(String(100))
    status = Column(String(20), default="new", index=True)  # new | warm | hot | converted
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    messages = relationship("Message", back_populates="conversation", order_by="Message.created_at")
    lead = relationship("Lead", back_populates="conversation", uselist=False)


class Message(Base):
    __tablename__ = "messages"

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    conversation_id = Column(Uuid, ForeignKey("conversations.id"), nullable=False, index=True)
    role = Column(String(20), nullable=False)               # user | assistant
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=_utcnow)

    conversation = relationship("Conversation", back_populates="messages")


class ProcessedEvent(Base):
    """Idempotencia: registra los ids de eventos de Meta ya procesados
    (wamid de WhatsApp, mid de Instagram, leadgen_id) para descartar reintentos."""
    __tablename__ = "processed_events"

    event_id = Column(String(200), primary_key=True)
    created_at = Column(DateTime, default=_utcnow)


class Lead(Base):
    __tablename__ = "leads"

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    conversation_id = Column(Uuid, ForeignKey("conversations.id"), unique=True)
    project = Column(String(50), nullable=False, index=True)
    name = Column(String(200))
    phone = Column(String(50))
    email = Column(String(200))
    instagram = Column(String(100))
    interests = Column(JSON)
    status = Column(String(20), default="new", index=True)  # new | contacted | qualified | lost
    notes = Column(Text)
    created_at = Column(DateTime, default=_utcnow, index=True)

    conversation = relationship("Conversation", back_populates="lead")
