"""Vista de solo lectura de la configuración para la pantalla «Ajustes».

La configuración se sigue cambiando donde siempre (config.ini y .env) y se
aplica al reiniciar; aquí solo se reúne lo que está en vigor para enseñarlo
agrupado. Decisiones:

  - El token de Telegram nunca sale de este módulo, ni enmascarado: la interfaz
    solo necesita saber si está definido. Un fragmento del token no aporta nada
    a quien administra y sí a quien consiga leer la respuesta.
  - Los IDs de Telegram se devuelven ya validados con ``telegrama.parsearIds``;
    si están mal escritos se devuelve el error en vez de la lista, que es lo que
    explica por qué el bot no arranca.
  - El módulo de configuración se recibe como parámetro (duck typing) para
    probarlo con un objeto falso, sin tocar config.ini ni el entorno.

Ejemplo:
    import config
    resumen(config, usuario="admin", telegramActivo=True)
"""

import time
from typing import Any

import telegrama

# Orden y descripción con que se muestran las categorías de aviso. Las claves
# tienen que coincidir con telegrama.categoriasValidas (lo comprueba un test).
descripcionCategorias: dict[str, str] = {
    "sistema": "Arranque de la app",
    "contenedores": "Caída con error, OOM, unhealthy y bucle de reinicios",
    "seguridad": "Login correcto, IP bloqueada y cambio de contraseña",
    "actualizaciones": "Versión nueva publicada y resultado de la actualización",
    "recursos": "Temperatura de CPU crítica y disco lleno",
}


def estadoTelegram(token: str, idsTexto: str, activo: bool) -> str:
    """Resume en una palabra por qué Telegram está o no funcionando.

    Args:
        token: Valor de ``TELEGRAM_BOT_TOKEN``.
        idsTexto: Valor de ``TELEGRAM_IDS_PERMITIDOS``.
        activo: Si el servicio de Telegram arrancó.

    Returns:
        ``"activo"``; ``"sin_configurar"`` si faltan las dos variables;
        ``"incompleto"`` si falta una; ``"error"`` si están las dos pero el
        servicio no arrancó (valor inválido: el motivo está en el log).
    """
    if activo:
        return "activo"
    if not token and not idsTexto:
        return "sin_configurar"
    if not token or not idsTexto:
        return "incompleto"
    return "error"


def _ids(idsTexto: str) -> tuple[list[int], str | None]:
    """Valida los IDs autorizados sin lanzar.

    Returns:
        (IDs ordenados, None) o ([], mensaje de error).
    """
    if not idsTexto:
        return [], None
    try:
        return sorted(telegrama.parsearIds(idsTexto)), None
    except telegrama.ConfiguracionInvalida as error:
        return [], str(error)


def _categorias(categoriasTexto: str) -> tuple[list[dict[str, Any]], str | None]:
    """Lista todas las categorías conocidas indicando cuáles están activadas.

    Returns:
        (categorías, None) o (categorías todas desactivadas, mensaje de error).
    """
    error = None
    try:
        activas = telegrama.parsearCategorias(categoriasTexto)
    except telegrama.ConfiguracionInvalida as excepcion:
        activas, error = frozenset(), str(excepcion)
    lista = [{"id": clave, "descripcion": texto, "activa": clave in activas}
             for clave, texto in descripcionCategorias.items()]
    return lista, error


def _telegram(cfg: Any, activo: bool) -> dict[str, Any]:
    ids, errorIds = _ids(cfg.telegramIdsPermitidos)
    categorias, errorCategorias = _categorias(cfg.telegramCategorias)
    return {
        "estado": estadoTelegram(cfg.telegramToken, cfg.telegramIdsPermitidos, activo),
        "tokenConfigurado": bool(cfg.telegramToken),
        "ids": ids,
        "errorIds": errorIds,
        "categorias": categorias,
        "errorCategorias": errorCategorias,
        "intervaloSegundos": cfg.telegramIntervalo,
        "timeoutSegundos": cfg.telegramTimeout,
        "discoAvisoPct": cfg.telegramDiscoAviso,
        "reiniciosBucle": cfg.telegramReiniciosBucle,
        "ventanaBucleMinutos": cfg.telegramVentanaBucleMinutos,
    }


def resumen(cfg: Any, usuario: str | None, telegramActivo: bool) -> dict[str, Any]:
    """Reúne la configuración en vigor agrupada por secciones de la pantalla.

    Args:
        cfg: Módulo ``config`` (o un objeto con los mismos atributos).
        usuario: Usuario administrador guardado en data/config.json.
        telegramActivo: Si el servicio de Telegram arrancó.

    Returns:
        Diccionario con las claves ``cuenta``, ``telegram``, ``actualizacion``,
        ``servidor`` y ``limpieza``. No contiene secretos.
    """
    return {
        "cuenta": {
            "usuario": usuario,
            "sesionDias": cfg.SESSION_MAX_AGE_DAYS,
            "intentosMaximos": cfg.MAX_LOGIN_ATTEMPTS,
            "bloqueoMinutos": cfg.LOCKOUT_MINUTES,
            "costeBcrypt": cfg.BCRYPT_COST,
        },
        "telegram": _telegram(cfg, telegramActivo),
        "actualizacion": {
            "comprobar": cfg.UPDATE_CHECK,
            "repositorio": cfg.UPDATE_REPO,
            "rama": cfg.UPDATE_BRANCH,
            "cacheHoras": cfg.UPDATE_CACHE_HOURS,
            "timeoutSegundos": cfg.UPDATE_TIMEOUT_SECONDS,
        },
        "servidor": {
            "puerto": cfg.PORT,
            "bindHost": cfg.BIND_HOST,
            "temperaturaAviso": cfg.servidorTempAviso,
            "temperaturaCritica": cfg.servidorTempCritica,
            "rutaDisco": cfg.servidorRutaDisco,
        },
        "limpieza": {
            "diasPorDefecto": cfg.CLEANUP_DEFAULT_DAYS,
            "diasMaximos": cfg.CLEANUP_MAX_DAYS,
        },
    }


def textoPrueba(version: str, equipo: str, momento: float | None = None) -> str:
    """Mensaje del botón «Enviar prueba».

    Lleva la hora con segundos para que dos pruebas seguidas no se descarten
    como duplicadas en el Notificador.

    Args:
        version: Versión de DockerManager.
        equipo: Nombre del host.
        momento: Instante a mostrar (epoch); por defecto, ahora.

    Returns:
        Texto HTML con los valores dinámicos escapados.
    """
    hora = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(momento))
    return (f"<b>Prueba de DockerManager</b> {telegrama.escapar(version)}\n"
            f"Equipo: <code>{telegrama.escapar(equipo)}</code>\n"
            f"Enviada desde Ajustes a las {telegrama.escapar(hora)}. Si lees esto, los avisos llegan.")
