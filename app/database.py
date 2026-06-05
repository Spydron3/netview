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
                # allow MAC-only unmanaged switches
                conn.execute(text(
                    "ALTER TABLE switches ALTER COLUMN ip_address DROP NOT NULL"
                ))
                conn.execute(text(
                    "ALTER TABLE switches ADD COLUMN IF NOT EXISTS "
                    "mac_address VARCHAR(17) UNIQUE"
                ))
                # drop legacy topology_links table
                conn.execute(text("DROP TABLE IF EXISTS topology_links"))
                # drop legacy device_id column from switch_ports (moved to port_links)
                conn.execute(text("""
                    DO $$
                    BEGIN
                        IF EXISTS (
                            SELECT 1 FROM information_schema.columns
                            WHERE table_name='switch_ports' AND column_name='device_id'
                        ) THEN
                            ALTER TABLE switch_ports DROP COLUMN device_id;
                        END IF;
                    END $$
                """))
                # migrate port_connections + switch_links → unified port_links table
                conn.execute(text("""
                    DO $$
                    BEGIN
                        IF EXISTS (
                            SELECT 1 FROM information_schema.tables
                            WHERE table_name = 'port_connections'
                        ) THEN
                            INSERT INTO port_links (port_a_id, device_id)
                            SELECT switch_port_id, device_id FROM port_connections
                            ON CONFLICT (port_a_id) DO NOTHING;
                            DROP TABLE port_connections;
                        END IF;
                        IF EXISTS (
                            SELECT 1 FROM information_schema.tables
                            WHERE table_name = 'switch_links'
                        ) THEN
                            INSERT INTO port_links (port_a_id, port_b_id)
                            SELECT LEAST(port_a_id, port_b_id),
                                   GREATEST(port_a_id, port_b_id)
                            FROM switch_links
                            ON CONFLICT (port_a_id) DO NOTHING;
                            DROP TABLE switch_links;
                        END IF;
                    END $$
                """))
                # device_ports table is created by create_all above;
                # migrate port_links.device_id → port_links.dev_port_id
                conn.execute(text(
                    "ALTER TABLE port_links ADD COLUMN IF NOT EXISTS "
                    "dev_port_id INTEGER UNIQUE REFERENCES device_ports(id) ON DELETE CASCADE"
                ))
                conn.execute(text("""
                    DO $$
                    DECLARE
                        _dev_id  INTEGER;
                        _dp_id   INTEGER;
                    BEGIN
                        IF EXISTS (
                            SELECT 1 FROM information_schema.columns
                            WHERE table_name='port_links' AND column_name='device_id'
                        ) THEN
                            FOR _dev_id IN (
                                SELECT DISTINCT device_id FROM port_links WHERE device_id IS NOT NULL
                            ) LOOP
                                INSERT INTO device_ports (device_id, label)
                                VALUES (_dev_id, 'eth0')
                                RETURNING id INTO _dp_id;
                                UPDATE port_links
                                SET dev_port_id = _dp_id
                                WHERE device_id = _dev_id AND dev_port_id IS NULL;
                            END LOOP;
                            ALTER TABLE port_links DROP COLUMN device_id;
                        END IF;
                    END $$
                """))
                # virtual / wireless device fields
                conn.execute(text(
                    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS "
                    "is_virtual BOOLEAN NOT NULL DEFAULT FALSE"
                ))
                conn.execute(text(
                    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS "
                    "parent_id INTEGER REFERENCES devices(id) ON DELETE SET NULL"
                ))
                conn.execute(text(
                    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS "
                    "is_wireless BOOLEAN NOT NULL DEFAULT FALSE"
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
