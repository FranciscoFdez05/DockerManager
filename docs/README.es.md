# DockerManager

> [🇬🇧 English](../README.md) | 🇪🇸 Español

Interfaz web ligera para gestionar Docker desde el navegador. Construida con FastAPI y JavaScript puro — sin frameworks de frontend, sin dependencias de runtime externas.

![Python](https://img.shields.io/badge/Python-3.12-blue) ![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green) ![Docker](https://img.shields.io/badge/Docker-SDK-blue)

---

## Inicio Rápido

```bash
git clone https://github.com/FranciscoFdez05/DockerManager.git
cd DockerManager
docker compose up -d --build
```

Abre `http://<ip-del-host>:3000` en el navegador. En el primer inicio se te pedirá crear una cuenta de administrador. Las credenciales se almacenan de forma persistente en un volumen Docker (`dockermanager-data`).

---

## Funcionalidades

### Contenedores
- Lista todos los contenedores con nombre, imagen, estado, puertos y estadísticas en vivo de CPU/RAM
- Iniciar, detener, reiniciar, pausar y reanudar contenedores
- Crear contenedores desde la UI: imagen, nombre, comando, política de reinicio, puertos, variables de entorno, volúmenes y red
- Streaming de logs en tiempo real con desplazamiento automático, limpiar y descargar como `.log`
- Terminal interactiva (xterm.js) con detección automática de `bash`/`sh` y redimensionado dinámico
- Modal de estadísticas en vivo: CPU%, RAM%, I/O de red y gráfico deslizante de 60 puntos (Chart.js)
- Modal de inspección: interfaces de red, montajes, entorno, política de reinicio y límites de recursos
- Lista de procesos (`docker top`) con actualización manual
- Notificaciones del navegador cuando un contenedor sale del estado `running`

### Imágenes
- Lista imágenes con etiqueta, ID, tamaño y fecha de creación
- Descargar imágenes por etiqueta
- Eliminar imágenes
- Ver historial de capas

### Volúmenes y Redes
- Listar, inspeccionar y eliminar volúmenes y redes
- Conectar y desconectar contenedores de redes desde el modal de inspección

### Sistema
- Barra de estadísticas del host (CPU%, RAM, disco) actualizada cada 4 segundos vía `psutil`
- Información del demonio Docker: versión, SO, kernel, arquitectura y driver de almacenamiento
- Limpieza con un clic: contenedores detenidos, imágenes huérfanas, volúmenes sin uso y redes orphan

### Seguridad
- Cookie de sesión: `HttpOnly`, `SameSite=Strict`
- Hash de contraseñas: bcrypt (coste 12)
- Protección contra fuerza bruta: 5 intentos fallidos → bloqueo de IP por 15 minutos
- Mitigación de XSS: todo el contenido dinámico escapado; cabeceras CSP
- Anti-clickjacking: `X-Frame-Options: DENY`
- Prevención de sniffing MIME: `X-Content-Type-Options: nosniff`
- Comparación de credenciales en tiempo constante con `secrets.compare_digest()`

### PWA
Instalable como aplicación de escritorio o móvil desde Chrome, Edge o Safari.

---

## Stack

| Capa | Tecnología |
|---|---|
| Backend | Python 3.12, FastAPI, Uvicorn |
| Docker SDK | `docker` 7.1 |
| Estadísticas del host | `psutil` 6.1 |
| Autenticación | `bcrypt` (coste 12) |
| Tiempo real | WebSocket (logs, stats, terminal) |
| Terminal | xterm.js 5.3 + FitAddon |
| Gráficas | Chart.js 4.4 |
| Frontend | HTML + CSS + Vanilla JS |
| Despliegue | Docker Compose |

---

## Estructura del Proyecto

```
DockerManager/
├── main.py              # App FastAPI: REST API, WebSockets, autenticación
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── update.sh
├── docs/
│   └── README.es.md     # Documentación en español
└── static/
    ├── index.html
    ├── style.css
    ├── app.js
    ├── favicon.svg
    └── manifest.json
```

---

## Referencia de la API

### Autenticación

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/api/auth/status` | Estado de auth (`setup_needed`, `authenticated`) |
| `POST` | `/api/auth/setup` | Creación de cuenta inicial |
| `POST` | `/api/auth/login` | Login (limitado: 5 intentos / 15 min) |
| `POST` | `/api/auth/logout` | Logout |
| `POST` | `/api/auth/change-password` | Cambiar contraseña |

### Contenedores

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/api/containers` | Listar todos |
| `POST` | `/api/containers/create` | Crear |
| `POST` | `/api/containers/{id}/start` | Iniciar |
| `POST` | `/api/containers/{id}/stop` | Detener |
| `POST` | `/api/containers/{id}/restart` | Reiniciar |
| `POST` | `/api/containers/{id}/pause` | Pausar |
| `POST` | `/api/containers/{id}/unpause` | Reanudar |
| `DELETE` | `/api/containers/{id}` | Eliminar |
| `GET` | `/api/containers/{id}/inspect` | Inspección completa |
| `GET` | `/api/containers/{id}/processes` | Lista de procesos (top) |
| `GET` | `/api/containers/{id}/stats_once` | Estadísticas puntuales |
| `GET` | `/api/containers/{id}/logs/download` | Descargar logs |
| `POST` | `/api/containers/{id}/networks/connect` | Conectar a red |
| `POST` | `/api/containers/{id}/networks/disconnect` | Desconectar de red |

### Imágenes

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/api/images` | Listar |
| `DELETE` | `/api/images/{id}` | Eliminar |
| `POST` | `/api/images/pull` | Descargar por etiqueta |
| `GET` | `/api/images/{id}/history` | Historial de capas |

### Volúmenes, Redes y Sistema

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/api/volumes` | Listar volúmenes |
| `DELETE` | `/api/volumes/{name}` | Eliminar volumen |
| `GET` | `/api/networks` | Listar redes |
| `DELETE` | `/api/networks/{id}` | Eliminar red |
| `GET` | `/api/system/info` | Info Docker + host |
| `GET` | `/api/system/host` | CPU / RAM / disco |
| `POST` | `/api/system/prune` | Limpieza del sistema |

### WebSockets

| Ruta | Descripción |
|---|---|
| `WS /ws/containers/{id}/logs` | Stream de logs en tiempo real |
| `WS /ws/containers/{id}/stats` | Estadísticas CPU / RAM / red (~1 s de intervalo) |
| `WS /ws/containers/{id}/exec` | Shell interactiva |

---

## Actualización

```bash
bash update.sh
```

O manualmente:

```bash
docker compose down
docker compose up -d --build
```
