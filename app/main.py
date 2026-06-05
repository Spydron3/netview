import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime

import sqlalchemy as sa
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from database import get_db, get_setting, init_db, set_setting
from models import Device, PortConnection, ScanRun, Setting, Switch, SwitchLink, SwitchPort
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

        with get_db() as db:
            db.execute(sa.update(Device).values(is_online=False))

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


app = FastAPI(title="Netview", lifespan=lifespan)
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
            sa.select(sa.func.count(Device.id)).where(Device.is_online.is_(True))
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
    return db.execute(
        sa.select(SwitchPort, Switch)
        .join(PortConnection, PortConnection.switch_port_id == SwitchPort.id)
        .join(Switch, Switch.id == SwitchPort.switch_id)
        .where(PortConnection.device_id == device_id)
        .order_by(Switch.ip_address, SwitchPort.port_number)
    ).all()


@app.get("/api/devices")
def api_devices():
    with get_db() as db:
        from sqlalchemy.dialects.postgresql import INET
        rows = db.execute(
            sa.select(Device, SwitchPort, Switch)
            .outerjoin(PortConnection, PortConnection.device_id == Device.id)
            .outerjoin(SwitchPort, SwitchPort.id == PortConnection.switch_port_id)
            .outerjoin(Switch, Switch.id == SwitchPort.switch_id)
            .order_by(Device.is_online.desc(), sa.cast(Device.ip_address, INET))
        ).all()
        # aggregate: group port rows per device while preserving query order
        order: list[int] = []
        by_id: dict[int, tuple] = {}
        for dev, port, sw in rows:
            if dev.id not in by_id:
                order.append(dev.id)
                by_id[dev.id] = (dev, [])
            if port and sw:
                by_id[dev.id][1].append((port, sw))
        return [_device_to_dict(*by_id[did]) for did in order]


@app.get("/api/devices/{device_id}")
def api_device(device_id: int):
    with get_db() as db:
        dev = db.get(Device, device_id)
        if not dev:
            raise HTTPException(status_code=404, detail="Device not found")
        return _device_to_dict(dev, _device_ports(db, device_id))


class DeviceUpdate(BaseModel):
    name: str | None = None


@app.patch("/api/devices/{device_id}")
def api_update_device(device_id: int, body: DeviceUpdate):
    with get_db() as db:
        d = db.get(Device, device_id)
        if not d:
            raise HTTPException(status_code=404, detail="Device not found")
        d.name = body.name.strip() if body.name and body.name.strip() else None
        db.flush()
        return _device_to_dict(d, _device_ports(db, device_id))


class DevicePortAssign(BaseModel):
    switch_port_id: int


@app.put("/api/devices/{device_id}/port")
def api_add_device_port(device_id: int, body: DevicePortAssign):
    with get_db() as db:
        device = db.get(Device, device_id)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")
        port = db.get(SwitchPort, body.switch_port_id)
        if not port:
            raise HTTPException(status_code=404, detail="Port not found")
        existing = db.execute(
            sa.select(PortConnection).where(PortConnection.switch_port_id == body.switch_port_id)
        ).scalar_one_or_none()
        if existing:
            existing.device_id = device_id
        else:
            db.add(PortConnection(switch_port_id=body.switch_port_id, device_id=device_id))
        db.flush()
        return _device_to_dict(device, _device_ports(db, device_id))


@app.delete("/api/devices/{device_id}/ports/{port_id}", status_code=204)
def api_remove_device_port(device_id: int, port_id: int):
    with get_db() as db:
        conn = db.execute(
            sa.select(PortConnection).where(
                PortConnection.switch_port_id == port_id,
                PortConnection.device_id == device_id,
            )
        ).scalar_one_or_none()
        if not conn:
            raise HTTPException(status_code=404, detail="Port assignment not found")
        db.delete(conn)


# ── settings ──────────────────────────────────────────────────────────────────

class SettingsUpdate(BaseModel):
    scan_interval: int | None = None
    port_scan_enabled: bool | None = None
    network_range: str | None = None


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
            sa.select(Switch).order_by(
                Switch.ip_address.nullslast(), Switch.mac_address
            )
        ).scalars().all()
        switch_ids = [s.id for s in rows]
        counts = {}
        if switch_ids:
            for sw_id, cnt in db.execute(
                sa.select(SwitchPort.switch_id, sa.func.count(SwitchPort.id))
                .where(SwitchPort.switch_id.in_(switch_ids))
                .group_by(SwitchPort.switch_id)
            ).all():
                counts[sw_id] = cnt
        return [_switch_to_dict(s, counts.get(s.id, 0)) for s in rows]


@app.post("/api/switches", status_code=201)
def api_add_switch(body: SwitchCreate):
    ip  = body.ip_address.strip()  if body.ip_address  else None
    try:
        mac = _norm_mac(body.mac_address)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if not ip and not mac:
        raise HTTPException(status_code=422, detail="ip_address or mac_address is required")
    with get_db() as db:
        if ip and db.execute(sa.select(Switch).where(Switch.ip_address == ip)).scalar_one_or_none():
            raise HTTPException(status_code=409, detail="A switch with that IP already exists")
        if mac and db.execute(sa.select(Switch).where(Switch.mac_address == mac)).scalar_one_or_none():
            raise HTTPException(status_code=409, detail="A switch with that MAC already exists")
        sw = Switch(ip_address=ip, mac_address=mac, name=body.name or None)
        db.add(sw)
        db.flush()
        return _switch_to_dict(sw, 0)


@app.delete("/api/switches/{switch_id}", status_code=204)
def api_delete_switch(switch_id: int):
    with get_db() as db:
        sw = db.get(Switch, switch_id)
        if not sw:
            raise HTTPException(status_code=404, detail="Switch not found")
        db.delete(sw)


@app.patch("/api/switches/{switch_id}")
def api_update_switch(switch_id: int, body: SwitchCreate):
    ip  = body.ip_address.strip()  if body.ip_address  else None
    try:
        mac = _norm_mac(body.mac_address)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if not ip and not mac:
        raise HTTPException(status_code=422, detail="ip_address or mac_address is required")
    with get_db() as db:
        sw = db.get(Switch, switch_id)
        if not sw:
            raise HTTPException(status_code=404, detail="Switch not found")
        sw.ip_address  = ip
        sw.mac_address = mac
        sw.name        = body.name or None
        port_count = db.execute(
            sa.select(sa.func.count(SwitchPort.id)).where(SwitchPort.switch_id == switch_id)
        ).scalar_one()
        return _switch_to_dict(sw, port_count)


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
    device_id: int | None = None


@app.get("/api/switches/{switch_id}/ports")
def api_list_ports(switch_id: int):
    with get_db() as db:
        sw = db.get(Switch, switch_id)
        if not sw:
            raise HTTPException(status_code=404, detail="Switch not found")
        rows = db.execute(
            sa.select(SwitchPort, Device)
            .outerjoin(PortConnection, PortConnection.switch_port_id == SwitchPort.id)
            .outerjoin(Device, Device.id == PortConnection.device_id)
            .where(SwitchPort.switch_id == switch_id)
            .order_by(SwitchPort.port_number)
        ).all()
        return [_port_to_dict(p, sw, d) for p, d in rows]


@app.post("/api/switches/{switch_id}/ports", status_code=201)
def api_add_port(switch_id: int, body: PortCreate):
    with get_db() as db:
        sw = db.get(Switch, switch_id)
        if not sw:
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
        sw = db.get(Switch, switch_id)
        if not sw:
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
        if "device_id" in fields:
            conn = db.execute(
                sa.select(PortConnection).where(PortConnection.switch_port_id == port_id)
            ).scalar_one_or_none()
            if body.device_id is None:
                if conn:
                    db.delete(conn)
            else:
                if conn:
                    conn.device_id = body.device_id
                else:
                    db.add(PortConnection(switch_port_id=port_id, device_id=body.device_id))

        db.flush()
        conn = db.execute(
            sa.select(PortConnection).where(PortConnection.switch_port_id == port_id)
        ).scalar_one_or_none()
        dev = db.get(Device, conn.device_id) if conn else None
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
        rows = db.execute(
            sa.select(SwitchPort, Switch, Device)
            .join(Switch, Switch.id == SwitchPort.switch_id)
            .outerjoin(PortConnection, PortConnection.switch_port_id == SwitchPort.id)
            .outerjoin(Device, Device.id == PortConnection.device_id)
            .order_by(Switch.ip_address, SwitchPort.port_number)
        ).all()
        return [_port_to_dict(p, s, d) for p, s, d in rows]


# ── switch links ──────────────────────────────────────────────────────────────

class SwitchLinkCreate(BaseModel):
    port_a_id: int
    port_b_id: int


@app.get("/api/switch-links")
def api_list_switch_links():
    with get_db() as db:
        links = db.execute(sa.select(SwitchLink)).scalars().all()
        result = []
        for lnk in links:
            pa = db.get(SwitchPort, lnk.port_a_id)
            pb = db.get(SwitchPort, lnk.port_b_id)
            sa_ = db.get(Switch, lnk.switch_a_id)
            sb  = db.get(Switch, lnk.switch_b_id)
            if pa and pb and sa_ and sb:
                result.append(_link_to_dict(lnk, sa_, pa, sb, pb))
        return result


@app.post("/api/switch-links", status_code=201)
def api_add_switch_link(body: SwitchLinkCreate):
    with get_db() as db:
        pa = db.get(SwitchPort, body.port_a_id)
        pb = db.get(SwitchPort, body.port_b_id)
        if not pa:
            raise HTTPException(status_code=404, detail="Port A not found")
        if not pb:
            raise HTTPException(status_code=404, detail="Port B not found")
        if pa.switch_id == pb.switch_id:
            raise HTTPException(status_code=422, detail="Cannot link a switch to itself")
        lnk = SwitchLink(
            switch_a_id=pa.switch_id,
            port_a_id=body.port_a_id,
            switch_b_id=pb.switch_id,
            port_b_id=body.port_b_id,
        )
        db.add(lnk)
        db.flush()
        sa_ = db.get(Switch, lnk.switch_a_id)
        sb  = db.get(Switch, lnk.switch_b_id)
        return _link_to_dict(lnk, sa_, pa, sb, pb)


@app.delete("/api/switch-links/{link_id}", status_code=204)
def api_delete_switch_link(link_id: int):
    with get_db() as db:
        lnk = db.get(SwitchLink, link_id)
        if not lnk:
            raise HTTPException(status_code=404, detail="Link not found")
        db.delete(lnk)


# ── topology (manual) ─────────────────────────────────────────────────────────

@app.get("/api/topology")
def api_topology():
    with get_db() as db:
        switches = db.execute(sa.select(Switch)).scalars().all()

        ports_with_devices = db.execute(
            sa.select(SwitchPort, Device)
            .join(PortConnection, PortConnection.switch_port_id == SwitchPort.id)
            .join(Device, Device.id == PortConnection.device_id)
        ).all()

        sw_links = db.execute(sa.select(SwitchLink)).scalars().all()

        # pre-fetch ports referenced by switch links
        link_port_ids = {lnk.port_a_id for lnk in sw_links} | {lnk.port_b_id for lnk in sw_links}
        ports_by_id: dict[int, SwitchPort] = {}
        if link_port_ids:
            for p in db.execute(
                sa.select(SwitchPort).where(SwitchPort.id.in_(link_port_ids))
            ).scalars().all():
                ports_by_id[p.id] = p

        nodes: list[dict] = []
        edges: list[dict] = []
        seen: set[str] = set()

        # Match devices → switches by IP and MAC to avoid duplicate nodes.
        # MAC lookup: prefer the MAC stored on the switch row; fall back to
        # finding a device record with the same IP (covers IP-only switches).
        sw_by_ip:  dict[str, Switch] = {sw.ip_address:  sw for sw in switches if sw.ip_address}
        sw_by_mac: dict[str, Switch] = {sw.mac_address.lower(): sw for sw in switches if sw.mac_address}
        for sw in switches:
            if not sw.mac_address and sw.ip_address:
                dev_row = db.execute(
                    sa.select(Device).where(Device.ip_address == sw.ip_address)
                ).scalar_one_or_none()
                if dev_row and dev_row.mac_address:
                    mac_lower = dev_row.mac_address.lower()
                    if mac_lower not in sw_by_mac:
                        sw_by_mac[mac_lower] = sw

        for sw in switches:
            nid = f"sw_{sw.id}"
            nodes.append({
                "id": nid, "type": "switch",
                "label": _sw_label(sw),
                "ip": sw.ip_address, "name": sw.name,
            })
            seen.add(nid)

        # Buffer switch→switch port edges so we can deduplicate them before
        # adding switch_link edges (which take precedence for the same pair).
        # Key: frozenset of the two node IDs (direction-independent).
        sw_port_edges: dict[frozenset, dict] = {}

        for port, dev in ports_with_devices:
            src = f"sw_{port.switch_id}"
            if src not in seen:
                continue

            matched_sw = (
                sw_by_ip.get(dev.ip_address)
                or sw_by_mac.get((dev.mac_address or "").lower())
            )
            if matched_sw:
                tgt = f"sw_{matched_sw.id}"
                if src == tgt:
                    continue  # self-loop: switch connected to its own mgmt port
                key = frozenset([src, tgt])
                if key not in sw_port_edges:
                    sw_port_edges[key] = {
                        "source": src, "target": tgt,
                        "port": port.label or f"Port {port.port_number}",
                        "port_type": port.port_type,
                        "speed": port.speed,
                        "type": "port",
                    }
            else:
                tgt = f"dev_{dev.id}"
                if tgt not in seen:
                    nodes.append({
                        "id": tgt, "type": "device",
                        "label": dev.name or dev.hostname or dev.ip_address,
                        "ip": dev.ip_address, "mac": dev.mac_address,
                        "hostname": dev.hostname, "vendor": dev.vendor,
                        "name": dev.name, "is_online": dev.is_online,
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
            src = f"sw_{lnk.switch_a_id}"
            tgt = f"sw_{lnk.switch_b_id}"
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

        # Add switch→switch port edges only where no switch_link covers the pair
        for key, edge in sw_port_edges.items():
            if key not in sw_link_pairs:
                edges.append(edge)

    return {"nodes": nodes, "edges": edges}


# ── helpers ───────────────────────────────────────────────────────────────────

def _switch_to_dict(s: Switch, port_count: int = 0) -> dict:
    return {
        "id": s.id,
        "ip_address": s.ip_address,
        "mac_address": s.mac_address,
        "name": s.name,
        "port_count": port_count,
    }


def _sw_label(sw: Switch) -> str:
    return sw.name or sw.ip_address or sw.mac_address or f"Switch {sw.id}"


def _port_to_dict(p: SwitchPort, sw: Switch, dev: Device | None) -> dict:
    return {
        "id": p.id,
        "switch_id": p.switch_id,
        "switch_name": _sw_label(sw),
        "switch_ip": sw.ip_address,
        "port_number": p.port_number,
        "label": p.label,
        "port_type": p.port_type,
        "speed": p.speed,
        "device_id": dev.id if dev else None,
        "device_label": (dev.name or dev.hostname or dev.ip_address) if dev else None,
    }


def _device_to_dict(d: Device, ports: list | None = None) -> dict:
    return {
        "id": d.id,
        "ip_address": d.ip_address,
        "name": d.name,
        "mac_address": d.mac_address,
        "hostname": d.hostname,
        "vendor": d.vendor,
        "os_info": d.os_info,
        "is_online": d.is_online,
        "open_ports": d.open_ports or [],
        "response_time": d.response_time,
        "first_seen": d.first_seen.isoformat() if d.first_seen else None,
        "last_seen": d.last_seen.isoformat() if d.last_seen else None,
        "scan_count": d.scan_count,
        "switch_ports": [
            {
                "id": p.id,
                "switch_id": sw.id,
                "switch_name": _sw_label(sw),
                "switch_ip": sw.ip_address,
                "port_number": p.port_number,
                "label": p.label,
                "port_type": p.port_type,
                "speed": p.speed,
            }
            for p, sw in (ports or [])
        ],
    }


def _link_to_dict(lnk: SwitchLink, sw_a: Switch, pa: SwitchPort, sw_b: Switch, pb: SwitchPort) -> dict:
    def _p(port: SwitchPort, sw: Switch) -> dict:
        return {
            "id": port.id,
            "switch_id": sw.id,
            "switch_name": _sw_label(sw),
            "port_number": port.port_number,
            "label": port.label,
            "port_type": port.port_type,
            "speed": port.speed,
        }
    return {
        "id": lnk.id,
        "switch_a_id": lnk.switch_a_id,
        "port_a_id": lnk.port_a_id,
        "switch_b_id": lnk.switch_b_id,
        "port_b_id": lnk.port_b_id,
        "port_a": _p(pa, sw_a),
        "port_b": _p(pb, sw_b),
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
