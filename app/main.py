import logging
import os
import re
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
from lldp_scanner import listen_lldp
from models import Device, ScanRun, Setting, Switch, TopologyLink
from scanner import get_network_range, scan_network
from snmp_scanner import poll_switch

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

_scan_lock  = threading.Lock()
_scan_state: dict = {"running": False, "started_at": None}
_topo_lock  = threading.Lock()
_topo_state: dict = {"running": False, "started_at": None, "log": []}
_scheduler  = BackgroundScheduler(daemon=True)


def _tlog(msg: str) -> None:
    ts = datetime.utcnow().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    logger.info("topo: %s", msg)
    _topo_state["log"].append(line)


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

        # Start passive LLDP capture in background while nmap runs
        _lldp_stop = threading.Event()
        _lldp_results: list[dict] = []
        def _lldp_worker():
            _lldp_results.extend(listen_lldp(duration=120, stop_event=_lldp_stop))
        lldp_thread = threading.Thread(target=_lldp_worker, daemon=True, name="lldp-capture")
        lldp_thread.start()

        try:
            devices, network_range = scan_network(network_range=nr, port_scan=ps)
        finally:
            _lldp_stop.set()
            lldp_thread.join(timeout=5)

        # Build LLDP lookup indexes
        lldp_by_mac = {r["src_mac"].lower(): r for r in _lldp_results if r.get("src_mac")}
        lldp_by_ip  = {r["mgmt_ip"]: r for r in _lldp_results if r.get("mgmt_ip")}
        if _lldp_results:
            logger.info("LLDP captured %d device(s)", len(_lldp_results))

        now = datetime.utcnow()

        with get_db() as db:
            db.execute(sa.update(Device).values(is_online=False))

            for d in devices:
                # Enrich with LLDP data if available
                lldp = lldp_by_mac.get((d["mac_address"] or "").lower()) \
                    or lldp_by_ip.get(d["ip_address"])
                if lldp:
                    if not d["hostname"] and lldp.get("system_name"):
                        d["hostname"] = lldp["system_name"]
                    if not d["os_info"] and lldp.get("system_desc"):
                        d["os_info"] = lldp["system_desc"]
                    if not d["mac_address"] and lldp.get("chassis_mac"):
                        d["mac_address"] = lldp["chassis_mac"]

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
                    if d["os_info"] and not existing.os_info:
                        existing.os_info = d["os_info"]
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


def _run_topo_scan() -> None:
    if not _topo_lock.acquire(blocking=False):
        logger.info("Topology scan already running — skipping")
        return

    _topo_state["running"] = True
    _topo_state["started_at"] = datetime.utcnow()
    _topo_state["log"] = []

    try:
        with get_db() as db:
            switch_rows = db.execute(
                sa.select(Switch).where(Switch.enabled.is_(True))
            ).scalars().all()
            switches = [
                {"id": sw.id, "ip_address": sw.ip_address, "name": sw.name, "community": sw.community}
                for sw in switch_rows
            ]

        if not switches:
            _tlog("No enabled switches configured.")
        else:
            _tlog(f"Found {len(switches)} enabled switch(es).")

        for sw in switches:
            label = sw["name"] or sw["ip_address"]
            _tlog(f"Polling {label} ({sw['ip_address']}) via SNMP…")
            try:
                data = poll_switch(sw["ip_address"], sw["community"])
            except Exception as exc:
                _tlog(f"  ERROR: {exc}")
                data = {"error": str(exc), "mac_table": [], "lldp_neighbors": []}

            if data.get("error"):
                _tlog(f"  SNMP error: {data['error']}")
            else:
                mac_count  = len(data.get("mac_table", []))
                lldp_count = len(data.get("lldp_neighbors", []))
                _tlog(f"  FDB entries: {mac_count}   LLDP neighbours: {lldp_count}")

            with get_db() as db:
                switch = db.get(Switch, sw["id"])
                switch.last_polled = datetime.utcnow()
                switch.status = "error" if data.get("error") else "ok"

                if not data.get("error"):
                    db.execute(sa.delete(TopologyLink).where(TopologyLink.switch_id == sw["id"]))

                    for entry in data.get("mac_table", []):
                        db.add(TopologyLink(
                            switch_id=sw["id"],
                            local_port=entry["port_name"],
                            local_port_index=entry["port_index"],
                            remote_mac=entry["mac"],
                            link_type="device",
                            last_seen=datetime.utcnow(),
                        ))

                    for nb in data.get("lldp_neighbors", []):
                        db.add(TopologyLink(
                            switch_id=sw["id"],
                            local_port=nb["local_port"],
                            local_port_index=nb["local_port_index"],
                            remote_mac=nb["remote_mac"],
                            remote_sysname=nb["remote_sysname"],
                            link_type="lldp",
                            last_seen=datetime.utcnow(),
                        ))

                    saved = len(data.get("mac_table", [])) + len(data.get("lldp_neighbors", []))
                    _tlog(f"  Saved {saved} topology link(s) for {label}.")

        _tlog("Topology scan complete.")

    except Exception as exc:
        _tlog(f"FATAL: {exc}")
        logger.exception("Topology scan failed: %s", exc)
    finally:
        _topo_state["running"] = False
        _topo_lock.release()


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


# ── API ──────────────────────────────────────────────────────────────────────

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


@app.get("/api/devices")
def api_devices():
    with get_db() as db:
        # Sort: online first, then by inet order (cast required for numeric IP sort in PG)
        from sqlalchemy.dialects.postgresql import INET
        rows = db.execute(
            sa.select(Device).order_by(
                Device.is_online.desc(),
                sa.cast(Device.ip_address, INET),
            )
        ).scalars().all()
        return [_device_to_dict(d) for d in rows]


@app.get("/api/devices/{device_id}")
def api_device(device_id: int):
    with get_db() as db:
        d = db.get(Device, device_id)
        if not d:
            raise HTTPException(status_code=404, detail="Device not found")
        return _device_to_dict(d)


class SettingsUpdate(BaseModel):
    scan_interval: int | None = None   # seconds
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


class DeviceUpdate(BaseModel):
    name: str | None = None


@app.patch("/api/devices/{device_id}")
def api_update_device(device_id: int, body: DeviceUpdate):
    with get_db() as db:
        d = db.get(Device, device_id)
        if not d:
            raise HTTPException(status_code=404, detail="Device not found")
        d.name = body.name.strip() if body.name and body.name.strip() else None
        return _device_to_dict(d)


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


# ── switches ─────────────────────────────────────────────────────────────────

class SwitchCreate(BaseModel):
    ip_address: str
    name: str | None = None
    community: str = "public"


@app.get("/api/switches")
def api_list_switches():
    with get_db() as db:
        rows = db.execute(sa.select(Switch).order_by(Switch.ip_address)).scalars().all()
        return [_switch_to_dict(s) for s in rows]


@app.post("/api/switches", status_code=201)
def api_add_switch(body: SwitchCreate):
    with get_db() as db:
        existing = db.execute(
            sa.select(Switch).where(Switch.ip_address == body.ip_address)
        ).scalar_one_or_none()
        if existing:
            raise HTTPException(status_code=409, detail="Switch already exists")
        sw = Switch(
            ip_address=body.ip_address,
            name=body.name,
            community=body.community or "public",
        )
        db.add(sw)
        db.flush()
        return _switch_to_dict(sw)


@app.delete("/api/switches/{switch_id}", status_code=204)
def api_delete_switch(switch_id: int):
    with get_db() as db:
        sw = db.get(Switch, switch_id)
        if not sw:
            raise HTTPException(status_code=404, detail="Switch not found")
        db.execute(sa.delete(TopologyLink).where(TopologyLink.switch_id == switch_id))
        db.delete(sw)


@app.get("/api/switches/{switch_id}/poll")
def api_poll_switch(switch_id: int):
    """Debug endpoint — runs poll_switch and returns the raw result."""
    with get_db() as db:
        sw = db.get(Switch, switch_id)
        if not sw:
            raise HTTPException(status_code=404, detail="Switch not found")
        ip, community = sw.ip_address, sw.community
    data = poll_switch(ip, community)
    data["if_names"] = {str(k): v for k, v in data.get("if_names", {}).items()}
    return data


@app.patch("/api/switches/{switch_id}")
def api_update_switch(switch_id: int, body: SwitchCreate):
    with get_db() as db:
        sw = db.get(Switch, switch_id)
        if not sw:
            raise HTTPException(status_code=404, detail="Switch not found")
        sw.ip_address = body.ip_address
        sw.name = body.name
        sw.community = body.community or "public"
        return _switch_to_dict(sw)


# ── topology ──────────────────────────────────────────────────────────────────

@app.post("/api/topology/scan")
def api_topo_scan(background_tasks: BackgroundTasks):
    if _topo_state["running"]:
        return {"status": "already_running", "started_at": _topo_state["started_at"]}
    background_tasks.add_task(_run_topo_scan)
    return {"status": "started"}


@app.get("/api/topology/status")
def api_topo_status():
    return {
        "running": _topo_state["running"],
        "started_at": _topo_state["started_at"],
    }


@app.get("/api/topology/log")
def api_topo_log():
    return {
        "running": _topo_state["running"],
        "lines": list(_topo_state["log"]),
    }


@app.get("/api/topology")
def api_topology():
    with get_db() as db:
        switches = db.execute(sa.select(Switch)).scalars().all()
        links    = db.execute(sa.select(TopologyLink)).scalars().all()
        devices  = db.execute(sa.select(Device)).scalars().all()

        # MAC → device lookup (inside session so attributes are accessible)
        mac_to_dev = {
            d.mac_address.lower(): d
            for d in devices
            if d.mac_address
        }

        # IP → switch lookup (for MAC-based LLDP matching)
        sw_by_ip = {sw.ip_address: sw for sw in switches}

        nodes: list[dict] = []
        edges: list[dict] = []
        seen_nodes: set[str] = set()

        def _add_node(nid: str, node: dict):
            if nid not in seen_nodes:
                nodes.append({"id": nid, **node})
                seen_nodes.add(nid)

        # Switch nodes
        for sw in switches:
            _add_node(f"sw_{sw.id}", {
                "type": "switch",
                "label": sw.name or sw.ip_address,
                "ip": sw.ip_address,
                "name": sw.name,
                "status": sw.status,
                "last_polled": sw.last_polled.isoformat() if sw.last_polled else None,
            })

        # Build edges from topology links
        for link in links:
            src = f"sw_{link.switch_id}"
            if src not in seen_nodes:
                continue  # orphaned link

            if link.link_type == "lldp":
                target_sw = None

                # 1. match by sysname vs configured switch name/IP
                if link.remote_sysname:
                    for sw in switches:
                        if sw.ip_address == link.remote_sysname or (
                            sw.name and sw.name.lower() == link.remote_sysname.lower()
                        ):
                            target_sw = sw
                            break

                # 2. match by remote MAC → device IP → configured switch IP
                if not target_sw and link.remote_mac:
                    dev = mac_to_dev.get(link.remote_mac.lower())
                    if dev:
                        target_sw = sw_by_ip.get(dev.ip_address)

                if target_sw:
                    tgt = f"sw_{target_sw.id}"
                else:
                    # Unknown neighbour — ghost node
                    nid_raw = link.remote_mac or link.remote_sysname or str(link.id)
                    tgt = "ext_" + re.sub(r"[^a-z0-9]", "_", nid_raw.lower())
                    _add_node(tgt, {
                        "type": "external_switch",
                        "label": link.remote_sysname or link.remote_mac or "Unknown switch",
                        "mac": link.remote_mac,
                        "sysname": link.remote_sysname,
                    })
                edges.append({
                    "source": src, "target": tgt,
                    "port": link.local_port, "type": "lldp",
                })

            else:  # device link from MAC table
                if not link.remote_mac:
                    continue
                dev = mac_to_dev.get(link.remote_mac.lower())
                if dev:
                    tgt = f"dev_{dev.id}"
                    _add_node(tgt, {
                        "type": "device",
                        "label": dev.name or dev.hostname or dev.ip_address,
                        "ip": dev.ip_address,
                        "mac": dev.mac_address,
                        "hostname": dev.hostname,
                        "vendor": dev.vendor,
                        "name": dev.name,
                        "is_online": dev.is_online,
                    })
                else:
                    # MAC seen on switch but not yet in devices table
                    tgt = "mac_" + link.remote_mac.replace(":", "")
                    _add_node(tgt, {
                        "type": "unknown_device",
                        "label": link.remote_mac,
                        "mac": link.remote_mac,
                        "is_online": None,
                    })
                edges.append({
                    "source": src, "target": tgt,
                    "port": link.local_port, "type": "device",
                })

    return {"nodes": nodes, "edges": edges}


# ── helpers ───────────────────────────────────────────────────────────────────

def _switch_to_dict(s: Switch) -> dict:
    return {
        "id": s.id,
        "ip_address": s.ip_address,
        "name": s.name,
        "community": s.community,
        "enabled": s.enabled,
        "status": s.status,
        "last_polled": s.last_polled.isoformat() if s.last_polled else None,
    }


def _device_to_dict(d: Device) -> dict:
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
