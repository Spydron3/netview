import ipaddress
import logging
import os
import socket

import nmap
import psutil

logger = logging.getLogger(__name__)


def get_network_range() -> str:
    env = os.environ.get("NETWORK_RANGE", "").strip()
    if env:
        return env

    try:
        for iface, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if addr.family != socket.AF_INET:
                    continue
                ip = addr.address
                netmask = addr.netmask
                if not ip or not netmask:
                    continue
                if ip.startswith("127.") or ip.startswith("169.254."):
                    continue
                try:
                    net = ipaddress.IPv4Network(f"{ip}/{netmask}", strict=False)
                    if 8 <= net.prefixlen <= 24:
                        logger.info("Auto-detected network range: %s", net)
                        return str(net)
                except ValueError:
                    continue
    except Exception as exc:
        logger.error("Network range detection failed: %s", exc)

    logger.warning("Falling back to default network range 192.168.1.0/24")
    return "192.168.1.0/24"


def _parse_host(nm: nmap.PortScanner, host: str) -> dict:
    device = {
        "ip_address": host,
        "mac_address": None,
        "hostname": None,
        "vendor": None,
        "os_info": None,
        "is_online": True,
        "open_ports": [],
        "response_time": None,
    }

    addrs = nm[host].get("addresses", {})
    device["mac_address"] = addrs.get("mac") or None

    vendor_map = nm[host].get("vendor", {})
    if vendor_map:
        device["vendor"] = next(iter(vendor_map.values()), None)

    for h in nm[host].hostnames():
        if h.get("name"):
            device["hostname"] = h["name"]
            break

    # open ports
    ports = []
    for proto in nm[host].all_protocols():
        for port, info in nm[host][proto].items():
            if info.get("state") == "open":
                ports.append(
                    {
                        "port": port,
                        "protocol": proto,
                        "service": info.get("name", ""),
                        "version": (
                            f"{info.get('product','')} {info.get('version','')}".strip()
                        ),
                    }
                )
    ports.sort(key=lambda p: p["port"])
    device["open_ports"] = ports

    return device


def scan_network(network_range: str | None = None) -> tuple[list[dict], str]:
    if network_range is None:
        network_range = get_network_range()

    port_scan = os.environ.get("PORT_SCAN_ENABLED", "true").lower() == "true"
    logger.info("Scanning %s (port_scan=%s)", network_range, port_scan)

    nm = nmap.PortScanner()

    # Phase 1 — host discovery (fast ARP/ICMP sweep)
    discovery_args = "-sn -T4 --host-timeout 10s"
    nm.scan(hosts=network_range, arguments=discovery_args)
    live_hosts = [h for h in nm.all_hosts() if nm[h].state() == "up"]
    logger.info("Discovery: %d live hosts", len(live_hosts))

    if not live_hosts:
        return [], network_range

    # Build base device records from discovery
    devices_by_ip: dict[str, dict] = {}
    for host in live_hosts:
        devices_by_ip[host] = _parse_host(nm, host)

    # Phase 2 — optional port scan on live hosts only
    if port_scan and live_hosts:
        host_list = " ".join(live_hosts)
        try:
            nm2 = nmap.PortScanner()
            port_args = "-T4 -F --open --host-timeout 15s"
            nm2.scan(hosts=host_list, arguments=port_args)
            for host in nm2.all_hosts():
                if host in devices_by_ip:
                    ports = []
                    for proto in nm2[host].all_protocols():
                        for port, info in nm2[host][proto].items():
                            if info.get("state") == "open":
                                ports.append(
                                    {
                                        "port": port,
                                        "protocol": proto,
                                        "service": info.get("name", ""),
                                        "version": (
                                            f"{info.get('product','')} {info.get('version','')}".strip()
                                        ),
                                    }
                                )
                    ports.sort(key=lambda p: p["port"])
                    devices_by_ip[host]["open_ports"] = ports
                    # grab hostname/vendor from port scan too if missing
                    if not devices_by_ip[host]["hostname"]:
                        for h in nm2[host].hostnames():
                            if h.get("name"):
                                devices_by_ip[host]["hostname"] = h["name"]
                                break
                    if not devices_by_ip[host]["mac_address"]:
                        mac = nm2[host].get("addresses", {}).get("mac")
                        if mac:
                            devices_by_ip[host]["mac_address"] = mac
                    if not devices_by_ip[host]["vendor"]:
                        vmap = nm2[host].get("vendor", {})
                        if vmap:
                            devices_by_ip[host]["vendor"] = next(iter(vmap.values()), None)
        except Exception as exc:
            logger.error("Port scan failed: %s", exc)

    devices = list(devices_by_ip.values())
    logger.info("Scan complete: %d devices", len(devices))
    return devices, network_range
