"""Tests de las migraciones de Alembic.

Verifican que la baseline:
- crea el schema completo en una DB fresca,
- ADOPTA (stamp, sin recrear ni perder datos) una DB preexistente estilo prod,
- es idempotente,
- y no tiene drift contra los modelos (si alguien cambia un modelo sin generar la
  migración correspondiente, este test falla).
"""
import logging

import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory

import database
import models
import observability
from database import SessionLocal

APP_TABLES = {"conversations", "messages", "leads", "processed_events"}


def _head_revision():
    """La revisión HEAD según los archivos de migración (no hardcodear: a medida que
    se agregan migraciones, init_db lleva la DB al HEAD actual, no al baseline)."""
    return ScriptDirectory.from_config(database._alembic_config()).get_current_head()


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
    assert _alembic_version() == _head_revision()


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

    database.init_db()  # debe adoptar (stamp baseline) y luego aplicar lo pendiente

    # Tras adoptar el baseline, init_db sigue aplicando las migraciones posteriores
    # (ej. 0002 last_inbound_at) hasta HEAD, sin perder los datos preexistentes.
    assert _alembic_version() == _head_revision()
    db = SessionLocal()
    got = db.get(models.Conversation, cid)
    assert got is not None and got.session_id == "sess-preexist"
    db.close()


def test_idempotent_second_run():
    _reset_db()
    database.init_db()
    database.init_db()  # no debe romper ni cambiar la versión
    assert _alembic_version() == _head_revision()


def test_no_drift_baseline_matches_models():
    """Si alguien cambia un modelo sin generar la migración, esto falla."""
    _reset_db()
    database.init_db()
    with database.engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        diff = compare_metadata(ctx, models.Base.metadata)
    assert diff == [], f"Drift entre modelos y migraciones: {diff}"


def test_alembic_failure_falls_back_and_alarms(monkeypatch, caplog):
    """Si Alembic falla en el arranque, init_db debe:
    - seguir arrancando (crear el schema con create_all), y
    - NO hacerlo en silencio: alarmar fuerte (log CRITICAL) y registrar el
      fallback en observability para no quedar ciegos ante un schema desincronizado.
    """
    _reset_db()
    observability.clear_errors()

    def _boom(*_args, **_kwargs):
        raise RuntimeError("alembic tooling roto")

    # Forzar que la rama de Alembic explote (sin tocar la DB real).
    monkeypatch.setattr(database, "_alembic_config", _boom)

    with caplog.at_level(logging.CRITICAL, logger="database"):
        database.init_db()  # NO debe propagar: la app arranca igual

    # Arrancó igual: el schema quedó creado por el fallback create_all.
    assert APP_TABLES <= _table_names()

    # Alarmó fuerte: hay un log CRITICAL mencionando el fallback.
    critical_msgs = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert any("create_all" in r.getMessage() for r in critical_msgs), (
        "Se esperaba un log CRITICAL avisando del fallback a create_all"
    )

    # Quedó registrado en observability para verlo desde el panel.
    contexts = [e["context"] for e in observability.recent_errors()]
    assert "database.init_db.alembic_fallback" in contexts
