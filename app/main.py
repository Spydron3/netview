import logging
import os
import smtplib
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from email.message import EmailMessage

import sqlalchemy as sa
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from database import get_db, get_setting, init_db, set_setting
from sqlalchemy.orm import aliased as _aliased
from models import Device, DevicePort, PortLink, Room, ScanRun, Setting, SwitchPort, TopologyPosition
from scanner import get_network_range, scan_network

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

_scan_lock  = threading.Lock()
_scan_state: dict = {"running": False, "started_at": None}
_scheduler  = BackgroundScheduler(daemon=True)

_VALID_PORT_TYPES = {"RJ45", "SFP+"}
_VALID_SPEEDS     = {"10M", "100M", "1G", "2.5G", "10G", "25G", "40G", "100G"}


def _norm_mac(mac: str | None) -> str | None:
    if not mac:
        return None
    digits = mac.strip().lower().replace(":", "").replace("-", "").replace(".", "")
    if len(digits) != 12 or not all(c in "0123456789abcdef" for c in digits):
        raise ValueError(f"Invalid MAC address: {mac!r}")
    return ":".join(digits[i:i+2] for i in range(0, 12, 2))


def _send_new_device_email(new_devices: list[dict]) -> None:
    host     = get_setting("smtp_host", "")
    port     = int(get_setting("smtp_port", "587") or 587)
    user     = get_setting("smtp_user", "")
    password = get_setting("smtp_password", "")
    from_    = get_setting("smtp_from", "") or user or "netviewmyhome@localhost"
    to_      = get_setting("smtp_to", "")
    tls      = get_setting("smtp_tls", "true").lower() == "true"

    if not host or not to_:
        raise ValueError("smtp_host and smtp_to must be configured")

    n = len(new_devices)
    subject = f"NetViewMyHome: {n} new device{'s' if n != 1 else ''} discovered"
    lines = [f"{n} new device{'s' if n != 1 else ''} discovered on your network:\n"]
    for d in new_devices:
        lines.append(f"  IP:       {d.get('ip_address', '—')}")
        if d.get("hostname"):
            lines.append(f"  Hostname: {d['hostname']}")
        if d.get("mac_address"):
            lines.append(f"  MAC:      {d['mac_address']}")
        if d.get("vendor"):
            lines.append(f"  Vendor:   {d['vendor']}")
        lines.append("")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"]    = from_
    msg["To"]      = to_
    msg.set_content("\n".join(lines))

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=10) as smtp:
            if user and password:
                smtp.login(user, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=10) as smtp:
            if tls:
                smtp.starttls()
            if user and password:
                smtp.login(user, password)
            smtp.send_message(msg)
    logger.info("New device notification sent to %s (%d device(s))", to_, n)


def _run_scan() -> None:
    if not _scan_lock.acquire(blocking=False):
        logger.info("Scan already running — skipping")
        return

    _scan_state["running"] = True
    _scan_state["started_at"] = datetime.utcnow()
    scan_id: int | None = None

    try:
        with get_db() as db:
            run = ScanRun(started_at=_scan_state["started_at"], status="running")
            db.add(run)
            db.flush()
            scan_id = run.id

        nr = get_setting("network_range") or None
        ps = get_setting("port_scan_enabled", "true").lower() == "true"
        devices, network_range = scan_network(network_range=nr, port_scan=ps)
        now = datetime.utcnow()

        new_devices: list[dict] = []

        with get_db() as db:
            db.execute(
                sa.update(Device).values(is_online=False)
                .where(sa.or_(Device.is_switch == False, Device.ip_address.isnot(None)))  # noqa: E712
            )

            for d in devices:
                existing = db.execute(
                    sa.select(Device).where(Device.ip_address == d["ip_address"])
                ).scalar_one_or_none()

                if existing:
                    existing.is_online = True
                    existing.last_seen = now
                    existing.scan_count += 1
                    existing.open_ports = d["open_ports"]
                    if d["mac_address"]:
                        existing.mac_address = d["mac_address"]
                    if d["hostname"]:
                        existing.hostname = d["hostname"]
                    if d["vendor"]:
                        existing.vendor = d["vendor"]
                else:
                    db.add(
                        Device(
                            ip_address=d["ip_address"],
                            mac_address=d["mac_address"],
                            hostname=d["hostname"],
                            vendor=d["vendor"],
                            os_info=d["os_info"],
                            is_online=True,
                            open_ports=d["open_ports"],
                            first_seen=now,
                            last_seen=now,
                            scan_count=1,
                        )
                    )
                    new_devices.append(d)

            online = len([d for d in devices if d["is_online"]])
            finished = datetime.utcnow()
            db.execute(
                sa.update(ScanRun)
                .where(ScanRun.id == scan_id)
                .values(
                    finished_at=finished,
                    status="completed",
                    network_range=network_range,
                    devices_found=len(devices),
                    devices_online=online,
                    duration_seconds=(finished - _scan_state["started_at"]).total_seconds(),
                )
            )

        if new_devices and get_setting("notify_new_device", "false").lower() == "true":
            threading.Thread(
                target=lambda: _send_new_device_email(new_devices),
                daemon=True, name="email-notify",
            ).start()

    except Exception as exc:
        logger.exception("Scan failed")
        if scan_id:
            try:
                with get_db() as db:
                    db.execute(
                        sa.update(ScanRun)
                        .where(ScanRun.id == scan_id)
                        .values(
                            finished_at=datetime.utcnow(),
                            status="failed",
                            error_message=str(exc)[:500],
                        )
                    )
            except Exception:
                pass
    finally:
        _scan_state["running"] = False
        _scan_lock.release()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()

    interval = int(get_setting("scan_interval", os.environ.get("SCAN_INTERVAL", "300")))
    _scheduler.add_job(_run_scan, "interval", seconds=interval, id="network_scan")
    _scheduler.start()

    threading.Thread(target=_run_scan, daemon=True, name="initial-scan").start()

    yield

    _scheduler.shutdown(wait=False)


app = FastAPI(title="NetViewMyHome", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse("static/index.html")


# ── stats ─────────────────────────────────────────────────────────────────────

@app.get("/api/stats")
def api_stats():
    with get_db() as db:
        total = db.execute(sa.select(sa.func.count(Device.id))).scalar_one()
        online = db.execute(
            sa.select(sa.func.count(Device.id)).where(
                sa.or_(
                    Device.is_online.is_(True),
                    sa.and_(Device.is_switch == True, Device.ip_address.is_(None))  # noqa: E712
                )
            )
        ).scalar_one()
        last_scan = db.execute(
            sa.select(ScanRun)
            .where(ScanRun.status == "completed")
            .order_by(ScanRun.finished_at.desc())
            .limit(1)
        ).scalar_one_or_none()

        return {
            "total_devices": total,
            "online_devices": online,
            "offline_devices": total - online,
            "scan_running": _scan_state["running"],
            "scan_started_at": _scan_state["started_at"],
            "network_range": get_network_range(),
            "last_scan": _scan_to_dict(last_scan),
        }


# ── devices ───────────────────────────────────────────────────────────────────

def _device_ports(db, device_id: int) -> list:
    """Returns list of (SwitchPort, Device[switch], DevicePort) for the given device."""
    SwDev = _aliased(Device)
    return db.execute(
        sa.select(SwitchPort, SwDev, DevicePort)
        .join(PortLink, PortLink.port_a_id == SwitchPort.id)
        .join(DevicePort, DevicePort.id == PortLink.dev_port_id)
        .join(SwDev, SwDev.id == SwitchPort.switch_id)
        .where(DevicePort.device_id == device_id)
        .order_by(SwDev.ip_address, SwitchPort.port_number)
    ).all()


@app.get("/api/devices")
def api_devices():
    with get_db() as db:
        from sqlalchemy.dialects.postgresql import INET
        devices = db.execute(
            sa.select(Device)
            .order_by(Device.is_online.desc(), sa.nullslast(sa.cast(Device.ip_address, INET)))
        ).scalars().all()
        # fetch all connections in one query and group by device
        SwDev = _aliased(Device)
        conn_rows = db.execute(
            sa.select(SwitchPort, SwDev, DevicePort)
            .join(PortLink, PortLink.port_a_id == SwitchPort.id)
            .join(DevicePort, DevicePort.id == PortLink.dev_port_id)
            .join(SwDev, SwDev.id == SwitchPort.switch_id)
        ).all()
        conns: dict[int, list] = {}
        for sp, sw, dp in conn_rows:
            conns.setdefault(dp.device_id, []).append((sp, sw, dp))
        rooms = _rooms_dict(db)
        return [_device_to_dict(dev, conns.get(dev.id, []), rooms) for dev in devices]


@app.get("/api/devices/{device_id}")
def api_device(device_id: int):
    with get_db() as db:
        dev = db.get(Device, device_id)
        if not dev:
            raise HTTPException(status_code=404, detail="Device not found")
        return _device_to_dict(dev, _device_ports(db, device_id), _rooms_dict(db))


class DeviceUpdate(BaseModel):
    name: str | None = None
    is_switch: bool | None = None
    is_virtual: bool | None = None
    parent_id: int | None = None
    is_wireless: bool | None = None
    room_id: int | None = None


@app.patch("/api/devices/{device_id}")
def api_update_device(device_id: int, body: DeviceUpdate):
    with get_db() as db:
        d = db.get(Device, device_id)
        if not d:
            raise HTTPException(status_code=404, detail="Device not found")
        if "name" in body.model_fields_set:
            d.name = body.name.strip() if body.name and body.name.strip() else None
        if "is_switch" in body.model_fields_set:
            d.is_switch = bool(body.is_switch)
        if "is_virtual" in body.model_fields_set:
            d.is_virtual = bool(body.is_virtual)
            if not d.is_virtual:
                d.parent_id = None
        if "is_wireless" in body.model_fields_set:
            d.is_wireless = bool(body.is_wireless)
        if "parent_id" in body.model_fields_set:
            if body.parent_id is not None:
                parent = db.get(Device, body.parent_id)
                if not parent:
                    raise HTTPException(status_code=404, detail="Parent device not found")
                if parent.is_virtual:
                    raise HTTPException(status_code=422, detail="Parent cannot itself be virtual")
                if body.parent_id == device_id:
                    raise HTTPException(status_code=422, detail="Device cannot be its own parent")
            d.parent_id = body.parent_id
        if "room_id" in body.model_fields_set:
            d.room_id = body.room_id
        db.flush()
        rooms = _rooms_dict(db)
        return _device_to_dict(d, _device_ports(db, device_id), rooms)


# ── device ports (interfaces) ─────────────────────────────────────────────────

class DevicePortCreate(BaseModel):
    label: str


@app.get("/api/devices/{device_id}/device-ports")
def api_list_device_ports(device_id: int):
    with get_db() as db:
        if not db.get(Device, device_id):
            raise HTTPException(status_code=404, detail="Device not found")
        ports = db.execute(
            sa.select(DevicePort).where(DevicePort.device_id == device_id)
            .order_by(DevicePort.label)
        ).scalars().all()
        return [{"id": p.id, "label": p.label} for p in ports]


@app.post("/api/devices/{device_id}/device-ports", status_code=201)
def api_create_device_port(device_id: int, body: DevicePortCreate):
    with get_db() as db:
        if not db.get(Device, device_id):
            raise HTTPException(status_code=404, detail="Device not found")
        lbl = body.label.strip()
        if not lbl:
            raise HTTPException(status_code=422, detail="Label is required")
        dp = DevicePort(device_id=device_id, label=lbl)
        db.add(dp)
        db.flush()
        return {"id": dp.id, "label": dp.label}


@app.delete("/api/devices/{device_id}/device-ports/{dp_id}", status_code=204)
def api_delete_device_port(device_id: int, dp_id: int):
    with get_db() as db:
        dp = db.get(DevicePort, dp_id)
        if not dp or dp.device_id != device_id:
            raise HTTPException(status_code=404, detail="Device port not found")
        db.delete(dp)


class DeviceConnectionCreate(BaseModel):
    dev_port_id:   int
    switch_port_id: int


@app.put("/api/devices/{device_id}/port")
def api_connect_device_port(device_id: int, body: DeviceConnectionCreate):
    with get_db() as db:
        if not db.get(Device, device_id):
            raise HTTPException(status_code=404, detail="Device not found")
        dp = db.get(DevicePort, body.dev_port_id)
        if not dp or dp.device_id != device_id:
            raise HTTPException(status_code=404, detail="Device port not found")
        sp = db.get(SwitchPort, body.switch_port_id)
        if not sp:
            raise HTTPException(status_code=404, detail="Switch port not found")
        # each port appears in at most one link
        if db.execute(sa.select(PortLink).where(PortLink.port_a_id == body.switch_port_id)).scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Switch port already in use")
        if db.execute(sa.select(PortLink).where(PortLink.dev_port_id == body.dev_port_id)).scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Device interface already connected")
        db.add(PortLink(port_a_id=body.switch_port_id, dev_port_id=body.dev_port_id))
        db.flush()
        return _device_to_dict(db.get(Device, device_id), _device_ports(db, device_id), _rooms_dict(db))


@app.delete("/api/devices/{device_id}/ports/{port_id}", status_code=204)
def api_disconnect_device_port(device_id: int, port_id: int):
    """Disconnect a switch port from this device (port_id = switch_port_id)."""
    with get_db() as db:
        lnk = db.execute(
            sa.select(PortLink)
            .join(DevicePort, DevicePort.id == PortLink.dev_port_id)
            .where(
                PortLink.port_a_id == port_id,
                DevicePort.device_id == device_id,
            )
        ).scalar_one_or_none()
        if not lnk:
            raise HTTPException(status_code=404, detail="Connection not found")
        db.delete(lnk)


# ── rooms ─────────────────────────────────────────────────────────────────────

@app.get("/api/rooms")
def api_get_rooms():
    with get_db() as db:
        rooms = db.execute(sa.select(Room).order_by(Room.name)).scalars().all()
        return [{"id": r.id, "name": r.name} for r in rooms]


class RoomCreate(BaseModel):
    name: str


@app.post("/api/rooms", status_code=201)
def api_create_room(body: RoomCreate):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Name required")
    with get_db() as db:
        existing = db.execute(
            sa.select(Room).where(Room.name == name)
        ).scalar_one_or_none()
        if existing:
            return {"id": existing.id, "name": existing.name}
        r = Room(name=name)
        db.add(r)
        db.flush()
        return {"id": r.id, "name": r.name}


@app.delete("/api/rooms/{room_id}", status_code=204)
def api_delete_room(room_id: int):
    with get_db() as db:
        r = db.get(Room, room_id)
        if r:
            db.delete(r)


# ── unified port links ────────────────────────────────────────────────────────

class PortLinkCreate(BaseModel):
    device_a_id:     int
    switch_port_a_id: int | None = None
    device_b_id:     int
    switch_port_b_id: int | None = None


def _port_already_linked(db, sp_id: int) -> bool:
    return db.execute(
        sa.select(PortLink).where(
            sa.or_(PortLink.port_a_id == sp_id, PortLink.port_b_id == sp_id)
        )
    ).scalar_one_or_none() is not None


def _dev_port_already_linked(db, dp_id: int) -> bool:
    return db.execute(
        sa.select(PortLink).where(
            sa.or_(PortLink.dev_port_id == dp_id, PortLink.dev_port_id_b == dp_id)
        )
    ).scalar_one_or_none() is not None


@app.get("/api/devices/{device_id}/connections")
def api_device_connections(device_id: int):
    """All port connections for a device (all link types)."""
    with get_db() as db:
        if not db.get(Device, device_id):
            raise HTTPException(status_code=404, detail="Device not found")
        result = []

        # switch port → this device's DevicePort
        for lnk, sp, dp in db.execute(
            sa.select(PortLink, SwitchPort, DevicePort)
            .join(DevicePort, DevicePort.id == PortLink.dev_port_id)
            .join(SwitchPort, SwitchPort.id == PortLink.port_a_id)
            .where(DevicePort.device_id == device_id)
        ).all():
            other_sw = db.get(Device, sp.switch_id)
            result.append({
                "link_id": lnk.id,
                "other_device_id": sp.switch_id,
                "other_device_label": _sw_label(other_sw) if other_sw else "?",
                "other_device_is_switch": other_sw.is_switch if other_sw else False,
                "port_label": sp.label or f"Port {sp.port_number}",
                "port_type": sp.port_type,
                "speed": sp.speed,
            })

        # dev-dev links where this device is side A
        for lnk, dp_a in db.execute(
            sa.select(PortLink, DevicePort)
            .join(DevicePort, DevicePort.id == PortLink.dev_port_id)
            .where(
                DevicePort.device_id == device_id,
                PortLink.dev_port_id_b.isnot(None),
                PortLink.port_a_id.is_(None),
            )
        ).all():
            dp_b = db.get(DevicePort, lnk.dev_port_id_b)
            other = db.get(Device, dp_b.device_id) if dp_b else None
            result.append({
                "link_id": lnk.id,
                "other_device_id": other.id if other else None,
                "other_device_label": _dev_label(other) if other else "?",
                "other_device_is_switch": other.is_switch if other else False,
                "port_label": None, "port_type": None, "speed": None,
            })

        # dev-dev links where this device is side B
        for lnk, dp_b in db.execute(
            sa.select(PortLink, DevicePort)
            .join(DevicePort, DevicePort.id == PortLink.dev_port_id_b)
            .where(
                DevicePort.device_id == device_id,
                PortLink.port_a_id.is_(None),
            )
        ).all():
            dp_a = db.get(DevicePort, lnk.dev_port_id)
            other = db.get(Device, dp_a.device_id) if dp_a else None
            result.append({
                "link_id": lnk.id,
                "other_device_id": other.id if other else None,
                "other_device_label": _dev_label(other) if other else "?",
                "other_device_is_switch": other.is_switch if other else False,
                "port_label": None, "port_type": None, "speed": None,
            })

        return result


@app.post("/api/port-link", status_code=201)
def api_create_port_link(body: PortLinkCreate):
    with get_db() as db:
        dev_a = db.get(Device, body.device_a_id)
        dev_b = db.get(Device, body.device_b_id)
        if not dev_a: raise HTTPException(404, "Device A not found")
        if not dev_b: raise HTTPException(404, "Device B not found")
        if dev_a.id == dev_b.id: raise HTTPException(422, "Cannot link a device to itself")

        sp_a = sp_b = dp_a = dp_b = None

        if body.switch_port_a_id is not None:
            sp_a = db.get(SwitchPort, body.switch_port_a_id)
            if not sp_a or sp_a.switch_id != dev_a.id:
                raise HTTPException(404, "Port A not found on device A")
            if _port_already_linked(db, sp_a.id):
                raise HTTPException(409, "Port A is already connected")
        elif not dev_a.is_switch:
            dp_a = db.execute(
                sa.select(DevicePort).where(DevicePort.device_id == dev_a.id)
            ).scalars().first()
            if not dp_a:
                dp_a = DevicePort(device_id=dev_a.id, label="Port")
                db.add(dp_a)
                db.flush()
            if _dev_port_already_linked(db, dp_a.id):
                raise HTTPException(409, "Device A already has a connection")
        else:
            raise HTTPException(422, "switch_port_a_id required for switch device A")

        if body.switch_port_b_id is not None:
            sp_b = db.get(SwitchPort, body.switch_port_b_id)
            if not sp_b or sp_b.switch_id != dev_b.id:
                raise HTTPException(404, "Port B not found on device B")
            if _port_already_linked(db, sp_b.id):
                raise HTTPException(409, "Port B is already connected")
        elif not dev_b.is_switch:
            dp_b = db.execute(
                sa.select(DevicePort).where(DevicePort.device_id == dev_b.id)
            ).scalars().first()
            if not dp_b:
                dp_b = DevicePort(device_id=dev_b.id, label="Port")
                db.add(dp_b)
                db.flush()
            if _dev_port_already_linked(db, dp_b.id):
                raise HTTPException(409, "Device B already has a connection")
        else:
            raise HTTPException(422, "switch_port_b_id required for switch device B")

        if sp_a and sp_b:
            lnk = PortLink(port_a_id=sp_a.id, port_b_id=sp_b.id)
        elif sp_a and dp_b:
            lnk = PortLink(port_a_id=sp_a.id, dev_port_id=dp_b.id)
        elif dp_a and sp_b:
            lnk = PortLink(port_a_id=sp_b.id, dev_port_id=dp_a.id)
        elif dp_a and dp_b:
            lnk = PortLink(dev_port_id=dp_a.id, dev_port_id_b=dp_b.id)
        else:
            raise HTTPException(422, "Invalid port combination")

        db.add(lnk)
        db.flush()
        return {"id": lnk.id}


@app.delete("/api/port-link/{link_id}", status_code=204)
def api_delete_port_link(link_id: int):
    with get_db() as db:
        lnk = db.get(PortLink, link_id)
        if not lnk:
            raise HTTPException(status_code=404, detail="Link not found")
        db.delete(lnk)


# ── settings ──────────────────────────────────────────────────────────────────

class SettingsUpdate(BaseModel):
    scan_interval: int | None = None
    port_scan_enabled: bool | None = None
    network_range: str | None = None
    notify_new_device: bool | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_to: str | None = None
    smtp_tls: bool | None = None


@app.get("/api/settings")
def api_get_settings():
    with get_db() as db:
        rows = db.execute(sa.select(Setting)).scalars().all()
        return {r.key: r.value for r in rows}


@app.put("/api/settings")
def api_put_settings(body: SettingsUpdate):
    if body.scan_interval is not None:
        if body.scan_interval < 30:
            raise HTTPException(status_code=422, detail="scan_interval must be >= 30 seconds")
        set_setting("scan_interval", str(body.scan_interval))
        _scheduler.reschedule_job("network_scan", trigger="interval", seconds=body.scan_interval)

    if body.port_scan_enabled is not None:
        set_setting("port_scan_enabled", "true" if body.port_scan_enabled else "false")

    if body.network_range is not None:
        set_setting("network_range", body.network_range.strip())

    if body.notify_new_device is not None:
        set_setting("notify_new_device", "true" if body.notify_new_device else "false")

    str_fields = ("smtp_host", "smtp_user", "smtp_password", "smtp_from", "smtp_to")
    for field in str_fields:
        val = getattr(body, field)
        if val is not None:
            set_setting(field, val.strip())

    if body.smtp_port is not None:
        set_setting("smtp_port", str(body.smtp_port))

    if body.smtp_tls is not None:
        set_setting("smtp_tls", "true" if body.smtp_tls else "false")

    return {"status": "ok"}


@app.post("/api/settings/test-email")
def api_test_email():
    try:
        _send_new_device_email([{
            "ip_address": "192.168.1.1",
            "hostname": "test-device.local",
            "mac_address": "aa:bb:cc:dd:ee:ff",
            "vendor": "Test (NetViewMyHome configuration check)",
        }])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"status": "ok"}


# ── scan ──────────────────────────────────────────────────────────────────────

@app.post("/api/scan")
def api_scan(background_tasks: BackgroundTasks):
    if _scan_state["running"]:
        return {"status": "already_running", "started_at": _scan_state["started_at"]}
    background_tasks.add_task(_run_scan)
    return {"status": "started"}


@app.get("/api/scan/status")
def api_scan_status():
    return {
        "running": _scan_state["running"],
        "started_at": _scan_state["started_at"],
    }


@app.get("/api/scan/history")
def api_scan_history():
    with get_db() as db:
        rows = db.execute(
            sa.select(ScanRun).order_by(ScanRun.started_at.desc()).limit(50)
        ).scalars().all()
        return [_scan_to_dict(r) for r in rows]


# ── switches ──────────────────────────────────────────────────────────────────

class SwitchCreate(BaseModel):
    ip_address:  str | None = None
    mac_address: str | None = None
    name:        str | None = None


@app.get("/api/switches")
def api_list_switches():
    with get_db() as db:
        rows = db.execute(
            sa.select(Device).where(Device.is_switch == True)  # noqa: E712
            .order_by(Device.ip_address.nullslast(), Device.mac_address)
        ).scalars().all()
        dev_ids = [d.id for d in rows]
        counts: dict[int, int] = {}
        if dev_ids:
            for sw_id, cnt in db.execute(
                sa.select(SwitchPort.switch_id, sa.func.count(SwitchPort.id))
                .where(SwitchPort.switch_id.in_(dev_ids))
                .group_by(SwitchPort.switch_id)
            ).all():
                counts[sw_id] = cnt
        return [_switch_to_dict(d, counts.get(d.id, 0)) for d in rows]


@app.post("/api/switches", status_code=201)
def api_add_switch(body: SwitchCreate):
    ip   = body.ip_address.strip()  if body.ip_address  else None
    name = body.name.strip()        if body.name        else None
    try:
        mac = _norm_mac(body.mac_address) if body.mac_address else None
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if not ip and not mac and not name:
        raise HTTPException(status_code=422,
                            detail="At least one of ip_address, mac_address, or name is required")
    with get_db() as db:
        dev = None
        if ip:
            dev = db.execute(sa.select(Device).where(Device.ip_address == ip)).scalar_one_or_none()
        if not dev and mac:
            dev = db.execute(sa.select(Device).where(Device.mac_address == mac)).scalar_one_or_none()
        if dev:
            dev.is_switch = True
            if name and not dev.name:
                dev.name = name
        else:
            dev = Device(
                ip_address=ip, mac_address=mac, name=name,
                is_switch=True, is_online=False, open_ports=[],
                first_seen=datetime.utcnow(), last_seen=datetime.utcnow(), scan_count=0,
            )
            db.add(dev)
        db.flush()
        count = db.execute(
            sa.select(sa.func.count(SwitchPort.id)).where(SwitchPort.switch_id == dev.id)
        ).scalar_one()
        return _switch_to_dict(dev, count)


@app.delete("/api/switches/{switch_id}", status_code=204)
def api_delete_switch(switch_id: int):
    with get_db() as db:
        dev = db.get(Device, switch_id)
        if not dev or not dev.is_switch:
            raise HTTPException(status_code=404, detail="Switch not found")
        pos = db.get(TopologyPosition, f"sw_{switch_id}")
        if pos:
            db.delete(pos)
        if dev.ip_address is None and dev.scan_count == 0:
            db.delete(dev)  # purely manual entry — delete device too
        else:
            dev.is_switch = False
            db.execute(sa.delete(SwitchPort).where(SwitchPort.switch_id == switch_id))


@app.patch("/api/switches/{switch_id}")
def api_update_switch(switch_id: int, body: SwitchCreate):
    ip   = body.ip_address.strip()  if body.ip_address  else None
    name = body.name.strip()        if body.name        else None
    try:
        mac = _norm_mac(body.mac_address) if body.mac_address else None
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    with get_db() as db:
        dev = db.get(Device, switch_id)
        if not dev or not dev.is_switch:
            raise HTTPException(status_code=404, detail="Switch not found")
        dev.ip_address  = ip
        dev.mac_address = mac
        dev.name        = name
        db.flush()
        count = db.execute(
            sa.select(sa.func.count(SwitchPort.id)).where(SwitchPort.switch_id == switch_id)
        ).scalar_one()
        return _switch_to_dict(dev, count)


# ── switch ports ──────────────────────────────────────────────────────────────

class PortCreate(BaseModel):
    port_number: int
    port_type: str = "RJ45"
    speed: str = "1G"
    label: str | None = None


class PortUpdate(BaseModel):
    port_type: str | None = None
    speed: str | None = None
    label: str | None = None


@app.get("/api/switches/{switch_id}/ports")
def api_list_ports(switch_id: int):
    with get_db() as db:
        sw = db.get(Device, switch_id)
        if not sw or not sw.is_switch:
            raise HTTPException(status_code=404, detail="Switch not found")

        rows = db.execute(
            sa.select(SwitchPort, Device, PortLink)
            .outerjoin(PortLink, sa.and_(
                PortLink.port_a_id == SwitchPort.id, PortLink.dev_port_id.isnot(None)
            ))
            .outerjoin(DevicePort, DevicePort.id == PortLink.dev_port_id)
            .outerjoin(Device, Device.id == DevicePort.device_id)
            .where(SwitchPort.switch_id == switch_id)
            .order_by(SwitchPort.port_number)
        ).all()

        port_ids = [p.id for p, d, l in rows]

        # Bulk-fetch switch-to-switch links where any of these ports appear
        sw_links = db.execute(
            sa.select(PortLink).where(
                sa.and_(
                    sa.or_(PortLink.port_a_id.in_(port_ids), PortLink.port_b_id.in_(port_ids)),
                    PortLink.port_b_id.isnot(None),
                )
            )
        ).scalars().all()

        sw_link_by_port: dict[int, tuple] = {}
        other_port_ids: set[int] = set()
        for lnk in sw_links:
            if lnk.port_a_id in port_ids:
                sw_link_by_port[lnk.port_a_id] = (lnk, lnk.port_b_id)
                other_port_ids.add(lnk.port_b_id)
            if lnk.port_b_id in port_ids:
                sw_link_by_port[lnk.port_b_id] = (lnk, lnk.port_a_id)
                other_port_ids.add(lnk.port_a_id)

        other_ports: dict[int, SwitchPort] = {}
        if other_port_ids:
            for op in db.execute(
                sa.select(SwitchPort).where(SwitchPort.id.in_(other_port_ids))
            ).scalars().all():
                other_ports[op.id] = op

        other_switch_ids = {op.switch_id for op in other_ports.values()}
        other_switches: dict[int, Device] = {}
        if other_switch_ids:
            for osw in db.execute(
                sa.select(Device).where(Device.id.in_(other_switch_ids))
            ).scalars().all():
                other_switches[osw.id] = osw

        result = []
        for p, d, dev_lnk in rows:
            link_id = dev_lnk.id if dev_lnk else None
            conn_dev = d
            if not conn_dev and p.id in sw_link_by_port:
                lnk_sw, other_port_id = sw_link_by_port[p.id]
                link_id = lnk_sw.id
                op = other_ports.get(other_port_id)
                if op:
                    conn_dev = other_switches.get(op.switch_id)
            result.append(_port_to_dict(p, sw, conn_dev, link_id))
        return result


@app.post("/api/switches/{switch_id}/ports", status_code=201)
def api_add_port(switch_id: int, body: PortCreate):
    with get_db() as db:
        sw = db.get(Device, switch_id)
        if not sw or not sw.is_switch:
            raise HTTPException(status_code=404, detail="Switch not found")
        if body.port_type not in _VALID_PORT_TYPES:
            raise HTTPException(status_code=422, detail=f"port_type must be one of {sorted(_VALID_PORT_TYPES)}")
        if body.speed not in _VALID_SPEEDS:
            raise HTTPException(status_code=422, detail=f"speed must be one of {sorted(_VALID_SPEEDS)}")
        port = SwitchPort(
            switch_id=switch_id,
            port_number=body.port_number,
            port_type=body.port_type,
            speed=body.speed,
            label=body.label,
        )
        db.add(port)
        db.flush()
        return _port_to_dict(port, sw, None)


@app.patch("/api/switches/{switch_id}/ports/{port_id}")
def api_update_port(switch_id: int, port_id: int, body: PortUpdate):
    with get_db() as db:
        sw = db.get(Device, switch_id)
        if not sw or not sw.is_switch:
            raise HTTPException(status_code=404, detail="Switch not found")
        port = db.get(SwitchPort, port_id)
        if not port or port.switch_id != switch_id:
            raise HTTPException(status_code=404, detail="Port not found")

        fields = body.model_fields_set if hasattr(body, "model_fields_set") else body.__fields_set__

        if "port_type" in fields and body.port_type is not None:
            if body.port_type not in _VALID_PORT_TYPES:
                raise HTTPException(status_code=422, detail=f"port_type must be one of {sorted(_VALID_PORT_TYPES)}")
            port.port_type = body.port_type
        if "speed" in fields and body.speed is not None:
            if body.speed not in _VALID_SPEEDS:
                raise HTTPException(status_code=422, detail=f"speed must be one of {sorted(_VALID_SPEEDS)}")
            port.speed = body.speed
        if "label" in fields:
            port.label = body.label

        db.flush()
        lnk = db.execute(
            sa.select(PortLink).where(
                PortLink.port_a_id == port_id, PortLink.dev_port_id.isnot(None)
            )
        ).scalar_one_or_none()
        dev = None
        if lnk:
            dp = db.get(DevicePort, lnk.dev_port_id)
            dev = db.get(Device, dp.device_id) if dp else None
        return _port_to_dict(port, sw, dev)


@app.delete("/api/switches/{switch_id}/ports/{port_id}", status_code=204)
def api_delete_port(switch_id: int, port_id: int):
    with get_db() as db:
        port = db.get(SwitchPort, port_id)
        if not port or port.switch_id != switch_id:
            raise HTTPException(status_code=404, detail="Port not found")
        db.delete(port)


# ── all ports (for dropdowns) ─────────────────────────────────────────────────

@app.get("/api/ports")
def api_all_ports():
    with get_db() as db:
        SwDev = _aliased(Device)
        rows = db.execute(
            sa.select(SwitchPort, SwDev, Device)
            .join(SwDev, SwDev.id == SwitchPort.switch_id)
            .outerjoin(PortLink, sa.and_(
                PortLink.port_a_id == SwitchPort.id, PortLink.dev_port_id.isnot(None)
            ))
            .outerjoin(DevicePort, DevicePort.id == PortLink.dev_port_id)
            .outerjoin(Device, Device.id == DevicePort.device_id)
            .order_by(SwDev.ip_address, SwitchPort.port_number)
        ).all()
        return [_port_to_dict(p, s, d) for p, s, d in rows]


# ── topology (manual) ─────────────────────────────────────────────────────────

@app.get("/api/topology")
def api_topology():
    with get_db() as db:
        switch_devs = db.execute(
            sa.select(Device).where(Device.is_switch == True)  # noqa: E712
        ).scalars().all()

        ports_with_devices = db.execute(
            sa.select(SwitchPort, Device)
            .join(PortLink, sa.and_(
                PortLink.port_a_id == SwitchPort.id,
                PortLink.dev_port_id.isnot(None),
            ))
            .join(DevicePort, DevicePort.id == PortLink.dev_port_id)
            .join(Device, Device.id == DevicePort.device_id)
        ).all()

        sw_links = db.execute(
            sa.select(PortLink).where(PortLink.port_b_id.isnot(None))
        ).scalars().all()

        link_port_ids = {lnk.port_a_id for lnk in sw_links} | {lnk.port_b_id for lnk in sw_links}
        ports_by_id: dict[int, SwitchPort] = {}
        if link_port_ids:
            for p in db.execute(
                sa.select(SwitchPort).where(SwitchPort.id.in_(link_port_ids))
            ).scalars().all():
                ports_by_id[p.id] = p

        rooms = _rooms_dict(db)
        sw_device_ids = {sw.id for sw in switch_devs}
        nodes: list[dict] = []
        edges: list[dict] = []
        seen: set[str] = set()

        for sw in switch_devs:
            nid = f"sw_{sw.id}"
            nodes.append({
                "id": nid, "type": "switch",
                "label": _sw_label(sw),
                "ip": sw.ip_address, "mac": sw.mac_address,
                "hostname": sw.hostname, "vendor": sw.vendor,
                "name": sw.name, "room": rooms.get(sw.room_id),
                "is_online": True if not sw.ip_address else sw.is_online,
            })
            seen.add(nid)

        # Buffer switch→switch port edges so we can deduplicate them before
        # adding switch_link edges (which take precedence for the same pair).
        sw_port_edges: dict[frozenset, dict] = {}

        for port, dev in ports_with_devices:
            if dev.is_virtual:
                continue
            src = f"sw_{port.switch_id}"
            if src not in seen:
                continue

            if dev.id in sw_device_ids:
                tgt = f"sw_{dev.id}"
                if src == tgt:
                    continue
                key = frozenset([src, tgt])
                lbl = port.label or f"Port {port.port_number}"
                if key not in sw_port_edges:
                    sw_port_edges[key] = {
                        "source": src, "target": tgt,
                        "port_a": lbl, "port_a_type": port.port_type, "speed_a": port.speed,
                        "type": "switch_link",
                    }
                else:
                    e = sw_port_edges[key]
                    e["port_b"] = lbl
                    e["port_b_type"] = port.port_type
                    e["speed_b"] = port.speed
            else:
                tgt = f"dev_{dev.id}"
                if tgt not in seen:
                    nodes.append({
                        "id": tgt, "type": "device",
                        "label": _dev_label(dev),
                        "ip": dev.ip_address, "mac": dev.mac_address,
                        "hostname": dev.hostname, "vendor": dev.vendor,
                        "name": dev.name, "is_online": True if (dev.is_switch and not dev.ip_address) else dev.is_online,
                        "is_wireless": dev.is_wireless,
                        "room": rooms.get(dev.room_id),
                    })
                    seen.add(tgt)
                edges.append({
                    "source": src, "target": tgt,
                    "port": port.label or f"Port {port.port_number}",
                    "port_type": port.port_type,
                    "speed": port.speed,
                    "type": "port",
                })

        sw_link_pairs: set[frozenset] = set()
        for lnk in sw_links:
            pa_l = ports_by_id.get(lnk.port_a_id)
            pb_l = ports_by_id.get(lnk.port_b_id)
            if not pa_l or not pb_l:
                continue
            src = f"sw_{pa_l.switch_id}"
            tgt = f"sw_{pb_l.switch_id}"
            if src not in seen or tgt not in seen:
                continue
            sw_link_pairs.add(frozenset([src, tgt]))
            pa = ports_by_id.get(lnk.port_a_id)
            pb = ports_by_id.get(lnk.port_b_id)
            edges.append({
                "source": src, "target": tgt,
                "port_a": pa.label or f"Port {pa.port_number}" if pa else "?",
                "port_b": pb.label or f"Port {pb.port_number}" if pb else "?",
                "port_a_type": pa.port_type if pa else "",
                "port_b_type": pb.port_type if pb else "",
                "speed_a": pa.speed if pa else "",
                "speed_b": pb.speed if pb else "",
                "type": "switch_link",
            })

        for key, edge in sw_port_edges.items():
            if key not in sw_link_pairs:
                edges.append(edge)

        # Device-to-device direct links
        dev_links = db.execute(
            sa.select(PortLink, DevicePort)
            .join(DevicePort, DevicePort.id == PortLink.dev_port_id)
            .where(PortLink.port_a_id.is_(None), PortLink.dev_port_id_b.isnot(None))
        ).all()
        for lnk, dp_a in dev_links:
            dp_b = db.get(DevicePort, lnk.dev_port_id_b)
            if not dp_b:
                continue
            dev_a = db.get(Device, dp_a.device_id)
            dev_b = db.get(Device, dp_b.device_id)
            if not dev_a or not dev_b or dev_a.is_virtual or dev_b.is_virtual:
                continue
            src = f"dev_{dev_a.id}"
            tgt = f"dev_{dev_b.id}"
            for _d, _nid in [(dev_a, src), (dev_b, tgt)]:
                if _nid not in seen:
                    nodes.append({
                        "id": _nid, "type": "device",
                        "label": _dev_label(_d),
                        "ip": _d.ip_address, "mac": _d.mac_address,
                        "hostname": _d.hostname, "vendor": _d.vendor,
                        "name": _d.name, "is_online": True if (_d.is_switch and not _d.ip_address) else _d.is_online,
                        "is_wireless": _d.is_wireless,
                        "room": rooms.get(_d.room_id),
                    })
                    seen.add(_nid)
            edges.append({"source": src, "target": tgt, "type": "device_link"})

        # Always include wireless devices even without a switch port connection
        wireless_devs = db.execute(
            sa.select(Device).where(
                sa.and_(Device.is_wireless == True, Device.is_virtual == False)  # noqa: E712
            )
        ).scalars().all()
        for dev in wireless_devs:
            nid = f"dev_{dev.id}"
            if nid not in seen:
                nodes.append({
                    "id": nid, "type": "device",
                    "label": _dev_label(dev),
                    "ip": dev.ip_address, "mac": dev.mac_address,
                    "hostname": dev.hostname, "vendor": dev.vendor,
                    "name": dev.name, "is_online": True if (dev.is_switch and not dev.ip_address) else dev.is_online,
                    "is_wireless": True,
                    "room": rooms.get(dev.room_id),
                })
                seen.add(nid)

        # Attach virtual devices as children of their parent device node
        virtual_devs = db.execute(
            sa.select(Device).where(
                sa.and_(Device.is_virtual == True, Device.parent_id.isnot(None))  # noqa: E712
            )
        ).scalars().all()
        vchildren: dict[int, list] = {}
        for vd in virtual_devs:
            vchildren.setdefault(vd.parent_id, []).append({
                "id": vd.id,
                "label": _dev_label(vd),
                "ip": vd.ip_address,
                "is_online": vd.is_online,
            })
        for node in nodes:
            if node["type"] == "device":
                dev_id = int(node["id"][4:])  # strip "dev_"
                kids = vchildren.get(dev_id)
                if kids:
                    node["virtual_children"] = kids

        # Embed saved positions so the frontend can pin nodes immediately
        saved = {r.node_id: (r.x, r.y) for r in
                 db.execute(sa.select(TopologyPosition)).scalars().all()}
        for node in nodes:
            pos = saved.get(node["id"])
            if pos:
                node["x"], node["y"] = pos

    return {"nodes": nodes, "edges": edges}


# ── topology positions ────────────────────────────────────────────────────────

class TopoPositionUpdate(BaseModel):
    node_id: str
    x: float
    y: float


@app.post("/api/topology/positions", status_code=204)
def api_save_topo_position(body: TopoPositionUpdate):
    with get_db() as db:
        row = db.get(TopologyPosition, body.node_id)
        if row:
            row.x, row.y = body.x, body.y
        else:
            db.add(TopologyPosition(node_id=body.node_id, x=body.x, y=body.y))


@app.delete("/api/topology/positions/{node_id}", status_code=204)
def api_delete_topo_position(node_id: str):
    with get_db() as db:
        row = db.get(TopologyPosition, node_id)
        if row:
            db.delete(row)


# ── helpers ───────────────────────────────────────────────────────────────────

def _switch_to_dict(s: Device, port_count: int = 0) -> dict:
    return {
        "id": s.id,
        "ip_address": s.ip_address,
        "mac_address": s.mac_address,
        "name": s.name,
        "port_count": port_count,
    }


def _sw_label(sw: Device) -> str:
    return sw.name or sw.ip_address or sw.mac_address or f"Switch {sw.id}"


def _dev_label(d: Device) -> str:
    return d.name or d.hostname or d.ip_address or d.mac_address or f"Device {d.id}"


def _port_to_dict(p: SwitchPort, sw: Device, dev: Device | None, link_id: int | None = None) -> dict:
    return {
        "id": p.id,
        "switch_id": p.switch_id,
        "switch_name": _sw_label(sw),
        "switch_ip": sw.ip_address,
        "port_number": p.port_number,
        "label": p.label,
        "port_type": p.port_type,
        "speed": p.speed,
        "link_id": link_id,
        "device_id": dev.id if dev else None,
        "device_label": _dev_label(dev) if dev else None,
        "device_is_switch": dev.is_switch if dev else False,
    }


def _rooms_dict(db) -> dict[int, str]:
    return {r.id: r.name for r in db.execute(sa.select(Room)).scalars().all()}


def _device_to_dict(d: Device, ports: list | None = None, rooms: dict | None = None) -> dict:
    return {
        "id": d.id,
        "ip_address": d.ip_address,
        "name": d.name,
        "mac_address": d.mac_address,
        "hostname": d.hostname,
        "vendor": d.vendor,
        "os_info": d.os_info,
        "is_online": True if (d.is_switch and not d.ip_address) else d.is_online,
        "open_ports": d.open_ports or [],
        "response_time": d.response_time,
        "first_seen": d.first_seen.isoformat() if d.first_seen else None,
        "last_seen": d.last_seen.isoformat() if d.last_seen else None,
        "scan_count": d.scan_count,
        "is_switch": d.is_switch,
        "is_virtual": d.is_virtual,
        "parent_id": d.parent_id,
        "is_wireless": d.is_wireless,
        "room_id": d.room_id,
        "room": rooms.get(d.room_id) if (rooms is not None and d.room_id) else None,
        "switch_ports": [
            {
                "id": p.id,
                "dev_port_id": dp.id,
                "dev_port_label": dp.label,
                "switch_id": sw.id,
                "switch_name": _sw_label(sw),
                "switch_ip": sw.ip_address,
                "port_number": p.port_number,
                "label": p.label,
                "port_type": p.port_type,
                "speed": p.speed,
            }
            for p, sw, dp in (ports or [])
        ],
    }



def _scan_to_dict(s: ScanRun | None) -> dict | None:
    if s is None:
        return None
    return {
        "id": s.id,
        "started_at": s.started_at.isoformat() if s.started_at else None,
        "finished_at": s.finished_at.isoformat() if s.finished_at else None,
        "status": s.status,
        "network_range": s.network_range,
        "devices_found": s.devices_found,
        "devices_online": s.devices_online,
        "duration_seconds": s.duration_seconds,
        "error_message": s.error_message,
    }
