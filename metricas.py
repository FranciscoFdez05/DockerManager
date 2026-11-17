"""Histórico en memoria del rendimiento del host y del propio DockerManager.

Un hilo en segundo plano toma una muestra cada `intervalo` segundos y la guarda
en un buffer circular, de modo que al abrir el panel «Rendimiento» ya se ve la
última hora sin esperar. El histórico vive solo en memoria: se pierde al
reiniciar, a cambio de no escribir en disco ni añadir dependencias.

Se usa un hilo propio y no `asyncio.to_thread` porque `psutil.cpu_percent(None)`
guarda la lectura anterior por hilo: si cada muestra cayera en un hilo distinto
del pool, el porcentaje se calcularía contra una referencia equivocada.

Ejemplo:
    muestreador = MuestreadorRendimiento(intervaloSegundos=5, maxMuestras=720)
    muestreador.iniciar()
    ...
    muestreador.historico(desde=ultimoTimestamp)
    muestreador.detener()
"""

import logging
import threading
import time
from collections import deque
from typing import Callable

import psutil

logger = logging.getLogger(__name__)


class MuestreadorRendimiento:
    """Toma muestras periódicas de CPU, memoria, disco y red.

    Args:
        intervaloSegundos: Segundos entre muestras.
        maxMuestras: Tamaño del buffer circular; las más antiguas se descartan.
        proceso: Proceso cuyo consumo se mide como «DockerManager». Por defecto,
            el proceso actual. Inyectable para tests.
        reloj: Función que devuelve el instante actual en segundos. Inyectable
            para tests.
    """

    def __init__(
        self,
        intervaloSegundos: float,
        maxMuestras: int,
        proceso: psutil.Process | None = None,
        reloj: Callable[[], float] = time.time,
    ) -> None:
        if intervaloSegundos <= 0:
            raise ValueError("intervaloSegundos debe ser mayor que 0")
        if maxMuestras <= 0:
            raise ValueError("maxMuestras debe ser mayor que 0")
        self.intervaloSegundos = intervaloSegundos
        self._muestras: deque[dict] = deque(maxlen=maxMuestras)
        self._proceso = proceso or psutil.Process()
        self._reloj = reloj
        self._numCpus = psutil.cpu_count() or 1
        self._lock = threading.Lock()
        self._parar = threading.Event()
        self._hilo: threading.Thread | None = None
        self._anterior: tuple[float, dict] | None = None

    def iniciar(self) -> None:
        """Arranca el hilo de muestreo. Llamarlo dos veces no crea otro hilo."""
        if self._hilo and self._hilo.is_alive():
            return
        self._parar.clear()
        self._hilo = threading.Thread(target=self._bucle, name="metricas", daemon=True)
        self._hilo.start()

    def detener(self, timeoutSegundos: float = 2.0) -> None:
        """Detiene el hilo de muestreo y espera a que termine.

        Args:
            timeoutSegundos: Espera máxima a que el hilo acabe.
        """
        self._parar.set()
        if self._hilo:
            self._hilo.join(timeoutSegundos)

    def historico(self, desde: float | None = None) -> list[dict]:
        """Devuelve las muestras guardadas, de la más antigua a la más reciente.

        Args:
            desde: Si se indica, solo las muestras con `t` estrictamente mayor
                (en milisegundos epoch). Permite al cliente pedir solo lo nuevo.

        Returns:
            Lista de muestras (ver `muestrear`).
        """
        with self._lock:
            muestras = list(self._muestras)
        if desde is None:
            return muestras
        return [m for m in muestras if m["t"] > desde]

    def muestrear(self) -> dict:
        """Toma una muestra, la añade al histórico y la devuelve.

        Las tasas (red y disco) se calculan contra la muestra anterior, así que
        la primera muestra las da a 0.

        Returns:
            Diccionario con `t` (ms epoch), porcentajes de host (`cpu_pct`,
            `mem_pct`, `disk_pct`), tasas en bytes/s (`net_rx`, `net_tx`,
            `disk_read`, `disk_write`) y consumo del proceso (`app_cpu_pct`,
            `app_mem`, `app_threads`).
        """
        ahora = self._reloj()
        contadores = self._leerContadores()
        tasas = self._calcularTasas(ahora, contadores)
        self._anterior = (ahora, contadores)

        muestra = {
            "t": int(ahora * 1000),
            **self._leerHost(),
            **tasas,
            **self._leerProceso(),
        }
        with self._lock:
            self._muestras.append(muestra)
        return muestra

    def _bucle(self) -> None:
        # Primera llamada para fijar la referencia de cpu_percent en este hilo;
        # devuelve 0 y no tiene sentido guardarla.
        psutil.cpu_percent(interval=None)
        self._proceso.cpu_percent(interval=None)
        while not self._parar.wait(self.intervaloSegundos):
            try:
                self.muestrear()
            except (psutil.Error, OSError):
                # Un fallo puntual de lectura no debe matar el hilo: se pierde
                # una muestra y se reintenta en la siguiente vuelta.
                logger.exception("No se pudo tomar la muestra de rendimiento")

    def _leerHost(self) -> dict:
        disco = psutil.disk_usage("/")
        return {
            "cpu_pct": round(psutil.cpu_percent(interval=None), 1),
            "mem_pct": round(psutil.virtual_memory().percent, 1),
            "disk_pct": round(disco.used / disco.total * 100, 1) if disco.total else 0.0,
        }

    def _leerProceso(self) -> dict:
        with self._proceso.oneshot():
            # cpu_percent de un proceso puede pasar de 100 con varios núcleos;
            # se normaliza para compararlo en la misma escala que el host.
            cpuPct = self._proceso.cpu_percent(interval=None) / self._numCpus
            return {
                "app_cpu_pct": round(cpuPct, 1),
                "app_mem": self._proceso.memory_info().rss,
                "app_threads": self._proceso.num_threads(),
            }

    @staticmethod
    def _leerContadores() -> dict:
        red = psutil.net_io_counters()
        # disk_io_counters devuelve None en algunos contenedores y sistemas
        # sin discos físicos visibles.
        disco = psutil.disk_io_counters()
        return {
            "net_rx": red.bytes_recv if red else 0,
            "net_tx": red.bytes_sent if red else 0,
            "disk_read": disco.read_bytes if disco else 0,
            "disk_write": disco.write_bytes if disco else 0,
        }

    def _calcularTasas(self, ahora: float, contadores: dict) -> dict:
        if self._anterior is None:
            return {clave: 0 for clave in contadores}
        antes, previos = self._anterior
        transcurrido = ahora - antes
        if transcurrido <= 0:
            return {clave: 0 for clave in contadores}
        # max(0, ...) porque los contadores se reinician si cambia una interfaz
        # o se desmonta un disco; una tasa negativa no tiene sentido.
        return {
            clave: max(0, round((contadores[clave] - previos[clave]) / transcurrido))
            for clave in contadores
        }
