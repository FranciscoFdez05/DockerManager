"""Conexión con Telegram: avisos salientes y comandos de solo lectura.

Decisiones de seguridad:
  - El token y los IDs autorizados solo llegan por variables de entorno (.env),
    nunca por config.ini, que se publica en el repositorio.
  - Se usa long-polling (getUpdates) en vez de webhook: no hay que exponer
    ningún puerto ni aceptar conexiones entrantes de Internet.
  - Un update solo se procesa si es un mensaje de chat privado cuyo remitente
    está en la lista blanca. Lo demás se descarta sin leer el texto y sin
    responder: contestar confirmaría a un desconocido que el bot está vivo.
  - Configuración incompleta o mal escrita = Telegram desactivado (falla en
    cerrado), nunca «aceptar a cualquiera».
  - El token va en la URL de la API; los errores que se registran nunca la
    incluyen.

Este módulo solo sabe hablar con Telegram. Qué se avisa y qué responden los
comandos lo decide avisos.py.
"""

import html
import json
import logging
import queue
import re
import threading
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

urlApi = "https://api.telegram.org"
patronToken = re.compile(r"^\d{5,16}:[A-Za-z0-9_-]{30,64}$")

# Límite de sendMessage. Se recorta antes para no rozarlo.
maxCaracteres = 4000
# getUpdates con 20 mensajes cabe de sobra; el tope es por si la respuesta no es la esperada.
maxBytesRespuesta = 1024 * 1024
maxActualizacionesPorPeticion = 20

# Si Telegram pide esperar más que esto (flood control), se descarta el aviso:
# retener el hilo varios minutos atrasaría todos los demás.
maxEsperaReintentoSegundos = 60
esperaMinimaErrorSegundos = 5
esperaMaximaErrorSegundos = 300
intervaloRegistroDescartadosSegundos = 60

categoriasValidas = frozenset({"sistema", "contenedores", "seguridad", "actualizaciones", "recursos"})

Comandos = dict[str, Callable[[], str]]


class ErrorTelegram(Exception):
    """Fallo al llamar a la Bot API.

    Attributes:
        reintentarEn: Segundos que Telegram pide esperar (HTTP 429), o None.
        codigo: Código HTTP, o None si no hubo respuesta.
    """

    def __init__(self, mensaje: str, reintentarEn: float | None = None, codigo: int | None = None) -> None:
        super().__init__(mensaje)
        self.reintentarEn = reintentarEn
        self.codigo = codigo


class ConfiguracionInvalida(ValueError):
    """Token, IDs o categorías de Telegram mal configurados."""


def parsearIds(texto: str) -> frozenset[int]:
    """Convierte la lista de IDs autorizados del entorno en un conjunto.

    Solo se admiten IDs de usuario (positivos). Los negativos son grupos o
    canales, y un grupo autorizado daría acceso a todos sus miembros.

    Args:
        texto: IDs separados por comas, por ejemplo ``"123456789, 987654321"``.

    Returns:
        Conjunto de IDs. Vacío si el texto está vacío.

    Raises:
        ConfiguracionInvalida: Si algún valor no es un entero positivo.
    """
    ids: set[int] = set()
    for posicion, parte in enumerate(texto.split(","), start=1):
        parte = parte.strip()
        if not parte:
            continue
        # No se repite el valor en el mensaje: si alguien pega aquí el token
        # por error, acabaría en los logs.
        if not parte.isdigit() or int(parte) <= 0:
            raise ConfiguracionInvalida(
                f"TELEGRAM_IDS_PERMITIDOS: el valor {posicion} no es un ID de usuario (entero positivo)")
        ids.add(int(parte))
    return frozenset(ids)


def parsearCategorias(texto: str) -> frozenset[str]:
    """Convierte la lista de categorías de config.ini en un conjunto.

    Args:
        texto: Categorías separadas por comas.

    Returns:
        Conjunto de categorías válidas.

    Raises:
        ConfiguracionInvalida: Si alguna categoría no existe.
    """
    categorias = {parte.strip().lower() for parte in texto.split(",") if parte.strip()}
    desconocidas = categorias - categoriasValidas
    if desconocidas:
        raise ConfiguracionInvalida(
            f"[telegram] notificar: categorías desconocidas {sorted(desconocidas)}; "
            f"válidas: {sorted(categoriasValidas)}")
    return frozenset(categorias)


def escapar(valor: object) -> str:
    """Escapa un valor para insertarlo en un mensaje con ``parse_mode=HTML``."""
    return html.escape(str(valor), quote=False)


def recortar(texto: str, limite: int = maxCaracteres) -> str:
    """Recorta un mensaje al límite de Telegram sin partir una línea.

    Los mensajes abren y cierran sus etiquetas HTML en la misma línea, así que
    cortar por un salto de línea nunca deja una etiqueta o entidad a medias
    (Telegram rechazaría el mensaje entero).

    Args:
        texto: Mensaje completo.
        limite: Número máximo de caracteres.

    Returns:
        El texto original si cabe; si no, las líneas que caben más una marca.
    """
    if len(texto) <= limite:
        return texto
    marca = "\n[mensaje recortado]"
    corte = texto.rfind("\n", 0, limite - len(marca))
    if corte <= 0:
        # Una única línea enorme: sin saltos no hay corte seguro para el HTML.
        return escapar(html.unescape(texto)[: limite // 2]) + marca
    return texto[:corte] + marca


class ClienteTelegram:
    """Adapter mínimo sobre la Bot API con urllib.

    Se usa la librería estándar porque solo hacen falta dos métodos y así no se
    añade ninguna dependencia.

    Args:
        token: Token del bot (``<id>:<clave>``).
        timeout: Segundos máximos de cada petición.
        abrir: Función compatible con ``urllib.request.urlopen`` (inyectable en tests).

    Raises:
        ConfiguracionInvalida: Si el token no tiene el formato esperado. Se
            valida porque va dentro de la ruta de la URL.
    """

    def __init__(self, token: str, timeout: float, abrir: Callable[..., Any] = urlopen) -> None:
        if not patronToken.match(token):
            raise ConfiguracionInvalida("TELEGRAM_BOT_TOKEN no tiene el formato <id>:<clave> que da @BotFather")
        self._token = token
        self._timeout = timeout
        self._abrir = abrir

    def llamar(self, metodo: str, datos: dict, timeout: float | None = None) -> Any:
        """Llama a un método de la Bot API.

        Args:
            metodo: Nombre del método (``sendMessage``, ``getUpdates``...).
            datos: Parámetros, enviados como JSON.
            timeout: Timeout de esta llamada; por defecto el del cliente.

        Returns:
            El campo ``result`` de la respuesta.

        Raises:
            ErrorTelegram: Si no hay respuesta o Telegram la rechaza.
        """
        peticion = Request(
            f"{urlApi}/bot{self._token}/{metodo}",
            data=json.dumps(datos).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._abrir(peticion, timeout=timeout or self._timeout) as respuesta:
                cuerpo = json.loads(respuesta.read(maxBytesRespuesta).decode("utf-8"))
        except HTTPError as error:
            # `from None`: HTTPError guarda la URL, que contiene el token.
            raise self._errorHttp(metodo, error) from None
        except (TimeoutError, URLError, OSError, ValueError) as error:
            raise ErrorTelegram(f"{metodo}: sin respuesta válida de Telegram ({self._ocultar(error)})") from None
        if not isinstance(cuerpo, dict) or not cuerpo.get("ok"):
            descripcion = cuerpo.get("description") if isinstance(cuerpo, dict) else None
            raise ErrorTelegram(f"{metodo}: Telegram respondió un error ({self._ocultar(descripcion)})")
        return cuerpo.get("result")

    def enviarMensaje(self, chatId: int, texto: str) -> None:
        """Envía un mensaje HTML a un chat.

        Raises:
            ErrorTelegram: Si el envío falla.
        """
        self.llamar("sendMessage", {
            "chat_id": chatId,
            "text": recortar(texto),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })

    def obtenerActualizaciones(self, offset: int | None, esperaSegundos: int) -> list[dict]:
        """Long-polling de mensajes nuevos.

        Args:
            offset: ID del primer update que interesa; confirma los anteriores.
            esperaSegundos: Cuánto mantiene Telegram la conexión abierta si no hay nada.

        Returns:
            Lista de updates (puede estar vacía).

        Raises:
            ErrorTelegram: Si la petición falla.
        """
        datos: dict[str, Any] = {
            "timeout": esperaSegundos,
            "limit": maxActualizacionesPorPeticion,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            datos["offset"] = offset
        resultado = self.llamar("getUpdates", datos, timeout=esperaSegundos + self._timeout)
        return resultado if isinstance(resultado, list) else []

    def _errorHttp(self, metodo: str, error: HTTPError) -> ErrorTelegram:
        descripcion, reintentarEn = None, None
        try:
            cuerpo = json.loads(error.read(maxBytesRespuesta).decode("utf-8"))
            descripcion = cuerpo.get("description")
            reintentarEn = (cuerpo.get("parameters") or {}).get("retry_after")
        except (OSError, ValueError, AttributeError):
            pass  # cuerpo ilegible: basta con el código HTTP
        finally:
            error.close()
        return ErrorTelegram(f"{metodo}: HTTP {error.code} ({self._ocultar(descripcion)})",
                             reintentarEn=reintentarEn, codigo=error.code)

    def _ocultar(self, valor: object) -> str:
        return str(valor).replace(self._token, "<token>")


class Notificador:
    """Cola de avisos salientes con un hilo que los envía.

    Quien avisa (una petición HTTP, el hilo de eventos de Docker) nunca espera
    a la red: deja el texto en la cola y sigue. La cola es acotada y los
    avisos repetidos dentro de una ventana se ignoran, para que un contenedor
    en bucle de reinicios no inunde el chat ni la memoria.

    Args:
        cliente: Cliente de la Bot API.
        destinatarios: IDs de chat que reciben los avisos.
        maxCola: Avisos pendientes como máximo; los que no caben se cuentan y se descartan.
        ventanaDuplicados: Segundos durante los que un aviso con la misma clave no se repite.
        intervaloEnvio: Pausa entre mensajes (Telegram limita a ~1 mensaje/s por chat).
        reloj: Fuente de tiempo monotónico (inyectable en tests).
    """

    def __init__(self, cliente: ClienteTelegram, destinatarios: frozenset[int], maxCola: int = 100,
                 ventanaDuplicados: float = 300.0, intervaloEnvio: float = 1.0,
                 reloj: Callable[[], float] = time.monotonic) -> None:
        self._cliente = cliente
        self._destinatarios = destinatarios
        self._cola: queue.Queue[str] = queue.Queue(maxsize=maxCola)
        self._ventana = ventanaDuplicados
        self._intervalo = intervaloEnvio
        self._reloj = reloj
        self._recientes: dict[str, float] = {}
        self._descartados = 0
        self._cerrojo = threading.Lock()
        self._parada = threading.Event()
        self._hilo: threading.Thread | None = None

    def notificar(self, texto: str, clave: str | None = None) -> bool:
        """Encola un aviso sin bloquear.

        Args:
            texto: Mensaje HTML (los valores dinámicos ya escapados con ``escapar``).
            clave: Identifica avisos equivalentes para no repetirlos; por defecto el propio texto.

        Returns:
            True si se encoló; False si era un duplicado o la cola estaba llena.
        """
        clave = clave or texto
        with self._cerrojo:
            ahora = self._reloj()
            self._recientes = {k: t for k, t in self._recientes.items() if ahora - t < self._ventana}
            if clave in self._recientes:
                return False
            try:
                self._cola.put_nowait(texto)
            except queue.Full:
                self._descartados += 1
                return False
            self._recientes[clave] = ahora
            return True

    def enviarSiguiente(self, espera: float | None = None) -> bool:
        """Saca un aviso de la cola y lo envía a todos los destinatarios.

        Args:
            espera: Segundos a esperar si la cola está vacía (None = sin límite).

        Returns:
            True si había un aviso; False si la cola seguía vacía.
        """
        try:
            texto = self._cola.get(timeout=espera)
        except queue.Empty:
            return False
        with self._cerrojo:
            descartados, self._descartados = self._descartados, 0
        if descartados:
            texto += f"\n\n<i>{descartados} avisos descartados por exceso de mensajes.</i>"
        for chatId in self._destinatarios:
            self._enviarA(chatId, texto)
        return True

    def iniciar(self) -> None:
        """Arranca el hilo de envío (daemon)."""
        self._hilo = threading.Thread(target=self._bucle, name="telegram-envio", daemon=True)
        self._hilo.start()

    def detener(self) -> None:
        """Pide al hilo que termine tras el envío en curso."""
        self._parada.set()

    def _bucle(self) -> None:
        while not self._parada.is_set():
            # Espera acotada para poder ver la señal de parada aunque no haya avisos.
            if self.enviarSiguiente(espera=1.0):
                self._parada.wait(self._intervalo)

    def _enviarA(self, chatId: int, texto: str) -> None:
        try:
            self._cliente.enviarMensaje(chatId, texto)
            return
        except ErrorTelegram as error:
            if not error.reintentarEn or error.reintentarEn > maxEsperaReintentoSegundos:
                logger.warning("No se pudo enviar un aviso por Telegram: %s", error)
                return
            self._parada.wait(error.reintentarEn)
        try:
            self._cliente.enviarMensaje(chatId, texto)
        except ErrorTelegram as error:
            logger.warning("No se pudo enviar un aviso por Telegram tras reintentar: %s", error)


def mensajeAutorizado(actualizacion: dict, idsPermitidos: frozenset[int]) -> dict | None:
    """Devuelve el mensaje solo si viene de un usuario autorizado por chat privado.

    El remitente se comprueba antes de mirar nada más: de un update no
    autorizado no se lee ni el texto.

    Args:
        actualizacion: Update tal como lo devuelve ``getUpdates``.
        idsPermitidos: Lista blanca de IDs de usuario.

    Returns:
        El mensaje, o None si hay que descartarlo.
    """
    mensaje = actualizacion.get("message")
    if not isinstance(mensaje, dict):
        return None
    remitente = mensaje.get("from")
    if not isinstance(remitente, dict):
        return None
    idRemitente = remitente.get("id")
    # type() y no isinstance(): True es un int y valdría como ID 1.
    if type(idRemitente) is not int or idRemitente not in idsPermitidos:
        return None
    if remitente.get("is_bot"):
        return None
    chat = mensaje.get("chat")
    # Solo chat privado con el propio usuario: en un grupo responderíamos
    # delante de todos los miembros.
    if not isinstance(chat, dict) or chat.get("type") != "private" or chat.get("id") != idRemitente:
        return None
    return mensaje


def extraerComando(texto: str) -> str:
    """Normaliza ``"/Estado@MiBot argumentos"`` a ``"/estado"``."""
    partes = texto.strip().split(maxsplit=1)
    if not partes:
        return ""
    return partes[0].split("@", 1)[0].lower()


class Receptor:
    """Recibe mensajes por long-polling y responde a los comandos autorizados.

    Args:
        cliente: Cliente de la Bot API.
        idsPermitidos: Lista blanca de IDs de usuario.
        comandos: ``{"/comando": función sin argumentos que devuelve el texto HTML}``.
            Debe incluir ``"/ayuda"``, que responde a los comandos desconocidos.
        esperaSegundos: Duración del long-polling.
        antiguedadMaxima: Los mensajes más viejos se ignoran (enviados mientras
            la app estaba parada).
        reloj: Fuente de tiempo Unix (inyectable en tests).
    """

    def __init__(self, cliente: ClienteTelegram, idsPermitidos: frozenset[int], comandos: Comandos,
                 esperaSegundos: int = 30, antiguedadMaxima: float = 300.0,
                 reloj: Callable[[], float] = time.time) -> None:
        if "/ayuda" not in comandos:
            raise ValueError("Los comandos deben incluir /ayuda")
        self._cliente = cliente
        self._ids = idsPermitidos
        self._comandos = comandos
        self._espera = esperaSegundos
        self._antiguedadMaxima = antiguedadMaxima
        self._reloj = reloj
        self._offset: int | None = None
        self._descartados = 0
        self._ultimoRegistro = 0.0
        self._parada = threading.Event()
        self._hilo: threading.Thread | None = None

    @property
    def offset(self) -> int | None:
        """Siguiente update que se pedirá a Telegram."""
        return self._offset

    def procesar(self, actualizaciones: list[dict]) -> None:
        """Atiende un lote de updates.

        El offset avanza también con los descartados: si no, Telegram los
        devolvería en cada petición.

        Args:
            actualizaciones: Lista devuelta por ``getUpdates``.
        """
        for actualizacion in actualizaciones:
            if not isinstance(actualizacion, dict):
                continue
            idActualizacion = actualizacion.get("update_id")
            if type(idActualizacion) is int:
                self._offset = max(self._offset or 0, idActualizacion + 1)
            mensaje = mensajeAutorizado(actualizacion, self._ids)
            if mensaje is None:
                self._registrarDescartado(actualizacion)
                continue
            fecha = mensaje.get("date")
            if type(fecha) is not int or self._reloj() - fecha > self._antiguedadMaxima:
                continue
            self._atender(mensaje)

    def iniciar(self) -> None:
        """Arranca el hilo de long-polling (daemon)."""
        self._hilo = threading.Thread(target=self._bucle, name="telegram-recepcion", daemon=True)
        self._hilo.start()

    def detener(self) -> None:
        """Pide al hilo que termine al acabar la petición en curso."""
        self._parada.set()

    def _bucle(self) -> None:
        espera = esperaMinimaErrorSegundos
        while not self._parada.is_set():
            try:
                self.procesar(self._cliente.obtenerActualizaciones(self._offset, self._espera))
                espera = esperaMinimaErrorSegundos
            except ErrorTelegram as error:
                # 409 = hay otra instancia leyendo con el mismo token o un webhook activo.
                logger.warning("Telegram: %s. Reintento en %d s", error, espera)
                self._parada.wait(espera)
                espera = min(espera * 2, esperaMaximaErrorSegundos)

    def _atender(self, mensaje: dict) -> None:
        texto = mensaje.get("text")
        if not isinstance(texto, str):
            return
        comando = extraerComando(texto)
        manejador = self._comandos.get(comando, self._comandos["/ayuda"])
        try:
            respuesta = manejador()
        except Exception:
            # El manejador consulta Docker o el host; cualquier fallo se
            # registra y se contesta, pero no puede tumbar el hilo de recepción.
            logger.exception("Fallo al responder al comando de Telegram %s", comando)
            respuesta = "No se pudo obtener la información. Revisa los logs de DockerManager."
        try:
            self._cliente.enviarMensaje(mensaje["chat"]["id"], respuesta)
        except ErrorTelegram as error:
            logger.warning("No se pudo responder por Telegram: %s", error)

    def _registrarDescartado(self, actualizacion: dict) -> None:
        # Se registra el ID (ayuda a configurar el propio) pero nunca el
        # contenido, y como mucho una línea por minuto para que nadie pueda
        # llenar los logs mandando mensajes.
        self._descartados += 1
        ahora = self._reloj()
        if ahora - self._ultimoRegistro < intervaloRegistroDescartadosSegundos:
            return
        mensaje = actualizacion.get("message")
        remitente = mensaje.get("from") if isinstance(mensaje, dict) else None
        idRemitente = remitente.get("id") if isinstance(remitente, dict) else None
        logger.warning("Telegram: %d mensajes descartados de remitentes no autorizados (último ID: %s)",
                       self._descartados, idRemitente if type(idRemitente) is int else "desconocido")
        self._descartados = 0
        self._ultimoRegistro = ahora


class ServicioTelegram:
    """Fachada que une notificador, receptor y el filtro por categorías.

    Args:
        notificador: Cola de avisos salientes.
        receptor: Receptor de comandos.
        categorias: Categorías de aviso activadas en config.ini.
    """

    def __init__(self, notificador: Notificador, receptor: Receptor, categorias: frozenset[str]) -> None:
        self._notificador = notificador
        self._receptor = receptor
        self._categorias = categorias

    def acepta(self, categoria: str) -> bool:
        """Indica si una categoría de avisos está activada."""
        return categoria in self._categorias

    def avisar(self, categoria: str, texto: str, clave: str | None = None) -> bool:
        """Encola un aviso si su categoría está activada.

        Args:
            categoria: Una de ``categoriasValidas``.
            texto: Mensaje HTML con los valores dinámicos escapados.
            clave: Clave de deduplicación (ver ``Notificador.notificar``).

        Returns:
            True si se encoló.
        """
        if not self.acepta(categoria):
            return False
        return self._notificador.notificar(texto, clave)

    def enviarPrueba(self, texto: str) -> bool:
        """Encola un mensaje de prueba sin filtrar por categoría.

        La prueba tiene que llegar aunque todas las categorías estén apagadas:
        lo que comprueba es la conexión, no la selección de avisos.

        Args:
            texto: Mensaje HTML con los valores dinámicos escapados.

        Returns:
            True si se encoló; False si la cola estaba llena o era un duplicado.
        """
        return self._notificador.notificar(texto)

    def iniciar(self) -> None:
        """Arranca los hilos de envío y recepción."""
        self._notificador.iniciar()
        self._receptor.iniciar()

    def detener(self) -> None:
        """Pide a los hilos que terminen."""
        self._notificador.detener()
        self._receptor.detener()


def construir(token: str, idsTexto: str, categoriasTexto: str, comandos: Comandos,
              timeout: float) -> ServicioTelegram | None:
    """Factory: crea el servicio a partir de la configuración, o None si queda desactivado.

    Cualquier dato que falte o no sea válido desactiva Telegram entero, con un
    mensaje en el log que dice por qué.

    Args:
        token: Valor de ``TELEGRAM_BOT_TOKEN``.
        idsTexto: Valor de ``TELEGRAM_IDS_PERMITIDOS``.
        categoriasTexto: ``[telegram] notificar`` de config.ini.
        comandos: Comandos de solo lectura (ver ``Receptor``).
        timeout: Timeout de las peticiones a la Bot API.

    Returns:
        El servicio sin arrancar, o None.
    """
    if not token and not idsTexto:
        logger.info("Telegram desactivado: TELEGRAM_BOT_TOKEN y TELEGRAM_IDS_PERMITIDOS no están definidos")
        return None
    if not token or not idsTexto:
        logger.warning("Telegram desactivado: hacen falta TELEGRAM_BOT_TOKEN y TELEGRAM_IDS_PERMITIDOS")
        return None
    try:
        ids = parsearIds(idsTexto)
        categorias = parsearCategorias(categoriasTexto)
        cliente = ClienteTelegram(token, timeout)
    except ConfiguracionInvalida as error:
        logger.error("Telegram desactivado: %s", error)
        return None
    if not ids:
        logger.warning("Telegram desactivado: TELEGRAM_IDS_PERMITIDOS no contiene ningún ID")
        return None
    logger.info("Telegram activado para %d usuario(s); avisos: %s", len(ids), ", ".join(sorted(categorias)))
    return ServicioTelegram(Notificador(cliente, ids), Receptor(cliente, ids, comandos), categorias)
