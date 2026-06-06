# NetViewMyHome

A self-hosted network discovery and topology visualisation tool for home and small-office networks. Runs as a Docker stack, scans your LAN with nmap, and draws an interactive topology map in your browser.

![NetViewMyHome](app/static/netscanner_logo.svg)

## Features

- **Auto-discovery** — nmap two-pass scan (host discovery, then port scan on live hosts) on a configurable interval
- **Topology map** — D3.js force-directed graph with parallel edge curving for multi-link connections
- **LLDP capture** — passive raw-socket LLDP frame capture to detect physical switch links
- **SNMP topology** — Bridge MIB + LLDP MIB polling via `snmpwalk` to map managed-switch port connections
- **Device management** — click-to-edit names, assign rooms, flag access points, manage WLANs
- **Switch management** — add managed switches with typed ports (RJ45, SFP+) and link speed
- **Scan history** — browseable log of past scan runs and discovered devices
- **New-device alerts** — optional SMTP email notification when unknown devices appear
- **Topology live log** — real-time popup streamed from the topology scan process

## Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.11, FastAPI, SQLAlchemy (sync), APScheduler |
| Database | PostgreSQL 16 |
| Scanner | nmap, python-nmap, AF_PACKET raw sockets |
| Frontend | Vanilla JS, D3.js v7 |
| Container | Docker, `network_mode: host` |

## Quick start

### Prerequisites

- Docker and Docker Compose (or Portainer)
- Linux host recommended — `network_mode: host` is required for ARP scanning and raw-socket LLDP capture

### Run with Docker Compose

```bash
docker compose up -d
```

The UI is available at `http://<host-ip>:8080`.

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `PORT` | `8080` | Web UI listen port |
| `SCAN_INTERVAL` | `300` | Seconds between automatic scans |
| `NETWORK_RANGE` | _(auto-detect)_ | CIDR range to scan, e.g. `192.168.1.0/24` |
| `PORT_SCAN_ENABLED` | `true` | Set `false` for faster host-only scans |
| `DATABASE_URL` | _(see compose)_ | PostgreSQL connection string |

### Portainer stack

Point Portainer at the `docker-compose.yml` in this repo, or use the pre-built image directly:

```yaml
image: ghcr.io/spydron3/netviewmyhome:latest
```

The image is rebuilt automatically by GitHub Actions on every push to `main`.

## Network requirements

The app container runs with `network_mode: host` and the `NET_ADMIN` + `NET_RAW` capabilities. This is necessary for:

- nmap ARP host discovery
- Passive LLDP frame capture via AF_PACKET raw sockets

> **macOS / Docker Desktop:** `network_mode: host` maps to the Docker VM network, not your physical LAN. Set `NETWORK_RANGE` explicitly and expect ARP/LLDP features to be limited.

## Development

```bash
# clone
git clone https://github.com/Spydron3/netview.git
cd netview

# start database only, then run the app locally
docker compose up db -d
cd app
pip install -r requirements.txt
DATABASE_URL=postgresql://netview:netview@localhost:5432/netview uvicorn main:app --reload --port 8080
```

## License

MIT
