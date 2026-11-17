from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from fastapi.middleware.gzip import GZipMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
import docker
import docker.errors
import asyncio
import threading
import queue as thread_queue
import json
import psutil
import socket
import os
import secrets
import select as sel
import bcrypt
from collections import defaultdict
from datetime import datetime, timezone
from functools import lru_cache
import config as cfg_ini
import actualizacion
import servidor
import limpieza
import telegrama
import avisos
import configTelegram
import ajustes
import seguridad
from clienteDocker import proveedor as dockerProveedor
from contextlib import asynccontextmanager
from version import __version__


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Arranca y detiene los hilos de Telegram junto con la app."""
    _iniciarTelegram()
    yield
    _detenerTelegram()
    dockerProveedor.cerrar()


app = FastAPI(title="DockerManager", version=__version__, lifespan=lifespan)

# ── Configuración persistente ─────────────────────────────────────────────────
DATA_DIR = cfg_ini.DATA_DIR
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
SESSION_FILE = os.path.join(DATA_DIR, "session.json")

# Caché en memoria de config.json: el middleware lo consulta en cada petición.
_credenciales = seguridad.AlmacenCredenciales(CONFIG_FILE)


def load_config() -> dict | None:
    return _credenciales.cargar()


def save_config(cfg: dict):
    _credenciales.guardar(cfg)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(cfg_ini.BCRYPT_COST)).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:  # hash vacío o corrupto
        return False


@lru_cache(maxsize=1)
def _hash_relleno() -> str:
    """Hash con el mismo coste que el real, para comparar cuando el usuario no
    existe: así el login tarda lo mismo y no revela qué usuarios son válidos."""
    return hash_password(secrets.token_hex(16))


# ── Sesión ────────────────────────────────────────────────────────────────────
# Un único token, regenerado en cada login. En DATA_DIR se guarda su HMAC con
# SECRET_KEY (la genera docker-up.sh en el .env) y su caducidad: la sesión
# sobrevive a los reinicios y el logout la invalida de verdad. Sin SECRET_KEY,
# cada reinicio cierra la sesión.
_sesion = seguridad.GestorSesion(SESSION_FILE, os.getenv("SECRET_KEY", ""), cfg_ini.SESSION_MAX_AGE_SECONDS)


def _sesion_valida(token: str | None) -> bool:
    return _sesion.valida(token)


def _new_token() -> str:
    return _sesion.crear()


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(cfg_ini.SESSION_COOKIE_NAME, token, httponly=True, samesite="strict",
                        max_age=cfg_ini.SESSION_MAX_AGE_SECONDS)


# ── Rate limiting anti-fuerza-bruta ───────────────────────────────────────────
MAX_ATTEMPTS = cfg_ini.MAX_LOGIN_ATTEMPTS
LOCKOUT_MIN = cfg_ini.LOCKOUT_MINUTES
_limitador = seguridad.LimitadorIntentos(MAX_ATTEMPTS, LOCKOUT_MIN * 60)
_proxies_confiables = seguridad.parsearProxies(cfg_ini.proxiesConfiables)


def _is_locked(ip: str) -> bool:
    return _limitador.bloqueada(ip)


def _record_fail(ip: str):
    _limitador.registrarFallo(ip)


def _clear_attempts(ip: str):
    _limitador.limpiar(ip)


def _client_ip(request: Request) -> str:
    return seguridad.ipCliente(request.client.host if request.client else None,
                               request.headers, _proxies_confiables)


# ── Middleware: cabeceras de seguridad ────────────────────────────────────────
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if not request.url.path.startswith(("/api", "/ws")):
            # Revalidar siempre los estáticos: sin esto el navegador sigue sirviendo
            # un app.js antiguo tras actualizar y la interfaz nueva no aparece.
            response.headers["Cache-Control"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        # "0" desactiva el filtro XSS heredado: el modo block permitía filtrar
        # información entre orígenes y la protección real ya la da la CSP.
        response.headers["X-XSS-Protection"] = "0"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        cdn = cfg_ini.CDN_JSDELIVR
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            f"script-src 'self' 'unsafe-inline' {cdn}; "
            f"style-src 'self' 'unsafe-inline' {cdn}; "
            f"font-src 'self' {cdn} data:; "
            "img-src 'self' data:; "
            "connect-src 'self' ws: wss:;"
        )
        return response


# ── Middleware: autenticación ─────────────────────────────────────────────────
class AuthMiddleware(BaseHTTPMiddleware):
    _PUBLIC = {"/api/auth/login", "/api/auth/status", "/api/auth/setup", "/api/health"}

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api") or path in self._PUBLIC:
            return await call_next(request)
        cfg = load_config()
        if not cfg:
            return JSONResponse({"detail": "Setup requerido"}, status_code=403)
        if not _sesion_valida(request.cookies.get(cfg_ini.SESSION_COOKIE_NAME)):
            return JSONResponse({"detail": "No autorizado"}, status_code=401)
        return await call_next(request)


app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(AuthMiddleware)
# app.js y style.css pesan decenas de KB; JSON pequeños no compensa comprimirlos.
app.add_middleware(GZipMiddleware, minimum_size=1024)


# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    """Público. Comprueba que la app puede hablar con el daemon de Docker; lo usan
    docker-update.sh y el HEALTHCHECK de la imagen para distinguir «el proceso está
    arriba» de «la aplicación funciona»."""
    try:
        dockerProveedor.obtener().ping()
    except Exception:
        return JSONResponse({"status": "error", "docker": False, "version": __version__}, status_code=503)
    return {"status": "ok", "docker": True, "version": __version__}


# ── Auth endpoints ────────────────────────────────────────────────────────────
@app.get("/api/auth/status")
def auth_status(request: Request):
    cfg = load_config()
    if not cfg:
        return {"setup_needed": True, "authenticated": False}
    authenticated = _sesion_valida(request.cookies.get(cfg_ini.SESSION_COOKIE_NAME))
    return {"setup_needed": False, "authenticated": authenticated}


@app.post("/api/auth/setup")
def auth_setup(body: dict, response: Response, request: Request):
    if load_config():
        raise HTTPException(403, "La aplicación ya está configurada")

    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    confirm  = body.get("confirm") or ""

    if len(username) < 3 or len(username) > 32:
        raise HTTPException(400, "El usuario debe tener entre 3 y 32 caracteres")
    if not username.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(400, "El usuario solo puede contener letras, números, _ y -")
    if len(password) < 8:
        raise HTTPException(400, "La contraseña debe tener al menos 8 caracteres")
    if password != confirm:
        raise HTTPException(400, "Las contraseñas no coinciden")

    # Dos setups simultáneos: solo el primero crea el usuario.
    if not _credenciales.crearSiNoExiste({"username": username, "password_hash": hash_password(password)}):
        raise HTTPException(403, "La aplicación ya está configurada")
    _avisar("seguridad", avisos.textoConfiguracionInicial(_client_ip(request)), "configuracion-inicial")
    _set_session_cookie(response, _new_token())
    return {"ok": True}


@app.post("/api/auth/login")
def auth_login(body: dict, response: Response, request: Request):
    ip = _client_ip(request)
    if _is_locked(ip):
        raise HTTPException(429, f"Demasiados intentos fallidos. Espera {LOCKOUT_MIN} minutos.")

    cfg = load_config()
    if not cfg:
        raise HTTPException(503, "Setup no completado")

    username = body.get("username") or ""
    password = body.get("password") or ""

    # Timing-safe comparison para evitar timing attacks
    username_ok = secrets.compare_digest(username.encode("utf-8"), cfg.get("username", "").encode("utf-8"))
    # bcrypt se ejecuta siempre (con un hash de relleno si el usuario no
    # coincide): si no, el tiempo de respuesta delata qué usuarios existen.
    password_ok = verify_password(password, cfg.get("password_hash", "") if username_ok else _hash_relleno())
    password_ok = password_ok and username_ok

    if not username_ok or not password_ok:
        _record_fail(ip)
        if _is_locked(ip):
            _avisar("seguridad", avisos.textoBloqueo(ip, LOCKOUT_MIN), f"bloqueo:{ip}")
        raise HTTPException(401, "Credenciales incorrectas")

    _clear_attempts(ip)
    _avisar("seguridad", avisos.textoLogin(ip), f"login:{ip}")
    token = _new_token()  # Regenerar token en cada login (anti session-fixation)
    _set_session_cookie(response, token)
    return {"ok": True}


@app.post("/api/auth/logout")
def auth_logout(response: Response):
    _sesion.invalidar()  # Invalida la sesión actual, también tras reiniciar
    response.delete_cookie(cfg_ini.SESSION_COOKIE_NAME)
    return {"ok": True}


@app.post("/api/auth/change-password")
def change_password(body: dict, request: Request, response: Response):
    cfg = load_config()
    if not cfg:
        raise HTTPException(503, "Sin configuración")

    current  = body.get("current") or ""
    new_pass = body.get("new_password") or ""
    confirm  = body.get("confirm") or ""

    if not verify_password(current, cfg["password_hash"]):
        raise HTTPException(401, "Contraseña actual incorrecta")
    if len(new_pass) < 8:
        raise HTTPException(400, "La nueva contraseña debe tener al menos 8 caracteres")
    if new_pass != confirm:
        raise HTTPException(400, "Las contraseñas no coinciden")

    cfg["password_hash"] = hash_password(new_pass)
    save_config(cfg)
    _avisar("seguridad", avisos.textoCambioPassword(_client_ip(request)), "cambio-password")
    # Token nuevo: si alguien había robado la cookie anterior, deja de valer.
    _set_session_cookie(response, _new_token())
    return {"ok": True}


# ── Helpers ───────────────────────────────────────────────────────────────────
def get_docker():
    try:
        return dockerProveedor.obtener()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Docker no disponible: {e}")


def get_container(client, container_id: str):
    try:
        return client.containers.get(container_id)
    except docker.errors.NotFound:
        raise HTTPException(404, "Contenedor no encontrado")


def _ws_auth(websocket: WebSocket) -> bool:
    # Origin primero: el exec da una shell en los contenedores y no debe poder
    # abrirlo otra web aprovechando la cookie (Cross-Site WebSocket Hijacking).
    if not seguridad.origenPermitido(websocket.headers.get("origin"), websocket.headers.get("host")):
        return False
    cfg = load_config()
    if not cfg:
        return False
    return _sesion_valida(websocket.cookies.get(cfg_ini.SESSION_COOKIE_NAME))


# ── Uso de recursos por contenedor ────────────────────────────────────────────
PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"
_HEALTH_MARKS = (("(healthy)", "healthy"), ("(unhealthy)", "unhealthy"), ("(health: starting)", "starting"))


def _container_image_name(raw: dict) -> str:
    """Nombre de imagen; si se borró la etiqueta, Docker da el sha256 y se acorta."""
    name = raw.get("Image") or ""
    if not name or name.startswith("sha256:"):
        name = (raw.get("ImageID") or "")[7:19]
    return name


def _container_health(status: str) -> str | None:
    # El listado no trae State.Health; Docker lo añade al texto de Status.
    for mark, health in _HEALTH_MARKS:
        if mark in status:
            return health
    return None


def _container_info(raw: dict) -> dict:
    """Resumen de un contenedor a partir de una entrada de ``GET /containers/json``.

    Se trabaja con el listado en bruto en vez de con ``containers.list()``,
    que hace un ``inspect`` por contenedor (N+1 llamadas al daemon en cada
    refresco de la interfaz).
    """
    labels = raw.get("Labels") or {}
    ports: dict[str, set] = defaultdict(set)
    for p in raw.get("Ports") or []:
        if p.get("PublicPort"):
            ports[f"{p['PrivatePort']}/{p.get('Type', 'tcp')}"].add(str(p["PublicPort"]))
    names = raw.get("Names") or []
    created = raw.get("Created")
    return {
        "id": raw["Id"][:12],
        "name": names[0].lstrip("/") if names else raw["Id"][:12],
        "image": _container_image_name(raw),
        "image_id": (raw.get("ImageID") or "").replace("sha256:", "")[:12],
        "status": raw.get("State") or "unknown",
        "health": _container_health(raw.get("Status") or ""),
        "project": labels.get(PROJECT_LABEL),
        "service": labels.get(SERVICE_LABEL),
        "created": datetime.fromtimestamp(created, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                   if isinstance(created, (int, float)) else "",
        "ports": {k: sorted(v) for k, v in ports.items()},
        "volumes": [m["Name"] for m in raw.get("Mounts") or [] if m.get("Type") == "volume" and m.get("Name")],
    }


def _all_container_infos(client) -> list[dict]:
    infos = []
    for raw in client.api.containers(all=True):
        try:
            infos.append(_container_info(raw))
        except (KeyError, TypeError, ValueError):
            continue  # un contenedor corrupto no debe tumbar todo el listado
    return infos


def _user_ref(ci: dict) -> dict:
    return {"name": ci["name"], "project": ci["project"], "status": ci["status"]}


# ── Containers ────────────────────────────────────────────────────────────────
@app.get("/api/containers")
def list_containers():
    return _all_container_infos(get_docker())


@app.post("/api/projects/{project}/{op}")
def project_action(project: str, op: str):
    """Acción masiva sobre todos los contenedores de un stack de compose."""
    if op not in ("start", "stop", "restart"):
        raise HTTPException(400, "Operación inválida")
    client = get_docker()
    members = client.containers.list(all=True, filters={"label": f"{PROJECT_LABEL}={project}"})
    if not members:
        raise HTTPException(404, "Stack no encontrado")
    errors = []
    for c in members:
        try:
            getattr(c, op)()
        except Exception as e:
            errors.append(f"{c.name}: {e}")
    if errors:
        raise HTTPException(500, "; ".join(errors))
    return {"status": op, "containers": len(members)}


_PORT_PROTOCOLS = {"tcp", "udp", "sctp"}
_RESTART_POLICIES = {"no", "always", "unless-stopped", "on-failure"}


def _parse_port(value: str) -> int:
    if not value.isdigit() or not 1 <= int(value) <= 65535:
        raise HTTPException(400, f"Puerto inválido: '{value}' (debe ser un número entre 1 y 65535)")
    return int(value)


def _parse_port_binding(spec: str) -> tuple[str, int] | None:
    """Convierte ``"8080:80/tcp"`` en ``("80/tcp", 8080)``.

    Raises:
        HTTPException: 400 si el puerto o el protocolo no son válidos (antes un
            puerto no numérico acababa en un 500 genérico).
    """
    parts = spec.split(":")
    if len(parts) != 2:
        return None
    host_port, container_port = parts
    proto = "tcp"
    if "/" in container_port:
        container_port, proto = container_port.split("/", 1)
    if proto not in _PORT_PROTOCOLS:
        raise HTTPException(400, f"Protocolo inválido: '{proto}' (tcp, udp o sctp)")
    return f"{_parse_port(container_port)}/{proto}", _parse_port(host_port)


@app.post("/api/containers/create")
def create_container(body: dict):
    client = get_docker()
    image = (body.get("image") or "").strip()
    if not image or len(image) > 256:
        raise HTTPException(400, "Imagen inválida")
    restart_policy = body.get("restart_policy", "no")
    if restart_policy not in _RESTART_POLICIES:
        raise HTTPException(400, "Política de reinicio inválida")
    try:
        port_bindings = {}
        for p in body.get("ports", [])[:20]:
            p = str(p).strip()
            if not p or len(p) > 30:
                continue
            binding = _parse_port_binding(p)
            if binding:
                port_bindings[binding[0]] = binding[1]

        volumes = {}
        for v in body.get("volumes", [])[:20]:
            v = str(v).strip()
            if not v or len(v) > 512:
                continue
            parts = v.split(":")
            if len(parts) >= 2:
                mode = parts[2] if len(parts) > 2 else "rw"
                volumes[parts[0]] = {"bind": parts[1], "mode": mode}

        env = [str(e).strip() for e in body.get("env", [])[:50] if str(e).strip() and len(str(e)) < 512]
        network = (body.get("network") or "").strip() or None
        command = (body.get("command") or "").strip() or None
        name    = (body.get("name") or "").strip() or None

        c = client.containers.run(
            image=image,
            name=name,
            ports=port_bindings if port_bindings else None,
            environment=env if env else None,
            volumes=volumes if volumes else None,
            restart_policy={"Name": restart_policy},
            network=network,
            command=command,
            detach=True,
        )
        return {"status": "created", "id": c.short_id, "name": c.name}
    except HTTPException:
        raise
    except docker.errors.ImageNotFound:
        raise HTTPException(404, f"Imagen '{image}' no encontrada. Haz pull primero.")
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/containers/{container_id}/start")
def start_container(container_id: str):
    client = get_docker()
    try:
        get_container(client, container_id).start()
        return {"status": "started"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/containers/{container_id}/stop")
def stop_container(container_id: str):
    client = get_docker()
    try:
        get_container(client, container_id).stop()
        return {"status": "stopped"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/containers/{container_id}/restart")
def restart_container(container_id: str):
    client = get_docker()
    try:
        get_container(client, container_id).restart()
        return {"status": "restarted"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/containers/{container_id}/pause")
def pause_container(container_id: str):
    client = get_docker()
    try:
        get_container(client, container_id).pause()
        return {"status": "paused"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/containers/{container_id}/unpause")
def unpause_container(container_id: str):
    client = get_docker()
    try:
        get_container(client, container_id).unpause()
        return {"status": "unpaused"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.delete("/api/containers/{container_id}")
def remove_container(container_id: str):
    client = get_docker()
    try:
        get_container(client, container_id).remove(force=True)
        return {"status": "removed"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/containers/{container_id}/inspect")
def inspect_container(container_id: str):
    client = get_docker()
    c = get_container(client, container_id)
    a = c.attrs
    net = a.get("NetworkSettings", {})
    networks = {}
    for name, cfg in (net.get("Networks") or {}).items():
        networks[name] = {
            "ip": cfg.get("IPAddress", ""),
            "gateway": cfg.get("Gateway", ""),
            "mac": cfg.get("MacAddress", ""),
        }
    mounts = [
        {"type": m.get("Type"), "source": m.get("Source", ""),
         "destination": m.get("Destination", ""), "mode": m.get("Mode", ""), "rw": m.get("RW", True)}
        for m in (a.get("Mounts") or [])
    ]
    cfg = a.get("Config", {})
    host_cfg = a.get("HostConfig", {})
    return {
        "id": a["Id"][:12], "name": a["Name"].lstrip("/"),
        "image": cfg.get("Image", ""),
        "created": a.get("Created", "")[:19].replace("T", " "),
        "status": a["State"]["Status"],
        "started_at": a["State"].get("StartedAt", "")[:19].replace("T", " "),
        "restart_policy": host_cfg.get("RestartPolicy", {}).get("Name", "no"),
        "hostname": cfg.get("Hostname", ""),
        "env": cfg.get("Env") or [], "cmd": cfg.get("Cmd") or [],
        "networks": networks, "mounts": mounts,
        "cpu_shares": host_cfg.get("CpuShares", 0),
        "memory_limit": host_cfg.get("Memory", 0),
    }


@app.get("/api/containers/{container_id}/processes")
def container_processes(container_id: str):
    client = get_docker()
    c = get_container(client, container_id)
    if c.status != "running":
        raise HTTPException(400, "El contenedor no está corriendo")
    try:
        top = c.top()
        return {"titles": top.get("Titles", []), "processes": top.get("Processes", [])}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/containers/{container_id}/stats_once")
def container_stats_once(container_id: str):
    client = get_docker()
    c = get_container(client, container_id)
    if c.status != "running":
        return {"cpu_pct": 0, "mem_usage": 0, "mem_limit": 0, "mem_pct": 0, "net_rx": 0, "net_tx": 0}
    try:
        return _parse_stats(c.stats(stream=False))
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/containers/{container_id}/logs/download")
def download_logs(container_id: str):
    client = get_docker()
    c = get_container(client, container_id)
    try:
        logs = c.logs(stream=False, timestamps=True).decode("utf-8", errors="replace")
        from fastapi.responses import Response as FResponse
        return FResponse(
            content=logs, media_type="text/plain",
            headers={"Content-Disposition": f'attachment; filename="{c.name}.log"'},
        )
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/containers/{container_id}/networks/connect")
def network_connect(container_id: str, body: dict):
    client = get_docker()
    network_name = (body.get("network") or "").strip()
    if not network_name:
        raise HTTPException(400, "Se requiere 'network'")
    try:
        client.networks.get(network_name).connect(container_id)
        return {"status": "connected"}
    except docker.errors.NotFound:
        raise HTTPException(404, "Red no encontrada")
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/containers/{container_id}/networks/disconnect")
def network_disconnect(container_id: str, body: dict):
    client = get_docker()
    network_name = (body.get("network") or "").strip()
    if not network_name:
        raise HTTPException(400, "Se requiere 'network'")
    try:
        client.networks.get(network_name).disconnect(container_id)
        return {"status": "disconnected"}
    except docker.errors.NotFound:
        raise HTTPException(404, "Red no encontrada")
    except Exception as e:
        raise HTTPException(500, str(e))


def _parse_stats(s: dict) -> dict:
    cpu_delta = s["cpu_stats"]["cpu_usage"]["total_usage"] - s["precpu_stats"]["cpu_usage"]["total_usage"]
    sys_delta = s["cpu_stats"].get("system_cpu_usage", 0) - s["precpu_stats"].get("system_cpu_usage", 0)
    num_cpus  = s["cpu_stats"].get("online_cpus") or len(s["cpu_stats"]["cpu_usage"].get("percpu_usage", [1]))
    cpu_pct   = (cpu_delta / sys_delta * num_cpus * 100.0) if sys_delta > 0 else 0.0
    mem       = s.get("memory_stats", {})
    mem_usage = mem.get("usage", 0) - mem.get("stats", {}).get("cache", 0)
    mem_limit = mem.get("limit", 1)
    net_rx, net_tx = 0, 0
    for iface in (s.get("networks") or {}).values():
        net_rx += iface.get("rx_bytes", 0)
        net_tx += iface.get("tx_bytes", 0)
    return {
        "cpu_pct": round(cpu_pct, 2),
        "mem_usage": mem_usage, "mem_limit": mem_limit,
        "mem_pct": round((mem_usage / mem_limit * 100) if mem_limit else 0, 2),
        "net_rx": net_rx, "net_tx": net_tx,
    }


# ── Images ────────────────────────────────────────────────────────────────────
@app.get("/api/images")
def list_images():
    client = get_docker()
    users: dict[str, list] = defaultdict(list)
    for ci in _all_container_infos(client):
        users[ci["image_id"]].append(_user_ref(ci))
    result = []
    for img in client.images.list():
        sid = img.id.replace("sha256:", "")[:12]
        result.append({
            "id": sid, "tags": img.tags,
            "size_mb": round(img.attrs["Size"] / (1024**2), 1), "created": img.attrs["Created"][:10],
            "used_by": users.get(sid, []),
        })
    return result


@app.delete("/api/images/{image_id}")
def remove_image(image_id: str, force: bool = False):
    client = get_docker()
    try:
        client.images.remove(image_id, force=force)
        return {"status": "removed"}
    except docker.errors.ImageNotFound:
        raise HTTPException(404, "Imagen no encontrada")
    except docker.errors.APIError as e:
        raise HTTPException(409 if e.status_code == 409 else 500, e.explanation or str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/images/pull")
def pull_image(body: dict):
    tag = (body.get("tag") or "").strip()
    if not tag or len(tag) > 256:
        raise HTTPException(400, "Tag inválido")
    try:
        get_docker().images.pull(tag)
        return {"status": "pulled", "tag": tag}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/images/{image_id}/history")
def image_history(image_id: str):
    client = get_docker()
    try:
        return [
            {"created_by": (h.get("CreatedBy") or "").replace("/bin/sh -c #(nop) ", "").strip()[:120],
             "size": h.get("Size", 0), "tags": h.get("Tags") or []}
            for h in client.api.history(image_id)
        ]
    except docker.errors.ImageNotFound:
        raise HTTPException(404, "Imagen no encontrada")
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Volumes ───────────────────────────────────────────────────────────────────
@app.get("/api/volumes")
def list_volumes():
    client = get_docker()
    users: dict[str, list] = defaultdict(list)
    for ci in _all_container_infos(client):
        for vol in ci["volumes"]:
            users[vol].append(_user_ref(ci))
    return [
        {"name": v.name, "driver": v.attrs.get("Driver", ""),
         "mountpoint": v.attrs.get("Mountpoint", ""),
         "created": (v.attrs.get("CreatedAt") or "")[:10], "labels": v.attrs.get("Labels") or {},
         "used_by": users.get(v.name, [])}
        for v in client.volumes.list()
    ]


@app.delete("/api/volumes/{volume_name}")
def remove_volume(volume_name: str):
    client = get_docker()
    try:
        client.volumes.get(volume_name).remove()
        return {"status": "removed"}
    except docker.errors.NotFound:
        raise HTTPException(404, "Volumen no encontrado")
    except docker.errors.APIError as e:
        raise HTTPException(409 if e.status_code == 409 else 500, e.explanation or str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Networks ──────────────────────────────────────────────────────────────────
@app.get("/api/networks")
def list_networks():
    client = get_docker()
    result = []
    for n in client.networks.list():
        a = n.attrs
        ipam = a.get("IPAM", {}).get("Config") or []
        result.append({
            "id": a["Id"][:12], "name": n.name, "driver": a.get("Driver", ""),
            "scope": a.get("Scope", ""), "subnet": ipam[0].get("Subnet", "") if ipam else "",
            "internal": a.get("Internal", False), "containers": len(a.get("Containers") or {}),
        })
    return result


@app.delete("/api/networks/{network_id}")
def remove_network(network_id: str):
    client = get_docker()
    try:
        client.networks.get(network_id).remove()
        return {"status": "removed"}
    except docker.errors.NotFound:
        raise HTTPException(404, "Red no encontrada")
    except Exception as e:
        raise HTTPException(500, str(e))


# ── System ────────────────────────────────────────────────────────────────────
@app.get("/api/system/info")
def system_info():
    client = get_docker()
    try:
        info = client.info()
        version = client.version()
        host_ip = ""
        try:
            host_ip = socket.gethostbyname(socket.gethostname())
        except Exception:
            pass
        return {
            "app_version": __version__,
            "docker_version": version.get("Version", ""), "api_version": version.get("ApiVersion", ""),
            "os": info.get("OperatingSystem", ""), "kernel": info.get("KernelVersion", ""),
            "arch": info.get("Architecture", ""), "hostname": info.get("Name", ""),
            "host_ip": host_ip, "cpus": info.get("NCPU", 0),
            "memory_gb": round(info.get("MemTotal", 0) / (1024**3), 2),
            "containers_running": info.get("ContainersRunning", 0),
            "containers_stopped": info.get("ContainersStopped", 0),
            "containers_paused": info.get("ContainersPaused", 0),
            "images": info.get("Images", 0),
            "storage_driver": info.get("Driver", ""), "logging_driver": info.get("LoggingDriver", ""),
        }
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/system/host")
def host_stats():
    cpu  = servidor.lector.porcentaje_cpu_total()
    mem  = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    return {
        "cpu_pct": cpu, "mem_total": mem.total, "mem_used": mem.used, "mem_pct": mem.percent,
        "disk_total": disk.total, "disk_used": disk.used,
        "disk_pct": round(disk.used / disk.total * 100, 1),
        "cpu_temp": servidor.lector.temperatura_cpu(),
        "temp_warn": cfg_ini.servidorTempAviso, "temp_crit": cfg_ini.servidorTempCritica,
    }


@app.get("/api/system/metrics")
def host_metrics():
    """Métricas detalladas del host para el panel «Servidor» (ver servidor.py)."""
    try:
        return servidor.lector.leer()
    except OSError as e:
        raise HTTPException(500, f"No se pudieron leer las métricas del servidor: {e}")


@app.post("/api/system/prune")
def system_prune():
    client = get_docker()
    try:
        c_r = client.containers.prune()
        i_r = client.images.prune(filters={"dangling": True})
        v_r = client.volumes.prune()
        n_r = client.networks.prune()
        freed = (c_r.get("SpaceReclaimed") or 0) + (i_r.get("SpaceReclaimed") or 0) + (v_r.get("SpaceReclaimed") or 0)
        return {
            "status": "ok", "freed_mb": round(freed / (1024**2), 1),
            "containers_deleted": len(c_r.get("ContainersDeleted") or []),
            "images_deleted": len(i_r.get("ImagesDeleted") or []),
            "volumes_deleted": len(v_r.get("VolumesDeleted") or []),
            "networks_deleted": len(n_r.get("NetworksDeleted") or []),
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Limpieza selectiva ────────────────────────────────────────────────────────
# Ver limpieza.py: imágenes y volúmenes que no usa ningún contenedor y tienen
# cierta antigüedad. GET devuelve la vista previa; POST borra.
def _cleanup_days(dias: int | None) -> int:
    try:
        return limpieza.validarDias(cfg_ini.CLEANUP_DEFAULT_DAYS if dias is None else dias,
                                    cfg_ini.CLEANUP_MAX_DAYS)
    except limpieza.LimpiezaInvalida as e:
        raise HTTPException(400, str(e))


@app.get("/api/limpieza")
def cleanup_preview(dias: int | None = None):
    try:
        return limpieza.candidatos(get_docker(), _cleanup_days(dias))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/limpieza")
def cleanup_run(body: dict):
    dias = body.get("dias")
    if dias is not None and not isinstance(dias, int):
        raise HTTPException(400, "'dias' debe ser un número entero")
    tipos = body.get("tipos")
    if not isinstance(tipos, list):
        raise HTTPException(400, "'tipos' debe ser una lista")
    try:
        return {"status": "ok", **limpieza.limpiar(get_docker(), _cleanup_days(dias), set(tipos))}
    except limpieza.LimpiezaInvalida as e:
        raise HTTPException(400, str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Actualización ─────────────────────────────────────────────────────────────
# El desplegable de la versión de la cabecera. Ver actualizacion.py: la app solo
# comprueba y deja la petición; actualiza el vigilante del host.
@app.get("/api/actualizacion")
def update_status(refrescar: bool = False):
    return {"ok": True, **actualizacion.estado(), "remota": actualizacion.version_remota(forzar=refrescar)}


@app.post("/api/actualizacion")
def update_request():
    try:
        return {"ok": True, **actualizacion.solicitar()}
    except actualizacion.VigilanteAusente as e:
        raise HTTPException(409, str(e))
    except OSError as e:
        raise HTTPException(500, f"No se pudo dejar la petición para el vigilante: {e}")


# ── Telegram ──────────────────────────────────────────────────────────────────
# Ver telegrama.py (conexión, lista blanca), avisos.py (qué se avisa) y
# configTelegram.py (de dónde sale la configuración: .env > web > config.ini).
# Sin token e IDs no arranca nada.
_telegram: telegrama.ServicioTelegram | None = None
_vigilancia: avisos.Vigilancia | None = None
_almacenTelegram = configTelegram.AlmacenTelegram(os.path.join(DATA_DIR, configTelegram.nombreFichero))
# Serializa guardar + reconfigurar: dos «Guardar» a la vez dejarían hilos huérfanos.
_cerrojoTelegram = threading.Lock()


def _avisar(categoria: str, texto: str, clave: str | None = None) -> None:
    """Encola un aviso por Telegram si está activado. Nunca bloquea ni lanza."""
    if _telegram is not None:
        _telegram.avisar(categoria, texto, clave)


def _disco_pct() -> float:
    return psutil.disk_usage(cfg_ini.servidorRutaDisco).percent


def _configTelegram() -> configTelegram.ConfigTelegram:
    return configTelegram.efectiva(cfg_ini, _almacenTelegram.leer())


def _construirTelegram(anterior: telegrama.ServicioTelegram | None = None) -> None:
    """Construye el servicio de Telegram y los hilos de vigilancia de lo que esté activado.

    Args:
        anterior: Servicio recién detenido al reconfigurar (ver ``ServicioTelegram.iniciar``).
    """
    global _telegram, _vigilancia
    config = _configTelegram()
    comandos = avisos.crearComandos(__version__, lambda: _all_container_infos(dockerProveedor.obtener()), host_stats)
    _telegram = telegrama.construir(config.token, config.idsTexto, config.categoriasTexto, comandos,
                                    config.timeoutSegundos)
    if _telegram is None:
        return
    _telegram.iniciar(anterior)

    _vigilancia = avisos.Vigilancia(_avisar, config.intervaloSegundos)
    # Solo se registra lo activado: VigilanteActualizaciones marca como avisado
    # lo que ve, y con la categoría apagada se perdería el aviso al encenderla.
    if _telegram.acepta("contenedores"):
        _vigilancia.vigilarEventos(
            lambda: docker.from_env().events(decode=True, filters={"type": "container"}),
            avisos.ProcesadorEventosDocker(config.reiniciosBucle, config.ventanaBucleMinutos * 60),
        )
    if _telegram.acepta("recursos"):
        _vigilancia.comprobarPeriodicamente("recursos", avisos.VigilanteRecursos(
            servidor.lector.temperatura_cpu, _disco_pct, cfg_ini.servidorTempCritica, config.discoAvisoPct,
        ).comprobar)
    if _telegram.acepta("actualizaciones"):
        _vigilancia.comprobarPeriodicamente("actualizaciones", avisos.VigilanteActualizaciones(
            actualizacion.version_remota, actualizacion.estado,
            avisos.EstadoPersistente(os.path.join(DATA_DIR, "telegram.json")), __version__,
        ).comprobar)
    _vigilancia.iniciar()


def _iniciarTelegram() -> None:
    """Arranque de la app: servicio de Telegram y aviso de arranque."""
    with _cerrojoTelegram:
        _construirTelegram()
    _avisar("sistema", avisos.textoArranque(__version__, socket.gethostname()), "arranque")


def _detenerTelegram() -> None:
    if _vigilancia is not None:
        _vigilancia.detener()
    if _telegram is not None:
        _telegram.detener()


def _reconfigurarTelegram(antes: configTelegram.ConfigTelegram, ip: str) -> None:
    """Sustituye el servicio en marcha por uno con la configuración guardada ahora.

    Si cambian el token o los IDs, se avisa por el servicio *anterior*: así
    quien deja de recibir avisos se entera de que alguien lo ha cambiado.
    Llamar con ``_cerrojoTelegram`` adquirido.
    """
    global _telegram, _vigilancia
    anterior = _telegram
    if anterior is not None and antes.credenciales() != _configTelegram().credenciales():
        anterior.notificarSiempre(avisos.textoCambioTelegram(ip), "cambio-telegram")
    _detenerTelegram()
    _telegram, _vigilancia = None, None
    _construirTelegram(anterior)


def _comprobarTokenTelegram(token: str, timeout: int) -> str:
    return telegrama.ClienteTelegram(token, timeout).obtenerBot()


# ── Ajustes ───────────────────────────────────────────────────────────────────
# Pantalla «Ajustes»: solo lectura de lo que hay en config.ini/.env (ver
# ajustes.py) y la prueba de Telegram. Cambiar valores sigue siendo editar
# esos archivos y reiniciar.
@app.get("/api/ajustes")
def settings_summary():
    usuario = (load_config() or {}).get("username")
    config = _configTelegram()
    datos = ajustes.resumen(configTelegram.vistaConfig(cfg_ini, config), usuario, _telegram is not None)
    datos["telegram"].update(configTelegram.detalles(config, bool(_almacenTelegram.leer())))
    return {"ok": True, "version": __version__, **datos}


@app.put("/api/ajustes/telegram")
def settings_telegram_update(body: dict, request: Request):
    """Guarda la configuración de Telegram desde la web y la aplica sin reiniciar.

    Solo se cambian los campos enviados. El token se valida contra Telegram
    (``getMe``) antes de guardarlo y nunca se devuelve.
    """
    with _cerrojoTelegram:
        antes = _configTelegram()
        try:
            nuevo = configTelegram.aplicarCambios(
                body, _almacenTelegram.leer(), cfg_ini,
                lambda token: _comprobarTokenTelegram(token, antes.timeoutSegundos))
        except telegrama.ConfiguracionInvalida as e:
            raise HTTPException(400, str(e))
        try:
            _almacenTelegram.guardar(nuevo)
        except OSError as e:
            raise HTTPException(500, f"No se pudo guardar la configuración de Telegram: {e}")
        _reconfigurarTelegram(antes, _client_ip(request))
        return {"ok": True, "activo": _telegram is not None}


@app.delete("/api/ajustes/telegram")
def settings_telegram_reset(request: Request):
    """Borra lo guardado desde la web: vuelven a mandar el .env y config.ini."""
    with _cerrojoTelegram:
        antes = _configTelegram()
        try:
            _almacenTelegram.borrar()
        except OSError as e:
            raise HTTPException(500, f"No se pudo borrar la configuración de Telegram: {e}")
        _reconfigurarTelegram(antes, _client_ip(request))
        return {"ok": True, "activo": _telegram is not None}


@app.post("/api/ajustes/telegram/prueba")
def settings_telegram_test():
    if _telegram is None:
        raise HTTPException(409, "Telegram no está activo: revisa TELEGRAM_BOT_TOKEN y TELEGRAM_IDS_PERMITIDOS en el .env")
    if not _telegram.enviarPrueba(ajustes.textoPrueba(__version__, socket.gethostname())):
        raise HTTPException(429, "No se pudo encolar la prueba: hay demasiados avisos pendientes")
    return {"ok": True}


# ── WebSockets ────────────────────────────────────────────────────────────────
@app.websocket("/ws/containers/{container_id}/logs")
async def container_logs_ws(websocket: WebSocket, container_id: str):
    await websocket.accept()
    if not _ws_auth(websocket):
        await websocket.close(code=4001)
        return
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()
    stop_event = threading.Event()

    def produce():
        client = None
        try:
            # Cliente propio: el seguimiento retiene la conexión mientras dure.
            client = docker.from_env()
            container = client.containers.get(container_id)
            # Logs históricos primero (timestamps, sin follow)
            hist = container.logs(stream=False, timestamps=True, tail=500)
            if hist:
                asyncio.run_coroutine_threadsafe(
                    queue.put(hist.decode("utf-8", errors="replace")), loop
                )
            # Luego seguimiento en vivo si el contenedor corre
            container.reload()
            if container.status == "running":
                for chunk in container.logs(stream=True, follow=True, tail=0, timestamps=True):
                    if stop_event.is_set():
                        break
                    asyncio.run_coroutine_threadsafe(
                        queue.put(chunk.decode("utf-8", errors="replace")), loop
                    )
        except Exception as e:
            asyncio.run_coroutine_threadsafe(queue.put(f"\n[Error: {e}]\n"), loop)
        finally:
            if client is not None:
                client.close()
            asyncio.run_coroutine_threadsafe(queue.put(None), loop)

    threading.Thread(target=produce, daemon=True).start()
    try:
        while True:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                # Contenedor en silencio: mantener la conexión viva
                try:
                    await websocket.send_text("")
                except Exception:
                    break
                continue
            if msg is None:
                break
            await websocket.send_text(msg)
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        stop_event.set()


@app.websocket("/ws/containers/{container_id}/stats")
async def container_stats_ws(websocket: WebSocket, container_id: str):
    await websocket.accept()
    if not _ws_auth(websocket):
        await websocket.close(code=4001)
        return
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()
    stop_event = threading.Event()

    def produce():
        client = None
        try:
            client = docker.from_env()
            for s in client.containers.get(container_id).stats(stream=True, decode=True):
                if stop_event.is_set():
                    break
                try:
                    asyncio.run_coroutine_threadsafe(queue.put(json.dumps(_parse_stats(s))), loop)
                except Exception:
                    pass
        except Exception as e:
            asyncio.run_coroutine_threadsafe(queue.put(json.dumps({"error": str(e)})), loop)
        finally:
            if client is not None:
                client.close()
            asyncio.run_coroutine_threadsafe(queue.put(None), loop)

    threading.Thread(target=produce, daemon=True).start()
    try:
        while True:
            msg = await asyncio.wait_for(queue.get(), timeout=10.0)
            if msg is None:
                break
            await websocket.send_text(msg)
    except (WebSocketDisconnect, asyncio.TimeoutError, Exception):
        pass
    finally:
        stop_event.set()


@app.websocket("/ws/containers/{container_id}/exec")
async def container_exec_ws(websocket: WebSocket, container_id: str):
    await websocket.accept()
    if not _ws_auth(websocket):
        await websocket.close(code=4001)
        return

    loop = asyncio.get_event_loop()
    out_queue: asyncio.Queue = asyncio.Queue()
    in_queue: thread_queue.Queue = thread_queue.Queue()
    stop_event = threading.Event()
    exec_info: dict = {}

    def exec_thread():
        client = None
        try:
            client = docker.from_env()
            container = client.containers.get(container_id)
            eid = client.api.exec_create(
                container.id,
                cmd=["/bin/sh", "-c", "command -v bash > /dev/null 2>&1 && exec bash || exec sh"],
                stdin=True, stdout=True, stderr=True, tty=True,
                environment={"TERM": "xterm-256color"},
            )
            exec_info["id"] = eid["Id"]
            exec_info["client"] = client
            sock = client.api.exec_start(eid["Id"], socket=True, tty=True)
            raw = getattr(sock, "_sock", None)
            if raw is None and hasattr(sock, "raw"):
                raw = getattr(sock.raw, "_sock", None)
            if raw is None:
                raise Exception("No se pudo obtener el socket del exec")
            raw.setblocking(False)
            while not stop_event.is_set():
                r, _, _ = sel.select([raw], [], [], 0.05)
                if r:
                    try:
                        data = raw.recv(4096)
                        if not data:
                            break
                        asyncio.run_coroutine_threadsafe(out_queue.put(data), loop)
                    except Exception:
                        break
                try:
                    inp = in_queue.get_nowait()
                    raw.sendall(inp)
                except thread_queue.Empty:
                    pass
        except Exception as e:
            asyncio.run_coroutine_threadsafe(
                out_queue.put(f"\r\n\x1b[31m[Error: {e}]\x1b[0m\r\n".encode()), loop
            )
        finally:
            if client is not None:
                client.close()
            asyncio.run_coroutine_threadsafe(out_queue.put(None), loop)

    threading.Thread(target=exec_thread, daemon=True).start()

    async def send_loop():
        while True:
            data = await out_queue.get()
            if data is None:
                break
            if isinstance(data, bytes):
                await websocket.send_bytes(data)
            else:
                await websocket.send_text(data)

    async def recv_loop():
        while True:
            try:
                msg = await asyncio.wait_for(websocket.receive(), timeout=60.0)
                if msg.get("type") == "websocket.disconnect":
                    break
                if msg.get("bytes"):
                    in_queue.put(msg["bytes"])
                elif msg.get("text"):
                    try:
                        data = json.loads(msg["text"])
                        if data.get("type") == "resize" and "id" in exec_info:
                            exec_info["client"].api.exec_resize(
                                exec_info["id"], height=data.get("rows", 24), width=data.get("cols", 80),
                            )
                        elif data.get("type") == "input":
                            in_queue.put(data["data"].encode())
                    except (json.JSONDecodeError, Exception):
                        in_queue.put(msg["text"].encode())
            except asyncio.TimeoutError:
                pass
            except Exception:
                break
        stop_event.set()

    try:
        await asyncio.gather(send_loop(), recv_loop())
    except Exception:
        pass
    finally:
        stop_event.set()


app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    # proxy_headers=False: uvicorn reescribiría la IP del cliente con
    # X-Forwarded-For en conexiones desde 127.0.0.1; de eso se encarga
    # [security] trusted_proxies (ver seguridad.ipCliente) y solo ahí.
    uvicorn.run(app, host=cfg_ini.BIND_HOST, port=cfg_ini.PORT, proxy_headers=False)
