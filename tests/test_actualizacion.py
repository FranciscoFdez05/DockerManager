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


# ── Commit fijo: evita la caché de 5 minutos del CDN de raw.githubusercontent ──
SHA = "bcd08e161d5fcb44ff5dcc653dffb5df93ef7f72"


class RespuestaFalsa:
    """Imita lo justo de la respuesta de urlopen (context manager + read)."""

    def __init__(self, cuerpo: bytes):
        self.cuerpo = cuerpo

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, maxBytes: int = -1) -> bytes:
        return self.cuerpo


@pytest.fixture
def cacheLimpia(monkeypatch):
    monkeypatch.setattr(actualizacion, "_cache", {})
    monkeypatch.setattr(actualizacion.cfg_ini, "UPDATE_CHECK", True)
    monkeypatch.setattr(actualizacion.cfg_ini, "UPDATE_REPO", "usuario/repo")
    monkeypatch.setattr(actualizacion.cfg_ini, "UPDATE_BRANCH", "main")


def test_url_usa_el_sha_si_lo_hay(cacheLimpia):
    assert actualizacion._url("version.py", SHA) == f"https://raw.githubusercontent.com/usuario/repo/{SHA}/version.py"
    assert actualizacion._url("version.py") == "https://raw.githubusercontent.com/usuario/repo/main/version.py"


def test_resolver_commit_devuelve_el_sha(cacheLimpia, monkeypatch):
    pedidas = []

    def urlopenFalso(peticion, timeout):
        pedidas.append((peticion.full_url, peticion.get_header("Accept")))
        return RespuestaFalsa(SHA.encode() + b"\n")

    monkeypatch.setattr(actualizacion, "urlopen", urlopenFalso)
    assert actualizacion._resolver_commit() == (SHA, None)
    assert pedidas == [("https://api.github.com/repos/usuario/repo/commits/main", "application/vnd.github.sha")]


def test_resolver_commit_rechaza_respuesta_que_no_es_un_sha(cacheLimpia, monkeypatch):
    monkeypatch.setattr(actualizacion, "urlopen", lambda peticion, timeout: RespuestaFalsa(b"<html>nope</html>"))
    sha, error = actualizacion._resolver_commit()
    assert sha is None and "commit" in error


def test_resolver_commit_con_red_caida_no_lanza(cacheLimpia, monkeypatch):
    def urlopenFalso(peticion, timeout):
        raise actualizacion.URLError("sin red")

    monkeypatch.setattr(actualizacion, "urlopen", urlopenFalso)
    sha, error = actualizacion._resolver_commit()
    assert sha is None and "API de GitHub" in error


def test_resolver_commit_con_repositorio_invalido_no_pide_nada(cacheLimpia, monkeypatch):
    monkeypatch.setattr(actualizacion.cfg_ini, "UPDATE_REPO", "../otro")
    monkeypatch.setattr(actualizacion, "urlopen", lambda *a, **k: pytest.fail("no debe pedir nada"))
    assert actualizacion._resolver_commit()[0] is None


def test_version_remota_lee_del_commit_resuelto(cacheLimpia, monkeypatch):
    urls = []

    def urlopenFalso(peticion, timeout):
        urls.append(peticion.full_url)
        if "api.github.com" in peticion.full_url:
            return RespuestaFalsa(SHA.encode())
        if peticion.full_url.endswith("version.py"):
            return RespuestaFalsa(b'__version__ = "999.0.0"\n')
        return RespuestaFalsa("## [999.0.0] - 2030-01-01\n### Añadido\n- Nuevo\n".encode())

    monkeypatch.setattr(actualizacion, "urlopen", urlopenFalso)
    remota = actualizacion.version_remota(forzar=True)
    assert remota["publicada"] == "999.0.0" and remota["hayNueva"] is True
    assert all(SHA in url for url in urls if "raw.githubusercontent.com" in url)
    assert len([u for u in urls if "raw.githubusercontent.com" in u]) == 2


def test_version_remota_sin_api_sigue_por_la_rama(cacheLimpia, monkeypatch):
    urls = []

    def urlopenFalso(peticion, timeout):
        urls.append(peticion.full_url)
        if "api.github.com" in peticion.full_url:
            raise actualizacion.HTTPError(peticion.full_url, 403, "rate limit", {}, None)
        return RespuestaFalsa(f'__version__ = "{actualizacion.__version__}"\n'.encode())

    monkeypatch.setattr(actualizacion, "urlopen", urlopenFalso)
    remota = actualizacion.version_remota(forzar=True)
    assert remota["publicada"] == actualizacion.__version__ and remota["error"] is None
    assert "https://raw.githubusercontent.com/usuario/repo/main/version.py" in urls
