"""Comprobación de versión nueva y petición de actualización.

La comprobación lee `version.py` de la rama configurada en GitHub: lo que va a
instalar `docker-update.sh` no es una etiqueta ni una release, es lo que traiga
`git pull --ff-only` de esa rama, así que esa es la versión que responde a «si
pulso, ¿a qué versión voy?».

Actualizar no lo hace la app. Dentro del contenedor no hay repositorio ni
compose, y aunque tenga el socket de Docker, `docker-update.sh` recrea el
contenedor de DockerManager: se mataría a sí misma antes de comprobar la salud
y volver atrás. Por eso aquí solo se deja una señal en un directorio compartido
con el host, y quien actualiza es el vigilante (tools/actualizador/), un
temporizador de systemd que corre fuera del contenedor.

Ficheros del directorio compartido:
  solicitada.json  lo escribe la app al pulsar «Actualizar»; el vigilante lo
                   borra antes de empezar.
  estado.json      lo escribe el vigilante: en_marcha, ok o fallo.
  latido           el vigilante lo toca en cada pasada (cada 30 s): si no se
                   ha tocado hace rato, el vigilante no está instalado.
"""

import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import config as cfg_ini
from version import __version__

SOLICITADA = "solicitada.json"
ESTADO = "estado.json"
LATIDO = "latido"

# Una petición que nadie recoge en este tiempo se da por perdida: el botón no
# puede quedarse girando para siempre.
ESPERA_MAXIMA_SEGUNDOS = 900
LATIDO_MAX_SEGUNDOS = 120

# Un fallo se recuerda mucho menos que un acierto: puede ser un corte de un
# minuto. Y «Comprobar» salta la caché, pero no deja pulsar treinta veces.
ESPERA_TRAS_FALLO_SEGUNDOS = 600
ESPERA_MINIMA_FORZADO_SEGUNDOS = 20

# version.py son unos bytes; el tope es por si la URL acaba sirviendo otra cosa.
MAX_BYTES = 64 * 1024
# El CHANGELOG crece con cada versión: tope más holgado, pero tope.
MAX_BYTES_CHANGELOG = 512 * 1024
# Con muchas versiones de salto, la ventana no puede convertirse en un libro.
MAX_NOVEDADES = 10

_PATRON_SHA = re.compile(r"^[0-9a-f]{40}$")
_PATRON_VERSION = re.compile(r'^__version__\s*=\s*"(\d+(?:\.\d+)*)"', re.M)
_PATRON_REPOSITORIO = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_PATRON_RAMA = re.compile(r"^[A-Za-z0-9._/-]+$")
# «## [1.2.0] - 2026-11-17»; «## [Sin publicar]» no casa a propósito: no es
# algo que vaya a instalar el pull.
_PATRON_SECCION = re.compile(r"^##\s+\[(\d+(?:\.\d+)*)\](?:\s*-\s*(\S+))?\s*$")
_PATRON_GRUPO = re.compile(r"^###\s+(.+?)\s*$")
_PATRON_ELEMENTO = re.compile(r"^[-*]\s+(.*)$")

_cerrojo = threading.Lock()
_cache: dict = {}

logger = logging.getLogger(__name__)


class VigilanteAusente(Exception):
    pass


def _ruta(nombre: str) -> str:
    return os.path.join(cfg_ini.UPDATE_DIR, nombre)


def _tupla(version):
    try:
        return tuple(int(p) for p in str(version).strip().split("."))
    except (TypeError, ValueError):
        return None


def hay_nueva(instalada, publicada):
    """True, False o None si no se pueden comparar. None no es «al día»."""
    a, b = _tupla(instalada), _tupla(publicada)
    if not a or not b:
        return None
    return b > a


# ── Versión publicada ─────────────────────────────────────────────────────────
def _repositorio_y_rama() -> tuple | None:
    """(repositorio, rama) de config.ini ya validados, o None si no son seguros para una URL."""
    repositorio = cfg_ini.UPDATE_REPO.strip().strip("/")
    rama = cfg_ini.UPDATE_BRANCH.strip().strip("/")
    if not _PATRON_REPOSITORIO.match(repositorio) or not _PATRON_RAMA.match(rama):
        return None
    if ".." in repositorio or ".." in rama:
        return None
    return repositorio, rama


def _url(fichero: str = "version.py", ref: str | None = None):
    """URL de `fichero` en raw.githubusercontent.com.

    Args:
        fichero: Ruta del fichero dentro del repositorio.
        ref: SHA del commit. Sin él se usa la rama, que el CDN de GitHub sirve
            con hasta 5 minutos de retraso (Cache-Control: max-age=300).

    Returns:
        La URL, o None si el repositorio o la rama configurados no son válidos.
    """
    destino = _repositorio_y_rama()
    if destino is None:
        return None
    repositorio, rama = destino
    return f"https://raw.githubusercontent.com/{repositorio}/{ref or rama}/{fichero}"


def _resolver_commit() -> tuple:
    """SHA del último commit de la rama configurada, consultado a la API de GitHub.

    raw.githubusercontent.com cachea cada rama 5 minutos en su CDN y no lo salta
    ni con parámetros en la URL: justo tras publicar, «Comprobar» seguía viendo
    la versión anterior. Una URL por SHA es inmutable y nunca sale caducada, y la
    API sí responde al momento. Cuesta una petición del cupo anónimo de la API
    (60 por hora y IP), de sobra con la caché de horas y la espera mínima al forzar.

    Returns:
        Tupla (sha, error): uno de los dos es None. No lanza.
    """
    destino = _repositorio_y_rama()
    if destino is None:
        return None, "El repositorio o la rama configurados no son válidos"
    repositorio, rama = destino
    peticion = Request(
        f"https://api.github.com/repos/{repositorio}/commits/{rama}",
        headers={"User-Agent": f"DockerManager/{__version__}", "Accept": "application/vnd.github.sha"},
    )
    try:
        with urlopen(peticion, timeout=cfg_ini.UPDATE_TIMEOUT_SECONDS) as respuesta:
            sha = respuesta.read(128).decode("ascii", errors="replace").strip()
    except HTTPError as error:
        return None, f"La API de GitHub respondió {error.code}"
    except (TimeoutError, URLError, OSError) as error:
        return None, f"No se pudo conectar con la API de GitHub ({error})"
    if not _PATRON_SHA.match(sha):
        return None, "La API de GitHub no devolvió un commit válido"
    return sha, None


def _descargar(fichero: str, maxBytes: int) -> tuple:
    """Descarga un fichero del commit resuelto en la última comprobación o, si no lo hay, de la rama.

    Args:
        fichero: Ruta del fichero dentro del repositorio.
        maxBytes: Bytes máximos que se leen de la respuesta.

    Returns:
        Tupla (texto, error): uno de los dos es None. No lanza.
    """
    url = _url(fichero, _cache.get("ref"))
    if url is None:
        return None, "El repositorio o la rama configurados no son válidos"
    peticion = Request(url, headers={"User-Agent": f"DockerManager/{__version__}", "Accept": "text/plain"})
    try:
        with urlopen(peticion, timeout=cfg_ini.UPDATE_TIMEOUT_SECONDS) as respuesta:
            return respuesta.read(maxBytes).decode("utf-8", errors="replace"), None
    except HTTPError as error:
        if error.code == 404:
            return None, "GitHub no encuentra el repositorio o la rama configurados"
        return None, f"GitHub respondió {error.code}"
    except (TimeoutError, URLError, OSError) as error:
        return None, f"No se pudo conectar con GitHub ({error})"


def _consultar_github():
    """(version, error). No lanza."""
    texto, error = _descargar("version.py", MAX_BYTES)
    if error:
        return None, error
    encontrado = _PATRON_VERSION.search(texto)
    if not encontrado:
        return None, "La respuesta de GitHub no declara ninguna versión"
    return encontrado.group(1), None


# ── Novedades (CHANGELOG.md publicado) ────────────────────────────────────────
def leer_changelog(texto: str) -> list:
    """Extrae las secciones versionadas de un CHANGELOG en formato Keep a Changelog.

    Solo entiende lo que usa este proyecto: «## [x.y.z] - fecha», «### Grupo» y
    elementos de lista, que pueden seguir en líneas sangradas. Lo demás se
    ignora. El texto de cada elemento queda tal cual (Markdown en línea): lo
    escapa y formatea el navegador.

    Args:
        texto: Contenido completo del CHANGELOG.

    Returns:
        Lista de dicts {"version", "fecha", "grupos": [{"titulo", "elementos"}]}
        en el orden del fichero.

    Example:
        >>> leer_changelog("## [1.1.0] - 2026-10-11\n### Añadido\n- Algo")[0]["version"]
        '1.1.0'
    """
    secciones = []
    seccion = grupo = None
    for linea in texto.splitlines():
        if linea.startswith("## "):
            encontrada = _PATRON_SECCION.match(linea)
            # Una sección no versionada («Sin publicar») corta la anterior aunque no se recoja.
            seccion = grupo = None
            if encontrada:
                seccion = {"version": encontrada.group(1), "fecha": encontrada.group(2), "grupos": []}
                secciones.append(seccion)
            continue
        if seccion is None:
            continue
        titulo = _PATRON_GRUPO.match(linea)
        if titulo:
            grupo = {"titulo": titulo.group(1), "elementos": []}
            seccion["grupos"].append(grupo)
            continue
        elemento = _PATRON_ELEMENTO.match(linea)
        if elemento:
            if grupo is None:
                grupo = {"titulo": "", "elementos": []}
                seccion["grupos"].append(grupo)
            grupo["elementos"].append(elemento.group(1).strip())
        elif linea[:1] in (" ", "\t") and linea.strip() and grupo and grupo["elementos"]:
            grupo["elementos"][-1] += " " + linea.strip()
    return secciones


def novedades_entre(secciones: list, instalada: str, publicada: str) -> list:
    """Secciones que traería pasar de `instalada` a `publicada`, la más nueva primero.

    Args:
        secciones: Salida de `leer_changelog`.
        instalada: Versión en marcha.
        publicada: Versión que instalaría la actualización.

    Returns:
        Las secciones con instalada < versión <= publicada, como mucho
        MAX_NOVEDADES. Lista vacía si alguna versión no se puede comparar.
    """
    desde, hasta = _tupla(instalada), _tupla(publicada)
    if not desde or not hasta:
        return []
    elegidas = [s for s in secciones if (v := _tupla(s["version"])) and desde < v <= hasta]
    elegidas.sort(key=lambda s: _tupla(s["version"]), reverse=True)
    return elegidas[:MAX_NOVEDADES]


def _consultar_novedades(publicada: str) -> tuple:
    """(secciones, error) del CHANGELOG publicado. No lanza.

    Solo se descarga si hay versión nueva: estando al día no hay nada que contar
    y se ahorra la petición.
    """
    if not hay_nueva(__version__, publicada):
        return [], None
    texto, error = _descargar("CHANGELOG.md", MAX_BYTES_CHANGELOG)
    if error:
        return None, error
    return novedades_entre(leer_changelog(texto), __version__, publicada), None


def _vigente(forzar: bool) -> bool:
    if not _cache:
        return False
    edad = time.time() - _cache["momento"]
    if forzar:
        return edad < ESPERA_MINIMA_FORZADO_SEGUNDOS
    if _cache.get("error"):
        return edad < ESPERA_TRAS_FALLO_SEGUNDOS
    return edad < cfg_ini.UPDATE_CACHE_HOURS * 3600


def version_remota(forzar: bool = False) -> dict:
    if not cfg_ini.UPDATE_CHECK:
        return {"activo": False, "publicada": None, "hayNueva": None, "comprobado": None, "error": None,
                "novedades": None, "errorNovedades": None}

    with _cerrojo:
        if not _vigente(forzar):
            # version.py y CHANGELOG.md se leen del mismo commit. Si la API falla
            # (cupo agotado, caída) se sigue por la rama: puede llegar con unos
            # minutos de retraso, pero llega.
            ref, errorRef = _resolver_commit()
            if errorRef:
                logger.warning("Comprobación de versión por la rama, sin commit fijo: %s", errorRef)
            _cache["ref"] = ref
            publicada, error = _consultar_github()
            # Con GitHub caído se conserva la última versión conocida: es más
            # útil que un hueco, y el error dice de cuándo es.
            _cache.update({
                "publicada": publicada or _cache.get("publicada"),
                "error": error,
                "momento": time.time(),
            })
            if publicada:
                # Un CHANGELOG que no baja no invalida la versión: solo faltan las notas.
                novedades, errorNovedades = _consultar_novedades(publicada)
                _cache.update({"novedades": novedades, "errorNovedades": errorNovedades})
        publicada = _cache.get("publicada")
        return {
            "activo": True,
            "publicada": publicada,
            "hayNueva": hay_nueva(__version__, publicada) if publicada else None,
            "comprobado": datetime.fromtimestamp(_cache["momento"]).isoformat(timespec="seconds"),
            "error": _cache.get("error"),
            "novedades": _cache.get("novedades"),
            "errorNovedades": _cache.get("errorNovedades"),
        }


# ── Señal para el vigilante ───────────────────────────────────────────────────
def _leer_json(nombre: str):
    try:
        with open(_ruta(nombre), encoding="utf-8") as f:
            datos = json.load(f)
        return datos if isinstance(datos, dict) else None
    except (OSError, ValueError):
        return None


def _escribir_json_atomico(nombre: str, datos: dict):
    # Temporal + rename: el vigilante nunca lee un JSON a medio escribir.
    os.makedirs(cfg_ini.UPDATE_DIR, exist_ok=True)
    temporal = _ruta(nombre + ".tmp")
    with open(temporal, "w", encoding="utf-8") as f:
        json.dump(datos, f)
    os.replace(temporal, _ruta(nombre))


def _segundos_desde(iso):
    try:
        return time.time() - datetime.fromisoformat(str(iso)).timestamp()
    except (TypeError, ValueError):
        return None


def estado() -> dict:
    """Si hay una actualización en marcha, si el vigilante vive y cómo acabó la última."""
    solicitud = _leer_json(SOLICITADA)
    estado_vigilante = _leer_json(ESTADO) or {}

    # «En marcha» es tanto «pedida y aún sin recoger» como «el vigilante está en ello».
    en_marcha = solicitud is not None or estado_vigilante.get("estado") == "en_marcha"
    solicitada_en = (solicitud or {}).get("solicitadaEn") or estado_vigilante.get("solicitadaEn")
    edad = _segundos_desde(solicitada_en)
    if en_marcha and (edad is None or edad > ESPERA_MAXIMA_SEGUNDOS):
        en_marcha = False

    try:
        vigilante_visto = time.time() - os.path.getmtime(_ruta(LATIDO)) < LATIDO_MAX_SEGUNDOS
    except OSError:
        vigilante_visto = False

    ultimo = None
    if estado_vigilante.get("estado") in ("ok", "fallo"):
        ultimo = {
            "estado": estado_vigilante["estado"],
            "codigo": estado_vigilante.get("codigo"),
            "solicitadaEn": estado_vigilante.get("solicitadaEn"),
            "actualizadoEn": estado_vigilante.get("actualizadoEn"),
            "detalle": estado_vigilante.get("detalle", ""),
        }

    return {
        "version": __version__,
        "enMarcha": en_marcha,
        "solicitadaEn": solicitada_en if en_marcha else None,
        "vigilanteVisto": vigilante_visto,
        "ultimo": ultimo,
    }


def solicitar() -> dict:
    """Deja la señal. No hace pull, ni build, ni reinicio: eso es del vigilante."""
    actual = estado()
    if actual["enMarcha"]:
        return {"aceptada": False, **actual}
    # Sin vigilante la petición se quedaría ahí y saltaría por sorpresa el día
    # que alguien lo instale.
    if not actual["vigilanteVisto"]:
        raise VigilanteAusente("El vigilante de actualizaciones no está instalado o no da señales de vida")
    _escribir_json_atomico(SOLICITADA, {
        "solicitadaEn": datetime.now().isoformat(timespec="seconds"),
        "versionInstalada": __version__,
    })
    return {"aceptada": True, **estado()}
