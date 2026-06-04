import logging
import os
import time
from contextlib import contextmanager

import sqlalchemy as sa
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from models import Base

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://netview:netview@localhost:5432/netview",
)

_engine = None
_SessionLocal = None


def _get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    return _engine


def init_db(retries: int = 30, delay: float = 2.0) -> None:
    global _SessionLocal
    for attempt in range(1, retries + 1):
        try:
            engine = _get_engine()
            Base.metadata.create_all(engine)
            _SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
            # lightweight migrations for columns added after initial deploy
            with engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS name VARCHAR(255)"
                ))
                conn.commit()
            logger.info("Database ready")
            return
        except Exception as exc:
            logger.warning("DB not ready (attempt %d/%d): %s", attempt, retries, exc)
            time.sleep(delay)
    raise RuntimeError("Could not connect to the database after %d attempts" % retries)


@contextmanager
def get_db():
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
