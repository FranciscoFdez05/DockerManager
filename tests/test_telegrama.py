"""Tests de telegrama.py con un urlopen y un cliente falsos (sin red)."""

import io
import json
import os
import sys
import threading
from urllib.error import HTTPError, URLError

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import telegrama  # noqa: E402

TOKEN = "123456789:AAH" + "x" * 32
AHORA = 1_800_000_000
USUARIO = 111111


# ── Fakes ─────────────────────────────────────────────────────────────────────
class RespuestaFalsa(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class AbrirFalso:
    """Sustituye a urlopen: guarda las peticiones y devuelve o lanza lo programado."""

    def __init__(self, resultado=None, error=None):
        self.resultado = resultado
        self.error = error
        self.peticiones = []

    def __call__(self, peticion, timeout):
        self.peticiones.append((peticion, timeout))
        if self.error:
            raise self.error
        return RespuestaFalsa(json.dumps(self.resultado).encode())


class ClienteFalso:
    def __init__(self, fallos=()):
        self.enviados = []
        self.fallos = list(fallos)

    def enviarMensaje(self, chatId, texto):
        if self.fallos:
            raise self.fallos.pop(0)
        self.enviados.append((chatId, texto))


class TextoProhibido(dict):
    """Mensaje cuyo texto no se puede leer: demuestra que no se procesa."""

    def get(self, clave, defecto=None):
        if clave == "text":
            raise AssertionError("se ha leído el texto de un mensaje no autorizado")
        return super().get(clave, defecto)

    def __getitem__(self, clave):
        if clave == "text":
            raise AssertionError("se ha leído el texto de un mensaje no autorizado")
        return super().__getitem__(clave)


def actualizacion(idUpdate=1, remitente=USUARIO, chatId=None, tipo="private", texto="/estado",
                  fecha=AHORA, esBot=False, claseMensaje=dict):
    mensaje = claseMensaje({
        "message_id": 1,
        "from": {"id": remitente, "is_bot": esBot},
        "chat": {"id": remitente if chatId is None else chatId, "type": tipo},
        "date": fecha,
        "text": texto,
    })
    return {"update_id": idUpdate, "message": mensaje}


def errorHttp(codigo, cuerpo):
    return HTTPError(f"https://api.telegram.org/bot{TOKEN}/sendMessage", codigo, "error", {},
                     io.BytesIO(json.dumps(cuerpo).encode()))


def receptor(cliente, comandos=None, ids=frozenset({USUARIO})):
    comandos = comandos or {"/ayuda": lambda: "ayuda", "/estado": lambda: "estado"}
    return telegrama.Receptor(cliente, ids, comandos, reloj=lambda: AHORA)


# ── Parseo de configuración ───────────────────────────────────────────────────
def test_parsearIds_admite_espacios_y_vacios():
    assert telegrama.parsearIds(" 123, 456 ,,789 ") == frozenset({123, 456, 789})
    assert telegrama.parsearIds("") == frozenset()


@pytest.mark.parametrize("texto", ["-100123", "abc", "12.5", "0", TOKEN])
def test_parsearIds_rechaza_lo_que_no_es_id_de_usuario(texto):
    with pytest.raises(telegrama.ConfiguracionInvalida) as error:
        telegrama.parsearIds(texto)
    assert texto not in str(error.value)  # nunca repetir el valor (podría ser el token)


def test_parsearCategorias():
    assert telegrama.parsearCategorias("Seguridad, recursos") == frozenset({"seguridad", "recursos"})
    with pytest.raises(telegrama.ConfiguracionInvalida):
        telegrama.parsearCategorias("seguridad, inventada")


def test_recortar_no_parte_lineas():
    texto = "\n".join(f"<b>linea {i}</b>" for i in range(1000))
    recortado = telegrama.recortar(texto, limite=200)
    assert len(recortado) <= 200
    assert recortado.endswith("[mensaje recortado]")
    assert recortado.count("<b>") == recortado.count("</b>")
    assert telegrama.recortar("corto") == "corto"


def test_escapar():
    assert telegrama.escapar("<a & b>") == "&lt;a &amp; b&gt;"


# ── ClienteTelegram ───────────────────────────────────────────────────────────
def test_cliente_rechaza_token_mal_formado():
    with pytest.raises(telegrama.ConfiguracionInvalida):
        telegrama.ClienteTelegram("123:abc/../../otra", timeout=5)


def test_cliente_envia_json_con_timeout():
    abrir = AbrirFalso({"ok": True, "result": {"message_id": 7}})
    cliente = telegrama.ClienteTelegram(TOKEN, timeout=5, abrir=abrir)
    cliente.enviarMensaje(42, "hola")
    peticion, timeout = abrir.peticiones[0]
    assert peticion.full_url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert timeout == 5
    datos = json.loads(peticion.data)
    assert datos == {"chat_id": 42, "text": "hola", "parse_mode": "HTML", "disable_web_page_preview": True}


def test_cliente_getUpdates_amplia_el_timeout_con_el_long_polling():
    abrir = AbrirFalso({"ok": True, "result": [{"update_id": 1}]})
    cliente = telegrama.ClienteTelegram(TOKEN, timeout=5, abrir=abrir)
    assert cliente.obtenerActualizaciones(10, 30) == [{"update_id": 1}]
    peticion, timeout = abrir.peticiones[0]
    assert timeout == 35
    assert json.loads(peticion.data)["allowed_updates"] == ["message"]


def test_cliente_429_devuelve_reintento_y_oculta_token():
    abrir = AbrirFalso(error=errorHttp(429, {"ok": False, "description": f"Too Many {TOKEN}",
                                             "parameters": {"retry_after": 3}}))
    cliente = telegrama.ClienteTelegram(TOKEN, timeout=5, abrir=abrir)
    with pytest.raises(telegrama.ErrorTelegram) as error:
        cliente.enviarMensaje(1, "x")
    assert error.value.reintentarEn == 3
    assert error.value.codigo == 429
    assert TOKEN not in str(error.value)
    assert error.value.__cause__ is None and error.value.__suppress_context__


def test_cliente_error_de_red_oculta_token():
    abrir = AbrirFalso(error=URLError(f"fallo en /bot{TOKEN}/getUpdates"))
    cliente = telegrama.ClienteTelegram(TOKEN, timeout=5, abrir=abrir)
    with pytest.raises(telegrama.ErrorTelegram) as error:
        cliente.obtenerActualizaciones(None, 30)
    assert TOKEN not in str(error.value)


def test_cliente_respuesta_ok_false():
    cliente = telegrama.ClienteTelegram(TOKEN, timeout=5, abrir=AbrirFalso({"ok": False, "description": "malo"}))
    with pytest.raises(telegrama.ErrorTelegram, match="malo"):
        cliente.llamar("getMe", {})


# ── Lista blanca ──────────────────────────────────────────────────────────────
def test_mensajeAutorizado_acepta_usuario_en_privado():
    assert telegrama.mensajeAutorizado(actualizacion(), frozenset({USUARIO})) is not None


@pytest.mark.parametrize("kwargs", [
    {"remitente": 999},                       # ID desconocido
    {"tipo": "group", "chatId": -100500},     # usuario autorizado, pero en un grupo
    {"chatId": 222},                          # chat distinto del remitente
    {"esBot": True},
])
def test_mensajeAutorizado_descarta(kwargs):
    assert telegrama.mensajeAutorizado(actualizacion(**kwargs), frozenset({USUARIO})) is None


def test_mensajeAutorizado_no_acepta_bool_como_id():
    # True == 1 en Python: no debe colarse como el usuario 1.
    assert telegrama.mensajeAutorizado(actualizacion(remitente=True), frozenset({1})) is None


@pytest.mark.parametrize("update", [{"update_id": 1}, {"update_id": 1, "message": "x"},
                                    {"update_id": 1, "message": {"chat": {"id": USUARIO}}}])
def test_mensajeAutorizado_updates_malformados(update):
    assert telegrama.mensajeAutorizado(update, frozenset({USUARIO})) is None


def test_extraerComando():
    assert telegrama.extraerComando("  /Estado@MiBot ahora ") == "/estado"
    assert telegrama.extraerComando("") == ""


# ── Receptor ──────────────────────────────────────────────────────────────────
def test_receptor_descarta_desconocidos_sin_leer_ni_responder():
    cliente = ClienteFalso()
    llamados = []
    r = receptor(cliente, {"/ayuda": lambda: llamados.append("ayuda") or "ayuda"})
    r.procesar([actualizacion(idUpdate=5, remitente=999, claseMensaje=TextoProhibido),
                actualizacion(idUpdate=6, tipo="group", chatId=-1, claseMensaje=TextoProhibido)])
    assert cliente.enviados == []
    assert llamados == []
    assert r.offset == 7  # se confirman igualmente para que Telegram no los reenvíe


def test_receptor_responde_al_autorizado():
    cliente = ClienteFalso()
    r = receptor(cliente)
    r.procesar([actualizacion(texto="/estado")])
    assert cliente.enviados == [(USUARIO, "estado")]


def test_receptor_comando_desconocido_responde_ayuda():
    cliente = ClienteFalso()
    receptor(cliente).procesar([actualizacion(texto="/borrar_todo")])
    assert cliente.enviados == [(USUARIO, "ayuda")]


def test_receptor_ignora_mensajes_antiguos_y_sin_texto():
    cliente = ClienteFalso()
    r = receptor(cliente)
    r.procesar([actualizacion(idUpdate=1, fecha=AHORA - 3600), actualizacion(idUpdate=2, texto=None)])
    assert cliente.enviados == []
    assert r.offset == 3


def test_receptor_fallo_del_comando_no_tumba_el_hilo():
    cliente = ClienteFalso()

    def falla():
        raise RuntimeError("docker caído")

    receptor(cliente, {"/ayuda": lambda: "ayuda", "/estado": falla}).procesar([actualizacion()])
    assert len(cliente.enviados) == 1
    assert "No se pudo" in cliente.enviados[0][1]


def test_receptor_exige_ayuda():
    with pytest.raises(ValueError):
        telegrama.Receptor(ClienteFalso(), frozenset({1}), {"/estado": lambda: ""})


# ── Notificador ───────────────────────────────────────────────────────────────
def test_notificador_envia_a_todos_los_destinatarios():
    cliente = ClienteFalso()
    n = telegrama.Notificador(cliente, frozenset({1, 2}))
    assert n.notificar("hola")
    assert n.enviarSiguiente(0)
    assert sorted(cliente.enviados) == [(1, "hola"), (2, "hola")]
    assert not n.enviarSiguiente(0)


def test_notificador_deduplica_dentro_de_la_ventana():
    momento = [0.0]
    n = telegrama.Notificador(ClienteFalso(), frozenset({1}), ventanaDuplicados=300, reloj=lambda: momento[0])
    assert n.notificar("a", clave="k")
    assert not n.notificar("b", clave="k")
    momento[0] = 301
    assert n.notificar("c", clave="k")


def test_notificador_cola_llena_avisa_de_descartados():
    cliente = ClienteFalso()
    n = telegrama.Notificador(cliente, frozenset({1}), maxCola=1)
    assert n.notificar("uno")
    assert not n.notificar("dos")
    assert not n.notificar("tres")
    n.enviarSiguiente(0)
    assert "2 avisos descartados" in cliente.enviados[0][1]


def test_notificador_reintenta_tras_429_corto():
    cliente = ClienteFalso(fallos=[telegrama.ErrorTelegram("429", reintentarEn=0.01)])
    n = telegrama.Notificador(cliente, frozenset({1}))
    n.notificar("hola")
    n.enviarSiguiente(0)
    assert cliente.enviados == [(1, "hola")]


def test_notificador_no_reintenta_otros_errores():
    cliente = ClienteFalso(fallos=[telegrama.ErrorTelegram("403 bloqueado", codigo=403)])
    n = telegrama.Notificador(cliente, frozenset({1}))
    n.notificar("hola")
    n.enviarSiguiente(0)
    assert cliente.enviados == []


# ── Factory y servicio ────────────────────────────────────────────────────────
COMANDOS = {"/ayuda": lambda: "ayuda"}


@pytest.mark.parametrize("token, ids, categorias", [
    ("", "", "seguridad"),
    (TOKEN, "", "seguridad"),
    ("", "123", "seguridad"),
    ("malo", "123", "seguridad"),
    (TOKEN, "123,-5", "seguridad"),
    (TOKEN, " , ", "seguridad"),
    (TOKEN, "123", "inventada"),
])
def test_construir_falla_en_cerrado(token, ids, categorias):
    assert telegrama.construir(token, ids, categorias, COMANDOS, 5) is None


def test_construir_y_filtrar_categorias():
    servicio = telegrama.construir(TOKEN, "123", "seguridad", COMANDOS, 5)
    assert servicio is not None
    assert servicio.acepta("seguridad")
    assert not servicio.acepta("recursos")
    assert servicio.avisar("seguridad", "x")
    assert not servicio.avisar("recursos", "y")


# ── Reconfiguración desde la web ──────────────────────────────────────────────
def test_obtenerBot():
    cliente = telegrama.ClienteTelegram(TOKEN, timeout=5,
                                        abrir=AbrirFalso({"ok": True, "result": {"is_bot": True, "username": "mi_bot"}}))
    assert cliente.obtenerBot() == "mi_bot"


def test_obtenerBot_respuesta_que_no_es_un_bot():
    cliente = telegrama.ClienteTelegram(TOKEN, timeout=5, abrir=AbrirFalso({"ok": True, "result": {"is_bot": False}}))
    with pytest.raises(telegrama.ErrorTelegram):
        cliente.obtenerBot()


class ClientePolling(ClienteFalso):
    """getUpdates que, mientras está «en vuelo», ve cómo se detiene el receptor."""

    def __init__(self, alVolver):
        super().__init__()
        self.alVolver = alVolver

    def obtenerActualizaciones(self, offset, espera):
        self.alVolver()
        return [actualizacion(texto="/estado")]


def test_receptor_detenido_no_atiende_el_lote_en_vuelo():
    r = None
    cliente = ClientePolling(lambda: r.detener())
    r = receptor(cliente)
    r.iniciar()
    r.esperarFin(5)
    assert cliente.enviados == []  # el lote llegó tras detenerlo: no se responde con la lista vieja


def test_notificador_vacia_la_cola_al_detenerse():
    cliente = ClienteFalso()
    n = telegrama.Notificador(cliente, frozenset({1}), intervaloEnvio=0)
    n.notificar("uno")
    n.notificar("dos")
    n.detener()  # detenido antes de arrancar: aun así envía lo pendiente
    n.iniciar()
    n._hilo.join(5)
    assert [texto for _, texto in cliente.enviados] == ["uno", "dos"]


def test_servicio_relevo_espera_al_receptor_anterior():
    orden = []

    class ReceptorFalso:
        def __init__(self, nombre):
            self.nombre = nombre

        def iniciar(self):
            orden.append(f"inicia {self.nombre}")

        def esperarFin(self, timeout):
            orden.append(f"espera {self.nombre}")

        def detener(self):
            pass

    notificador = telegrama.Notificador(ClienteFalso(), frozenset({1}))
    anterior = telegrama.ServicioTelegram(notificador, ReceptorFalso("viejo"), frozenset())
    nuevo = telegrama.ServicioTelegram(telegrama.Notificador(ClienteFalso(), frozenset({1})),
                                       ReceptorFalso("nuevo"), frozenset())
    nuevo.iniciar(anterior)
    for _ in range(100):
        if len(orden) == 2:
            break
        threading.Event().wait(0.02)
    nuevo.detener()
    assert orden == ["espera viejo", "inicia nuevo"]


def test_notificarSiempre_ignora_las_categorias():
    servicio = telegrama.construir(TOKEN, "123", "", {"/ayuda": lambda: "ayuda"}, 5)
    assert not servicio.avisar("seguridad", "x")
    assert servicio.notificarSiempre("y")
