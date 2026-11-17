"""Piezas de seguridad de la app, separadas de FastAPI para poder testearlas.

- ``escribirJsonPrivado``: escritura atómica con permisos 0600.
- ``AlmacenCredenciales``: config.json (usuario + hash bcrypt) con caché en
  memoria que se invalida si el archivo cambia en disco.
- ``GestorSesion``: token de sesión persistente con caducidad en servidor.
- ``LimitadorIntentos``: bloqueo por IP tras N fallos de login.
- ``ipCliente`` y ``origenPermitido``: IP real del cliente (solo se fía de
  X-Forwarded-For si la conexión llega de un proxy de confianza) y defensa
  contra Cross-Site WebSocket Hijacking.
"""

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from collections import defaultdict
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

RedIp = ipaddress.IPv4Network | ipaddress.IPv6Network


def escribirJsonPrivado(ruta: str, datos: Any) -> None:
    """Escribe JSON de forma atómica y legible solo por el propietario.

    Se escribe en un temporal del mismo directorio y se renombra con
    ``os.replace``: un corte a mitad de escritura nunca deja el archivo a medias
    (antes eso dejaba la app sin usuario y abría de nuevo el setup inicial).

    Args:
        ruta: Ruta final del archivo.
        datos: Objeto serializable a JSON.

    Raises:
        OSError: Si no se puede escribir o renombrar.
    """
    directorio = os.path.dirname(os.path.abspath(ruta))
    os.makedirs(directorio, exist_ok=True)
    descriptor, rutaTemporal = tempfile.mkstemp(dir=directorio, prefix=".tmp-", suffix=".json")
    try:
        # mkstemp ya crea el archivo con 0600; el chmod cubre sistemas donde no.
        os.chmod(rutaTemporal, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as archivo:
            json.dump(datos, archivo, indent=2)
        os.replace(rutaTemporal, ruta)
    except BaseException:
        try:
            os.unlink(rutaTemporal)
        except OSError:
            pass
        raise


def _leerJson(ruta: str) -> Any | None:
    try:
        with open(ruta, encoding="utf-8") as archivo:
            return json.load(archivo)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        logger.error("No se pudo leer %s: %s", ruta, error)
        return None


class AlmacenCredenciales:
    """Acceso a config.json con caché en memoria.

    El middleware de autenticación lo consulta en cada petición; releer y
    parsear el JSON cada vez era trabajo inútil. Se comprueba ``mtime`` y
    tamaño con un ``stat`` (barato) para seguir detectando cambios hechos a
    mano, como borrar el archivo para volver al setup inicial.

    Args:
        ruta: Ruta de config.json.
    """

    def __init__(self, ruta: str) -> None:
        self._ruta = ruta
        self._cerrojo = threading.Lock()
        self._firma: tuple[int, int] | None = None
        self._datos: dict | None = None

    def cargar(self) -> dict | None:
        """Devuelve una copia de la configuración, o ``None`` si no hay setup.

        Returns:
            Diccionario con ``username`` y ``password_hash``, o ``None``.
        """
        try:
            estado = os.stat(self._ruta)
        except FileNotFoundError:
            with self._cerrojo:
                self._firma, self._datos = None, None
            return None
        firma = (estado.st_mtime_ns, estado.st_size)
        with self._cerrojo:
            if firma != self._firma:
                datos = _leerJson(self._ruta)
                self._datos = datos if isinstance(datos, dict) else None
                self._firma = firma
            return dict(self._datos) if self._datos else None

    def guardar(self, datos: dict) -> None:
        """Guarda la configuración (atómica, 0600) y actualiza la caché.

        Args:
            datos: Configuración completa a guardar.

        Raises:
            OSError: Si no se puede escribir.
        """
        with self._cerrojo:
            escribirJsonPrivado(self._ruta, datos)
            self._firma = None  # se relee en la próxima carga con su mtime real
            self._datos = None

    def crearSiNoExiste(self, datos: dict) -> bool:
        """Guarda la configuración solo si aún no existe (setup inicial).

        Evita que dos peticiones de setup simultáneas se pisen: solo la primera
        crea el usuario.

        Args:
            datos: Configuración inicial.

        Returns:
            ``True`` si se ha creado; ``False`` si ya existía.

        Raises:
            OSError: Si no se puede escribir.
        """
        with self._cerrojo:
            if os.path.exists(self._ruta):
                return False
            escribirJsonPrivado(self._ruta, datos)
            self._firma = None
            self._datos = None
            return True


class GestorSesion:
    """Token de sesión único, persistente y con caducidad en el servidor.

    En disco se guarda solo el HMAC-SHA256 del token (con ``claveSecreta``) y su
    caducidad, nunca el token: quien lea el volumen de datos no puede
    reutilizarlo. Así la sesión sobrevive a los reinicios del contenedor, el
    logout la invalida de verdad y cambiar SECRET_KEY cierra todas las sesiones.

    Args:
        ruta: Archivo donde persistir el hash y la caducidad.
        claveSecreta: SECRET_KEY. Vacía = clave aleatoria del proceso, y las
            sesiones no sobreviven a un reinicio.
        duracionSegundos: Vida de la sesión desde el login.
        reloj: Reloj de época Unix (inyectable en tests).
    """

    def __init__(self, ruta: str, claveSecreta: str, duracionSegundos: int,
                 reloj: Callable[[], float] = time.time) -> None:
        self._ruta = ruta
        self._clave = claveSecreta.encode("utf-8") if claveSecreta else secrets.token_bytes(32)
        self._duracion = duracionSegundos
        self._reloj = reloj
        self._cerrojo = threading.Lock()
        datos = _leerJson(ruta)
        self._hash: str | None = None
        self._caduca: float = 0.0
        if isinstance(datos, dict) and isinstance(datos.get("tokenHash"), str):
            self._hash = datos["tokenHash"]
            self._caduca = float(datos.get("caduca") or 0)

    def _hmac(self, token: str) -> str:
        return hmac.new(self._clave, token.encode("utf-8"), hashlib.sha256).hexdigest()

    def _persistir(self) -> None:
        try:
            escribirJsonPrivado(self._ruta, {"tokenHash": self._hash, "caduca": self._caduca})
        except OSError as error:
            # La sesión sigue valiendo en memoria; solo se pierde al reiniciar.
            logger.error("No se pudo guardar la sesión en %s: %s", self._ruta, error)

    def crear(self) -> str:
        """Genera un token nuevo e invalida el anterior (anti session-fixation).

        Returns:
            El token en claro, para la cookie.
        """
        token = secrets.token_hex(32)
        with self._cerrojo:
            self._hash = self._hmac(token)
            self._caduca = self._reloj() + self._duracion
            self._persistir()
        return token

    def invalidar(self) -> None:
        """Cierra la sesión actual (también tras un reinicio)."""
        with self._cerrojo:
            self._hash = None
            self._caduca = 0.0
            self._persistir()

    def valida(self, token: str | None) -> bool:
        """Comprueba el token de la cookie en tiempo constante.

        Args:
            token: Valor de la cookie, o ``None``.

        Returns:
            ``True`` si coincide con la sesión activa y no ha caducado.
        """
        if not token:
            return False
        with self._cerrojo:
            if self._hash is None or self._reloj() >= self._caduca:
                return False
            return hmac.compare_digest(self._hmac(token), self._hash)


class LimitadorIntentos:
    """Cuenta los fallos de login por IP y bloquea durante una ventana.

    Args:
        maxIntentos: Fallos dentro de la ventana que provocan el bloqueo.
        ventanaSegundos: Duración de la ventana y del bloqueo.
        reloj: Reloj monótono (inyectable en tests).
    """

    # Por encima de esta cantidad de IPs se purgan las caducadas en bloque,
    # para que una ráfaga desde muchas IPs no haga crecer el dict sin límite.
    _UMBRAL_PURGA = 1024

    def __init__(self, maxIntentos: int, ventanaSegundos: float,
                 reloj: Callable[[], float] = time.monotonic) -> None:
        self._max = maxIntentos
        self._ventana = ventanaSegundos
        self._reloj = reloj
        self._cerrojo = threading.Lock()
        self._intentos: dict[str, list[float]] = defaultdict(list)

    def _vigentes(self, ip: str, ahora: float) -> list[float]:
        limite = ahora - self._ventana
        vigentes = [t for t in self._intentos.get(ip, ()) if t > limite]
        if vigentes:
            self._intentos[ip] = vigentes
        else:
            self._intentos.pop(ip, None)
        return vigentes

    def _purgar(self, ahora: float) -> None:
        for ip in list(self._intentos):
            self._vigentes(ip, ahora)

    def bloqueada(self, ip: str) -> bool:
        """Indica si la IP ha agotado sus intentos.

        Args:
            ip: IP del cliente.

        Returns:
            ``True`` si está bloqueada.
        """
        with self._cerrojo:
            return len(self._vigentes(ip, self._reloj())) >= self._max

    def registrarFallo(self, ip: str) -> None:
        """Anota un login fallido.

        Args:
            ip: IP del cliente.
        """
        with self._cerrojo:
            ahora = self._reloj()
            if len(self._intentos) >= self._UMBRAL_PURGA:
                self._purgar(ahora)
            self._intentos[ip].append(ahora)

    def limpiar(self, ip: str) -> None:
        """Olvida los fallos de una IP tras un login correcto.

        Args:
            ip: IP del cliente.
        """
        with self._cerrojo:
            self._intentos.pop(ip, None)

    def __len__(self) -> int:
        with self._cerrojo:
            return len(self._intentos)


def parsearProxies(texto: str) -> list[RedIp]:
    """Convierte la lista de proxies de confianza de config.ini en redes.

    Args:
        texto: IPs o redes CIDR separadas por comas. Vacío = ninguno.

    Returns:
        Lista de redes.

    Raises:
        ValueError: Si alguna entrada no es una IP ni una red válida.

    Example:
        >>> parsearProxies("127.0.0.1, 172.16.0.0/12")
        [IPv4Network('127.0.0.1/32'), IPv4Network('172.16.0.0/12')]
    """
    return [ipaddress.ip_network(parte.strip(), strict=False) for parte in texto.split(",") if parte.strip()]


def _enRedes(ip: str, redes: Iterable[RedIp]) -> bool:
    try:
        direccion = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(direccion in red for red in redes)


def ipCliente(ipConexion: str | None, cabeceras: Mapping[str, str], proxiesConfiables: list[RedIp]) -> str:
    """IP real del cliente para el limitador de intentos y los avisos.

    X-Forwarded-For lo escribe el cliente: solo se le hace caso si la conexión
    viene de un proxy de confianza. Aun así se recorre de derecha a izquierda y
    se toma la primera IP que no es otro proxy de confianza, porque las entradas
    de la izquierda también las puede inventar el cliente.

    Args:
        ipConexion: IP del extremo TCP (``request.client.host``).
        cabeceras: Cabeceras de la petición.
        proxiesConfiables: Redes de proxies inversos de confianza.

    Returns:
        La IP del cliente, o ``"unknown"`` si no hay ninguna.
    """
    ip = ipConexion or "unknown"
    if not proxiesConfiables or not _enRedes(ip, proxiesConfiables):
        return ip
    reenviada = cabeceras.get("x-forwarded-for") or ""
    for candidata in reversed([parte.strip() for parte in reenviada.split(",") if parte.strip()]):
        if not _enRedes(candidata, proxiesConfiables):
            return candidata
    return ip


def origenPermitido(origen: str | None, host: str | None) -> bool:
    """Defensa contra Cross-Site WebSocket Hijacking.

    Los navegadores siempre envían Origin en el handshake de un WebSocket; si
    no coincide con el Host, la conexión la ha abierto otra web. Sin Origin no
    es un navegador (curl, scripts) y no hay cookie ajena que robar.

    Args:
        origen: Cabecera Origin.
        host: Cabecera Host.

    Returns:
        ``True`` si se acepta el handshake.
    """
    if not origen:
        return True
    if not host:
        return False
    return urlsplit(origen).netloc.lower() == host.lower()
