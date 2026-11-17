"""Métricas del servidor (host) para el panel «Servidor».

Qué refleja el host y qué no cuando la app corre en el contenedor de
docker-compose.yml (red bridge, sin montar nada extra):

  - CPU, carga, memoria, swap, uptime y E/S de disco salen de /proc/stat,
    /proc/meminfo, /proc/loadavg y /proc/diskstats, que el kernel no aísla por
    contenedor: son las del host.
  - Temperaturas y ventiladores salen de /sys/class/hwmon, que Docker monta en
    solo lectura: también son las del host, si el hardware las publica.
  - Red y procesos sí van por espacio de nombres: el contenedor solo vería su
    eth0 y sus propios PID. Por eso no se ofrecen, en vez de mostrar datos que
    parecen del servidor y no lo son.

El porcentaje de CPU se calcula aquí a partir de cpu_times() en lugar de usar
psutil.cpu_percent(interval=None): desde psutil 5.9.6 ese estado se guarda por
hilo, y FastAPI atiende cada petición síncrona en un hilo cualquiera del pool,
así que el delta saldría contra una muestra arbitraria (o 0.0 la primera vez en
cada hilo). Bloquear con interval>0 tampoco: retiene un hilo del pool en cada
refresco.
"""

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

import psutil

import config as cfgIni

logger = logging.getLogger(__name__)

# Orden de preferencia para la «temperatura de CPU»: Intel, AMD, Raspberry Pi y
# otros SoC ARM, y como último recurso la zona térmica ACPI de la placa.
sensoresCpu = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "cpu-thermal", "soc_thermal", "acpitz")

# Campos de cpu_times() que Linux ya cuenta dentro de user/nice; sumarlos otra
# vez inflaría el total (mismo criterio que psutil).
camposCpuDuplicados = ("guest", "guest_nice")
camposCpuOciosos = ("idle", "iowait")


def porcentaje_cpu(antes: Any, despues: Any) -> float | None:
    """Calcula el uso de CPU entre dos muestras de ``psutil.cpu_times()``.

    Args:
        antes: Muestra anterior (namedtuple con ``_asdict()``).
        despues: Muestra posterior del mismo tipo.

    Returns:
        Porcentaje de ocupación entre 0 y 100 con un decimal, o ``None`` si no
        ha pasado tiempo de CPU entre ambas muestras (no hay delta que medir).

    Example:
        >>> porcentaje_cpu(psutil.cpu_times(), psutil.cpu_times())  # doctest: +SKIP
        None
    """
    valoresAntes = antes._asdict()
    valoresDespues = despues._asdict()
    totalDelta = 0.0
    ociosoDelta = 0.0
    for campo, valorDespues in valoresDespues.items():
        if campo in camposCpuDuplicados:
            continue
        delta = valorDespues - valoresAntes.get(campo, 0.0)
        totalDelta += delta
        if campo in camposCpuOciosos:
            ociosoDelta += delta
    if totalDelta <= 0:
        return None
    ocupado = max(0.0, min(totalDelta, totalDelta - ociosoDelta))
    return round(ocupado / totalDelta * 100, 1)


class LectorMetricas:
    """Lee y compone las métricas del host.

    Guarda la muestra anterior de CPU y de E/S de disco para calcular uso y
    velocidades por diferencia entre llamadas. Una sola instancia compartida
    (``lector``) para que todos los clientes midan contra la misma referencia;
    el cerrojo evita que dos peticiones simultáneas pisen esas muestras.

    Args:
        fuente: Módulo o adaptador con la API de psutil. Inyectable para tests.
        relojMonotono: Reloj para medir intervalos (velocidad de disco).
        relojPared: Reloj de época Unix para calcular el uptime.
        rutaDisco: Ruta cuyo sistema de ficheros se mide.
        tempAviso: Umbral de aviso (ºC) cuando el sensor no publica ``high``.
        tempCritica: Umbral crítico (ºC) cuando el sensor no publica ``critical``.
    """

    def __init__(
        self,
        fuente: Any = psutil,
        relojMonotono: Callable[[], float] = time.monotonic,
        relojPared: Callable[[], float] = time.time,
        rutaDisco: str = cfgIni.servidorRutaDisco,
        tempAviso: int = cfgIni.servidorTempAviso,
        tempCritica: int = cfgIni.servidorTempCritica,
    ) -> None:
        self._fuente = fuente
        self._relojMonotono = relojMonotono
        self._relojPared = relojPared
        self._rutaDisco = rutaDisco
        self._tempAviso = tempAviso
        self._tempCritica = tempCritica
        self._cerrojo = threading.Lock()
        self._cpuAnterior = fuente.cpu_times()
        self._nucleosAnteriores = fuente.cpu_times(percpu=True)
        # Si dos peticiones llegan en el mismo tick de reloj no hay delta: se
        # repite el último valor en vez de devolver un hueco.
        self._ultimoCpu: float = 0.0
        self._ultimosNucleos: list[float] = [0.0] * len(self._nucleosAnteriores)
        self._discoAnterior: tuple[float, int, int] | None = None

    def leer(self) -> dict:
        """Devuelve todas las métricas del host en un único diccionario.

        Returns:
            Diccionario serializable a JSON con las claves ``cpu``, ``memory``,
            ``swap``, ``disk``, ``temperatures``, ``cpu_temp``, ``fans``,
            ``uptime_seconds``, ``boot_time`` y ``thresholds``.
        """
        with self._cerrojo:
            temperaturas = self._temperaturas()
            return {
                "cpu": self._cpu(),
                "memory": self._memoria(),
                "swap": self._swap(),
                "disk": self._disco(),
                "temperatures": temperaturas,
                "cpu_temp": self._temperatura_cpu(temperaturas),
                "fans": self._ventiladores(),
                **self._arranque(),
                "thresholds": {"temp_warn": self._tempAviso, "temp_crit": self._tempCritica},
            }

    def temperatura_cpu(self) -> float | None:
        """Temperatura de la CPU para la barra de la cabecera.

        Returns:
            Grados Celsius, o ``None`` si no hay un sensor de CPU legible.
        """
        return self._temperatura_cpu(self._temperaturas())

    def porcentaje_cpu_total(self) -> float:
        """Uso total de CPU desde la muestra anterior, sin bloquear.

        Lo usa la barra de la cabecera en lugar de ``psutil.cpu_percent(0.3)``,
        que retenía un hilo del pool 300 ms en cada refresco.

        Returns:
            Porcentaje de 0 a 100.
        """
        with self._cerrojo:
            return self._actualizar_cpu_total()

    def _actualizar_cpu_total(self) -> float:
        actual = self._fuente.cpu_times()
        pct = porcentaje_cpu(self._cpuAnterior, actual)
        if pct is not None:
            self._ultimoCpu = pct
        self._cpuAnterior = actual
        return self._ultimoCpu

    def _cpu(self) -> dict:
        self._actualizar_cpu_total()
        nucleos = self._fuente.cpu_times(percpu=True)
        # Un núcleo puede aparecer o desaparecer (hotplug): sin muestra previa
        # emparejable se reinicia la referencia de todos.
        if len(nucleos) == len(self._nucleosAnteriores):
            for indice, (previo, ahora) in enumerate(zip(self._nucleosAnteriores, nucleos)):
                pctNucleo = porcentaje_cpu(previo, ahora)
                if pctNucleo is not None:
                    self._ultimosNucleos[indice] = pctNucleo
        else:
            self._ultimosNucleos = [0.0] * len(nucleos)
        self._nucleosAnteriores = nucleos
        return {
            "pct": self._ultimoCpu,
            "per_core": list(self._ultimosNucleos),
            "cores_logical": self._fuente.cpu_count(logical=True),
            "cores_physical": self._fuente.cpu_count(logical=False),
            **self._frecuencia(),
            "load_avg": self._carga(),
        }

    def _frecuencia(self) -> dict:
        try:
            frecuencia = self._fuente.cpu_freq()
        except (OSError, NotImplementedError) as error:
            # Habitual en máquinas virtuales y algunos ARM: no hay cpufreq.
            logger.debug("Frecuencia de CPU no disponible: %s", error)
            frecuencia = None
        if not frecuencia:
            return {"freq_mhz": None, "freq_max_mhz": None}
        return {
            "freq_mhz": round(frecuencia.current),
            "freq_max_mhz": round(frecuencia.max) if frecuencia.max else None,
        }

    def _carga(self) -> list[float] | None:
        try:
            return [round(valor, 2) for valor in self._fuente.getloadavg()]
        except (OSError, AttributeError) as error:
            logger.debug("Carga media no disponible: %s", error)
            return None

    def _memoria(self) -> dict:
        memoria = self._fuente.virtual_memory()
        return {
            "total": memoria.total,
            "used": memoria.used,
            "available": memoria.available,
            "pct": memoria.percent,
            # Solo existen en Linux; en otros sistemas no se inventan.
            "cached": getattr(memoria, "cached", None),
            "buffers": getattr(memoria, "buffers", None),
        }

    def _swap(self) -> dict:
        swap = self._fuente.swap_memory()
        return {"total": swap.total, "used": swap.used, "pct": swap.percent}

    def _disco(self) -> dict:
        uso = self._fuente.disk_usage(self._rutaDisco)
        return {
            "path": self._rutaDisco,
            "total": uso.total,
            "used": uso.used,
            "free": uso.free,
            "pct": uso.percent,
            **self._velocidad_disco(),
        }

    def _velocidad_disco(self) -> dict:
        sinDatos = {"read_bps": None, "write_bps": None}
        try:
            contadores = self._fuente.disk_io_counters()
        except OSError as error:
            logger.debug("Contadores de disco no disponibles: %s", error)
            contadores = None
        if contadores is None:
            self._discoAnterior = None
            return sinDatos
        ahora = self._relojMonotono()
        anterior = self._discoAnterior
        self._discoAnterior = (ahora, contadores.read_bytes, contadores.write_bytes)
        if anterior is None:
            return sinDatos
        instanteAnterior, leidosAnterior, escritosAnterior = anterior
        segundos = ahora - instanteAnterior
        leidos = contadores.read_bytes - leidosAnterior
        escritos = contadores.write_bytes - escritosAnterior
        # Contadores que bajan = disco desconectado o reinicio de contadores:
        # ese intervalo no tiene una velocidad con sentido.
        if segundos <= 0 or leidos < 0 or escritos < 0:
            return sinDatos
        return {"read_bps": round(leidos / segundos), "write_bps": round(escritos / segundos)}

    def _temperaturas(self) -> list[dict]:
        lectura = getattr(self._fuente, "sensors_temperatures", None)
        if lectura is None:
            # psutil no lo implementa en Windows ni en macOS.
            return []
        try:
            sensores = lectura()
        except OSError as error:
            logger.warning("No se pudieron leer las temperaturas: %s", error)
            return []
        resultado = []
        for sensor, entradas in sensores.items():
            for indice, entrada in enumerate(entradas):
                if entrada.current is None:
                    continue
                resultado.append({
                    "sensor": sensor,
                    "label": entrada.label or f"{sensor} {indice + 1}",
                    "current": round(entrada.current, 1),
                    # Algunos drivers publican 0 o valores absurdos como umbral;
                    # en ese caso se usa el configurado.
                    "high": self._umbral_valido(entrada.high, self._tempAviso),
                    "critical": self._umbral_valido(entrada.critical, self._tempCritica),
                })
        return resultado

    @staticmethod
    def _umbral_valido(valor: float | None, porDefecto: int) -> float:
        if valor is None or valor <= 0 or valor >= 150:
            return porDefecto
        return round(valor, 1)

    @staticmethod
    def _temperatura_cpu(temperaturas: list[dict]) -> float | None:
        porSensor: dict[str, list[float]] = {}
        for entrada in temperaturas:
            porSensor.setdefault(entrada["sensor"], []).append(entrada["current"])
        for sensor in sensoresCpu:
            if sensor in porSensor:
                # La más alta: el paquete o el núcleo más caliente es lo que
                # importa para saber si hay que preocuparse.
                return max(porSensor[sensor])
        return None

    def _ventiladores(self) -> list[dict]:
        lectura = getattr(self._fuente, "sensors_fans", None)
        if lectura is None:
            return []
        try:
            ventiladores = lectura()
        except OSError as error:
            logger.warning("No se pudieron leer los ventiladores: %s", error)
            return []
        return [
            {"sensor": sensor, "label": entrada.label or f"{sensor} {indice + 1}", "rpm": entrada.current}
            for sensor, entradas in ventiladores.items()
            for indice, entrada in enumerate(entradas)
        ]

    def _arranque(self) -> dict:
        arranque = self._fuente.boot_time()
        return {
            "uptime_seconds": max(0, int(self._relojPared() - arranque)),
            "boot_time": datetime.fromtimestamp(arranque, tz=timezone.utc).isoformat(),
        }


# Instancia compartida: guarda las muestras previas de CPU y disco, que deben
# ser comunes a todas las peticiones para que los deltas tengan sentido.
lector = LectorMetricas()
