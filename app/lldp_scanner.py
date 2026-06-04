"""
Passive LLDP frame capture via raw AF_PACKET socket.
Listens for 802.1AB multicast frames and parses TLVs to extract
system name, system description, chassis MAC and management IP.
Requires CAP_NET_RAW (already set in docker-compose).
"""
import logging
import select
import socket
import struct
import threading
import time

logger = logging.getLogger(__name__)

ETH_P_LLDP = 0x88CC


def _parse_tlvs(payload: bytes) -> dict:
    result = {"chassis_mac": None, "system_name": None, "system_desc": None, "mgmt_ip": None}
    offset = 0
    while offset + 2 <= len(payload):
        word = struct.unpack_from(">H", payload, offset)[0]
        tlv_type = (word >> 9) & 0x7F
        tlv_len  = word & 0x1FF
        offset += 2
        if tlv_type == 0 or offset + tlv_len > len(payload):
            break
        value = payload[offset: offset + tlv_len]
        offset += tlv_len

        if tlv_type == 1 and tlv_len >= 7 and value[0] == 4:   # Chassis ID, subtype macAddress
            result["chassis_mac"] = ":".join(f"{b:02x}" for b in value[1:7])
        elif tlv_type == 5:                                      # System Name
            result["system_name"] = value.decode("utf-8", errors="replace").strip("\x00").strip()
        elif tlv_type == 6:                                      # System Description
            result["system_desc"] = value.decode("utf-8", errors="replace").strip("\x00").strip()
        elif tlv_type == 8 and tlv_len >= 6:                    # Management Address
            addr_len, addr_subtype = value[0], value[1]
            if addr_subtype == 1 and addr_len == 5:             # IPv4
                result["mgmt_ip"] = ".".join(str(b) for b in value[2:6])
    return result


def listen_lldp(duration: float = 60.0, stop_event: threading.Event | None = None) -> list[dict]:
    """
    Capture LLDP frames for up to `duration` seconds (or until stop_event is set).
    Returns list of dicts: {src_mac, chassis_mac, system_name, system_desc, mgmt_ip}.
    Returns [] silently if CAP_NET_RAW is not available.
    """
    try:
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_LLDP))
    except (PermissionError, OSError) as exc:
        logger.warning("LLDP passive capture unavailable: %s", exc)
        return []

    discovered: dict[str, dict] = {}
    deadline = time.monotonic() + duration

    try:
        while True:
            if stop_event and stop_event.is_set():
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([sock], [], [], min(remaining, 1.0))
            if not ready:
                continue
            frame = sock.recv(65535)
            if len(frame) < 14:
                continue
            ethertype = struct.unpack_from(">H", frame, 12)[0]
            if ethertype != ETH_P_LLDP:
                continue
            src_mac = ":".join(f"{b:02x}" for b in frame[6:12])
            parsed = _parse_tlvs(frame[14:])
            parsed["src_mac"] = src_mac
            if src_mac not in discovered:
                logger.info("LLDP: %s mac=%s ip=%s",
                            parsed.get("system_name") or src_mac,
                            src_mac, parsed.get("mgmt_ip"))
            discovered[src_mac] = parsed
    finally:
        sock.close()

    return list(discovered.values())
