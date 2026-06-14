# Docker Gestor

Interfaz web ligera para gestionar Docker desde el navegador. Sin dependencias externas, sin Docker-in-Docker. Corre como un servicio systemd sobre Python/FastAPI y se conecta directamente al socket de Docker.

![Python](https://img.shields.io/badge/Python-3.10+-blue) ![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green) ![Docker](https://img.shields.io/badge/Docker-SDK-blue)

---

## Funcionalidades

### Contenedores
| Función | Descripción |
|---|---|
| Listar | Todos los contenedores (corriendo y detenidos) con estado, imagen y puertos |
| Start / Stop / Restart | Control básico del ciclo de vida |
| Pause / Unpause | Suspender y reanudar contenedores |
| Remove | Eliminar contenedor (con confirmación) |
| **Logs** | Stream en vivo por WebSocket con auto-scroll y botón de limpiar |
| **Stats** | CPU%, RAM usada/total, red RX/TX en tiempo real vía WebSocket |
| **Inspect** | Hostname, IP, redes, mounts, variables de entorno, política de reinicio, límites de recursos |
| **Procesos** | Tabla de procesos activos dentro del contenedor (`docker top`) |
| Puertos clicables | Los puertos expuestos son links directos al servicio en el navegador |
| Mini-stats inline | CPU% y RAM visibles directamente en la tabla de contenedores |

### Imágenes
- Listar con tags, tamaño y fecha de creación
- Eliminar imagen
- **Pull** de cualquier imagen desde un modal con campo de texto

### Volúmenes
- Listar con driver, punto de montaje y fecha
- Eliminar volumen

### Redes
- Listar con driver, subred, scope y número de contenedores conectados
- Eliminar red

### Sistema
- **Barra de host** en el header: CPU%, RAM y disco del servidor actualizándose cada 4 segundos
- **Modal de sistema**: versión de Docker, API version, OS, kernel, arquitectura, hostname, IP, conteo de contenedores e imágenes
- **Prune global**: elimina contenedores parados, imágenes dangling, volúmenes y redes huérfanas en un clic, con reporte de espacio liberado

---

## Requisitos

- Ubuntu 20.04+ (o cualquier Linux con systemd)
- Docker instalado y corriendo
- Python 3.10+

---

## Instalación

```bash
git clone <repo>
cd DockerManager
bash install.sh
```

El instalador:
1. Verifica que Docker y Python3 estén disponibles
2. Copia los archivos a `/opt/dockergestor`
3. Crea un virtualenv e instala las dependencias
4. Añade el usuario al grupo `docker` si es necesario
5. Instala y arranca un servicio systemd en el puerto **3000**

Al terminar muestra la dirección de acceso:
```
http://<IP-del-servidor>:3000
```

---

## Actualización

```bash
bash update.sh
```

Copia los archivos nuevos, actualiza dependencias y reinicia el servicio automáticamente.

---

## Gestión del servicio

```bash
sudo systemctl status  dockergestor
sudo systemctl restart dockergestor
sudo systemctl stop    dockergestor
sudo journalctl -u     dockergestor -f
```

---

## Stack técnico

| Capa | Tecnología |
|---|---|
| Backend | Python 3 + FastAPI + uvicorn |
| Docker SDK | `docker` (Python) |
| Stats del host | `psutil` |
| Logs/Stats live | WebSocket |
| Frontend | HTML + CSS + JS vanilla (sin frameworks) |
| Servicio | systemd |

---

## Estructura

```
DockerManager/
├── main.py          # API FastAPI (backend + WebSockets)
├── requirements.txt # Dependencias Python
├── static/
│   ├── index.html   # Estructura HTML
│   ├── style.css    # Estilos (tema oscuro GitHub-style)
│   └── app.js       # Lógica frontend (fetch + WebSocket)
├── install.sh       # Instalador para Ubuntu
└── update.sh        # Script de actualización
```
