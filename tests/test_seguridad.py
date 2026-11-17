"""Tests de seguridad.py: persistencia, sesión, limitador, IP real y Origin."""

import json
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import seguridad  # noqa: E402


class Reloj:
    def __init__(self, valor: float = 1_000.0) -> None:
        self.valor = valor

    def __call__(self) -> float:
        return self.valor


# ── escribirJsonPrivado ───────────────────────────────────────────────────────
def test_escribir_json_crea_directorio_y_no_deja_temporales(tmp_path):
    ruta = tmp_path / "sub" / "config.json"
    seguridad.escribirJsonPrivado(str(ruta), {"a": 1})
    assert json.loads(ruta.read_text(encoding="utf-8")) == {"a": 1}
    assert os.listdir(ruta.parent) == ["config.json"]


@pytest.mark.skipif(os.name == "nt", reason="Windows no aplica permisos POSIX")
def test_escribir_json_con_permisos_0600(tmp_path):
    ruta = tmp_path / "config.json"
    seguridad.escribirJsonPrivado(str(ruta), {})
    assert stat.S_IMODE(os.stat(ruta).st_mode) == 0o600


def test_escribir_json_fallido_conserva_el_original(tmp_path):
    ruta = tmp_path / "config.json"
    seguridad.escribirJsonPrivado(str(ruta), {"bueno": True})
    with pytest.raises(TypeError):
        seguridad.escribirJsonPrivado(str(ruta), {"malo": object()})
    assert json.loads(ruta.read_text(encoding="utf-8")) == {"bueno": True}
    assert os.listdir(tmp_path) == ["config.json"]


# ── AlmacenCredenciales ───────────────────────────────────────────────────────
def test_almacen_sin_archivo_devuelve_none(tmp_path):
    assert seguridad.AlmacenCredenciales(str(tmp_path / "config.json")).cargar() is None


def test_almacen_cachea_mientras_no_cambia(tmp_path, monkeypatch):
    ruta = tmp_path / "config.json"
    almacen = seguridad.AlmacenCredenciales(str(ruta))
    almacen.guardar({"username": "admin"})
    lecturas = []
    original = seguridad._leerJson
    monkeypatch.setattr(seguridad, "_leerJson", lambda r: lecturas.append(r) or original(r))

    for _ in range(5):
        assert almacen.cargar() == {"username": "admin"}
    assert len(lecturas) == 1


def test_almacen_detecta_borrado_y_cambio_externo(tmp_path):
    ruta = tmp_path / "config.json"
    almacen = seguridad.AlmacenCredenciales(str(ruta))
    almacen.guardar({"username": "admin"})
    assert almacen.cargar()["username"] == "admin"

    ruta.write_text(json.dumps({"username": "otro-nombre-mas-largo"}), encoding="utf-8")
    assert almacen.cargar()["username"] == "otro-nombre-mas-largo"

    ruta.unlink()
    assert almacen.cargar() is None


def test_almacen_devuelve_copias(tmp_path):
    almacen = seguridad.AlmacenCredenciales(str(tmp_path / "config.json"))
    almacen.guardar({"username": "admin"})
    almacen.cargar()["username"] = "manipulado"
    assert almacen.cargar()["username"] == "admin"


def test_almacen_json_corrupto_es_none(tmp_path):
    ruta = tmp_path / "config.json"
    ruta.write_text("{roto", encoding="utf-8")
    assert seguridad.AlmacenCredenciales(str(ruta)).cargar() is None


def test_crear_si_no_existe_solo_la_primera_vez(tmp_path):
    almacen = seguridad.AlmacenCredenciales(str(tmp_path / "config.json"))
    assert almacen.crearSiNoExiste({"username": "primero"}) is True
    assert almacen.crearSiNoExiste({"username": "segundo"}) is False
    assert almacen.cargar()["username"] == "primero"


# ── GestorSesion ──────────────────────────────────────────────────────────────
@pytest.fixture
def reloj() -> Reloj:
    return Reloj()


def gestor(tmp_path, reloj, clave: str = "clave") -> seguridad.GestorSesion:
    return seguridad.GestorSesion(str(tmp_path / "session.json"), clave, 3600, reloj=reloj)


def test_sesion_valida_solo_el_token_actual(tmp_path, reloj):
    sesion = gestor(tmp_path, reloj)
    primero = sesion.crear()
    segundo = sesion.crear()
    assert sesion.valida(segundo)
    assert not sesion.valida(primero)
    assert not sesion.valida("")
    assert not sesion.valida(None)


def test_sesion_caduca_en_el_servidor(tmp_path, reloj):
    sesion = gestor(tmp_path, reloj)
    token = sesion.crear()
    reloj.valor += 3599
    assert sesion.valida(token)
    reloj.valor += 1
    assert not sesion.valida(token)


def test_sesion_sobrevive_al_reinicio(tmp_path, reloj):
    token = gestor(tmp_path, reloj).crear()
    assert gestor(tmp_path, reloj).valida(token)


def test_logout_persiste_tras_reinicio(tmp_path, reloj):
    sesion = gestor(tmp_path, reloj)
    token = sesion.crear()
    sesion.invalidar()
    assert not sesion.valida(token)
    assert not gestor(tmp_path, reloj).valida(token)


def test_cambiar_secret_key_invalida_la_sesion(tmp_path, reloj):
    token = gestor(tmp_path, reloj, "clave-a").crear()
    assert not gestor(tmp_path, reloj, "clave-b").valida(token)


def test_sin_secret_key_no_sobrevive_al_reinicio(tmp_path, reloj):
    token = gestor(tmp_path, reloj, "").crear()
    assert not gestor(tmp_path, reloj, "").valida(token)


def test_el_token_no_se_guarda_en_claro(tmp_path, reloj):
    token = gestor(tmp_path, reloj).crear()
    assert token not in (tmp_path / "session.json").read_text(encoding="utf-8")


def test_sesion_con_archivo_corrupto_arranca_sin_sesion(tmp_path, reloj):
    (tmp_path / "session.json").write_text("no es json", encoding="utf-8")
    sesion = gestor(tmp_path, reloj)
    assert not sesion.valida("cualquiera")
    assert sesion.valida(sesion.crear())


# ── LimitadorIntentos ─────────────────────────────────────────────────────────
def test_limitador_bloquea_al_llegar_al_maximo(reloj):
    limitador = seguridad.LimitadorIntentos(3, 60, reloj=reloj)
    for _ in range(2):
        limitador.registrarFallo("1.1.1.1")
    assert not limitador.bloqueada("1.1.1.1")
    limitador.registrarFallo("1.1.1.1")
    assert limitador.bloqueada("1.1.1.1")
    assert not limitador.bloqueada("2.2.2.2")


def test_limitador_desbloquea_al_pasar_la_ventana(reloj):
    limitador = seguridad.LimitadorIntentos(1, 60, reloj=reloj)
    limitador.registrarFallo("1.1.1.1")
    reloj.valor += 61
    assert not limitador.bloqueada("1.1.1.1")
    assert len(limitador) == 0  # la entrada vacía se borra


def test_limitador_limpiar(reloj):
    limitador = seguridad.LimitadorIntentos(1, 60, reloj=reloj)
    limitador.registrarFallo("1.1.1.1")
    limitador.limpiar("1.1.1.1")
    assert not limitador.bloqueada("1.1.1.1")


def test_limitador_purga_ips_caducadas(reloj, monkeypatch):
    monkeypatch.setattr(seguridad.LimitadorIntentos, "_UMBRAL_PURGA", 10)
    limitador = seguridad.LimitadorIntentos(5, 60, reloj=reloj)
    for i in range(10):
        limitador.registrarFallo(f"10.0.0.{i}")
    reloj.valor += 61
    limitador.registrarFallo("10.0.1.1")
    assert len(limitador) == 1


def test_consultar_ip_desconocida_no_crea_entradas(reloj):
    limitador = seguridad.LimitadorIntentos(5, 60, reloj=reloj)
    for i in range(100):
        limitador.bloqueada(f"10.0.0.{i}")
    assert len(limitador) == 0


# ── IP del cliente ────────────────────────────────────────────────────────────
def test_parsear_proxies():
    redes = seguridad.parsearProxies(" 127.0.0.1 , 10.0.0.0/8,, ::1 ")
    assert [str(r) for r in redes] == ["127.0.0.1/32", "10.0.0.0/8", "::1/128"]
    assert seguridad.parsearProxies("") == []


def test_parsear_proxies_invalido():
    with pytest.raises(ValueError):
        seguridad.parsearProxies("no-es-una-ip")


def test_sin_proxies_se_ignora_x_forwarded_for():
    assert seguridad.ipCliente("192.168.1.5", {"x-forwarded-for": "8.8.8.8"}, []) == "192.168.1.5"


def test_conexion_que_no_es_del_proxy_ignora_la_cabecera():
    proxies = seguridad.parsearProxies("127.0.0.1")
    assert seguridad.ipCliente("192.168.1.5", {"x-forwarded-for": "8.8.8.8"}, proxies) == "192.168.1.5"


def test_desde_proxy_toma_la_ip_mas_a_la_derecha_que_no_es_proxy():
    proxies = seguridad.parsearProxies("127.0.0.1, 10.0.0.0/8")
    cabeceras = {"x-forwarded-for": "6.6.6.6, 192.168.1.5, 10.0.0.2"}
    assert seguridad.ipCliente("127.0.0.1", cabeceras, proxies) == "192.168.1.5"


def test_desde_proxy_sin_cabecera_usa_la_conexion():
    proxies = seguridad.parsearProxies("127.0.0.1")
    assert seguridad.ipCliente("127.0.0.1", {}, proxies) == "127.0.0.1"


def test_sin_ip_de_conexion():
    assert seguridad.ipCliente(None, {}, []) == "unknown"


# ── Origin ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("origen, host, esperado", [
    ("http://192.168.1.10:3000", "192.168.1.10:3000", True),
    ("http://SERVIDOR:3000", "servidor:3000", True),
    (None, "servidor:3000", True),
    ("http://evil.example", "servidor:3000", False),
    ("http://servidor:3001", "servidor:3000", False),
    ("http://servidor:3000", None, False),
    ("null", "servidor:3000", False),
])
def test_origen_permitido(origen, host, esperado):
    assert seguridad.origenPermitido(origen, host) is esperado
