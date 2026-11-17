"""Configuración de Telegram editable desde la web.

Tres orígenes, por orden de prioridad:
  1. Variables de entorno (.env): ``TELEGRAM_BOT_TOKEN`` y ``TELEGRAM_IDS_PERMITIDOS``.
     Si están definidas mandan y la web no puede cambiarlas: quien controla el
     host tiene la última palabra.
  2. Lo guardado desde la web en ``data/telegram-config.json``.
  3. ``[telegram]`` de config.ini (categorías y umbrales) o sus valores por defecto.

El token se guarda en claro porque hace falta tal cual para llamar a la API;
el fichero se crea con permisos 0600 y vive en el volumen de datos, nunca en
el repositorio. Ninguna función de este módulo devuelve el token hacia la API.
"""

import json
import logging
import os
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable

import telegrama
from telegrama import ConfiguracionInvalida

logger = logging.getLogger(__name__)

nombreFichero = "telegram-config.json"

# (mínimo, máximo) de cada valor numérico editable. Fuera de rango no tiene
# sentido: un intervalo de 1 s machacaría los sensores y un timeout de 0 no envía nada.
rangosNumericos: dict[str, tuple[int, int]] = {
    "timeoutSegundos": (1, 60),
    "intervaloSegundos": (10, 3600),
    "discoAvisoPct": (50, 99),
    "reiniciosBucle": (2, 20),
    "ventanaBucleMinutos": (1, 1440),
}
camposEditables = frozenset({"token", "ids", "categorias", *rangosNumericos})


@dataclass(frozen=True)
class ConfigTelegram:
    """Configuración efectiva tras combinar entorno, web y config.ini.

    Attributes:
        origenToken: ``"env"``, ``"web"`` o ``""`` si no hay token.
        origenIds: ``"env"``, ``"web"`` o ``""`` si no hay IDs.
        bot: Usuario del bot (``@...``) comprobado al guardar el token desde la web, o None.
    """

    token: str
    idsTexto: str
    categoriasTexto: str
    timeoutSegundos: int
    intervaloSegundos: int
    discoAvisoPct: int
    reiniciosBucle: int
    ventanaBucleMinutos: int
    origenToken: str
    origenIds: str
    bot: str | None

    def credenciales(self) -> tuple[str, str]:
        """Token e IDs: si cambian, cambia quién recibe los avisos."""
        return (self.token, self.idsTexto)


class AlmacenTelegram:
    """Repository del JSON con la configuración guardada desde la web.

    Args:
        ruta: Ruta del fichero (normalmente ``DATA_DIR/telegram-config.json``).
    """

    def __init__(self, ruta: str) -> None:
        self._ruta = ruta

    def leer(self) -> dict[str, Any]:
        """Devuelve lo guardado, o ``{}`` si no hay nada o el fichero está corrupto."""
        try:
            with open(self._ruta, encoding="utf-8") as f:
                datos = json.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            logger.warning("Ignorando %s ilegible: %s", self._ruta, error)
            return {}
        return datos if isinstance(datos, dict) else {}

    def guardar(self, datos: dict[str, Any]) -> None:
        """Escribe de forma atómica con permisos 0600.

        Raises:
            OSError: Si no se puede escribir.
        """
        os.makedirs(os.path.dirname(self._ruta) or ".", exist_ok=True)
        temporal = self._ruta + ".tmp"
        # os.open con modo: el fichero nace ya con 0600, sin un instante legible por otros.
        descriptor = os.open(temporal, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as f:
            json.dump(datos, f)
        os.replace(temporal, self._ruta)

    def borrar(self) -> None:
        """Elimina lo guardado (vuelve a mandar el .env y config.ini).

        Raises:
            OSError: Si existe y no se puede borrar.
        """
        try:
            os.remove(self._ruta)
        except FileNotFoundError:
            pass


def efectiva(cfg: Any, guardado: dict[str, Any]) -> ConfigTelegram:
    """Combina entorno, web y config.ini.

    Args:
        cfg: Módulo ``config`` (o un objeto con sus atributos ``telegram*``).
        guardado: Lo que devuelve ``AlmacenTelegram.leer``.

    Returns:
        La configuración en vigor.
    """
    if cfg.telegramToken:
        token, origenToken, bot = cfg.telegramToken, "env", None
    else:
        token = str(guardado.get("token") or "")
        origenToken, bot = ("web", guardado.get("bot")) if token else ("", None)
    if cfg.telegramIdsPermitidos:
        idsTexto, origenIds = cfg.telegramIdsPermitidos, "env"
    else:
        idsTexto = str(guardado.get("ids") or "")
        origenIds = "web" if idsTexto else ""
    categorias = guardado.get("categorias")
    return ConfigTelegram(
        token=token,
        idsTexto=idsTexto,
        categoriasTexto=", ".join(categorias) if isinstance(categorias, list) else cfg.telegramCategorias,
        timeoutSegundos=_numero(guardado, "timeoutSegundos", cfg.telegramTimeout),
        intervaloSegundos=_numero(guardado, "intervaloSegundos", cfg.telegramIntervalo),
        discoAvisoPct=_numero(guardado, "discoAvisoPct", cfg.telegramDiscoAviso),
        reiniciosBucle=_numero(guardado, "reiniciosBucle", cfg.telegramReiniciosBucle),
        ventanaBucleMinutos=_numero(guardado, "ventanaBucleMinutos", cfg.telegramVentanaBucleMinutos),
        origenToken=origenToken,
        origenIds=origenIds,
        bot=bot,
    )


def _numero(guardado: dict[str, Any], clave: str, porDefecto: int) -> int:
    # Un JSON editado a mano con un valor fuera de rango no debe colarse: se
    # ignora y se usa el de config.ini.
    valor = guardado.get(clave)
    minimo, maximo = rangosNumericos[clave]
    if type(valor) is int and minimo <= valor <= maximo:
        return valor
    return porDefecto


def aplicarCambios(cambios: dict[str, Any], guardado: dict[str, Any], cfg: Any,
                   comprobarToken: Callable[[str], str]) -> dict[str, Any]:
    """Valida un cambio enviado desde la web y devuelve lo que hay que guardar.

    Solo se tocan los campos presentes en ``cambios``. ``token`` vacío o ausente
    conserva el actual (la web nunca lo recibe, así que no puede reenviarlo);
    ``ids`` vacío borra los IDs guardados.

    Args:
        cambios: Cuerpo de la petición.
        guardado: Configuración guardada actualmente.
        cfg: Módulo ``config``, para saber qué define el .env.
        comprobarToken: Valida el token contra Telegram y devuelve el usuario del
            bot; lanza ``ConfiguracionInvalida`` o ``telegrama.ErrorTelegram``.

    Returns:
        El nuevo contenido completo del almacén.

    Raises:
        ConfiguracionInvalida: Si algún campo no es válido o lo fija el .env.
    """
    desconocidos = set(cambios) - camposEditables
    if desconocidos:
        raise ConfiguracionInvalida(f"Campos no editables: {', '.join(sorted(desconocidos))}")
    nuevo = dict(guardado)
    if cambios.get("token"):
        nuevo.update(_validarToken(cambios["token"], cfg, comprobarToken))
    if "ids" in cambios:
        _validarIds(cambios["ids"], cfg, nuevo)
    if "categorias" in cambios:
        nuevo["categorias"] = _validarCategorias(cambios["categorias"])
    for clave, (minimo, maximo) in rangosNumericos.items():
        if clave in cambios:
            valor = cambios[clave]
            # type() y no isinstance(): True pasaría como 1.
            if type(valor) is not int or not minimo <= valor <= maximo:
                raise ConfiguracionInvalida(f"{clave} debe ser un entero entre {minimo} y {maximo}")
            nuevo[clave] = valor
    return nuevo


def _validarToken(token: Any, cfg: Any, comprobarToken: Callable[[str], str]) -> dict[str, Any]:
    if cfg.telegramToken:
        raise ConfiguracionInvalida("El token está definido en el .env; quítalo de ahí para gestionarlo desde la web")
    if not isinstance(token, str):
        raise ConfiguracionInvalida("El token debe ser texto")
    token = token.strip()
    try:
        bot = comprobarToken(token)
    except telegrama.ErrorTelegram as error:
        raise ConfiguracionInvalida(f"Telegram no acepta el token: {error}") from None
    return {"token": token, "bot": bot}


def _validarIds(ids: Any, cfg: Any, nuevo: dict[str, Any]) -> None:
    if cfg.telegramIdsPermitidos:
        raise ConfiguracionInvalida("Los IDs están definidos en el .env; quítalos de ahí para gestionarlos desde la web")
    if not isinstance(ids, str):
        raise ConfiguracionInvalida("Los IDs deben ser texto separado por comas")
    validos = telegrama.parsearIds(ids)
    if validos:
        nuevo["ids"] = ", ".join(str(i) for i in sorted(validos))
    else:
        nuevo.pop("ids", None)


def _validarCategorias(categorias: Any) -> list[str]:
    if not isinstance(categorias, list) or not all(isinstance(c, str) for c in categorias):
        raise ConfiguracionInvalida("Las categorías deben ser una lista de nombres")
    return sorted(telegrama.parsearCategorias(",".join(categorias)))


def vistaConfig(cfg: Any, config: ConfigTelegram) -> SimpleNamespace:
    """Copia de ``cfg`` con los valores de Telegram sustituidos por los efectivos.

    Permite reutilizar ``ajustes.resumen`` (que lee atributos ``telegram*``)
    sin que tenga que conocer los orígenes.
    """
    valores = {nombre: getattr(cfg, nombre) for nombre in dir(cfg) if not nombre.startswith("_")}
    valores.update(
        telegramToken=config.token,
        telegramIdsPermitidos=config.idsTexto,
        telegramCategorias=config.categoriasTexto,
        telegramTimeout=config.timeoutSegundos,
        telegramIntervalo=config.intervaloSegundos,
        telegramDiscoAviso=config.discoAvisoPct,
        telegramReiniciosBucle=config.reiniciosBucle,
        telegramVentanaBucleMinutos=config.ventanaBucleMinutos,
    )
    return SimpleNamespace(**valores)


def detalles(config: ConfigTelegram, hayGuardado: bool) -> dict[str, Any]:
    """Datos extra para la web: de dónde sale cada valor. Sin el token.

    Args:
        config: Configuración efectiva.
        hayGuardado: Si existe configuración guardada desde la web.
    """
    return {
        "origenToken": config.origenToken,
        "origenIds": config.origenIds,
        "bot": config.bot,
        "guardadoWeb": hayGuardado,
        "rangos": {clave: list(rango) for clave, rango in rangosNumericos.items()},
    }
