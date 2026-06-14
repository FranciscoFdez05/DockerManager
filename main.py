from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
import docker
import docker.errors
import asyncio
import threading
import json
import psutil
import socket

app = FastAPI(title="Docker Gestor")


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


# ── Containers ────────────────────────────────────────────────────────────────

@app.get("/api/containers")
def list_containers():
    client = get_docker()
    containers = client.containers.list(all=True)
    result = []
    for c in containers:
        ports = {}
        if c.ports:
            for k, v in c.ports.items():
                if v:
                    ports[k] = [p["HostPort"] for p in v]
        result.append({
            "id": c.short_id,
            "name": c.name,
            "image": c.image.tags[0] if c.image.tags else c.image.short_id,
            "status": c.status,
            "ports": ports,
        })
    return result


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
        {
            "type": m.get("Type"),
            "source": m.get("Source", ""),
            "destination": m.get("Destination", ""),
            "mode": m.get("Mode", ""),
            "rw": m.get("RW", True),
        }
        for m in (a.get("Mounts") or [])
    ]
    cfg = a.get("Config", {})
    host_cfg = a.get("HostConfig", {})
    return {
        "id": a["Id"][:12],
        "name": a["Name"].lstrip("/"),
        "image": cfg.get("Image", ""),
        "created": a.get("Created", "")[:19].replace("T", " "),
        "status": a["State"]["Status"],
        "started_at": a["State"].get("StartedAt", "")[:19].replace("T", " "),
        "restart_policy": host_cfg.get("RestartPolicy", {}).get("Name", "no"),
        "hostname": cfg.get("Hostname", ""),
        "env": cfg.get("Env") or [],
        "cmd": cfg.get("Cmd") or [],
        "entrypoint": cfg.get("Entrypoint") or [],
        "networks": networks,
        "mounts": mounts,
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
        s = c.stats(stream=False)
        return _parse_stats(s)
    except Exception as e:
        raise HTTPException(500, str(e))


def _parse_stats(s: dict) -> dict:
    # CPU
    cpu_delta = s["cpu_stats"]["cpu_usage"]["total_usage"] - s["precpu_stats"]["cpu_usage"]["total_usage"]
    sys_delta = s["cpu_stats"].get("system_cpu_usage", 0) - s["precpu_stats"].get("system_cpu_usage", 0)
    num_cpus = s["cpu_stats"].get("online_cpus") or len(s["cpu_stats"]["cpu_usage"].get("percpu_usage", [1]))
    cpu_pct = (cpu_delta / sys_delta * num_cpus * 100.0) if sys_delta > 0 else 0.0

    # Memory
    mem = s.get("memory_stats", {})
    usage = mem.get("usage", 0)
    cache = mem.get("stats", {}).get("cache", 0)
    mem_usage = usage - cache
    mem_limit = mem.get("limit", 1)
    mem_pct = (mem_usage / mem_limit * 100) if mem_limit else 0

    # Network
    net_rx, net_tx = 0, 0
    for iface in (s.get("networks") or {}).values():
        net_rx += iface.get("rx_bytes", 0)
        net_tx += iface.get("tx_bytes", 0)

    return {
        "cpu_pct": round(cpu_pct, 2),
        "mem_usage": mem_usage,
        "mem_limit": mem_limit,
        "mem_pct": round(mem_pct, 2),
        "net_rx": net_rx,
        "net_tx": net_tx,
    }


# ── Images ────────────────────────────────────────────────────────────────────

@app.get("/api/images")
def list_images():
    client = get_docker()
    result = []
    for img in client.images.list():
        result.append({
            "id": img.short_id.replace("sha256:", ""),
            "tags": img.tags,
            "size_mb": round(img.attrs["Size"] / (1024 ** 2), 1),
            "created": img.attrs["Created"][:10],
        })
    return result


@app.delete("/api/images/{image_id}")
def remove_image(image_id: str):
    client = get_docker()
    try:
        client.images.remove(image_id, force=True)
        return {"status": "removed"}
    except docker.errors.ImageNotFound:
        raise HTTPException(404, "Imagen no encontrada")
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/images/pull")
def pull_image(body: dict):
    tag = body.get("tag", "")
    if not tag:
        raise HTTPException(400, "Se requiere 'tag'")
    client = get_docker()
    try:
        client.images.pull(tag)
        return {"status": "pulled", "tag": tag}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Volumes ───────────────────────────────────────────────────────────────────

@app.get("/api/volumes")
def list_volumes():
    client = get_docker()
    result = []
    for v in client.volumes.list():
        result.append({
            "name": v.name,
            "driver": v.attrs.get("Driver", ""),
            "mountpoint": v.attrs.get("Mountpoint", ""),
            "created": (v.attrs.get("CreatedAt") or "")[:10],
            "labels": v.attrs.get("Labels") or {},
        })
    return result


@app.delete("/api/volumes/{volume_name}")
def remove_volume(volume_name: str):
    client = get_docker()
    try:
        client.volumes.get(volume_name).remove(force=True)
        return {"status": "removed"}
    except docker.errors.NotFound:
        raise HTTPException(404, "Volumen no encontrado")
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
        subnet = ipam[0].get("Subnet", "") if ipam else ""
        result.append({
            "id": a["Id"][:12],
            "name": n.name,
            "driver": a.get("Driver", ""),
            "scope": a.get("Scope", ""),
            "subnet": subnet,
            "internal": a.get("Internal", False),
            "containers": len(a.get("Containers") or {}),
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
            "docker_version": version.get("Version", ""),
            "api_version": version.get("ApiVersion", ""),
            "os": info.get("OperatingSystem", ""),
            "kernel": info.get("KernelVersion", ""),
            "arch": info.get("Architecture", ""),
            "hostname": info.get("Name", ""),
            "host_ip": host_ip,
            "cpus": info.get("NCPU", 0),
            "memory_gb": round(info.get("MemTotal", 0) / (1024 ** 3), 2),
            "containers_running": info.get("ContainersRunning", 0),
            "containers_stopped": info.get("ContainersStopped", 0),
            "containers_paused": info.get("ContainersPaused", 0),
            "images": info.get("Images", 0),
            "storage_driver": info.get("Driver", ""),
            "logging_driver": info.get("LoggingDriver", ""),
        }
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/system/host")
def host_stats():
    cpu = psutil.cpu_percent(interval=0.3)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    return {
        "cpu_pct": cpu,
        "mem_total": mem.total,
        "mem_used": mem.used,
        "mem_pct": mem.percent,
        "disk_total": disk.total,
        "disk_used": disk.used,
        "disk_pct": round(disk.used / disk.total * 100, 1),
    }


@app.post("/api/system/prune")
def system_prune():
    client = get_docker()
    try:
        c_result = client.containers.prune()
        i_result = client.images.prune(filters={"dangling": True})
        v_result = client.volumes.prune()
        n_result = client.networks.prune()
        freed = (
            (c_result.get("SpaceReclaimed") or 0)
            + (i_result.get("SpaceReclaimed") or 0)
            + (v_result.get("SpaceReclaimed") or 0)
        )
        return {
            "status": "ok",
            "freed_bytes": freed,
            "freed_mb": round(freed / (1024 ** 2), 1),
            "containers_deleted": len(c_result.get("ContainersDeleted") or []),
            "images_deleted": len(i_result.get("ImagesDeleted") or []),
            "volumes_deleted": len(v_result.get("VolumesDeleted") or []),
            "networks_deleted": len(n_result.get("NetworksDeleted") or []),
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ── WebSockets ────────────────────────────────────────────────────────────────

@app.websocket("/ws/containers/{container_id}/logs")
async def container_logs_ws(websocket: WebSocket, container_id: str):
    await websocket.accept()
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()
    stop_event = threading.Event()

    def produce():
        try:
            client = docker.from_env()
            container = client.containers.get(container_id)
            for chunk in container.logs(stream=True, follow=True, tail=300):
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
            msg = await asyncio.wait_for(queue.get(), timeout=60.0)
            if msg is None:
                break
            await websocket.send_text(msg)
    except (WebSocketDisconnect, asyncio.TimeoutError, Exception):
        pass
    finally:
        stop_event.set()


@app.websocket("/ws/containers/{container_id}/stats")
async def container_stats_ws(websocket: WebSocket, container_id: str):
    await websocket.accept()
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()
    stop_event = threading.Event()

    def produce():
        try:
            client = docker.from_env()
            container = client.containers.get(container_id)
            for s in container.stats(stream=True, decode=True):
                if stop_event.is_set():
                    break
                try:
                    parsed = _parse_stats(s)
                    asyncio.run_coroutine_threadsafe(queue.put(json.dumps(parsed)), loop)
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


app.mount("/", StaticFiles(directory="static", html=True), name="static")
