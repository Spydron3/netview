from datetime import datetime
from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Room(Base):
    __tablename__ = "rooms"
    id   = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False, unique=True)


class Device(Base):
    __tablename__ = "devices"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ip_address = Column(String(45), unique=True, nullable=True, index=True)
    name = Column(String(255), nullable=True)
    mac_address = Column(String(17), nullable=True)
    hostname = Column(String(255), nullable=True)
    vendor = Column(String(255), nullable=True)
    os_info = Column(String(255), nullable=True)
    is_online = Column(Boolean, default=True, nullable=False)
    open_ports = Column(JSON, default=list, nullable=False)
    response_time = Column(Float, nullable=True)
    first_seen = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_seen = Column(DateTime, default=datetime.utcnow, nullable=False)
    scan_count = Column(Integer, default=1, nullable=False)
    is_switch   = Column(Boolean, default=False, nullable=False, server_default="false")
    is_virtual  = Column(Boolean, default=False, nullable=False, server_default="false")
    parent_id   = Column(Integer, ForeignKey("devices.id", ondelete="SET NULL"), nullable=True)
    is_wireless = Column(Boolean, default=False, nullable=False, server_default="false")
    room_id     = Column(Integer, ForeignKey("rooms.id", ondelete="SET NULL"), nullable=True)


class Setting(Base):
    __tablename__ = "settings"

    key = Column(String(50), primary_key=True)
    value = Column(String(255), nullable=False)


class SwitchPort(Base):
    __tablename__ = "switch_ports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    switch_id = Column(Integer, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    port_number = Column(Integer, nullable=False)
    label = Column(String(100), nullable=True)
    port_type = Column(String(10), nullable=False, default="RJ45")  # RJ45 | SFP+
    speed = Column(String(10), nullable=False, default="1G")


class DevicePort(Base):
    """A named interface on a device (e.g. eth0, NIC 1). Used as one end of a PortLink."""
    __tablename__ = "device_ports"

    id        = Column(Integer, primary_key=True, autoincrement=True)
    device_id = Column(Integer, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    label     = Column(String(50), nullable=False)


class PortLink(Base):
    """One row per cable. port_a_id is always a switch port.
    - device connection:   dev_port_id set (references DevicePort), port_b_id NULL
    - switch-to-switch:    port_b_id set (another switch port),     dev_port_id NULL
    All three port columns are UNIQUE so each port appears in at most one link.
    """
    __tablename__ = "port_links"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    port_a_id     = Column(Integer, ForeignKey("switch_ports.id",  ondelete="CASCADE"), nullable=True,  unique=True)
    dev_port_id   = Column(Integer, ForeignKey("device_ports.id",  ondelete="CASCADE"), nullable=True,  unique=True)
    port_b_id     = Column(Integer, ForeignKey("switch_ports.id",  ondelete="CASCADE"), nullable=True,  unique=True)
    dev_port_id_b = Column(Integer, ForeignKey("device_ports.id",  ondelete="CASCADE"), nullable=True,  unique=True)


class TopologyPosition(Base):
    __tablename__ = "topology_positions"

    node_id = Column(String(50), primary_key=True)
    x       = Column(Float, nullable=False)
    y       = Column(Float, nullable=False)


class ScanRun(Base):
    __tablename__ = "scan_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    finished_at = Column(DateTime, nullable=True)
    status = Column(String(20), default="running", nullable=False)
    network_range = Column(String(50), nullable=True)
    devices_found = Column(Integer, default=0, nullable=False)
    devices_online = Column(Integer, default=0, nullable=False)
    duration_seconds = Column(Float, nullable=True)
    error_message = Column(String(500), nullable=True)
