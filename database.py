import logging

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()
_db_url = settings.database_url.replace("postgres://", "postgresql://", 1)
# pool_pre_ping: valida la conexión antes de usarla (Railway cierra conexiones idle).
# pool_recycle: recicla conexiones más viejas que 30 min antes de que el server las corte.
_engine_kwargs = {"pool_pre_ping": True, "pool_recycle": 1800}
# Dimensionar el pool para ráfagas (webhooks concurrentes + panel). SQLite usa otro
# pool que no acepta estos kwargs, así que solo se aplican en Postgres.
if not _db_url.startswith("sqlite"):
    _engine_kwargs.update(pool_size=10, max_overflow=20, pool_timeout=30)
engine = create_engine(_db_url, **_engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_tables():
    import models  # noqa: F401 — ensures models are registered before create_all
    Base.metadata.create_all(bind=engine)
    _ensure_indexes()


# Índices que aceleran los filtros de /leads y la consulta de historial.
# create_all NO agrega índices a tablas ya existentes (prod), así que los
# creamos explícitamente de forma idempotente. Nombres = convención de SQLAlchemy.
_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_messages_conversation_id ON messages (conversation_id)",
    "CREATE INDEX IF NOT EXISTS ix_leads_project ON leads (project)",
    "CREATE INDEX IF NOT EXISTS ix_leads_status ON leads (status)",
    "CREATE INDEX IF NOT EXISTS ix_leads_created_at ON leads (created_at)",
    "CREATE INDEX IF NOT EXISTS ix_conversations_project ON conversations (project)",
    "CREATE INDEX IF NOT EXISTS ix_conversations_status ON conversations (status)",
    "CREATE INDEX IF NOT EXISTS ix_processed_events_created_at ON processed_events (created_at)",
]


def _ensure_indexes():
    try:
        with engine.begin() as conn:
            for stmt in _INDEXES:
                conn.execute(text(stmt))
    except Exception:
        logger.exception("No se pudieron crear los índices (no es fatal)")
