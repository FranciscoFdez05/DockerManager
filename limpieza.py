"""Limpieza selectiva de imágenes y volúmenes en desuso.

El «Prune» general borra todo lo huérfano de golpe y sin enseñar qué. Aquí se
calcula primero la lista de candidatos (para que la interfaz la muestre) y
después se borra exactamente esa lista, filtrando por antigüedad: una imagen
recién descargada o un volumen recién creado que aún no usa nadie no es
«viejo», puede ser el paso previo a levantar un stack.

«En desuso» significa que ningún contenedor, ni siquiera uno parado, lo
referencia. Un contenedor parado puede volver a arrancarse y necesita su
imagen y sus volúmenes, así que no se tocan.

No se usa `volumes.prune()`: desde la API 1.42 solo borra volúmenes anónimos
salvo que se pase `all=true`, y no admite el filtro `until`, así que no
permite ni la vista previa ni el filtro por antigüedad.
"""

import logging
from datetime import datetime, timedelta, timezone

import docker.errors

logger = logging.getLogger(__name__)

TIPOS_VALIDOS = ("imagenes", "volumenes")


class LimpiezaInvalida(ValueError):
    """Parámetros de limpieza fuera de rango."""


def parsearFecha(texto: str | None) -> datetime | None:
    """Convierte la fecha que devuelve Docker a datetime UTC.

    Docker da las imágenes con nanosegundos («2024-05-01T10:00:00.123456789Z»)
    y los volúmenes a veces con desfase («2024-05-01T12:00:00+02:00»). Se
    toman los 19 primeros caracteres y se ignora el desfase: el filtro trabaja
    en días y unas horas de error no cambian el resultado de forma relevante.

    Args:
        texto: Fecha en formato ISO 8601 tal como la devuelve Docker.

    Returns:
        La fecha como datetime con zona UTC, o None si no se puede leer.
    """
    if not texto:
        return None
    try:
        return datetime.strptime(texto[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def validarDias(dias: int, maximo: int) -> int:
    """Comprueba que la antigüedad mínima está en rango.

    Args:
        dias: Antigüedad mínima en días (0 = cualquier antigüedad).
        maximo: Valor máximo admitido.

    Returns:
        Los días validados.

    Raises:
        LimpiezaInvalida: Si los días son negativos o superan el máximo.
    """
    if dias < 0 or dias > maximo:
        raise LimpiezaInvalida(f"Los días deben estar entre 0 y {maximo}")
    return dias


def _referenciasEnUso(client) -> tuple[set[str], set[str]]:
    """IDs de imagen y nombres de volumen que usa algún contenedor (de cualquier estado)."""
    imagenesUsadas: set[str] = set()
    volumenesUsados: set[str] = set()
    for contenedor in client.containers.list(all=True):
        atributos = contenedor.attrs or {}
        if atributos.get("Image"):
            imagenesUsadas.add(atributos["Image"])
        for montaje in atributos.get("Mounts") or []:
            if montaje.get("Type") == "volume" and montaje.get("Name"):
                volumenesUsados.add(montaje["Name"])
    return imagenesUsadas, volumenesUsados


def _esAntiguo(fecha: datetime | None, limite: datetime) -> bool:
    # Sin fecha legible no se puede afirmar que sea viejo: se deja en paz.
    return fecha is not None and fecha <= limite


def _imagenesCandidatas(client, imagenesUsadas: set[str], limite: datetime) -> list[dict]:
    candidatas = []
    for imagen in client.images.list():
        atributos = imagen.attrs or {}
        if imagen.id in imagenesUsadas:
            continue
        fecha = parsearFecha(atributos.get("Created"))
        if not _esAntiguo(fecha, limite):
            continue
        candidatas.append({
            "id": imagen.id.replace("sha256:", "")[:12],
            "tags": list(imagen.tags),
            "size_mb": round((atributos.get("Size") or 0) / (1024**2), 1),
            "created": fecha.strftime("%Y-%m-%d"),
        })
    return candidatas


def _volumenesCandidatos(client, volumenesUsados: set[str], limite: datetime) -> list[dict]:
    candidatos = []
    for volumen in client.volumes.list():
        if volumen.name in volumenesUsados:
            continue
        fecha = parsearFecha((volumen.attrs or {}).get("CreatedAt"))
        if not _esAntiguo(fecha, limite):
            continue
        candidatos.append({
            "name": volumen.name,
            "driver": (volumen.attrs or {}).get("Driver", ""),
            "created": fecha.strftime("%Y-%m-%d"),
        })
    return candidatos


def candidatos(client, dias: int, ahora: datetime | None = None) -> dict:
    """Calcula qué imágenes y volúmenes se borrarían.

    Args:
        client: Cliente de Docker (`docker.DockerClient` o un doble de test).
        dias: Antigüedad mínima en días; 0 incluye todo lo que no se usa.
        ahora: Instante de referencia; se inyecta en los tests.

    Returns:
        Diccionario con `imagenes`, `volumenes` y `dias`.

    Example:
        >>> candidatos(docker.from_env(), dias=30)["imagenes"]
        [{'id': '1a2b3c4d5e6f', 'tags': ['nginx:1.25'], 'size_mb': 187.4, 'created': '2024-02-01'}]
    """
    limite = (ahora or datetime.now(timezone.utc)) - timedelta(days=dias)
    imagenesUsadas, volumenesUsados = _referenciasEnUso(client)
    return {
        "dias": dias,
        "imagenes": _imagenesCandidatas(client, imagenesUsadas, limite),
        "volumenes": _volumenesCandidatos(client, volumenesUsados, limite),
    }


def _borrarImagen(client, imagen: dict) -> None:
    # Una imagen con varios tags no se puede borrar por ID sin forzar. Ya se
    # ha comprobado que ningún contenedor la usa, así que forzar solo quita
    # los tags; con un único tag no se fuerza para que Docker siga protegiendo
    # una imagen que alguien haya empezado a usar entre la vista y el borrado.
    client.images.remove(imagen["id"], force=len(imagen["tags"]) > 1)


def _borrarVolumen(client, volumen: dict) -> None:
    client.volumes.get(volumen["name"]).remove()


def _borrarLista(elementos: list[dict], borrar, clave: str) -> tuple[list[str], list[dict]]:
    """Borra cada elemento y sigue aunque alguno falle.

    Un fallo es normal (imagen con imágenes hijas, volumen que acaba de
    montarse) y no debe impedir borrar el resto.
    """
    borrados: list[str] = []
    errores: list[dict] = []
    for elemento in elementos:
        try:
            borrar(elemento)
            borrados.append(elemento[clave])
        except docker.errors.APIError as error:
            motivo = error.explanation or str(error)
            logger.warning("No se pudo borrar %s: %s", elemento[clave], motivo)
            errores.append({"item": elemento[clave], "error": str(motivo)})
    return borrados, errores


def limpiar(client, dias: int, tipos: set[str], ahora: datetime | None = None) -> dict:
    """Borra las imágenes y/o volúmenes en desuso con la antigüedad indicada.

    La lista se recalcula aquí en lugar de fiarse de la que vio el usuario:
    entre la vista previa y la confirmación algo pudo empezar a usarse.

    Args:
        client: Cliente de Docker.
        dias: Antigüedad mínima en días.
        tipos: Subconjunto de `TIPOS_VALIDOS` a limpiar.
        ahora: Instante de referencia; se inyecta en los tests.

    Returns:
        Diccionario con lo borrado, los errores y los MB liberados por imágenes.

    Raises:
        LimpiezaInvalida: Si `tipos` está vacío o contiene un tipo desconocido.
    """
    if not tipos or not tipos <= set(TIPOS_VALIDOS):
        raise LimpiezaInvalida(f"Tipos válidos: {', '.join(TIPOS_VALIDOS)}")

    lista = candidatos(client, dias, ahora)
    imagenes = lista["imagenes"] if "imagenes" in tipos else []
    volumenes = lista["volumenes"] if "volumenes" in tipos else []

    imagenesBorradas, erroresImagenes = _borrarLista(imagenes, lambda i: _borrarImagen(client, i), "id")
    volumenesBorrados, erroresVolumenes = _borrarLista(volumenes, lambda v: _borrarVolumen(client, v), "name")

    borradasSet = set(imagenesBorradas)
    # Aproximado: capas compartidas con otras imágenes no se liberan de verdad.
    liberadoMb = round(sum(i["size_mb"] for i in imagenes if i["id"] in borradasSet), 1)
    logger.info("Limpieza (%s días): %d imágenes, %d volúmenes borrados",
                dias, len(imagenesBorradas), len(volumenesBorrados))
    return {
        "images_deleted": imagenesBorradas,
        "volumes_deleted": volumenesBorrados,
        "errors": erroresImagenes + erroresVolumenes,
        "freed_mb": liberadoMb,
    }
