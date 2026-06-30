import logging
import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError, TimeoutError as PoolTimeoutError
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()
_db_url = settings.database_url.replace("postgres://", "postgresql://", 1)


def _int_env(name: str, default: int) -> int:
    """Lee un entero de entorno; ante valor ausente o inválido cae al default
    (no rompe el arranque por una env mal cargada)."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s inválido (%r); usando default %d", name, raw, default)
        return default


# pool_pre_ping: valida la conexión antes de usarla (Railway cierra conexiones idle).
# pool_recycle: recicla conexiones más viejas que 30 min antes de que el server las corte.
_engine_kwargs = {"pool_pre_ping": True, "pool_recycle": 1800}
# Dimensionar el pool para ráfagas (webhooks concurrentes + panel). SQLite usa otro
# pool que no acepta estos kwargs, así que solo se aplican en Postgres.
#
# Tunables por entorno (sin redeploy de código) para ráfagas de campaña: cada request
# toma una conexión del pool mientras corre su sección de DB, así que el techo de
# concurrencia efectiva ≈ pool_size + max_overflow. Si bajo carga aparecen timeouts
# "QueuePool limit ... reached", subir DB_POOL_SIZE / DB_MAX_OVERFLOW (sin pasarse del
# max_connections del Postgres) o DB_POOL_TIMEOUT.
if not _db_url.startswith("sqlite"):
    _engine_kwargs.update(
        pool_size=_int_env("DB_POOL_SIZE", 10),
        max_overflow=_int_env("DB_MAX_OVERFLOW", 20),
        pool_timeout=_int_env("DB_POOL_TIMEOUT", 30),
        # connect_timeout: cota el TCP-connect a Postgres. Sin esto, si la DB está
        # inalcanzable (host caído / red black-hole, no un "connection refused
        # inmediato"), abrir una conexión nueva bloquea al default del SO (~2 min),
        # colgando al worker bajo carga. Con la cota, una DB lenta/caída DEGRADA
        # (falla rápido y el request da 5xx) en vez de quedar colgado. psycopg2 lo
        # toma vía connect_args. Tunable por entorno como el resto del pool.
        connect_args={"connect_timeout": _int_env("DB_CONNECT_TIMEOUT", 10)},
    )
engine = create_engine(_db_url, **_engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def is_pool_exhaustion(exc: BaseException) -> bool:
    """¿Esta excepción es una saturación del pool de DB?

    Cubre los dos modos que delatan el techo de concurrencia bajo carga:
    - `sqlalchemy.exc.TimeoutError` ("QueuePool limit ... reached"): el pool no consiguió
      una conexión libre dentro de `pool_timeout`. Es la señal #1 de saturación.
    - `OperationalError` cuyo mensaje habla de conexiones/pool agotados (algunos drivers
      reportan el agotamiento del lado servidor así, no como pool timeout de SQLAlchemy).

    Detección por tipo + texto (no por instancia exacta) para no acoplarse a un driver.
    """
    if isinstance(exc, PoolTimeoutError):
        return True
    if isinstance(exc, OperationalError):
        msg = str(exc).lower()
        return "queuepool" in msg or "too many connections" in msg or "remaining connection slots" in msg
    return False


def get_db():
    """Dependencia de sesión para los endpoints. Si el pool está saturado, la `TimeoutError`
    de SQLAlchemy se lanza al ejecutar la primera query DENTRO del endpoint (el checkout es
    lazy), no acá; por eso la detección de saturación vive en el middleware HTTP de main.py
    (que ve la excepción propagándose) y no en este generador."""
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
    # Historial por turno: filtra por conversation_id y ordena por created_at DESC.
    # El índice compuesto cubre filtro + orden de una (evita el sort en memoria del
    # ORDER BY ... LIMIT cuando una conversación de WhatsApp acumula muchos mensajes).
    "CREATE INDEX IF NOT EXISTS ix_messages_conversation_created "
    "ON messages (conversation_id, created_at)",
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
