# DockerManager

Interfaz web completa para gestionar Docker desde el navegador. Sin frameworks frontend, sin dependencias externas en runtime. Corre como contenedor Docker con login seguro, terminal interactiva, gráficas en tiempo real y soporte PWA.

![Python](https://img.shields.io/badge/Python-3.12-blue) ![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green) ![Docker](https://img.shields.io/badge/Docker-SDK-blue)

---

## Inicio rápido

```bash
git clone <tu-repo>
cd DockerManager
docker compose up -d --build
```

Accede en `http://<IP>:3000`

La primera vez que abras la app te pedirá crear un usuario y contraseña. Esas credenciales se guardan de forma persistente en un volumen Docker (`dockermanager-data`) — no hace falta tocar ningún fichero ni variable de entorno.

---

## Actualizar

```bash
bash update.sh
```

O manualmente:

```bash
docker compose down
docker compose up -d --build
```

---

## Login y seguridad

### Primer arranque
Al abrir la app por primera vez aparece un modal de **Configuración inicial** donde introduces usuario y contraseña. La contraseña se almacena hasheada con **bcrypt (coste 12)** en `/data/config.json` dentro del volumen Docker. A partir de ahí la app queda protegida.

### Funcionalidades de autenticación
- **Login con usuario y contraseña** — cookie de sesión `httponly`, `SameSite=Strict`
- **Cambiar contraseña** — botón en la barra superior, requiere la contraseña actual
- **Cerrar sesión** — botón *Salir* en el header
- **Sesión persistente** — el token de sesión se regenera en cada login (anti session-fixation)

### Protecciones contra ataques
| Amenaza | Medida |
|---|---|
| XSS | Función `esc()` escapa todo el contenido dinámico; cabeceras CSP |
| SQL Injection | No hay base de datos SQL; se usa JSON en volumen Docker |
| Brute force | 5 intentos fallidos → bloqueo 15 minutos por IP |
| Session hijacking | Cookie `httponly` + `SameSite=Strict`; token rotado en cada login |
| Clickjacking | `X-Frame-Options: DENY` |
| MIME sniffing | `X-Content-Type-Options: nosniff` |
| Timing attacks | `secrets.compare_digest()` para comparar credenciales |
| CSRF | Cookie `SameSite=Strict` + endpoints JSON-only |

---

## Funcionalidades

### Contenedores

| Acción | Descripción |
|---|---|
| Listar | Tabla con nombre, imagen, estado, puertos y mini-stats inline |
| Iniciar / Detener | Botones por contenedor con feedback inmediato |
| Reiniciar | Reinicio con un clic |
| Pausar / Reanudar | `docker pause` / `docker unpause` |
| Eliminar | Con confirmación previa |
| Crear | Formulario completo desde la UI (ver abajo) |
| Filtrar | Búsqueda por nombre en tiempo real en la cabecera de la tabla |
| Puertos clicables | Links directos al servicio expuesto en el navegador |
| Mini-stats inline | CPU% y RAM visibles en la tabla, actualizadas automáticamente |

#### Crear contenedor
Botón **+ Nuevo** en el header. Permite configurar:
- Imagen, nombre, comando personalizado
- Política de reinicio (`unless-stopped`, `always`, `on-failure`, `no`)
- Red Docker a la que conectar
- **Puertos** (`8080:80`) — filas dinámicas, añade y elimina con un clic
- **Variables de entorno** (`KEY=VALUE`) — filas dinámicas
- **Volúmenes** (`/host:/contenedor`) — filas dinámicas

### Logs

- Stream de logs en tiempo real via **WebSocket** en un modal
- **Autoscroll** activable/desactivable con checkbox
- **Limpiar** el visor de logs
- **Descargar** el log completo del contenedor como fichero `.log` con timestamps

### Stats en tiempo real

Modal **Stats** con:
- Barra de CPU% con cambio de color según carga (verde → amarillo → rojo)
- Barra de RAM% con límite del contenedor
- Red I/O: bytes recibidos y enviados acumulados
- **Gráfica histórica** de los últimos 60 puntos (1 por segundo) de CPU% y RAM% — implementada con Chart.js

### Terminal interactiva

Botón **Terminal** en contenedores running. Abre una shell dentro del contenedor directamente en el navegador:
- Detecta automáticamente `bash` o `sh`
- Soporte completo de colores ANSI y caracteres especiales
- Redimensionado dinámico al cambiar el tamaño de la ventana
- Implementado con **xterm.js 5** + FitAddon + WebSocket (`/ws/containers/{id}/exec`)

### Inspect

Modal con toda la información detallada del contenedor:
- ID, hostname, estado, fecha de creación e inicio
- Política de reinicio, límite de RAM, CPU shares
- **Redes activas** con IP, gateway y MAC de cada interfaz
- **Desconectar** el contenedor de una red con un botón
- **Conectar** a cualquier red disponible desde un selector
- Mounts: tipo, ruta de origen y destino, modo lectura/escritura
- Variables de entorno completas

### Procesos

Modal **Procesos** con la tabla equivalente a `docker top`: lista todos los procesos corriendo dentro del contenedor con PID, usuario, CPU, MEM, comando, etc. Botón **Refrescar** para actualizar.

### Alertas de contenedor caído

Notificación nativa del navegador cuando un contenedor pasa de `running` a cualquier otro estado (exited, dead, etc.). El navegador pide permiso la primera vez que se carga la app.

---

### Imágenes

| Acción | Descripción |
|---|---|
| Listar | Tabla con tags, ID, tamaño y fecha |
| Eliminar | Con confirmación previa |
| Pull | Modal para descargar cualquier imagen por tag (`nginx:latest`, `ubuntu:22.04`, etc.) |
| Historial | Tabla de todas las capas de la imagen: comando que las creó y tamaño de cada capa |

---

### Volúmenes

- Listar todos los volúmenes con nombre, driver, mountpoint y fecha
- Eliminar con confirmación

---

### Redes

- Listar todas las redes con nombre, driver, subnet, scope y número de contenedores conectados
- Eliminar con confirmación

---

### Sistema

#### Info del sistema
Botón **Sistema** en el header. Muestra:
- Hostname e IP del host con link directo al servidor
- Versión de Docker y versión de la API
- Sistema operativo, kernel y arquitectura
- CPUs y memoria total del daemon Docker
- Número de contenedores corriendo / detenidos
- Storage driver y logging driver

#### Stats del host (barra superior)
CPU%, RAM (usada/total) y disco (usado/total) del **servidor físico** — actualizados cada 4 segundos via `psutil`. Visibles siempre en la barra superior.

#### Prune
Botón en el header para limpiar de una vez:
- Contenedores parados
- Imágenes sin usar (*dangling*)
- Volúmenes sin referencias
- Redes huérfanas

Muestra los MB liberados tras el prune.

---

## PWA — Instalable como app

DockerManager incluye `manifest.json` y favicon SVG, lo que permite instalarlo como aplicación desde Chrome, Edge o Safari en móvil y desktop. En Chrome: icono de instalación en la barra de direcciones o **Menú → Instalar DockerManager**.

---

## DockerManager vs Portainer

| | **DockerManager** | **Portainer** |
|---|---|---|
| **Imagen Docker** | ~60 MB | ~250 MB |
| **Login** | Primer arranque (volumen) | Obligatorio, base de datos |
| **Multi-host** | ❌ Solo host local | ✅ |
| **Stacks / Compose UI** | ❌ | ✅ |
| **Kubernetes** | ❌ | ✅ |
| **Terminal** | ✅ xterm.js | ✅ |
| **Stats + gráfica** | ✅ Chart.js | ✅ Con más historial |
| **Alertas navegador** | ✅ Notification API | ❌ |
| **PWA instalable** | ✅ | ❌ |
| **Brute-force protection** | ✅ | ✅ |
| **Personalizable** | ✅ Es tu código | ❌ Cerrado |

---

## Stack técnico

| Capa | Tecnología |
|---|---|
| Backend | Python 3.12 + FastAPI + uvicorn |
| Docker SDK | `docker` (Python) 7.1 |
| Stats del host | `psutil` 6.1 |
| Contraseñas | `bcrypt` coste 12 |
| Logs / Stats / Terminal | WebSocket (FastAPI + websockets) |
| Terminal UI | xterm.js 5.3 + FitAddon 0.8 |
| Gráficas | Chart.js 4.4 |
| Frontend | HTML + CSS + JS vanilla (sin frameworks) |
| Deploy | Docker Compose |

---

## Estructura del proyecto

```
DockerManager/
├── main.py              # API FastAPI + WebSockets (logs, stats, exec) + auth
├── requirements.txt     # Dependencias Python
├── Dockerfile
├── docker-compose.yml
├── .env.example         # Plantilla (sin uso activo, auth está en el volumen)
├── .dockerignore
├── update.sh            # Script de actualización
└── static/
    ├── index.html       # HTML con todos los modales
    ├── style.css        # Tema oscuro GitHub-style
    ├── app.js           # Lógica frontend completa
    ├── favicon.svg      # Icono Docker SVG
    └── manifest.json    # PWA manifest
```

---

## API endpoints

### Auth
| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/api/auth/status` | Estado de autenticación (setup_needed, authenticated) |
| `POST` | `/api/auth/setup` | Crear usuario y contraseña (solo si no existe config) |
| `POST` | `/api/auth/login` | Login (rate-limited: 5 intentos / 15 min) |
| `POST` | `/api/auth/logout` | Cerrar sesión |
| `POST` | `/api/auth/change-password` | Cambiar contraseña |

### Contenedores
| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/api/containers` | Listar todos |
| `POST` | `/api/containers/create` | Crear contenedor |
| `POST` | `/api/containers/{id}/start` | Iniciar |
| `POST` | `/api/containers/{id}/stop` | Detener |
| `POST` | `/api/containers/{id}/restart` | Reiniciar |
| `POST` | `/api/containers/{id}/pause` | Pausar |
| `POST` | `/api/containers/{id}/unpause` | Reanudar |
| `DELETE` | `/api/containers/{id}` | Eliminar |
| `GET` | `/api/containers/{id}/inspect` | Inspect completo |
| `GET` | `/api/containers/{id}/processes` | Procesos (`top`) |
| `GET` | `/api/containers/{id}/stats_once` | Stats puntuales (para mini-stats) |
| `GET` | `/api/containers/{id}/logs/download` | Descargar logs como fichero |
| `POST` | `/api/containers/{id}/networks/connect` | Conectar a red |
| `POST` | `/api/containers/{id}/networks/disconnect` | Desconectar de red |

### Imágenes
| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/api/images` | Listar |
| `DELETE` | `/api/images/{id}` | Eliminar |
| `POST` | `/api/images/pull` | Pull por tag |
| `GET` | `/api/images/{id}/history` | Historial de capas |

### Volúmenes / Redes / Sistema
| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/api/volumes` | Listar volúmenes |
| `DELETE` | `/api/volumes/{name}` | Eliminar volumen |
| `GET` | `/api/networks` | Listar redes |
| `DELETE` | `/api/networks/{id}` | Eliminar red |
| `GET` | `/api/system/info` | Info Docker + host |
| `GET` | `/api/system/host` | CPU/RAM/disco del host |
| `POST` | `/api/system/prune` | Prune global |

### WebSockets
| Ruta | Descripción |
|---|---|
| `WS /ws/containers/{id}/logs` | Stream de logs en tiempo real |
| `WS /ws/containers/{id}/stats` | Stats CPU/RAM/red cada ~1s |
| `WS /ws/containers/{id}/exec` | Terminal interactiva (shell) |
