import logging
import os

from sqlalchemy import create_engine, inspect, text
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


# --- Migraciones (Alembic) --------------------------------------------------
# El schema lo maneja Alembic (migrations/). create_tables() queda como fallback
# para que la app SIEMPRE arranque aunque el tooling de Alembic falle.

_MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")
BASELINE_REVISION = "0001_baseline"


def _alembic_config():
    """Config de Alembic armada en código (sin .ini) con script_location ABSOLUTO,
    para funcionar sin importar el CWD (Railway corre desde /app; los tests desde la
    raíz del repo). Al no setear config_file_name, env.py no toca el logging del host."""
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", _MIGRATIONS_DIR)
    return cfg


def init_db():
    """Inicializa/migra el schema en el arranque.

    - DB fresca (tests/dev/entorno nuevo): aplica todas las migraciones desde cero.
    - DB de prod creada por create_all ANTES de Alembic (tiene tablas pero no
      `alembic_version`): la ADOPTA con `stamp` al baseline —sin recrear ni tocar
      datos— y luego aplica las migraciones posteriores que hubiera.
    - DB ya bajo Alembic: solo aplica lo pendiente (no-op si está en head).

    Si Alembic falla por cualquier motivo (tooling, path, import), cae a create_all:
    la app arranca igual, con el mismo comportamiento que antes de introducir Alembic."""
    try:
        from alembic import command

        tables = set(inspect(engine).get_table_names())
        cfg = _alembic_config()
        if "alembic_version" not in tables and "conversations" in tables:
            # DB preexistente sin control de versiones (la de prod): adoptarla.
            logger.info("DB preexistente detectada; stamp a %s (sin recrear)", BASELINE_REVISION)
            command.stamp(cfg, BASELINE_REVISION)
        command.upgrade(cfg, "head")
        _ensure_indexes()  # backstop idempotente (prod ya los tiene)
    except Exception as exc:
        # Caer a create_all deja el schema desincronizado de las migraciones SIN
        # control de versiones (no se aplican las migraciones posteriores al
        # baseline): la app arranca "sana" pero puede romper en runtime por una
        # columna faltante. NO cambiamos el comportamiento (seguimos arrancando),
        # pero alarmamos fuerte para que el fallback nunca pase silencioso.
        logger.critical(
            "ALARMA: Alembic falló en el arranque; fallback a create_all(). "
            "El schema puede quedar DESINCRONIZADO de las migraciones y romper en "
            "runtime por columnas faltantes. Revisar el tooling de Alembic.",
            exc_info=True,
        )
        try:
            import observability

            observability.record_error("database.init_db.alembic_fallback", exc)
        except Exception:  # nunca dejar que la observabilidad impida el arranque
            logger.exception("No se pudo registrar el fallback en observability")
        create_tables()
