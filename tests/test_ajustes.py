"""Tests de ajustes.py con una configuración falsa (sin config.ini ni entorno)."""

import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ajustes  # noqa: E402
import telegrama  # noqa: E402

TOKEN = "123456789:AAH" + "x" * 32


def configFalsa(**cambios):
    valores = dict(
        SESSION_MAX_AGE_DAYS=30, MAX_LOGIN_ATTEMPTS=5, LOCKOUT_MINUTES=15, BCRYPT_COST=12,
        UPDATE_CHECK=True, UPDATE_REPO="usuario/repo", UPDATE_BRANCH="main",
        UPDATE_CACHE_HOURS=6, UPDATE_TIMEOUT_SECONDS=5,
        PORT=3000, BIND_HOST="0.0.0.0",
        servidorTempAviso=70, servidorTempCritica=85, servidorRutaDisco="/",
        CLEANUP_DEFAULT_DAYS=30, CLEANUP_MAX_DAYS=3650,
        telegramToken=TOKEN, telegramIdsPermitidos="222, 111",
        telegramCategorias="sistema, seguridad", telegramTimeout=10, telegramIntervalo=60,
        telegramDiscoAviso=90, telegramReiniciosBucle=3, telegramVentanaBucleMinutos=10,
    )
    valores.update(cambios)
    return SimpleNamespace(**valores)


# ── estadoTelegram ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("token, ids, activo, esperado", [
    (TOKEN, "1", True, "activo"),
    ("", "", False, "sin_configurar"),
    (TOKEN, "", False, "incompleto"),
    ("", "1", False, "incompleto"),
    (TOKEN, "1", False, "error"),
])
def test_estado_telegram(token, ids, activo, esperado):
    assert ajustes.estadoTelegram(token, ids, activo) == esperado


# ── resumen ───────────────────────────────────────────────────────────────────
def test_resumen_no_contiene_el_token():
    datos = ajustes.resumen(configFalsa(), "admin", telegramActivo=True)
    assert TOKEN not in json.dumps(datos)
    assert TOKEN[-6:] not in json.dumps(datos)
    assert datos["telegram"]["tokenConfigurado"] is True


def test_resumen_secciones_y_valores():
    datos = ajustes.resumen(configFalsa(), "admin", telegramActivo=True)
    assert set(datos) == {"cuenta", "telegram", "actualizacion", "servidor", "limpieza"}
    assert datos["cuenta"] == {"usuario": "admin", "sesionDias": 30, "intentosMaximos": 5,
                               "bloqueoMinutos": 15, "costeBcrypt": 12}
    assert datos["servidor"]["puerto"] == 3000
    assert datos["limpieza"] == {"diasPorDefecto": 30, "diasMaximos": 3650}
    assert datos["actualizacion"]["repositorio"] == "usuario/repo"


def test_resumen_ids_ordenados():
    datos = ajustes.resumen(configFalsa(), None, telegramActivo=True)
    assert datos["telegram"]["ids"] == [111, 222]
    assert datos["telegram"]["errorIds"] is None


def test_resumen_ids_invalidos_devuelve_error_y_lista_vacia():
    datos = ajustes.resumen(configFalsa(telegramIdsPermitidos="abc"), None, telegramActivo=False)
    assert datos["telegram"]["ids"] == []
    assert "TELEGRAM_IDS_PERMITIDOS" in datos["telegram"]["errorIds"]
    assert datos["telegram"]["estado"] == "error"


def test_resumen_sin_telegram():
    datos = ajustes.resumen(configFalsa(telegramToken="", telegramIdsPermitidos=""), None, telegramActivo=False)
    assert datos["telegram"]["estado"] == "sin_configurar"
    assert datos["telegram"]["tokenConfigurado"] is False
    assert datos["telegram"]["ids"] == []
    assert datos["telegram"]["errorIds"] is None


def test_resumen_marca_categorias_activas():
    datos = ajustes.resumen(configFalsa(), None, telegramActivo=True)
    activas = {c["id"] for c in datos["telegram"]["categorias"] if c["activa"]}
    assert activas == {"sistema", "seguridad"}
    assert len(datos["telegram"]["categorias"]) == len(telegrama.categoriasValidas)


def test_resumen_categorias_invalidas_quedan_apagadas_con_error():
    datos = ajustes.resumen(configFalsa(telegramCategorias="sistema, inventada"), None, telegramActivo=False)
    assert not any(c["activa"] for c in datos["telegram"]["categorias"])
    assert "inventada" in datos["telegram"]["errorCategorias"]


def test_descripciones_cubren_todas_las_categorias():
    assert set(ajustes.descripcionCategorias) == telegrama.categoriasValidas


# ── textoPrueba ───────────────────────────────────────────────────────────────
def test_texto_prueba_escapa_valores():
    texto = ajustes.textoPrueba("1.0.0", "<host>", momento=0)
    assert "&lt;host&gt;" in texto
    assert "<host>" not in texto


def test_texto_prueba_cambia_con_la_hora():
    assert ajustes.textoPrueba("1", "h", momento=0) != ajustes.textoPrueba("1", "h", momento=1)


# ── ServicioTelegram.enviarPrueba ─────────────────────────────────────────────
class NotificadorFalso:
    def __init__(self, resultado=True):
        self.resultado = resultado
        self.textos = []

    def notificar(self, texto, clave=None):
        self.textos.append(texto)
        return self.resultado


def test_enviar_prueba_ignora_categorias():
    notificador = NotificadorFalso()
    servicio = telegrama.ServicioTelegram(notificador, receptor=None, categorias=frozenset())
    assert servicio.avisar("sistema", "no") is False
    assert servicio.enviarPrueba("si") is True
    assert notificador.textos == ["si"]


def test_enviar_prueba_propaga_cola_llena():
    servicio = telegrama.ServicioTelegram(NotificadorFalso(False), receptor=None, categorias=frozenset())
    assert servicio.enviarPrueba("x") is False
