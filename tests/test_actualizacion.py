"""Tests de la lectura del CHANGELOG publicado (actualizacion.py), sin red."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import actualizacion  # noqa: E402

CHANGELOG = """# Changelog

Texto de cabecera que no es de ninguna versión.

## [Sin publicar]

### Añadido
- Algo que aún no se ha publicado.

## [1.3.0] - 2026-11-20

### Añadido
- Ventana de **novedades** al actualizar,
  con texto que sigue en la línea siguiente.
- Endpoint `GET /api/x`.

### Corregido
- Un fallo.

## [1.2.0] - 2026-11-01

- Elemento sin grupo.

## [1.1.0] - 2026-10-11

### Añadido
- Desplegable de versión.
"""


@pytest.fixture
def secciones():
    return actualizacion.leer_changelog(CHANGELOG)


def test_lee_solo_secciones_versionadas(secciones):
    assert [s["version"] for s in secciones] == ["1.3.0", "1.2.0", "1.1.0"]
    assert secciones[0]["fecha"] == "2026-11-20"


def test_sin_publicar_no_se_mezcla_con_la_anterior(secciones):
    textos = [e for s in secciones for g in s["grupos"] for e in g["elementos"]]
    assert not any("aún no se ha publicado" in t for t in textos)


def test_grupos_y_lineas_de_continuacion(secciones):
    grupos = secciones[0]["grupos"]
    assert [g["titulo"] for g in grupos] == ["Añadido", "Corregido"]
    assert grupos[0]["elementos"][0] == (
        "Ventana de **novedades** al actualizar, con texto que sigue en la línea siguiente."
    )
    assert grupos[0]["elementos"][1] == "Endpoint `GET /api/x`."


def test_elemento_sin_grupo(secciones):
    assert secciones[1]["grupos"] == [{"titulo": "", "elementos": ["Elemento sin grupo."]}]


def test_changelog_vacio_o_sin_versiones():
    assert actualizacion.leer_changelog("") == []
    assert actualizacion.leer_changelog("# Changelog\n\n## [Sin publicar]\n- x\n") == []


def test_novedades_entre_instalada_y_publicada(secciones):
    elegidas = actualizacion.novedades_entre(secciones, "1.1.0", "1.3.0")
    assert [s["version"] for s in elegidas] == ["1.3.0", "1.2.0"]


def test_novedades_no_incluye_posteriores_a_la_publicada(secciones):
    elegidas = actualizacion.novedades_entre(secciones, "1.1.0", "1.2.0")
    assert [s["version"] for s in elegidas] == ["1.2.0"]


def test_novedades_al_dia_o_versiones_raras(secciones):
    assert actualizacion.novedades_entre(secciones, "1.3.0", "1.3.0") == []
    assert actualizacion.novedades_entre(secciones, "abc", "1.3.0") == []
    assert actualizacion.novedades_entre(secciones, "1.1.0", None) == []


def test_novedades_ordenadas_y_con_tope(monkeypatch):
    texto = "".join(f"## [1.{n}.0] - 2026-01-01\n- cambio {n}\n" for n in range(1, 15))
    monkeypatch.setattr(actualizacion, "MAX_NOVEDADES", 3)
    elegidas = actualizacion.novedades_entre(actualizacion.leer_changelog(texto), "1.0.0", "1.14.0")
    assert [s["version"] for s in elegidas] == ["1.14.0", "1.13.0", "1.12.0"]


def test_consultar_novedades_al_dia_no_descarga(monkeypatch):
    def noDebeLlamarse(*args, **kwargs):
        raise AssertionError("no debería descargar el CHANGELOG")

    monkeypatch.setattr(actualizacion, "_descargar", noDebeLlamarse)
    assert actualizacion._consultar_novedades(actualizacion.__version__) == ([], None)


def test_consultar_novedades_error_de_descarga(monkeypatch):
    monkeypatch.setattr(actualizacion, "_descargar", lambda fichero, maxBytes: (None, "GitHub respondió 500"))
    assert actualizacion._consultar_novedades("999.0.0") == (None, "GitHub respondió 500")


def test_consultar_novedades_descarga_el_changelog(monkeypatch):
    pedidos = []

    def descargarFalso(fichero, maxBytes):
        pedidos.append(fichero)
        return "## [999.0.0] - 2030-01-01\n### Añadido\n- Todo nuevo\n", None

    monkeypatch.setattr(actualizacion, "_descargar", descargarFalso)
    novedades, error = actualizacion._consultar_novedades("999.0.0")
    assert error is None and pedidos == ["CHANGELOG.md"]
    assert novedades[0]["grupos"][0]["elementos"] == ["Todo nuevo"]
