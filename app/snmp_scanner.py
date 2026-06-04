"""
SNMP-based switch topology scanner.
Uses the system `snmpwalk` binary (net-snmp package) via subprocess.
Queries the Bridge MIB for MAC-to-port mappings and LLDP for switch neighbours.
"""
import logging
import re
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

# ── OID constants ─────────────────────────────────────────────────────────────
_FDB_ADDRESS      = "1.3.6.1.2.1.17.4.3.1.1"  # dot1dTpFdbAddress  – MACs in FDB
_FDB_PORT         = "1.3.6.1.2.1.17.4.3.1.2"  # dot1dTpFdbPort     – bridge-port for each MAC
_FDB_STATUS       = "1.3.6.1.2.1.17.4.3.1.3"  # dot1dTpFdbStatus   – 3=learned, 5=self
_BRIDGE_PORT_IFIDX= "1.3.6.1.2.1.17.1.4.1.2"  # dot1dBasePortIfIndex – bridge-port → ifIndex
_IF_NAME          = "1.3.6.1.2.1.31.1.1.1.1"  # ifName
_IF_DESCR         = "1.3.6.1.2.1.2.2.1.2"     # ifDescr (fallback)
_LLDP_REM_CHASSIS = "1.0.8802.1.1.2.1.4.1.1.5"  # lldpRemChassisId (often MAC)
_LLDP_REM_PORT    = "1.0.8802.1.1.2.1.4.1.1.8"  # lldpRemPortDesc
_LLDP_REM_SYSNAME = "1.0.8802.1.1.2.1.4.1.1.9"  # lldpRemSysName


# ── low-level walk ────────────────────────────────────────────────────────────

def _walk(host: str, community: str, oid: str, timeout: int = 15) -> dict[str, str]:
    """Return {full_oid: raw_value_string} from snmpwalk."""
    try:
        proc = subprocess.run(
            [
                "snmpwalk",
                "-v2c", "-c", community,
                "-On",          # numeric OIDs – consistent across devices
                "-t", str(timeout),
                "-r", "1",      # 1 retry
                host, oid,
            ],
            capture_output=True, text=True,
            timeout=timeout + 5,
        )
        results: dict[str, str] = {}
        for line in proc.stdout.splitlines():
            if " = " not in line:
                continue
            key, val = line.split(" = ", 1)
            results[key.strip().lstrip(".")] = val.strip()
        return results
    except FileNotFoundError:
        logger.error("snmpwalk not found – is the 'snmp' package installed?")
        return {}
    except subprocess.TimeoutExpired:
        logger.warning("snmpwalk timed out for %s OID %s", host, oid)
        return {}
    except Exception as exc:
        logger.error("snmpwalk error for %s: %s", host, exc)
        return {}


# ── value parsers ─────────────────────────────────────────────────────────────

def _int(val: str) -> Optional[int]:
    m = re.match(r"(?:INTEGER|Gauge32|Counter32|Counter64|TimeTicks):\s*(\d+)", val)
    return int(m.group(1)) if m else None


def _hex_to_mac(val: str) -> Optional[str]:
    """'Hex-STRING: 00 0D C8 0C 01 01'  →  '00:0d:c8:0c:01:01'"""
    m = re.match(r"Hex-STRING:\s*([\da-fA-F ]+)", val)
    if not m:
        return None
    try:
        raw = bytes.fromhex(m.group(1).replace(" ", ""))
        if len(raw) == 6:
            return ":".join(f"{b:02x}" for b in raw)
    except ValueError:
        pass
    return None


def _string(val: str) -> str:
    m = re.match(r'STRING:\s*"?(.*?)"?\s*$', val)
    return (m.group(1) if m else val).strip()


def _oid_suffix(full_oid: str, base_oid: str) -> str:
    """Strip leading base + dot to get the index suffix."""
    base = base_oid.lstrip(".")
    full = full_oid.lstrip(".")
    return full[len(base):].lstrip(".")


# ── main poll function ────────────────────────────────────────────────────────

_SYS_DESCR = "1.3.6.1.2.1.1.1.0"  # sysDescr – present on any SNMP-capable device


def poll_switch(host: str, community: str = "public") -> dict:
    """
    Poll one managed switch.  Returns:
      {
        error: str | None,
        if_names: {ifIndex: name},
        mac_table: [{mac, port_name, port_index}],
        lldp_neighbors: [{local_port, local_port_index, remote_sysname, remote_mac, remote_port}],
      }
    """
    result: dict = {
        "error": None,
        "if_names": {},
        "mac_table": [],
        "lldp_neighbors": [],
    }

    # 0 ── quick reachability probe (3 s) — bail fast if SNMP is unreachable
    if not _walk(host, community, _SYS_DESCR, timeout=3):
        result["error"] = "No SNMP response – check IP, community string and that SNMP is enabled"
        return result

    # 1 ── interface names
    if_names: dict[int, str] = {}
    for oid, val in _walk(host, community, _IF_NAME).items():
        suffix = _oid_suffix(oid, _IF_NAME)
        try:
            if_names[int(suffix)] = _string(val)
        except ValueError:
            pass

    if not if_names:
        for oid, val in _walk(host, community, _IF_DESCR).items():
            suffix = _oid_suffix(oid, _IF_DESCR)
            try:
                if_names[int(suffix)] = _string(val)
            except ValueError:
                pass

    if not if_names:
        # Interface MIBs not supported (some switches only expose LLDP).
        # Don't bail — continue so LLDP queries can still run.
        logger.warning("No interface names from %s; Bridge MIB may be unsupported. Trying LLDP anyway.", host)

    result["if_names"] = if_names

    # 2 ── bridge-port → ifIndex map
    bp_to_ifidx: dict[int, int] = {}
    for oid, val in _walk(host, community, _BRIDGE_PORT_IFIDX).items():
        bp = _oid_suffix(oid, _BRIDGE_PORT_IFIDX)
        v  = _int(val)
        if bp.isdigit() and v is not None:
            bp_to_ifidx[int(bp)] = v

    # 3 ── FDB: collect MACs, their bridge-port numbers and status
    fdb_mac:    dict[str, str] = {}  # suffix → MAC
    fdb_port:   dict[str, int] = {}  # suffix → bridge-port
    fdb_status: dict[str, int] = {}  # suffix → status

    for oid, val in _walk(host, community, _FDB_ADDRESS).items():
        suffix = _oid_suffix(oid, _FDB_ADDRESS)
        mac = _hex_to_mac(val)
        if not mac:
            # some devices encode MAC in OID suffix
            parts = suffix.split(".")
            if len(parts) >= 6:
                try:
                    mac = ":".join(f"{int(p):02x}" for p in parts[-6:])
                except ValueError:
                    pass
        if mac:
            fdb_mac[suffix] = mac

    for oid, val in _walk(host, community, _FDB_PORT).items():
        suffix = _oid_suffix(oid, _FDB_PORT)
        v = _int(val)
        if v is not None:
            fdb_port[suffix] = v

    for oid, val in _walk(host, community, _FDB_STATUS).items():
        suffix = _oid_suffix(oid, _FDB_STATUS)
        v = _int(val)
        if v is not None:
            fdb_status[suffix] = v

    mac_table: list[dict] = []
    for suffix, mac in fdb_mac.items():
        if fdb_status.get(suffix) == 5:   # 5 = self (switch's own MAC)
            continue
        bp = fdb_port.get(suffix)
        if bp is None or bp == 0:
            continue
        ifidx = bp_to_ifidx.get(bp)
        port_name = if_names.get(ifidx, f"port{bp}") if ifidx else f"port{bp}"
        mac_table.append({
            "mac": mac,
            "port_name": port_name,
            "port_index": ifidx or bp,
        })

    result["mac_table"] = mac_table

    # 4 ── LLDP neighbours
    # OID suffix after column OID: timeMark.localPortNum.remoteIndex
    def _lldp_key(oid: str, col: str) -> tuple[str, str, str]:
        s = _oid_suffix(oid, col).split(".")
        return (s[0], s[1], s[2]) if len(s) >= 3 else ("", "", "")

    lldp_sysnames: dict[tuple, str] = {}
    for oid, val in _walk(host, community, _LLDP_REM_SYSNAME).items():
        k = _lldp_key(oid, _LLDP_REM_SYSNAME)
        if k[0]:
            lldp_sysnames[k] = _string(val)

    lldp_chassis: dict[tuple, str] = {}
    for oid, val in _walk(host, community, _LLDP_REM_CHASSIS).items():
        k = _lldp_key(oid, _LLDP_REM_CHASSIS)
        if k[0]:
            mac = _hex_to_mac(val)
            if mac:
                lldp_chassis[k] = mac

    lldp_portdesc: dict[tuple, str] = {}
    for oid, val in _walk(host, community, _LLDP_REM_PORT).items():
        k = _lldp_key(oid, _LLDP_REM_PORT)
        if k[0]:
            lldp_portdesc[k] = _string(val)

    neighbors: list[dict] = []
    for key, sysname in lldp_sysnames.items():
        _, local_port_num, _ = key
        try:
            ifidx = int(local_port_num)
            local_port = if_names.get(ifidx, f"port{local_port_num}")
        except ValueError:
            local_port = f"port{local_port_num}"
            ifidx = None

        neighbors.append({
            "local_port": local_port,
            "local_port_index": ifidx,
            "remote_sysname": sysname,
            "remote_mac": lldp_chassis.get(key),
            "remote_port": lldp_portdesc.get(key, ""),
        })

    result["lldp_neighbors"] = neighbors

    # If we got nothing at all — no interfaces, no FDB, no LLDP — SNMP is unreachable
    if not if_names and not mac_table and not neighbors:
        result["error"] = "No SNMP response – check IP, community string and that SNMP is enabled"

    return result
