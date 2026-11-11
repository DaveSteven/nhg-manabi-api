import os
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


raw_database_url = os.getenv("DATABASE_URL")
if not raw_database_url and os.getenv("RENDER"):
    raise RuntimeError(
        "DATABASE_URL is required on Render. Create a Render PostgreSQL database "
        "and set DATABASE_URL to its internal connection string."
    )

DATABASE_URL = raw_database_url or "sqlite:///./data/nhg_manabi.db"

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

if DATABASE_URL.startswith("sqlite:///"):
    sqlite_path = DATABASE_URL.removeprefix("sqlite:///")
    if sqlite_path and sqlite_path != ":memory:":
        Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    pool_pre_ping=not DATABASE_URL.startswith("sqlite"),
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


@event.listens_for(engine, "connect")
def configure_sqlite(dbapi_connection, _connection_record):
    if not DATABASE_URL.startswith("sqlite"):
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
