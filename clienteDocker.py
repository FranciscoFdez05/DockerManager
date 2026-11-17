"""Cliente de Docker compartido por toda la app (Singleton perezoso).

``docker.from_env()`` crea un cliente con su propio pool de conexiones y, con
``version`` automática, hace además una llamada al daemon para negociar la
versión de la API. Hacerlo en cada petición (la interfaz sondea cada pocos
segundos) costaba una ida y vuelta extra y dejaba pools sin cerrar. El cliente
de docker-py es seguro entre hilos para peticiones normales; los flujos largos
(logs en vivo, stats, exec, eventos) siguen usando un cliente propio para no
retener conexiones del pool compartido.
"""

import logging
import threading
from typing import Callable

import docker

logger = logging.getLogger(__name__)


class ProveedorCliente:
    """Crea el cliente la primera vez que se pide y lo reutiliza después.

    Si el daemon no responde al crearlo, se lanza la excepción y se reintenta
    en la siguiente llamada: no se cachea un fallo.

    Args:
        fabrica: Función que construye el cliente (inyectable en tests).
    """

    def __init__(self, fabrica: Callable[[], docker.DockerClient] = docker.from_env) -> None:
        self._fabrica = fabrica
        self._cerrojo = threading.Lock()
        self._cliente: docker.DockerClient | None = None

    def obtener(self) -> docker.DockerClient:
        """Devuelve el cliente compartido, creándolo si hace falta.

        Returns:
            Cliente de Docker.

        Raises:
            docker.errors.DockerException: Si no se puede conectar con el daemon.
        """
        with self._cerrojo:
            if self._cliente is None:
                self._cliente = self._fabrica()
            return self._cliente

    def cerrar(self) -> None:
        """Cierra el pool de conexiones (al apagar la app)."""
        with self._cerrojo:
            if self._cliente is not None:
                try:
                    self._cliente.close()
                except Exception as error:  # cerrar nunca debe impedir el apagado
                    logger.warning("Error al cerrar el cliente de Docker: %s", error)
                self._cliente = None


proveedor = ProveedorCliente()
