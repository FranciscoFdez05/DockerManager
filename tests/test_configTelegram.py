"""Tests de configTelegram.py: prioridad .env > web > config.ini, validación y almacén."""

import json
import os
import stat
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import configTelegram  # noqa: E402
import telegrama  # noqa: E402
from telegrama import ConfiguracionInvalida  # noqa: E402

TOKEN_ENV = "111111111:ENV" + "e" * 32
TOKEN_WEB = "222222222:WEB" + "w" * 32


def cfg(**cambios):
    valores = dict(telegramToken="", telegramIdsPermitidos="", telegramCategorias="sistema, seguridad",
                   telegramTimeout=10, telegramIntervalo=60, telegramDiscoAviso=90,
                   telegramReiniciosBucle=3, telegramVentanaBucleMinutos=10, PORT=3000)
    valores.update(cambios)
    return SimpleNamespace(**valores)


def comprobarOk(token):
    return "mi_bot"


def aplicar(cambios, guardado=None, config=None, comprobar=comprobarOk):
    return configTelegram.aplicarCambios(cambios, guardado or {}, config or cfg(), comprobar)


# ── efectiva ──────────────────────────────────────────────────────────────────
def test_sin_nada_usa_config_ini():
    efectiva = configTelegram.efectiva(cfg(), {})
    assert (efectiva.token, efectiva.idsTexto, efectiva.origenToken, efectiva.origenIds) == ("", "", "", "")
    assert efectiva.categoriasTexto == "sistema, seguridad"
    assert efectiva.discoAvisoPct == 90


def test_lo_guardado_en_la_web_prevalece_sobre_config_ini():
    guardado = {"token": TOKEN_WEB, "bot": "mi_bot", "ids": "5", "categorias": ["recursos"], "discoAvisoPct": 80}
    efectiva = configTelegram.efectiva(cfg(), guardado)
    assert efectiva.token == TOKEN_WEB and efectiva.origenToken == "web" and efectiva.bot == "mi_bot"
    assert efectiva.idsTexto == "5" and efectiva.origenIds == "web"
    assert efectiva.categoriasTexto == "recursos"
    assert efectiva.discoAvisoPct == 80


def test_el_env_prevalece_sobre_la_web():
    guardado = {"token": TOKEN_WEB, "bot": "web_bot", "ids": "5"}
    efectiva = configTelegram.efectiva(cfg(telegramToken=TOKEN_ENV, telegramIdsPermitidos="7"), guardado)
    assert efectiva.token == TOKEN_ENV and efectiva.origenToken == "env"
    assert efectiva.bot is None  # el usuario del bot guardado era de otro token
    assert efectiva.idsTexto == "7" and efectiva.origenIds == "env"


def test_categorias_vacias_desde_la_web_apagan_todo():
    assert configTelegram.efectiva(cfg(), {"categorias": []}).categoriasTexto == ""


@pytest.mark.parametrize("valor", [5000, -1, "80", True, None])
def test_numero_guardado_invalido_se_ignora(valor):
    assert configTelegram.efectiva(cfg(), {"discoAvisoPct": valor}).discoAvisoPct == 90


# ── aplicarCambios ────────────────────────────────────────────────────────────
def test_guardar_token_lo_comprueba_y_guarda_el_bot():
    comprobados = []
    nuevo = aplicar({"token": f"  {TOKEN_WEB} "}, comprobar=lambda t: comprobados.append(t) or "mi_bot")
    assert comprobados == [TOKEN_WEB]
    assert nuevo == {"token": TOKEN_WEB, "bot": "mi_bot"}


def test_token_vacio_conserva_el_actual():
    guardado = {"token": TOKEN_WEB, "bot": "b"}

    def noLlamar(token):
        raise AssertionError("no debe comprobar un token vacío")

    assert aplicar({"token": "", "discoAvisoPct": 70}, guardado, comprobar=noLlamar) == {**guardado, "discoAvisoPct": 70}


def test_token_rechazado_por_telegram():
    def rechaza(token):
        raise telegrama.ErrorTelegram("getMe: HTTP 401 (Unauthorized)", codigo=401)

    with pytest.raises(ConfiguracionInvalida, match="no acepta el token"):
        aplicar({"token": TOKEN_WEB}, comprobar=rechaza)


def test_token_mal_formado():
    def comprobarReal(token):
        return telegrama.ClienteTelegram(token, 5).obtenerBot()

    with pytest.raises(ConfiguracionInvalida):
        aplicar({"token": "no-es-un-token"}, comprobar=comprobarReal)


def test_no_se_puede_pisar_lo_que_fija_el_env():
    config = cfg(telegramToken=TOKEN_ENV, telegramIdsPermitidos="7")
    with pytest.raises(ConfiguracionInvalida, match=".env"):
        aplicar({"token": TOKEN_WEB}, config=config)
    with pytest.raises(ConfiguracionInvalida, match=".env"):
        aplicar({"ids": "8"}, config=config)
    # Lo demás sí se puede cambiar aunque el token venga del .env.
    assert aplicar({"categorias": ["recursos"]}, config=config) == {"categorias": ["recursos"]}


def test_ids_se_normalizan_y_vacio_los_borra():
    assert aplicar({"ids": " 30,10 , 20"}) == {"ids": "10, 20, 30"}
    assert aplicar({"ids": ""}, {"ids": "1"}) == {}


@pytest.mark.parametrize("ids", ["-100123", "abc", 123, ["1"]])
def test_ids_invalidos(ids):
    with pytest.raises(ConfiguracionInvalida):
        aplicar({"ids": ids})


def test_categorias():
    assert aplicar({"categorias": ["seguridad", "recursos"]}) == {"categorias": ["recursos", "seguridad"]}
    assert aplicar({"categorias": []}) == {"categorias": []}
    for invalidas in (["inventada"], "seguridad", [1]):
        with pytest.raises(ConfiguracionInvalida):
            aplicar({"categorias": invalidas})


@pytest.mark.parametrize("clave, valor", [("discoAvisoPct", 100), ("discoAvisoPct", 49), ("intervaloSegundos", 5),
                                          ("timeoutSegundos", 0), ("reiniciosBucle", True), ("ventanaBucleMinutos", 2.5),
                                          ("reiniciosBucle", "3")])
def test_numeros_fuera_de_rango(clave, valor):
    with pytest.raises(ConfiguracionInvalida, match=clave):
        aplicar({clave: valor})


def test_numeros_en_rango():
    cambios = {"discoAvisoPct": 85, "intervaloSegundos": 30, "timeoutSegundos": 5,
               "reiniciosBucle": 4, "ventanaBucleMinutos": 15}
    assert aplicar(cambios) == cambios


def test_campos_desconocidos():
    with pytest.raises(ConfiguracionInvalida, match="bot"):
        aplicar({"bot": "suplantado"})


# ── Almacén ───────────────────────────────────────────────────────────────────
def test_almacen_ida_y_vuelta(tmp_path):
    almacen = configTelegram.AlmacenTelegram(str(tmp_path / "sub" / "telegram-config.json"))
    assert almacen.leer() == {}
    almacen.guardar({"token": TOKEN_WEB})
    assert almacen.leer() == {"token": TOKEN_WEB}
    almacen.borrar()
    assert almacen.leer() == {}
    almacen.borrar()  # borrar dos veces no falla


@pytest.mark.skipif(os.name == "nt", reason="Windows no aplica permisos POSIX")
def test_almacen_permisos_0600(tmp_path):
    ruta = tmp_path / "telegram-config.json"
    configTelegram.AlmacenTelegram(str(ruta)).guardar({"token": TOKEN_WEB})
    assert stat.S_IMODE(os.stat(ruta).st_mode) == 0o600


@pytest.mark.parametrize("contenido", ["{roto", "[1, 2]"])
def test_almacen_corrupto(tmp_path, contenido):
    ruta = tmp_path / "telegram-config.json"
    ruta.write_text(contenido, encoding="utf-8")
    assert configTelegram.AlmacenTelegram(str(ruta)).leer() == {}


# ── Vista para la web ─────────────────────────────────────────────────────────
def test_vista_y_detalles_no_incluyen_el_token():
    efectiva = configTelegram.efectiva(cfg(), {"token": TOKEN_WEB, "bot": "b", "ids": "1"})
    vista = configTelegram.vistaConfig(cfg(), efectiva)
    assert vista.telegramIdsPermitidos == "1" and vista.PORT == 3000
    datos = configTelegram.detalles(efectiva, hayGuardado=True)
    assert TOKEN_WEB not in json.dumps(datos)
    assert datos["origenToken"] == "web" and datos["bot"] == "b" and datos["guardadoWeb"] is True
    assert set(datos["rangos"]) == set(configTelegram.rangosNumericos)
