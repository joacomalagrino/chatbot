from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from config import get_settings

settings = get_settings()
_db_url = settings.database_url.replace("postgres://", "postgresql://", 1)
# pool_pre_ping: valida la conexión antes de usarla (Railway cierra conexiones idle).
# pool_recycle: recicla conexiones más viejas que 30 min antes de que el server las corte.
engine = create_engine(_db_url, pool_pre_ping=True, pool_recycle=1800)
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
