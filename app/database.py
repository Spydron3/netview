import logging
import os
import time
from contextlib import contextmanager

import sqlalchemy as sa
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import models  # noqa: F401 – all subclasses must be imported before create_all
from models import Base, Setting

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
                # remove legacy SNMP columns from switches (idempotent)
                for col in ("community", "enabled", "last_polled", "status"):
                    conn.execute(text(
                        f"ALTER TABLE switches DROP COLUMN IF EXISTS {col}"
                    ))
                # drop legacy topology_links table
                conn.execute(text("DROP TABLE IF EXISTS topology_links"))
                # migrate switch_ports.device_id → port_connections (M:N table)
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS port_connections (
                        id             SERIAL PRIMARY KEY,
                        switch_port_id INTEGER NOT NULL UNIQUE
                            REFERENCES switch_ports(id) ON DELETE CASCADE,
                        device_id      INTEGER NOT NULL
                            REFERENCES devices(id) ON DELETE CASCADE
                    )
                """))
                # copy existing assignments if the old column still exists
                conn.execute(text("""
                    INSERT INTO port_connections (switch_port_id, device_id)
                    SELECT sp.id, sp.device_id
                    FROM switch_ports sp
                    WHERE sp.device_id IS NOT NULL
                      AND EXISTS (
                          SELECT 1 FROM information_schema.columns
                          WHERE table_name='switch_ports' AND column_name='device_id'
                      )
                    ON CONFLICT (switch_port_id) DO NOTHING
                """))
                conn.execute(text(
                    "ALTER TABLE switch_ports DROP COLUMN IF EXISTS device_id"
                ))
                # seed default settings from env vars on first run
                defaults = {
                    "scan_interval": os.environ.get("SCAN_INTERVAL", "300"),
                    "port_scan_enabled": os.environ.get("PORT_SCAN_ENABLED", "true"),
                    "network_range": os.environ.get("NETWORK_RANGE", ""),
                }
                for key, value in defaults.items():
                    conn.execute(text(
                        "INSERT INTO settings (key, value) VALUES (:k, :v) "
                        "ON CONFLICT (key) DO NOTHING"
                    ), {"k": key, "v": value})
                conn.commit()
            logger.info("Database ready")
            return
        except Exception as exc:
            logger.warning("DB not ready (attempt %d/%d): %s", attempt, retries, exc)
            time.sleep(delay)
    raise RuntimeError("Could not connect to the database after %d attempts" % retries)


def get_setting(key: str, default: str = "") -> str:
    try:
        with get_db() as db:
            row = db.get(Setting, key)
            return row.value if row else default
    except Exception:
        return default


def set_setting(key: str, value: str) -> None:
    with get_db() as db:
        row = db.get(Setting, key)
        if row:
            row.value = value
        else:
            db.add(Setting(key=key, value=value))


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
