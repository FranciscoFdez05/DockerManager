"""Tests de los endpoints de main.py con FastAPI TestClient y un Docker falso."""

import json
import os
import threading
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

import seguridad  # noqa: E402

RAW_CONTENEDORES = [
    {
        "Id": "a" * 64, "Names": ["/web"], "Image": "nginx:1.27", "ImageID": "sha256:" + "b" * 64,
        "Created": 1_700_000_000, "State": "running", "Status": "Up 2 hours (healthy)",
        "Labels": {"com.docker.compose.project": "tienda", "com.docker.compose.service": "web"},
        "Ports": [
            {"IP": "0.0.0.0", "PrivatePort": 80, "PublicPort": 8080, "Type": "tcp"},
            {"IP": "::", "PrivatePort": 80, "PublicPort": 8080, "Type": "tcp"},
            {"PrivatePort": 443, "Type": "tcp"},
        ],
        "Mounts": [{"Type": "volume", "Name": "datos"}, {"Type": "bind", "Source": "/srv"}],
    },
    {
        "Id": "c" * 64, "Names": ["/huerfano"], "Image": "sha256:" + "d" * 64, "ImageID": "sha256:" + "d" * 64,
        "Created": 1_700_000_000, "State": "exited", "Status": "Exited (1) 3 days ago",
        "Labels": None, "Ports": [], "Mounts": None,
    },
    {"Names": ["/corrupto"]},  # sin Id: se descarta sin tumbar el listado
]


class FakeApi:
    def __init__(self) -> None:
        self.llamadas = 0

    def containers(self, all: bool = False) -> list[dict]:
        self.llamadas += 1
        return RAW_CONTENEDORES


class SinInspect:
    def list(self, *args, **kwargs):
        raise AssertionError("containers.list() hace un inspect por contenedor (N+1)")


class FakeDocker:
    def __init__(self) -> None:
        self.api = FakeApi()
        self.containers = SinInspect()
        self.images = SimpleNamespace(list=lambda: [])
        self.volumes = SimpleNamespace(list=lambda: [])


@pytest.fixture(scope="module")
def main():
    anterior = os.getcwd()
    os.chdir(RAIZ)  # StaticFiles se monta con la ruta relativa "static"
    try:
        import main as modulo
    finally:
        os.chdir(anterior)
    return modulo


@pytest.fixture
def docker(main, monkeypatch):
    falso = FakeDocker()
    monkeypatch.setattr(main, "dockerProveedor", SimpleNamespace(obtener=lambda: falso, cerrar=lambda: None))
    return falso


@pytest.fixture
def cliente(main, docker, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "_credenciales", seguridad.AlmacenCredenciales(str(tmp_path / "config.json")))
    monkeypatch.setattr(main, "_sesion", seguridad.GestorSesion(str(tmp_path / "session.json"), "clave", 3600))
    monkeypatch.setattr(main, "_limitador", seguridad.LimitadorIntentos(3, 60))
    monkeypatch.setattr(main, "_proxies_confiables", [])
    monkeypatch.setattr(main.cfg_ini, "BCRYPT_COST", 4)  # bcrypt rápido en tests
    main._hash_relleno.cache_clear()
    return TestClient(main.app)


def configurar(cliente: TestClient) -> None:
    respuesta = cliente.post("/api/auth/setup", json={"username": "admin", "password": "secreto123",
                                                      "confirm": "secreto123"})
    assert respuesta.status_code == 200


def login(cliente: TestClient, usuario: str = "admin", password: str = "secreto123", **cabeceras):
    return cliente.post("/api/auth/login", json={"username": usuario, "password": password}, headers=cabeceras)


# ── Autenticación ─────────────────────────────────────────────────────────────
def test_setup_inicia_sesion_y_no_se_repite(cliente):
    configurar(cliente)
    assert cliente.get("/api/auth/status").json() == {"setup_needed": False, "authenticated": True}
    otro = cliente.post("/api/auth/setup", json={"username": "otro", "password": "x" * 8, "confirm": "x" * 8})
    assert otro.status_code == 403


def test_x_forwarded_for_falso_no_salta_el_bloqueo(cliente):
    configurar(cliente)
    for i in range(3):
        assert login(cliente, password="mal", **{"X-Forwarded-For": f"10.0.0.{i}"}).status_code == 401
    assert login(cliente, **{"X-Forwarded-For": "10.0.0.99"}).status_code == 429


def test_usuario_inexistente_tambien_ejecuta_bcrypt(cliente, main, monkeypatch):
    configurar(cliente)
    comprobados = []
    original = main.verify_password
    monkeypatch.setattr(main, "verify_password", lambda p, h: comprobados.append(h) or original(p, h))
    assert login(cliente, usuario="nadie", password="secreto123").status_code == 401
    assert comprobados == [main._hash_relleno()]


def test_usuario_con_caracteres_no_ascii_no_da_500(cliente):
    configurar(cliente)
    assert login(cliente, usuario="ñandú").status_code == 401


def test_logout_invalida_la_cookie_aunque_se_reinicie(cliente, main, tmp_path):
    configurar(cliente)
    cookie = cliente.cookies.get("dm_session")
    cliente.post("/api/auth/logout")
    reiniciada = seguridad.GestorSesion(str(tmp_path / "session.json"), "clave", 3600)
    assert not reiniciada.valida(cookie)


def test_sesion_sobrevive_al_reinicio(cliente, main, monkeypatch, tmp_path):
    configurar(cliente)
    monkeypatch.setattr(main, "_sesion", seguridad.GestorSesion(str(tmp_path / "session.json"), "clave", 3600))
    assert cliente.get("/api/auth/status").json()["authenticated"] is True


def test_cambiar_password_rota_la_cookie(cliente):
    configurar(cliente)
    vieja = cliente.cookies.get("dm_session")
    respuesta = cliente.post("/api/auth/change-password", json={
        "current": "secreto123", "new_password": "nuevo12345", "confirm": "nuevo12345"})
    assert respuesta.status_code == 200
    assert cliente.cookies.get("dm_session") != vieja
    assert cliente.get("/api/containers").status_code == 200
    cliente.cookies.set("dm_session", vieja)
    assert cliente.get("/api/containers").status_code == 401


def test_api_sin_sesion_da_401(cliente):
    configurar(cliente)
    cliente.cookies.clear()
    assert cliente.get("/api/containers").status_code == 401


# ── WebSocket ─────────────────────────────────────────────────────────────────
def test_websocket_de_otro_origen_se_rechaza(cliente):
    configurar(cliente)
    with cliente.websocket_connect("/ws/containers/web/stats", headers={"origin": "http://evil.example"}) as ws:
        with pytest.raises(WebSocketDisconnect) as error:
            ws.receive_text()
    assert error.value.code == 4001


def test_ws_auth_acepta_el_mismo_origen(cliente, main):
    configurar(cliente)
    token = cliente.cookies.get("dm_session")
    ws = SimpleNamespace(headers={"origin": "http://servidor:3000", "host": "servidor:3000"},
                         cookies={"dm_session": token})
    assert main._ws_auth(ws) is True
    ws.headers["origin"] = "http://evil.example"
    assert main._ws_auth(ws) is False


# ── Contenedores ──────────────────────────────────────────────────────────────
def test_listado_de_contenedores_en_una_sola_llamada(cliente, docker):
    configurar(cliente)
    datos = cliente.get("/api/containers").json()
    assert docker.api.llamadas == 1
    assert [c["name"] for c in datos] == ["web", "huerfano"]
    web, huerfano = datos
    assert web == {
        "id": "a" * 12, "name": "web", "image": "nginx:1.27", "image_id": "b" * 12,
        "status": "running", "health": "healthy", "project": "tienda", "service": "web",
        "created": "2023-11-14 22:13:20", "ports": {"80/tcp": ["8080"]}, "volumes": ["datos"],
    }
    assert huerfano["image"] == "d" * 12
    assert huerfano["health"] is None
    assert huerfano["ports"] == {} and huerfano["volumes"] == [] and huerfano["project"] is None


@pytest.mark.parametrize("estado, esperado", [
    ("Up 1 minute (health: starting)", "starting"),
    ("Up 1 minute (unhealthy)", "unhealthy"),
    ("Up 1 minute (Paused)", None),
])
def test_salud_desde_el_texto_de_estado(main, estado, esperado):
    assert main._container_health(estado) == esperado


@pytest.mark.parametrize("puertos, politica", [
    (["abc:80"], "no"),
    (["8080:80/xyz"], "no"),
    (["70000:80"], "no"),
    (["8080:80"], "siempre"),
])
def test_crear_contenedor_valida_puertos_y_politica(cliente, puertos, politica):
    configurar(cliente)
    respuesta = cliente.post("/api/containers/create",
                             json={"image": "nginx", "ports": puertos, "restart_policy": politica})
    assert respuesta.status_code == 400


def test_parse_port_binding(main):
    assert main._parse_port_binding("8080:80") == ("80/tcp", 8080)
    assert main._parse_port_binding("53:53/udp") == ("53/udp", 53)
    assert main._parse_port_binding("80") is None


# ── Cabeceras y compresión ────────────────────────────────────────────────────
def test_cabeceras_de_seguridad(cliente):
    respuesta = cliente.get("/")
    assert respuesta.headers["X-XSS-Protection"] == "0"
    assert respuesta.headers["X-Frame-Options"] == "DENY"


def test_estaticos_comprimidos(cliente):
    respuesta = cliente.get("/app.js", headers={"Accept-Encoding": "gzip"})
    assert respuesta.status_code == 200
    assert respuesta.headers["content-encoding"] == "gzip"


# ── Ajustes › Telegram desde la web ───────────────────────────────────────────
TOKEN_TG = "222222222:WEB" + "w" * 32


@pytest.fixture
def telegramWeb(main, cliente, tmp_path, monkeypatch):
    """Telegram sin .env, con almacén temporal y sin red (getUpdates vacío, envíos registrados)."""
    import configTelegram
    import telegrama

    enviados = []

    def llamar(self, metodo, datos, timeout=None):
        if metodo == "getUpdates":
            threading.Event().wait(0.05)
            return []
        if metodo == "sendMessage":
            enviados.append((datos["chat_id"], datos["text"]))
        return {}

    monkeypatch.setattr(telegrama.ClienteTelegram, "llamar", llamar)
    monkeypatch.setattr(main, "_comprobarTokenTelegram", lambda token, timeout: "mi_bot")
    monkeypatch.setattr(main.cfg_ini, "telegramToken", "")
    monkeypatch.setattr(main.cfg_ini, "telegramIdsPermitidos", "")
    monkeypatch.setattr(main, "_almacenTelegram", configTelegram.AlmacenTelegram(str(tmp_path / "tg.json")))
    monkeypatch.setattr(main, "_telegram", None)
    monkeypatch.setattr(main, "_vigilancia", None)
    configurar(cliente)
    yield enviados
    main._detenerTelegram()


def test_ajustes_telegram_requiere_sesion(cliente):
    assert cliente.put("/api/ajustes/telegram", json={"ids": "1"}).status_code in (401, 403)


def test_ajustes_telegram_se_guarda_y_activa_sin_reiniciar(cliente, main, telegramWeb):
    respuesta = cliente.put("/api/ajustes/telegram", json={"token": TOKEN_TG, "ids": "123"})
    assert respuesta.status_code == 200
    assert respuesta.json()["activo"] is True
    assert main._telegram is not None
    datos = cliente.get("/api/ajustes").json()["telegram"]
    assert datos["estado"] == "activo" and datos["origenToken"] == "web" and datos["bot"] == "mi_bot"
    assert datos["ids"] == [123]
    assert TOKEN_TG not in json.dumps(cliente.get("/api/ajustes").json())


def test_ajustes_telegram_cambiar_ids_avisa_a_los_anteriores(cliente, main, telegramWeb):
    cliente.put("/api/ajustes/telegram", json={"token": TOKEN_TG, "ids": "123"})
    anterior = main._telegram
    assert cliente.put("/api/ajustes/telegram", json={"ids": "456"}).status_code == 200
    assert main._telegram is not anterior
    for _ in range(100):
        if telegramWeb:
            break
        threading.Event().wait(0.05)
    assert any(chat == 123 and "cambiada" in texto for chat, texto in telegramWeb)


def test_ajustes_telegram_valores_invalidos(cliente, telegramWeb):
    assert cliente.put("/api/ajustes/telegram", json={"ids": "-100"}).status_code == 400
    assert cliente.put("/api/ajustes/telegram", json={"discoAvisoPct": 5}).status_code == 400
    assert cliente.put("/api/ajustes/telegram", json={"origenToken": "env"}).status_code == 400


def test_ajustes_telegram_no_pisa_el_env(cliente, main, telegramWeb, monkeypatch):
    monkeypatch.setattr(main.cfg_ini, "telegramToken", "111111111:ENV" + "e" * 32)
    respuesta = cliente.put("/api/ajustes/telegram", json={"token": TOKEN_TG})
    assert respuesta.status_code == 400
    assert ".env" in respuesta.json()["detail"]


def test_ajustes_telegram_borrar(cliente, main, telegramWeb):
    cliente.put("/api/ajustes/telegram", json={"token": TOKEN_TG, "ids": "123"})
    respuesta = cliente.delete("/api/ajustes/telegram")
    assert respuesta.status_code == 200 and respuesta.json()["activo"] is False
    assert main._telegram is None
    assert cliente.get("/api/ajustes").json()["telegram"]["estado"] == "sin_configurar"
