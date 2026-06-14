# DockerManager

> 🇬🇧 English | [🇪🇸 Español](docs/README.es.md)

A lightweight web interface for managing Docker from the browser. Built with FastAPI and vanilla JavaScript — no frontend frameworks, no external runtime dependencies.

![Python](https://img.shields.io/badge/Python-3.12-blue) ![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green) ![Docker](https://img.shields.io/badge/Docker-SDK-blue)

---

## Quick Start

```bash
git clone https://github.com/FranciscoFdez05/DockerManager.git
cd DockerManager
docker compose up -d --build
```

Open `http://<host-ip>:3000` in your browser. On first launch, you will be prompted to create an admin account. Credentials are stored persistently in a Docker volume (`dockermanager-data`).

---

## Features

### Containers
- List all containers with name, image, status, ports, and live CPU/RAM stats
- Start, stop, restart, pause, and resume containers
- Create containers via UI: image, name, command, restart policy, ports, environment variables, volumes, and network
- Real-time log streaming with autoscroll, clear, and download as `.log`
- Interactive terminal (xterm.js) with automatic `bash`/`sh` detection and dynamic resize
- Live stats modal: CPU%, RAM%, network I/O, and a 60-point rolling chart (Chart.js)
- Inspect modal: network interfaces, mounts, environment, restart policy, resource limits
- Process list (`docker top`) with refresh
- Browser notifications when a container transitions out of `running`

### Images
- List images with tag, ID, size, and creation date
- Pull images by tag
- Delete images
- View layer history

### Volumes & Networks
- List, inspect, and delete volumes and networks
- Connect and disconnect containers from networks via the inspect modal

### System
- Host stats bar (CPU%, RAM, disk) updated every 4 seconds via `psutil`
- Docker daemon info: version, OS, kernel, architecture, storage driver
- One-click prune: stopped containers, dangling images, unused volumes, orphaned networks

### Security
- Session cookie: `HttpOnly`, `SameSite=Strict`
- Password hashing: bcrypt (cost 12)
- Brute-force protection: 5 failed attempts → 15-minute IP lockout
- XSS mitigation: all dynamic content escaped; CSP headers
- Anti-clickjacking: `X-Frame-Options: DENY`
- MIME sniffing prevention: `X-Content-Type-Options: nosniff`
- Timing-safe credential comparison via `secrets.compare_digest()`

### PWA
Installable as a desktop or mobile app via Chrome, Edge, or Safari.

---

## Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12, FastAPI, Uvicorn |
| Docker SDK | `docker` 7.1 |
| Host stats | `psutil` 6.1 |
| Auth | `bcrypt` (cost 12) |
| Real-time | WebSocket (logs, stats, terminal) |
| Terminal | xterm.js 5.3 + FitAddon |
| Charts | Chart.js 4.4 |
| Frontend | HTML + CSS + Vanilla JS |
| Deploy | Docker Compose |

---

## Project Structure

```
DockerManager/
├── main.py              # FastAPI app: REST API, WebSockets, auth
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── update.sh
└── static/
    ├── index.html
    ├── style.css
    ├── app.js
    ├── favicon.svg
    └── manifest.json
```

---

## API Reference

### Auth

| Method | Route | Description |
|---|---|---|
| `GET` | `/api/auth/status` | Auth status (`setup_needed`, `authenticated`) |
| `POST` | `/api/auth/setup` | Initial account creation |
| `POST` | `/api/auth/login` | Login (rate-limited: 5 attempts / 15 min) |
| `POST` | `/api/auth/logout` | Logout |
| `POST` | `/api/auth/change-password` | Change password |

### Containers

| Method | Route | Description |
|---|---|---|
| `GET` | `/api/containers` | List all |
| `POST` | `/api/containers/create` | Create |
| `POST` | `/api/containers/{id}/start` | Start |
| `POST` | `/api/containers/{id}/stop` | Stop |
| `POST` | `/api/containers/{id}/restart` | Restart |
| `POST` | `/api/containers/{id}/pause` | Pause |
| `POST` | `/api/containers/{id}/unpause` | Unpause |
| `DELETE` | `/api/containers/{id}` | Remove |
| `GET` | `/api/containers/{id}/inspect` | Full inspect |
| `GET` | `/api/containers/{id}/processes` | Process list (top) |
| `GET` | `/api/containers/{id}/stats_once` | Snapshot stats |
| `GET` | `/api/containers/{id}/logs/download` | Download logs |
| `POST` | `/api/containers/{id}/networks/connect` | Connect to network |
| `POST` | `/api/containers/{id}/networks/disconnect` | Disconnect from network |

### Images

| Method | Route | Description |
|---|---|---|
| `GET` | `/api/images` | List |
| `DELETE` | `/api/images/{id}` | Remove |
| `POST` | `/api/images/pull` | Pull by tag |
| `GET` | `/api/images/{id}/history` | Layer history |

### Volumes, Networks & System

| Method | Route | Description |
|---|---|---|
| `GET` | `/api/volumes` | List volumes |
| `DELETE` | `/api/volumes/{name}` | Remove volume |
| `GET` | `/api/networks` | List networks |
| `DELETE` | `/api/networks/{id}` | Remove network |
| `GET` | `/api/system/info` | Docker + host info |
| `GET` | `/api/system/host` | CPU / RAM / disk |
| `POST` | `/api/system/prune` | System prune |

### WebSockets

| Route | Description |
|---|---|
| `WS /ws/containers/{id}/logs` | Real-time log stream |
| `WS /ws/containers/{id}/stats` | CPU / RAM / network stats (~1 s interval) |
| `WS /ws/containers/{id}/exec` | Interactive shell |

---

## Updating

```bash
bash update.sh
```

Or manually:

```bash
docker compose down
docker compose up -d --build
```
