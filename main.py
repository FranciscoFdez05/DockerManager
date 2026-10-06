from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
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
import hashlib
import secrets
import select as sel
import bcrypt
from collections import defaultdict
from datetime import datetime, timedelta
import config as cfg_ini
from version import __version__

app = FastAPI(title="DockerManager", version=__version__)

# ── Configuración persistente ─────────────────────────────────────────────────
DATA_DIR = cfg_ini.DATA_DIR
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")


def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def load_config() -> dict | None:
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def save_config(cfg: dict):
    _ensure_data_dir()
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(cfg_ini.BCRYPT_COST)).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


# ── Sesión en memoria (se regenera en cada login) ─────────────────────────────
# El token inicial se deriva de SECRET_KEY (la genera docker-up.sh en el .env)
# para que reiniciar el contenedor no eche fuera a quien ya estaba dentro. Sin
# SECRET_KEY se usa un token aleatorio y cada reinicio invalida las sesiones.
_SECRET_KEY = os.getenv("SECRET_KEY", "")


def _initial_token() -> str:
    if _SECRET_KEY:
        return hashlib.sha256(f"{_SECRET_KEY}:session".encode("utf-8")).hexdigest()
    return secrets.token_hex(32)


_session: dict = {"token": _initial_token()}


def _get_token() -> str:
    return _session["token"]


def _new_token() -> str:
    _session["token"] = secrets.token_hex(32)
    return _session["token"]


# ── Rate limiting anti-fuerza-bruta ───────────────────────────────────────────
_rate_lock = threading.Lock()
_attempts: dict[str, list] = defaultdict(list)
MAX_ATTEMPTS = cfg_ini.MAX_LOGIN_ATTEMPTS
LOCKOUT_MIN = cfg_ini.LOCKOUT_MINUTES


def _is_locked(ip: str) -> bool:
    with _rate_lock:
        cutoff = datetime.utcnow() - timedelta(minutes=LOCKOUT_MIN)
        _attempts[ip] = [t for t in _attempts[ip] if t > cutoff]
        return len(_attempts[ip]) >= MAX_ATTEMPTS


def _record_fail(ip: str):
    with _rate_lock:
        _attempts[ip].append(datetime.utcnow())


def _clear_attempts(ip: str):
    with _rate_lock:
        _attempts[ip] = []


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


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
        response.headers["X-XSS-Protection"] = "1; mode=block"
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
        if request.cookies.get(cfg_ini.SESSION_COOKIE_NAME) != _get_token():
            return JSONResponse({"detail": "No autorizado"}, status_code=401)
        return await call_next(request)


app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(AuthMiddleware)


# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    """Público. Comprueba que la app puede hablar con el daemon de Docker; lo usan
    docker-update.sh y el HEALTHCHECK de la imagen para distinguir «el proceso está
    arriba» de «la aplicación funciona»."""
    try:
        docker.from_env().ping()
    except Exception:
        return JSONResponse({"status": "error", "docker": False, "version": __version__}, status_code=503)
    return {"status": "ok", "docker": True, "version": __version__}


# ── Auth endpoints ────────────────────────────────────────────────────────────
@app.get("/api/auth/status")
def auth_status(request: Request):
    cfg = load_config()
    if not cfg:
        return {"setup_needed": True, "authenticated": False}
    authenticated = request.cookies.get(cfg_ini.SESSION_COOKIE_NAME) == _get_token()
    return {"setup_needed": False, "authenticated": authenticated}


@app.post("/api/auth/setup")
def auth_setup(body: dict, response: Response):
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

    save_config({"username": username, "password_hash": hash_password(password)})
    token = _new_token()
    response.set_cookie(cfg_ini.SESSION_COOKIE_NAME, token, httponly=True, samesite="strict",
                        max_age=cfg_ini.SESSION_MAX_AGE_SECONDS)
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
    username_ok = secrets.compare_digest(username, cfg.get("username", ""))
    password_ok = verify_password(password, cfg.get("password_hash", "")) if username_ok else False

    if not username_ok or not password_ok:
        _record_fail(ip)
        raise HTTPException(401, "Credenciales incorrectas")

    _clear_attempts(ip)
    token = _new_token()  # Regenerar token en cada login (anti session-fixation)
    response.set_cookie(cfg_ini.SESSION_COOKIE_NAME, token, httponly=True, samesite="strict",
                        max_age=cfg_ini.SESSION_MAX_AGE_SECONDS)
    return {"ok": True}


@app.post("/api/auth/logout")
def auth_logout(response: Response):
    _new_token()  # Invalida la sesión actual
    response.delete_cookie(cfg_ini.SESSION_COOKIE_NAME)
    return {"ok": True}


@app.post("/api/auth/change-password")
def change_password(body: dict, request: Request):
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
    return {"ok": True}


# ── Helpers ───────────────────────────────────────────────────────────────────
def get_docker():
    try:
        return docker.from_env()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Docker no disponible: {e}")


def get_container(client, container_id: str):
    try:
        return client.containers.get(container_id)
    except docker.errors.NotFound:
        raise HTTPException(404, "Contenedor no encontrado")


def _ws_auth(websocket: WebSocket) -> bool:
    cfg = load_config()
    if not cfg:
        return False
    return websocket.cookies.get(cfg_ini.SESSION_COOKIE_NAME) == _get_token()


# ── Uso de recursos por contenedor ────────────────────────────────────────────
PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"


def _container_image_name(c) -> str:
    """Nombre de imagen sin tocar c.image: falla (ImageNotFound) si la imagen ya se borró."""
    a = c.attrs
    name = (a.get("Config") or {}).get("Image") or ""
    if not name or name.startswith("sha256:"):
        name = (a.get("Image") or "")[7:19]
    return name


def _container_info(c) -> dict:
    a = c.attrs
    labels = (a.get("Config") or {}).get("Labels") or {}
    ports = {}
    for k, v in ((a.get("NetworkSettings") or {}).get("Ports") or {}).items():
        if v:
            ports[k] = sorted({p["HostPort"] for p in v})
    mounts = a.get("Mounts") or []
    return {
        "id": c.short_id,
        "name": c.name,
        "image": _container_image_name(c),
        "image_id": (a.get("Image") or "").replace("sha256:", "")[:12],
        "status": (a.get("State") or {}).get("Status", "unknown"),
        "health": ((a.get("State") or {}).get("Health") or {}).get("Status"),
        "project": labels.get(PROJECT_LABEL),
        "service": labels.get(SERVICE_LABEL),
        "created": (a.get("Created") or "")[:19].replace("T", " "),
        "ports": ports,
        "volumes": [m["Name"] for m in mounts if m.get("Type") == "volume" and m.get("Name")],
    }


def _all_container_infos(client) -> list[dict]:
    infos = []
    for c in client.containers.list(all=True):
        try:
            infos.append(_container_info(c))
        except Exception:
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


@app.post("/api/containers/create")
def create_container(body: dict):
    client = get_docker()
    image = (body.get("image") or "").strip()
    if not image or len(image) > 256:
        raise HTTPException(400, "Imagen inválida")
    try:
        port_bindings = {}
        for p in body.get("ports", [])[:20]:
            p = str(p).strip()
            if not p or len(p) > 30:
                continue
            parts = p.split(":")
            if len(parts) == 2:
                host_port, container_port = parts
                proto = "tcp"
                if "/" in container_port:
                    container_port, proto = container_port.split("/")
                port_bindings[f"{container_port}/{proto}"] = int(host_port)

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
            restart_policy={"Name": body.get("restart_policy", "no")},
            network=network,
            command=command,
            detach=True,
        )
        return {"status": "created", "id": c.short_id, "name": c.name}
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
    cpu  = psutil.cpu_percent(interval=0.3)
    mem  = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    return {
        "cpu_pct": cpu, "mem_total": mem.total, "mem_used": mem.used, "mem_pct": mem.percent,
        "disk_total": disk.total, "disk_used": disk.used,
        "disk_pct": round(disk.used / disk.total * 100, 1),
    }


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
        try:
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
    uvicorn.run(app, host=cfg_ini.BIND_HOST, port=cfg_ini.PORT)
