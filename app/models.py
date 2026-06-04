from datetime import datetime
from sqlalchemy import Boolean, Column, DateTime, Float, Integer, JSON, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Device(Base):
    __tablename__ = "devices"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ip_address = Column(String(45), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=True)       # user-assigned label
    mac_address = Column(String(17), nullable=True)
    hostname = Column(String(255), nullable=True)
    vendor = Column(String(255), nullable=True)
    os_info = Column(String(255), nullable=True)
    is_online = Column(Boolean, default=True, nullable=False)
    open_ports = Column(JSON, default=list, nullable=False)
    response_time = Column(Float, nullable=True)  # ms
    first_seen = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_seen = Column(DateTime, default=datetime.utcnow, nullable=False)
    scan_count = Column(Integer, default=1, nullable=False)


class Setting(Base):
    __tablename__ = "settings"

    key = Column(String(50), primary_key=True)
    value = Column(String(255), nullable=False)


class ScanRun(Base):
    __tablename__ = "scan_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    finished_at = Column(DateTime, nullable=True)
    status = Column(String(20), default="running", nullable=False)  # running | completed | failed
    network_range = Column(String(50), nullable=True)
    devices_found = Column(Integer, default=0, nullable=False)
    devices_online = Column(Integer, default=0, nullable=False)
    duration_seconds = Column(Float, nullable=True)
    error_message = Column(String(500), nullable=True)
