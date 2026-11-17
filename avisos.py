"""Qué se avisa por Telegram, cuándo, y qué responden los comandos.

Este módulo no sabe nada de Telegram: produce textos HTML (con los valores
dinámicos escapados) y los entrega a una función ``avisar(categoria, texto,
clave)`` inyectada. Así la lógica se prueba sin red ni Docker.

Fuentes de avisos:
  - contenedores: eventos de Docker (caída con error, OOM, unhealthy, bucle de reinicios).
  - recursos: temperatura de CPU y disco, con histéresis para no avisar en cada oscilación.
  - actualizaciones: versión nueva publicada y resultado del vigilante del host.
  - seguridad y sistema: textos que main.py envía desde los endpoints y al arrancar.
"""

import json
import logging
import os
import threading
import time
from collections import deque
from typing import Any, Callable, Iterable

from telegrama import escapar

logger = logging.getLogger(__name__)

Avisar = Callable[[str, str, str | None], object]
Aviso = tuple[str, str]  # (clave de deduplicación, texto)

# Un «die» a menos de esto de un «kill»/«stop» es una parada pedida (docker
# stop, restart, compose down): no es una caída.
ventanaParadaVoluntariaSegundos = 60.0
margenTemperatura = 5.0
margenDisco = 5.0
maxContenedoresListados = 60
maxCaracteresDetalle = 500


# ── Contenedores ──────────────────────────────────────────────────────────────
class ProcesadorEventosDocker:
    """Convierte eventos de contenedor de ``docker events`` en avisos.

    Args:
        reiniciosBucle: Caídas dentro de la ventana que se consideran bucle de reinicios.
        ventanaBucle: Ventana en segundos para contar caídas.
        reloj: Fuente de tiempo monotónico (inyectable en tests).
    """

    def __init__(self, reiniciosBucle: int, ventanaBucle: float,
                 reloj: Callable[[], float] = time.monotonic) -> None:
        self._reiniciosBucle = max(2, reiniciosBucle)
        self._ventanaBucle = ventanaBucle
        self._reloj = reloj
        self._paradas: dict[str, float] = {}
        self._ooms: dict[str, float] = {}
        self._caidas: dict[str, deque[float]] = {}
        self._buclesAvisados: dict[str, float] = {}

    def procesar(self, evento: dict) -> Aviso | None:
        """Decide si un evento merece aviso.

        Args:
            evento: Evento decodificado de ``client.events(decode=True)``.

        Returns:
            ``(clave, texto)`` o None.
        """
        if evento.get("Type", "container") != "container":
            return None
        accion = str(evento.get("Action") or evento.get("status") or "")
        actor = evento.get("Actor") or {}
        atributos = actor.get("Attributes") or {}
        idContenedor = str(actor.get("ID") or evento.get("id") or "")
        if not idContenedor:
            return None
        nombre = atributos.get("name") or idContenedor[:12]
        ahora = self._reloj()
        self._purgar(ahora)

        if accion in ("kill", "stop"):
            self._paradas[idContenedor] = ahora
            return None
        if accion == "destroy":
            for registro in (self._paradas, self._ooms, self._caidas, self._buclesAvisados):
                registro.pop(idContenedor, None)
            return None
        if accion == "oom":
            self._ooms[idContenedor] = ahora
            return (f"oom:{idContenedor}",
                    f"<b>Contenedor sin memoria (OOM)</b>\n"
                    f"El kernel ha matado un proceso de <code>{escapar(nombre)}</code> por falta de memoria.")
        if accion.startswith("health_status") and accion.split(":", 1)[-1].strip() == "unhealthy":
            return (f"unhealthy:{idContenedor}",
                    f"<b>Contenedor unhealthy</b>\n"
                    f"El healthcheck de <code>{escapar(nombre)}</code> está fallando.")
        if accion == "die":
            return self._caida(idContenedor, nombre, atributos.get("exitCode"), ahora)
        return None

    def _caida(self, idContenedor: str, nombre: str, codigoSalida: Any, ahora: float) -> Aviso | None:
        if str(codigoSalida) == "0":
            return None
        if ahora - self._paradas.get(idContenedor, float("-inf")) < ventanaParadaVoluntariaSegundos:
            return None
        caidas = self._caidas.setdefault(idContenedor, deque())
        caidas.append(ahora)
        if ahora - self._buclesAvisados.get(idContenedor, float("-inf")) < self._ventanaBucle:
            return None  # ya avisado como bucle: no repetir cada caída
        if len(caidas) >= self._reiniciosBucle:
            self._buclesAvisados[idContenedor] = ahora
            minutos = round(self._ventanaBucle / 60)
            return (f"bucle:{idContenedor}",
                    f"<b>Bucle de reinicios</b>\n"
                    f"<code>{escapar(nombre)}</code> se ha caído {len(caidas)} veces en {minutos} min "
                    f"(último código de salida: {escapar(codigoSalida)}).")
        # Tras un OOM llega su «die» (137): el aviso de OOM ya lo explica.
        if ahora - self._ooms.get(idContenedor, float("-inf")) < ventanaParadaVoluntariaSegundos:
            return None
        return (f"caida:{idContenedor}",
                f"<b>Contenedor caído</b>\n"
                f"<code>{escapar(nombre)}</code> ha terminado con código de salida {escapar(codigoSalida)}.")

    def _purgar(self, ahora: float) -> None:
        # Acota la memoria: sin esto cada contenedor visto alguna vez se quedaría para siempre.
        limite = max(self._ventanaBucle, ventanaParadaVoluntariaSegundos)
        for registro in (self._paradas, self._ooms, self._buclesAvisados):
            for clave in [k for k, t in registro.items() if ahora - t >= limite]:
                del registro[clave]
        for clave in list(self._caidas):
            caidas = self._caidas[clave]
            while caidas and ahora - caidas[0] >= self._ventanaBucle:
                caidas.popleft()
            if not caidas:
                del self._caidas[clave]


# ── Recursos del host ─────────────────────────────────────────────────────────
class Umbral:
    """Umbral con histéresis: avisa al cruzarlo y al volver por debajo de ``limite - margen``.

    Args:
        limite: Valor a partir del cual se avisa.
        margen: Cuánto tiene que bajar para darlo por recuperado.
    """

    def __init__(self, limite: float, margen: float) -> None:
        self.limite = limite
        self.margen = margen
        self.activo = False

    def evaluar(self, valor: float | None) -> str | None:
        """Devuelve ``"alerta"``, ``"recuperado"`` o None si no hay cambio de estado."""
        if valor is None:
            return None
        if not self.activo and valor >= self.limite:
            self.activo = True
            return "alerta"
        if self.activo and valor < self.limite - self.margen:
            self.activo = False
            return "recuperado"
        return None


class VigilanteRecursos:
    """Avisa de temperatura de CPU crítica y de disco casi lleno.

    Args:
        leerTemperatura: Devuelve la temperatura de CPU en ºC o None.
        leerDiscoPct: Devuelve el porcentaje de disco usado o None.
        temperaturaCritica: ºC a partir de los que se avisa.
        discoAvisoPct: Porcentaje de disco a partir del que se avisa.
    """

    def __init__(self, leerTemperatura: Callable[[], float | None], leerDiscoPct: Callable[[], float | None],
                 temperaturaCritica: float, discoAvisoPct: float) -> None:
        self._leerTemperatura = leerTemperatura
        self._leerDiscoPct = leerDiscoPct
        self._temperatura = Umbral(temperaturaCritica, margenTemperatura)
        self._disco = Umbral(discoAvisoPct, margenDisco)

    def comprobar(self) -> list[Aviso]:
        """Lee los sensores y devuelve los avisos de cambio de estado."""
        avisos: list[Aviso] = []
        temperatura = self._leerTemperatura()
        cambio = self._temperatura.evaluar(temperatura)
        if cambio == "alerta":
            avisos.append(("temperatura:alerta",
                           f"<b>Temperatura crítica</b>\nCPU a {temperatura:.0f} ºC "
                           f"(umbral {self._temperatura.limite:.0f} ºC)."))
        elif cambio == "recuperado":
            avisos.append(("temperatura:ok", f"<b>Temperatura normal</b>\nCPU a {temperatura:.0f} ºC."))
        disco = self._leerDiscoPct()
        cambio = self._disco.evaluar(disco)
        if cambio == "alerta":
            avisos.append(("disco:alerta",
                           f"<b>Disco casi lleno</b>\nUso al {disco:.0f} % (umbral {self._disco.limite:.0f} %)."))
        elif cambio == "recuperado":
            avisos.append(("disco:ok", f"<b>Disco recuperado</b>\nUso al {disco:.0f} %."))
        return avisos


# ── Actualizaciones ───────────────────────────────────────────────────────────
class EstadoPersistente:
    """Pequeño JSON en el directorio de datos con lo ya avisado.

    Hace falta porque actualizar recrea el contenedor: sin persistir, cada
    arranque volvería a avisar de la misma versión o del mismo resultado.

    Args:
        ruta: Ruta del fichero JSON.
    """

    def __init__(self, ruta: str) -> None:
        self._ruta = ruta
        self._datos = self._leer()

    def contiene(self, clave: str) -> bool:
        """Indica si la clave se ha guardado alguna vez."""
        return clave in self._datos

    def obtener(self, clave: str) -> Any:
        """Valor guardado o None."""
        return self._datos.get(clave)

    def guardar(self, clave: str, valor: Any) -> None:
        """Guarda un valor de forma atómica. Un fallo se registra, no se propaga."""
        self._datos[clave] = valor
        temporal = self._ruta + ".tmp"
        try:
            os.makedirs(os.path.dirname(self._ruta) or ".", exist_ok=True)
            with open(temporal, "w", encoding="utf-8") as f:
                json.dump(self._datos, f)
            os.replace(temporal, self._ruta)
        except OSError as error:
            logger.warning("No se pudo guardar %s (puede repetirse algún aviso): %s", self._ruta, error)

    def _leer(self) -> dict:
        try:
            with open(self._ruta, encoding="utf-8") as f:
                datos = json.load(f)
            return datos if isinstance(datos, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            logger.warning("Ignorando %s ilegible: %s", self._ruta, error)
            return {}


class VigilanteActualizaciones:
    """Avisa de versión nueva publicada y del resultado de cada actualización.

    Args:
        consultarRemota: ``actualizacion.version_remota`` (usa su propia caché).
        consultarEstado: ``actualizacion.estado``.
        estado: Registro persistente de lo ya avisado.
        versionInstalada: Versión en ejecución.
    """

    def __init__(self, consultarRemota: Callable[[], dict], consultarEstado: Callable[[], dict],
                 estado: EstadoPersistente, versionInstalada: str) -> None:
        self._consultarRemota = consultarRemota
        self._consultarEstado = consultarEstado
        self._estado = estado
        self._versionInstalada = versionInstalada

    def comprobar(self) -> list[Aviso]:
        """Consulta versión publicada y estado del vigilante; devuelve los avisos nuevos."""
        return [aviso for aviso in (self._versionNueva(), self._resultado()) if aviso]

    def _versionNueva(self) -> Aviso | None:
        remota = self._consultarRemota()
        publicada = remota.get("publicada")
        if not remota.get("hayNueva") or not publicada or publicada == self._estado.obtener("versionAvisada"):
            return None
        self._estado.guardar("versionAvisada", publicada)
        return (f"version:{publicada}",
                f"<b>Nueva versión de DockerManager</b>\n"
                f"Instalada: {escapar(self._versionInstalada)}\nPublicada: {escapar(publicada)}\n"
                f"Se puede actualizar desde el menú de la versión en la web.")

    def _resultado(self) -> Aviso | None:
        ultimo = self._consultarEstado().get("ultimo")
        momento = (ultimo or {}).get("actualizadoEn")
        if not momento:
            return None
        if not self._estado.contiene("actualizacionAvisada"):
            # Primera vez con Telegram: el último resultado puede ser de hace
            # semanas. Se toma como referencia sin avisar.
            self._estado.guardar("actualizacionAvisada", momento)
            return None
        if momento == self._estado.obtener("actualizacionAvisada"):
            return None
        self._estado.guardar("actualizacionAvisada", momento)
        if ultimo.get("estado") == "ok":
            return (f"actualizacion:{momento}",
                    f"<b>Actualización completada</b>\nDockerManager {escapar(self._versionInstalada)} "
                    f"en marcha ({escapar(momento)}).")
        detalle = str(ultimo.get("detalle") or "")[-maxCaracteresDetalle:]
        return (f"actualizacion:{momento}",
                f"<b>La actualización ha fallado</b>\nCódigo {escapar(ultimo.get('codigo'))} "
                f"({escapar(momento)}).\n<pre>{escapar(detalle)}</pre>")


# ── Seguridad y sistema ───────────────────────────────────────────────────────
def textoArranque(version: str, equipo: str) -> str:
    """Aviso de arranque de la app (detecta reinicios inesperados)."""
    return f"<b>DockerManager {escapar(version)} iniciado</b>\nEquipo: <code>{escapar(equipo)}</code>"


def textoLogin(ip: str) -> str:
    """Aviso de inicio de sesión correcto."""
    return f"<b>Inicio de sesión</b>\nAcceso correcto a DockerManager desde <code>{escapar(ip)}</code>."


def textoBloqueo(ip: str, minutos: int) -> str:
    """Aviso de IP bloqueada por intentos fallidos."""
    return (f"<b>IP bloqueada</b>\nDemasiados intentos de login fallidos desde <code>{escapar(ip)}</code>. "
            f"Bloqueada {minutos} min.")


def textoCambioPassword(ip: str) -> str:
    """Aviso de cambio de contraseña."""
    return f"<b>Contraseña cambiada</b>\nLa contraseña de DockerManager se ha cambiado desde <code>{escapar(ip)}</code>."


def textoCambioTelegram(ip: str) -> str:
    """Aviso, por la configuración anterior, de que han cambiado el bot o los IDs autorizados."""
    return (f"<b>Configuración de Telegram cambiada</b>\nSe han cambiado el bot o los IDs autorizados "
            f"desde <code>{escapar(ip)}</code>. Si no has sido tú, revisa Ajustes › Avisos.")


def textoConfiguracionInicial(ip: str) -> str:
    """Aviso de creación del usuario administrador."""
    return f"<b>Configuración inicial</b>\nSe ha creado el usuario de DockerManager desde <code>{escapar(ip)}</code>."


# ── Comandos de solo lectura ──────────────────────────────────────────────────
def textoAyuda() -> str:
    """Respuesta a /ayuda, /start y a cualquier comando desconocido."""
    return ("<b>DockerManager</b>\n"
            "/estado - resumen del servidor y de los contenedores\n"
            "/contenedores - lista de contenedores y su estado\n"
            "/ayuda - esta ayuda")


def textoEstado(version: str, contenedores: list[dict], host: dict) -> str:
    """Resumen de /estado.

    Args:
        version: Versión de la app.
        contenedores: Dicts con al menos ``status`` y ``health``.
        host: Dict con ``cpu_pct``, ``mem_pct``, ``disk_pct`` y ``cpu_temp``.
    """
    enMarcha = sum(1 for c in contenedores if c.get("status") == "running")
    problemas = sum(1 for c in contenedores if c.get("health") == "unhealthy" or c.get("status") in ("dead", "restarting"))
    lineas = [
        f"<b>DockerManager {escapar(version)}</b>",
        f"Contenedores: {enMarcha}/{len(contenedores)} en marcha" + (f", {problemas} con problemas" if problemas else ""),
        f"CPU: {escapar(host.get('cpu_pct'))} %",
        f"RAM: {escapar(host.get('mem_pct'))} %",
        f"Disco: {escapar(host.get('disk_pct'))} %",
    ]
    if host.get("cpu_temp") is not None:
        lineas.append(f"Temperatura CPU: {host['cpu_temp']:.0f} ºC")
    return "\n".join(lineas)


def textoContenedores(contenedores: list[dict]) -> str:
    """Lista de /contenedores: primero los que tienen problemas, luego parados y en marcha.

    Args:
        contenedores: Dicts con ``name``, ``status`` y ``health``.
    """
    if not contenedores:
        return "No hay contenedores."

    def prioridad(c: dict) -> tuple[int, str]:
        if c.get("health") == "unhealthy" or c.get("status") in ("dead", "restarting"):
            return (0, c.get("name") or "")
        return (1 if c.get("status") != "running" else 2, c.get("name") or "")

    ordenados = sorted(contenedores, key=prioridad)
    lineas = [f"<b>Contenedores ({len(contenedores)})</b>"]
    for c in ordenados[:maxContenedoresListados]:
        estado = c.get("status") or "?"
        if c.get("health"):
            estado += f", {c['health']}"
        lineas.append(f"<code>{escapar(c.get('name'))}</code> - {escapar(estado)}")
    if len(ordenados) > maxContenedoresListados:
        lineas.append(f"... y {len(ordenados) - maxContenedoresListados} más")
    return "\n".join(lineas)


def crearComandos(version: str, listarContenedores: Callable[[], list[dict]],
                  leerHost: Callable[[], dict]) -> dict[str, Callable[[], str]]:
    """Construye el mapa de comandos de solo lectura para ``telegrama.Receptor``.

    Args:
        version: Versión de la app.
        listarContenedores: Devuelve la lista de contenedores (formato de /api/containers).
        leerHost: Devuelve las métricas del host (formato de /api/system/host).

    Returns:
        ``{"/comando": función}``.
    """
    return {
        "/start": textoAyuda,
        "/ayuda": textoAyuda,
        "/help": textoAyuda,
        "/estado": lambda: textoEstado(version, listarContenedores(), leerHost()),
        "/contenedores": lambda: textoContenedores(listarContenedores()),
    }


# ── Hilos de vigilancia ───────────────────────────────────────────────────────
class Vigilancia:
    """Hilos que alimentan los avisos: eventos de Docker y comprobaciones periódicas.

    Args:
        avisar: ``avisar(categoria, texto, clave)``.
        intervalo: Segundos entre comprobaciones periódicas.
        esperaReintento: Segundos antes de reconectar con Docker tras un corte.
    """

    def __init__(self, avisar: Avisar, intervalo: float, esperaReintento: float = 10.0) -> None:
        self._avisar = avisar
        self._intervalo = intervalo
        self._esperaReintento = esperaReintento
        self._comprobaciones: list[tuple[str, Callable[[], list[Aviso]]]] = []
        self._eventos: tuple[Callable[[], Iterable[dict]], ProcesadorEventosDocker] | None = None
        self._parada = threading.Event()

    def vigilarEventos(self, obtenerEventos: Callable[[], Iterable[dict]],
                       procesador: ProcesadorEventosDocker) -> None:
        """Registra el flujo de eventos de Docker (categoría «contenedores»)."""
        self._eventos = (obtenerEventos, procesador)

    def comprobarPeriodicamente(self, categoria: str, comprobar: Callable[[], list[Aviso]]) -> None:
        """Registra una comprobación periódica."""
        self._comprobaciones.append((categoria, comprobar))

    def ejecutarComprobaciones(self) -> None:
        """Una pasada de todas las comprobaciones periódicas."""
        for categoria, comprobar in self._comprobaciones:
            try:
                for clave, texto in comprobar():
                    self._avisar(categoria, texto, clave)
            except Exception:
                # Un sensor ilegible o GitHub caído no debe parar la vigilancia del resto.
                logger.exception("Fallo en la comprobación periódica de %s", categoria)

    def iniciar(self) -> None:
        """Arranca los hilos (daemon) de lo que se haya registrado."""
        if self._eventos:
            threading.Thread(target=self._bucleEventos, name="avisos-docker", daemon=True).start()
        if self._comprobaciones:
            threading.Thread(target=self._buclePeriodico, name="avisos-periodicos", daemon=True).start()

    def detener(self) -> None:
        """Pide a los hilos que terminen."""
        self._parada.set()

    def _buclePeriodico(self) -> None:
        while not self._parada.is_set():
            self.ejecutarComprobaciones()
            self._parada.wait(self._intervalo)

    def _bucleEventos(self) -> None:
        obtenerEventos, procesador = self._eventos
        while not self._parada.is_set():
            try:
                for evento in obtenerEventos():
                    if self._parada.is_set():
                        return
                    aviso = procesador.procesar(evento)
                    if aviso:
                        self._avisar("contenedores", aviso[1], aviso[0])
            except Exception as error:
                # El flujo se corta si el daemon de Docker se reinicia; la
                # librería puede lanzar errores de docker, requests o socket.
                logger.warning("Se perdió el flujo de eventos de Docker (%s); reconectando", error)
            self._parada.wait(self._esperaReintento)
