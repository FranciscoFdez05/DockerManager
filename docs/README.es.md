# DockerManager
---
> [🇬🇧 English](../README.md) | 🇪🇸 Español

Interfaz web ligera para gestionar Docker desde el navegador. Construida con FastAPI y JavaScript puro — sin frameworks de frontend, sin dependencias de runtime externas.

![Python](https://img.shields.io/badge/Python-3.12-blue) ![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green) ![Docker](https://img.shields.io/badge/Docker-SDK-blue)

![Menú principal de DockerManager](../img/mainMenu.png)

## ✨ Características ✨
---

### Contenedores
- Lista todos los contenedores con nombre, imagen, estado, puertos y estadísticas en vivo de CPU/RAM
- Contenedores agrupados por stack de Docker Compose, con grupos plegables e inicio/reinicio/parada de un stack completo
- Iniciar, detener, reiniciar, pausar y reanudar contenedores
- Estado del healthcheck y filtro por estado
- Crear contenedores desde la UI: imagen, nombre, comando, política de reinicio, puertos, variables de entorno, volúmenes y red
- Streaming de logs en tiempo real con desplazamiento automático, limpiar y descargar como `.log`
- Terminal interactiva (xterm.js) con detección automática de `bash`/`sh` y redimensionado dinámico
- Modal de estadísticas en vivo: CPU%, RAM%, I/O de red y gráfico deslizante de 60 puntos (Chart.js)
- Modal de inspección: interfaces de red, montajes, entorno, política de reinicio y límites de recursos
- Lista de procesos (`docker top`) con actualización manual
- Notificaciones del navegador cuando un contenedor sale del estado `running`

### Imágenes
- Lista imágenes con etiqueta, ID, tamaño y fecha de creación
- Imágenes y volúmenes clasificados por propietario (stack o contenedor), con un grupo «Sin usar»
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

## 🖥️ Requisitos
---

- **Docker Engine** con el plugin Compose (`docker compose`) — la vía recomendada
- Acceso al socket de Docker (`/var/run/docker.sock`)
- Un shell POSIX (`sh`), además de `awk` y `openssl` (o `/dev/urandom`) para `docker-up.sh`
- Un puerto TCP libre en el host (por defecto `3000`)

Solo si instalas de forma nativa en vez de con Docker (`install.sh`, Ubuntu + systemd):

- Python 3.12 con `python3-venv`
- El usuario actual en el grupo `docker`

**Stack**

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

## 📦 Guía de instalación ⚙️
---

### Con Docker (recomendado)

```bash
git clone https://github.com/FranciscoFdez05/DockerManager.git
cd DockerManager
./docker-up.sh
```

`docker-up.sh` crea el `.env` con una `SECRET_KEY` recién generada, lee el puerto de escucha de `[server] port` en `config.ini` y levanta el stack publicado en todas las interfaces. Al terminar imprime la URL de la LAN.

### Instalación nativa (Ubuntu + systemd)

```bash
bash install.sh
```

Instala la app en `/opt/dockergestor` con su propio entorno virtual y registra el servicio systemd `dockergestor`.

### Configuración

Todo se ajusta en `config.ini`. Las variables de entorno (`DATA_DIR`, `PORT`, `HOST`, `DOCKER_HOST`) tienen prioridad sobre el archivo.

| Sección | Clave | Por defecto | Descripción |
|---|---|---|---|
| `server` | `port` | `3000` | Puerto de escucha |
| `server` | `bind_host` | `0.0.0.0` | Dirección de bind |
| `docker` | `data_dir` | `./data` | Dónde se guarda `config.json` (usuario + hash de contraseña) |
| `docker` | `docker_socket` | `/var/run/docker.sock` | Socket Unix de Docker |
| `security` | `session_max_age_days` | `30` | Duración de la sesión |
| `security` | `bcrypt_cost` | `12` | Coste del hash bcrypt |
| `security` | `max_login_attempts` | `5` | Intentos fallidos antes del bloqueo |
| `security` | `lockout_minutes` | `15` | Duración del bloqueo |

### Actualización

```bash
./docker-update.sh
```

O manualmente:

```bash
docker compose down
./docker-up.sh
```

## 📋 Guía de uso 🕹️
---

Abre `http://<ip-del-host>:<puerto>` desde cualquier dispositivo de la misma red (puerto `3000` por defecto; cámbialo en `config.ini` y vuelve a ejecutar el script). En el primer inicio se te pedirá crear una cuenta de administrador. Las credenciales se almacenan de forma persistente en un volumen Docker (`dockermanager-data`).

A partir de ahí:

- **Contenedores** — la tabla principal. Botones por fila para iniciar/detener/reiniciar/pausar; haz clic en un contenedor para abrir logs, terminal, estadísticas o inspección.
- **Imágenes / Volúmenes / Redes** — pestañas para listar, descargar, inspeccionar y eliminar.
- **Sistema** — información del host y del demonio, además del botón de limpieza.

Si otro equipo de la red no conecta, abre el puerto en el firewall del servidor.

### Estructura del proyecto

```
DockerManager/
├── main.py              # App FastAPI: REST API, WebSockets, autenticación
├── config.py            # Carga de config.ini + variables de entorno
├── config.ini           # Configuración editable
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── docker-up.sh         # Script de arranque (.env + puerto + compose up)
├── install.sh           # Instalador nativo (Ubuntu + systemd)
├── docker-update.sh    # Actualiza, comprueba y vuelve atrás si falla
├── docs/
│   └── README.es.md     # Documentación en español
└── static/
    ├── index.html
    ├── style.css
    ├── app.js
    ├── favicon.svg
    └── manifest.json
```

### Referencia de la API

#### Autenticación

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/api/auth/status` | Estado de auth (`setup_needed`, `authenticated`) |
| `POST` | `/api/auth/setup` | Creación de cuenta inicial |
| `POST` | `/api/auth/login` | Login (limitado: 5 intentos / 15 min) |
| `POST` | `/api/auth/logout` | Logout |
| `POST` | `/api/auth/change-password` | Cambiar contraseña |

#### Contenedores

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

#### Imágenes

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/api/images` | Listar |
| `DELETE` | `/api/images/{id}` | Eliminar |
| `POST` | `/api/images/pull` | Descargar por etiqueta |
| `GET` | `/api/images/{id}/history` | Historial de capas |

#### Volúmenes, Redes y Sistema

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/api/volumes` | Listar volúmenes |
| `DELETE` | `/api/volumes/{name}` | Eliminar volumen |
| `GET` | `/api/networks` | Listar redes |
| `DELETE` | `/api/networks/{id}` | Eliminar red |
| `GET` | `/api/system/info` | Info Docker + host |
| `GET` | `/api/system/host` | CPU / RAM / disco |
| `POST` | `/api/system/prune` | Limpieza del sistema |

#### WebSockets

| Ruta | Descripción |
|---|---|
| `WS /ws/containers/{id}/logs` | Stream de logs en tiempo real |
| `WS /ws/containers/{id}/stats` | Estadísticas CPU / RAM / red (~1 s de intervalo) |
| `WS /ws/containers/{id}/exec` | Shell interactiva |

## 🤝 Contribuciones 🤝
---

Las contribuciones son bienvenidas. Para proponer un cambio:

1. Haz un fork del repositorio y crea una rama (`git checkout -b feature/mi-mejora`).
2. Realiza tus cambios manteniendo el estilo existente (JS puro, sin nuevas dependencias de runtime).
3. Prueba con `./docker-up.sh` antes de abrir el PR.
4. Haz commit (`git commit -m "Añade mi mejora"`), push y abre un Pull Request describiendo el cambio.

Para errores o ideas, abre un [issue](https://github.com/FranciscoFdez05/DockerManager/issues).

## 📜 Licencia
---
📄 Este proyecto está licenciado bajo la Licencia MIT. Consulta el archivo `LICENSE` para más detalles.

---

**Developed with ❤️ by [Francisco](https://github.com/FranciscoFdez05)**
