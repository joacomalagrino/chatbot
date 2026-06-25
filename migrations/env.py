"""Entorno de Alembic.

Reutiliza el engine y la metadata de la app: config.py (DATABASE_URL) es la única
fuente de verdad de la conexión, así el CLI (`alembic upgrade head`) y el bootstrap
en runtime (database.init_db) operan exactamente sobre la misma DB.
"""
import os
import sys
from logging.config import fileConfig

from alembic import context

# La raíz del repo en sys.path para poder importar database/models (igual que conftest).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import Base, engine  # noqa: E402
import models  # noqa: E402,F401 — registra los modelos en Base.metadata

config = context.config

# Configurar logging SOLO cuando se corre por CLI (hay archivo .ini). En runtime la
# Config se arma en código sin config_file_name, así no se pisa el logging del host.
if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except Exception:
        pass

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Genera SQL sin conectarse (alembic upgrade --sql)."""
    context.configure(
        url=str(engine.url),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,   # batch para que los ALTER futuros corran en SQLite
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Corre las migraciones contra la DB real (engine de la app)."""
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
