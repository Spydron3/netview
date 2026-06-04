# Netview — Local Network Discovery

## Overview
Docker application that discovers all devices on the local network, stores them in PostgreSQL, and provides a web interface for monitoring.

## Architecture
- **Scanner** — Python service using nmap for host discovery and port scanning
- **Database** — PostgreSQL 16 for persistent storage of device data and scan history
- **Web UI** — FastAPI backend serving a single-page HTML/JS frontend
- **Docker Compose** — Orchestrates both services

## Tech Stack
- Python 3.11 + FastAPI
- PostgreSQL 16
- nmap (network scanning)
- SQLAlchemy (ORM, sync)
- APScheduler (periodic background scanning)
- Vanilla HTML/CSS/JS (no framework)

## Features
- Automatic periodic network scanning (configurable interval)
- Manual scan trigger from web UI
- Stores: IP, MAC address, hostname, vendor, open ports, first/last seen, scan count
- Search and filter devices (text search, show/hide offline)
- Scan history log
- Live scan status indicator

## Configuration (environment variables)
| Variable             | Default                  | Description                                      |
|----------------------|--------------------------|--------------------------------------------------|
| `NETWORK_RANGE`      | auto-detect              | Network to scan, e.g. `192.168.1.0/24`           |
| `SCAN_INTERVAL`      | `300`                    | Seconds between automatic scans                  |
| `PORT_SCAN_ENABLED`  | `true`                   | Scan top-100 ports per live host                 |
| `DATABASE_URL`       | see docker-compose.yml   | PostgreSQL connection string                     |

## macOS note
Docker Desktop on macOS runs containers inside a Linux VM. `network_mode: host` maps to the
VM's loopback, not your Mac's physical NIC.  ARP-based MAC discovery is unavailable, but ICMP/TCP
probes still reach your LAN. **Set `NETWORK_RANGE` explicitly** to your home subnet
(e.g. `192.168.1.0/24`).  For full MAC discovery run on a Linux host.

## Implementation Progress
- [x] plan.md
- [x] docker-compose.yml
- [x] app/Dockerfile
- [x] app/requirements.txt
- [x] app/database.py
- [x] app/models.py
- [x] app/scanner.py
- [x] app/main.py
- [x] app/static/index.html
- [x] app/static/style.css
- [x] app/static/app.js
