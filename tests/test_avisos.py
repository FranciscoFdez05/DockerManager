"""Tests de avisos.py: eventos de Docker, umbrales, actualizaciones y textos."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import avisos  # noqa: E402


class Reloj:
    def __init__(self):
        self.ahora = 1000.0

    def __call__(self):
        return self.ahora


def evento(accion, idContenedor="abc123", nombre="web", **atributos):
    return {"Type": "container", "Action": accion,
            "Actor": {"ID": idContenedor, "Attributes": {"name": nombre, **atributos}}}


@pytest.fixture
def reloj():
    return Reloj()


@pytest.fixture
def procesador(reloj):
    return avisos.ProcesadorEventosDocker(reiniciosBucle=3, ventanaBucle=600, reloj=reloj)


# ── Eventos de Docker ─────────────────────────────────────────────────────────
def test_caida_con_error_avisa(procesador):
    clave, texto = procesador.procesar(evento("die", exitCode="1"))
    assert clave == "caida:abc123"
    assert "web" in texto and "1" in texto


def test_salida_limpia_no_avisa(procesador):
    assert procesador.procesar(evento("die", exitCode="0")) is None


@pytest.mark.parametrize("previa", ["kill", "stop"])
def test_parada_voluntaria_no_avisa(procesador, reloj, previa):
    procesador.procesar(evento(previa))
    reloj.ahora += 5
    assert procesador.procesar(evento("die", exitCode="143")) is None


def test_caida_tiempo_despues_de_una_parada_si_avisa(procesador, reloj):
    procesador.procesar(evento("kill"))
    reloj.ahora += 120
    assert procesador.procesar(evento("die", exitCode="1")) is not None


def test_oom_avisa_una_vez(procesador):
    clave, _ = procesador.procesar(evento("oom"))
    assert clave == "oom:abc123"
    assert procesador.procesar(evento("die", exitCode="137")) is None


def test_bucle_de_reinicios(procesador, reloj):
    assert procesador.procesar(evento("die", exitCode="1"))[0] == "caida:abc123"
    reloj.ahora += 10
    procesador.procesar(evento("die", exitCode="1"))
    reloj.ahora += 10
    clave, texto = procesador.procesar(evento("die", exitCode="2"))
    assert clave == "bucle:abc123"
    assert "3 veces" in texto
    reloj.ahora += 10
    assert procesador.procesar(evento("die", exitCode="1")) is None  # no repetir cada caída
    reloj.ahora += 700
    assert procesador.procesar(evento("die", exitCode="1"))[0] == "caida:abc123"


def test_unhealthy(procesador):
    assert procesador.procesar(evento("health_status: unhealthy"))[0] == "unhealthy:abc123"
    assert procesador.procesar(evento("health_status: healthy")) is None


def test_ignora_otros_tipos_y_eventos_sin_id(procesador):
    assert procesador.procesar({"Type": "network", "Action": "die"}) is None
    assert procesador.procesar({"Type": "container", "Action": "die", "Actor": {}}) is None
    assert procesador.procesar(evento("start")) is None


def test_destroy_limpia_estado(procesador):
    procesador.procesar(evento("kill"))
    procesador.procesar(evento("destroy"))
    assert procesador.procesar(evento("die", exitCode="1")) is not None


def test_nombre_escapado(procesador):
    _, texto = procesador.procesar(evento("die", nombre="<script>", exitCode="1"))
    assert "<script>" not in texto
    assert "&lt;script&gt;" in texto


# ── Recursos ──────────────────────────────────────────────────────────────────
def test_umbral_con_histeresis():
    umbral = avisos.Umbral(80, 5)
    assert umbral.evaluar(79) is None
    assert umbral.evaluar(80) == "alerta"
    assert umbral.evaluar(90) is None
    assert umbral.evaluar(76) is None  # dentro del margen
    assert umbral.evaluar(74) == "recuperado"
    assert umbral.evaluar(None) is None


def test_vigilante_recursos():
    lecturas = {"temp": 50.0, "disco": 50.0}
    vigilante = avisos.VigilanteRecursos(lambda: lecturas["temp"], lambda: lecturas["disco"], 85, 90)
    assert vigilante.comprobar() == []
    lecturas.update(temp=90.0, disco=95.0)
    assert [c for c, _ in vigilante.comprobar()] == ["temperatura:alerta", "disco:alerta"]
    assert vigilante.comprobar() == []
    lecturas.update(temp=60.0, disco=80.0)
    assert [c for c, _ in vigilante.comprobar()] == ["temperatura:ok", "disco:ok"]


def test_vigilante_recursos_sin_sensor():
    assert avisos.VigilanteRecursos(lambda: None, lambda: None, 85, 90).comprobar() == []


# ── Actualizaciones ───────────────────────────────────────────────────────────
def vigilanteActualizaciones(ruta, remota, estado):
    return avisos.VigilanteActualizaciones(lambda: remota, lambda: estado,
                                           avisos.EstadoPersistente(str(ruta)), "1.1.0")


def test_version_nueva_se_avisa_una_vez_incluso_tras_reiniciar(tmp_path):
    ruta = tmp_path / "telegram.json"
    remota = {"publicada": "1.2.0", "hayNueva": True}
    avisosEmitidos = vigilanteActualizaciones(ruta, remota, {}).comprobar()
    assert [c for c, _ in avisosEmitidos] == ["version:1.2.0"]
    assert vigilanteActualizaciones(ruta, remota, {}).comprobar() == []


def test_sin_version_nueva_no_avisa(tmp_path):
    assert vigilanteActualizaciones(tmp_path / "t.json", {"publicada": "1.1.0", "hayNueva": False}, {}).comprobar() == []


def test_resultado_de_actualizacion(tmp_path):
    ruta = tmp_path / "telegram.json"
    sinNueva = {"hayNueva": False}
    viejo = {"ultimo": {"estado": "ok", "actualizadoEn": "2026-01-01T00:00:00"}}
    # Primera vez: el resultado antiguo solo se toma como referencia.
    assert vigilanteActualizaciones(ruta, sinNueva, viejo).comprobar() == []
    fallo = {"ultimo": {"estado": "fallo", "codigo": 3, "actualizadoEn": "2026-11-17T10:00:00",
                        "detalle": "error <grave>"}}
    resultado = vigilanteActualizaciones(ruta, sinNueva, fallo).comprobar()
    assert len(resultado) == 1
    assert "ha fallado" in resultado[0][1]
    assert "&lt;grave&gt;" in resultado[0][1]
    assert vigilanteActualizaciones(ruta, sinNueva, fallo).comprobar() == []


def test_estado_persistente_ilegible(tmp_path):
    ruta = tmp_path / "telegram.json"
    ruta.write_text("{roto", encoding="utf-8")
    estado = avisos.EstadoPersistente(str(ruta))
    assert estado.obtener("x") is None
    estado.guardar("x", 1)
    assert avisos.EstadoPersistente(str(ruta)).obtener("x") == 1


# ── Textos y comandos ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("funcion", [avisos.textoLogin, avisos.textoCambioPassword,
                                     avisos.textoConfiguracionInicial])
def test_textos_de_seguridad_escapan_ip(funcion):
    assert "<img" not in funcion("<img src=x>")


def test_textoContenedores_ordena_y_limita():
    contenedores = [{"name": f"c{i:03}", "status": "running", "health": None} for i in range(70)]
    contenedores.append({"name": "roto", "status": "running", "health": "unhealthy"})
    contenedores.append({"name": "parado", "status": "exited", "health": None})
    lineas = avisos.textoContenedores(contenedores).splitlines()
    assert "roto" in lineas[1]
    assert "parado" in lineas[2]
    assert lineas[-1] == "... y 12 más"


def test_textoContenedores_vacio():
    assert avisos.textoContenedores([]) == "No hay contenedores."


def test_textoEstado():
    contenedores = [{"status": "running"}, {"status": "exited"}, {"status": "restarting"}]
    texto = avisos.textoEstado("1.1.0", contenedores, {"cpu_pct": 5, "mem_pct": 40, "disk_pct": 70, "cpu_temp": 51.4})
    assert "1/3 en marcha, 1 con problemas" in texto
    assert "51 ºC" in texto


def test_crearComandos_incluye_ayuda():
    comandos = avisos.crearComandos("1.0", lambda: [], lambda: {})
    assert {"/ayuda", "/start", "/estado", "/contenedores"} <= comandos.keys()
    assert comandos["/contenedores"]() == "No hay contenedores."


# ── Vigilancia ────────────────────────────────────────────────────────────────
def test_un_fallo_en_una_comprobacion_no_para_las_demas():
    emitidos = []
    vigilancia = avisos.Vigilancia(lambda cat, texto, clave: emitidos.append((cat, clave)), intervalo=60)

    def falla():
        raise OSError("sensor")

    vigilancia.comprobarPeriodicamente("recursos", falla)
    vigilancia.comprobarPeriodicamente("actualizaciones", lambda: [("k", "texto")])
    vigilancia.ejecutarComprobaciones()
    assert emitidos == [("actualizaciones", "k")]
