from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from config import get_settings

settings = get_settings()
_db_url = settings.database_url.replace("postgres://", "postgresql://", 1)
engine = create_engine(_db_url)
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
