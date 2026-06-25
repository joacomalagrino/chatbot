"""Tests de las migraciones de Alembic.

Verifican que la baseline:
- crea el schema completo en una DB fresca,
- ADOPTA (stamp, sin recrear ni perder datos) una DB preexistente estilo prod,
- es idempotente,
- y no tiene drift contra los modelos (si alguien cambia un modelo sin generar la
  migración correspondiente, este test falla).
"""
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

import database
import models
from database import SessionLocal

APP_TABLES = {"conversations", "messages", "leads", "processed_events"}


def _reset_db():
    """Deja la DB sin NINGUNA tabla, incluida alembic_version."""
    models.Base.metadata.drop_all(bind=database.engine)
    with database.engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE IF EXISTS alembic_version"))


def _table_names():
    return set(sa.inspect(database.engine).get_table_names())


def _alembic_version():
    with database.engine.connect() as conn:
        return conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()


def teardown_function():
    # Restaurar un estado limpio (create_all, sin alembic_version) para no
    # sorprender a los fixtures de otros archivos de test.
    _reset_db()
    models.Base.metadata.create_all(bind=database.engine)


def test_fresh_db_runs_all_migrations():
    _reset_db()
    database.init_db()
    names = _table_names()
    assert APP_TABLES <= names
    assert "alembic_version" in names
    assert _alembic_version() == database.BASELINE_REVISION


def test_adopts_preexisting_db_without_data_loss():
    """El caso de prod: tablas creadas por create_all (sin alembic_version) con
    datos existentes. init_db debe STAMPEAR (no recrear) y preservar los datos."""
    _reset_db()
    models.Base.metadata.create_all(bind=database.engine)  # DB estilo pre-Alembic
    db = SessionLocal()
    conv = models.Conversation(project="agencia", session_id="sess-preexist")
    db.add(conv)
    db.commit()
    cid = conv.id
    db.close()

    database.init_db()  # debe adoptar (stamp), no recrear

    assert _alembic_version() == database.BASELINE_REVISION
    db = SessionLocal()
    got = db.get(models.Conversation, cid)
    assert got is not None and got.session_id == "sess-preexist"
    db.close()


def test_idempotent_second_run():
    _reset_db()
    database.init_db()
    database.init_db()  # no debe romper ni cambiar la versión
    assert _alembic_version() == database.BASELINE_REVISION


def test_no_drift_baseline_matches_models():
    """Si alguien cambia un modelo sin generar la migración, esto falla."""
    _reset_db()
    database.init_db()
    with database.engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        diff = compare_metadata(ctx, models.Base.metadata)
    assert diff == [], f"Drift entre modelos y migraciones: {diff}"
