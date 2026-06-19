import logging
import os
import time
from contextlib import contextmanager

import sqlalchemy as sa
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import models  # noqa: F401 – all subclasses must be imported before create_all
from models import Base, Room, Setting  # noqa: F401

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
                # legacy switches-table migrations (no-op if table already gone)
                conn.execute(text("""
                    DO $$
                    BEGIN
                        IF EXISTS (
                            SELECT 1 FROM information_schema.tables
                            WHERE table_name = 'switches'
                        ) THEN
                            ALTER TABLE switches DROP COLUMN IF EXISTS community;
                            ALTER TABLE switches DROP COLUMN IF EXISTS enabled;
                            ALTER TABLE switches DROP COLUMN IF EXISTS last_polled;
                            ALTER TABLE switches DROP COLUMN IF EXISTS status;
                            ALTER TABLE switches ALTER COLUMN ip_address DROP NOT NULL;
                            ALTER TABLE switches ADD COLUMN IF NOT EXISTS
                                mac_address VARCHAR(17) UNIQUE;
                        END IF;
                    END $$
                """))
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
                        _rec   RECORD;
                        _dp_id INTEGER;
                    BEGIN
                        IF EXISTS (
                            SELECT 1 FROM information_schema.columns
                            WHERE table_name='port_links' AND column_name='device_id'
                        ) THEN
                            FOR _rec IN
                                SELECT id, device_id FROM port_links
                                WHERE device_id IS NOT NULL AND dev_port_id IS NULL
                            LOOP
                                INSERT INTO device_ports (device_id, label)
                                VALUES (_rec.device_id, 'eth0')
                                RETURNING id INTO _dp_id;
                                UPDATE port_links
                                SET dev_port_id = _dp_id
                                WHERE id = _rec.id;
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
                # switch-as-device-flag migration
                conn.execute(text(
                    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS "
                    "is_switch BOOLEAN NOT NULL DEFAULT FALSE"
                ))
                conn.execute(text(
                    "ALTER TABLE devices ALTER COLUMN ip_address DROP NOT NULL"
                ))
                conn.execute(text("""
                    DO $$
                    DECLARE
                        sw      RECORD;
                        dev_id  INTEGER;
                    BEGIN
                        IF EXISTS (
                            SELECT 1 FROM information_schema.tables
                            WHERE table_name = 'switches'
                        ) THEN
                            -- drop old FK so we can repoint switch_ports.switch_id
                            ALTER TABLE switch_ports
                                DROP CONSTRAINT IF EXISTS switch_ports_switch_id_fkey;
                            FOR sw IN SELECT * FROM switches LOOP
                                dev_id := NULL;
                                IF sw.ip_address IS NOT NULL THEN
                                    SELECT id INTO dev_id FROM devices
                                    WHERE ip_address = sw.ip_address LIMIT 1;
                                END IF;
                                IF dev_id IS NULL AND sw.mac_address IS NOT NULL THEN
                                    SELECT id INTO dev_id FROM devices
                                    WHERE mac_address = sw.mac_address LIMIT 1;
                                END IF;
                                IF dev_id IS NULL THEN
                                    INSERT INTO devices (
                                        ip_address, mac_address, name,
                                        is_switch, is_online, open_ports,
                                        first_seen, last_seen, scan_count,
                                        is_virtual, is_wireless
                                    ) VALUES (
                                        sw.ip_address, sw.mac_address, sw.name,
                                        TRUE, FALSE, '[]'::json,
                                        NOW(), NOW(), 0, FALSE, FALSE
                                    ) RETURNING id INTO dev_id;
                                ELSE
                                    UPDATE devices
                                    SET is_switch = TRUE,
                                        name = COALESCE(name, sw.name)
                                    WHERE id = dev_id;
                                END IF;
                                UPDATE switch_ports SET switch_id = dev_id
                                WHERE switch_id = sw.id;
                            END LOOP;
                            ALTER TABLE switch_ports
                                ADD CONSTRAINT switch_ports_switch_id_fkey
                                FOREIGN KEY (switch_id)
                                REFERENCES devices(id) ON DELETE CASCADE;
                            DROP TABLE switches;
                        END IF;
                    END $$
                """))
                # generalised port-link: port_a_id nullable + dev_port_id_b for device-device links
                conn.execute(text("""
                    DO $$
                    BEGIN
                        IF EXISTS (
                            SELECT 1 FROM information_schema.columns
                            WHERE table_name='port_links' AND column_name='port_a_id'
                            AND is_nullable='NO'
                        ) THEN
                            ALTER TABLE port_links ALTER COLUMN port_a_id DROP NOT NULL;
                        END IF;
                    END $$
                """))
                conn.execute(text(
                    "ALTER TABLE port_links ADD COLUMN IF NOT EXISTS "
                    "dev_port_id_b INTEGER UNIQUE REFERENCES device_ports(id) ON DELETE CASCADE"
                ))
                # remove fully-orphaned port_links (all four FK columns null)
                conn.execute(text("""
                    DELETE FROM port_links
                    WHERE port_a_id IS NULL
                      AND port_b_id IS NULL
                      AND dev_port_id IS NULL
                      AND dev_port_id_b IS NULL
                """))
                # rooms table is created by create_all; add FK to devices
                conn.execute(text(
                    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS "
                    "room_id INTEGER REFERENCES rooms(id) ON DELETE SET NULL"
                ))
                # migrate old room string column → room_id FK
                conn.execute(text("""
                    DO $$
                    DECLARE
                        _name TEXT;
                        _rid  INTEGER;
                    BEGIN
                        IF EXISTS (
                            SELECT 1 FROM information_schema.columns
                            WHERE table_name='devices' AND column_name='room'
                        ) THEN
                            FOR _name IN
                                SELECT DISTINCT room FROM devices
                                WHERE room IS NOT NULL AND room <> ''
                            LOOP
                                INSERT INTO rooms (name) VALUES (_name)
                                ON CONFLICT (name) DO NOTHING;
                                SELECT id INTO _rid FROM rooms WHERE name = _name;
                                UPDATE devices SET room_id = _rid WHERE room = _name;
                            END LOOP;
                            ALTER TABLE devices DROP COLUMN room;
                        END IF;
                    END $$
                """))
                conn.execute(text(
                    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS "
                    "is_access_point BOOLEAN NOT NULL DEFAULT FALSE"
                ))
                conn.execute(text(
                    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS "
                    "vendor_looked_up BOOLEAN NOT NULL DEFAULT FALSE"
                ))
                # device_macs table is created by create_all; seed from devices.mac_address
                conn.execute(text("""
                    INSERT INTO device_macs (device_id, mac_address, type)
                    SELECT id, mac_address, 'wired'
                    FROM devices
                    WHERE mac_address IS NOT NULL
                    ON CONFLICT (mac_address) DO NOTHING
                """))
                # seed default settings from env vars on first run
                defaults = {
                    "scan_interval": os.environ.get("SCAN_INTERVAL", "300"),
                    "port_scan_enabled": os.environ.get("PORT_SCAN_ENABLED", "true"),
                    "network_range": os.environ.get("NETWORK_RANGE", ""),
                    "notify_new_device": os.environ.get("NOTIFY_NEW_DEVICE", "false"),
                    "notify_ip_change":  os.environ.get("NOTIFY_IP_CHANGE",  "false"),
                    "smtp_host": os.environ.get("SMTP_HOST", ""),
                    "smtp_port": os.environ.get("SMTP_PORT", "587"),
                    "smtp_user": os.environ.get("SMTP_USER", ""),
                    "smtp_password": os.environ.get("SMTP_PASSWORD", ""),
                    "smtp_from": os.environ.get("SMTP_FROM", ""),
                    "smtp_to": os.environ.get("SMTP_TO", ""),
                    "smtp_tls": os.environ.get("SMTP_TLS", "true"),
                }
                for key, value in defaults.items():
                    conn.execute(text(
                        "INSERT INTO settings (key, value) VALUES (:k, :v) "
                        "ON CONFLICT (key) DO NOTHING"
                    ), {"k": key, "v": value})
                # Env-var overrides: when explicitly set, always win over DB value
                env_overrides = {
                    k: v for k, v in {
                        "notify_new_device": os.environ.get("NOTIFY_NEW_DEVICE"),
                        "notify_ip_change":  os.environ.get("NOTIFY_IP_CHANGE"),
                    }.items() if v is not None
                }
                for key, value in env_overrides.items():
                    conn.execute(text(
                        "UPDATE settings SET value = :v WHERE key = :k"
                    ), {"k": key, "v": value.lower()})
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
