"""Tests de servidor.py con un psutil falso (no dependen del hardware)."""

import os
import sys
from collections import namedtuple
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import servidor  # noqa: E402

CpuTimes = namedtuple("CpuTimes", "user nice system idle iowait irq softirq steal guest guest_nice")
Temp = namedtuple("Temp", "label current high critical")
Fan = namedtuple("Fan", "label current")
DiskIo = namedtuple("DiskIo", "read_bytes write_bytes")


def tiempos(user: float, idle: float, iowait: float = 0.0, guest: float = 0.0) -> CpuTimes:
    return CpuTimes(user, 0.0, 0.0, idle, iowait, 0.0, 0.0, 0.0, guest, 0.0)


class FakePsutil:
    """Imita la parte de psutil que usa LectorMetricas; cada test ajusta sus campos."""

    def __init__(self) -> None:
        self.total = tiempos(0, 0)
        self.nucleos = [tiempos(0, 0), tiempos(0, 0)]
        self.temperaturas = {}
        self.ventiladores = {}
        self.io = DiskIo(0, 0)
        self.frecuencia = SimpleNamespace(current=2400.4, min=800.0, max=3600.0)
        self.swap = SimpleNamespace(total=0, used=0, percent=0.0)
        # Atributos de instancia y no métodos: así un test puede borrarlos para
        # simular las plataformas donde psutil no los define.
        self.sensors_temperatures = lambda: self.temperaturas
        self.sensors_fans = lambda: self.ventiladores

    def cpu_times(self, percpu: bool = False):
        return self.nucleos if percpu else self.total

    def cpu_count(self, logical: bool = True) -> int:
        return len(self.nucleos) if logical else 1

    def cpu_freq(self):
        return self.frecuencia

    def getloadavg(self):
        return (0.5, 0.25, 0.125)

    def virtual_memory(self):
        return SimpleNamespace(total=1000, used=400, available=600, percent=40.0, cached=100, buffers=50)

    def swap_memory(self):
        return self.swap

    def disk_usage(self, ruta: str):
        return SimpleNamespace(total=100, used=25, free=75, percent=25.0)

    def disk_io_counters(self):
        return self.io

    def boot_time(self) -> float:
        return 1_000.0


class Reloj:
    def __init__(self, valor: float = 0.0) -> None:
        self.valor = valor

    def __call__(self) -> float:
        return self.valor


@pytest.fixture
def fuente() -> FakePsutil:
    return FakePsutil()


@pytest.fixture
def reloj() -> Reloj:
    return Reloj(100.0)


@pytest.fixture
def lector(fuente: FakePsutil, reloj: Reloj) -> servidor.LectorMetricas:
    return servidor.LectorMetricas(
        fuente=fuente, relojMonotono=reloj, relojPared=lambda: 4_600.0,
        rutaDisco="/", tempAviso=70, tempCritica=85,
    )


# ── porcentaje_cpu ────────────────────────────────────────────────────────────
def test_porcentaje_cpu_cuenta_iowait_como_ocioso():
    assert servidor.porcentaje_cpu(tiempos(0, 0), tiempos(30, 50, iowait=20)) == 30.0


def test_porcentaje_cpu_ignora_guest_ya_incluido_en_user():
    assert servidor.porcentaje_cpu(tiempos(0, 0), tiempos(50, 50, guest=40)) == 50.0


def test_porcentaje_cpu_sin_delta_devuelve_none():
    assert servidor.porcentaje_cpu(tiempos(10, 10), tiempos(10, 10)) is None


# ── CPU ───────────────────────────────────────────────────────────────────────
def test_cpu_total_y_por_nucleo(fuente, lector):
    fuente.total = tiempos(25, 75)
    fuente.nucleos = [tiempos(10, 40), tiempos(40, 10)]

    cpu = lector.leer()["cpu"]

    assert cpu["pct"] == 25.0
    assert cpu["per_core"] == [20.0, 80.0]
    assert cpu["cores_logical"] == 2
    assert cpu["freq_mhz"] == 2400
    assert cpu["freq_max_mhz"] == 3600
    assert cpu["load_avg"] == [0.5, 0.25, 0.12]


def test_porcentaje_cpu_total_no_bloquea_y_usa_el_delta(fuente, lector):
    fuente.total = tiempos(30, 70)
    assert lector.porcentaje_cpu_total() == 30.0
    # Sin tiempo transcurrido no hay delta: repite el último valor, no 0.
    assert lector.porcentaje_cpu_total() == 30.0
    fuente.total = tiempos(130, 70)
    assert lector.porcentaje_cpu_total() == 100.0


def test_porcentaje_cpu_total_comparte_referencia_con_leer(fuente, lector):
    fuente.total = tiempos(50, 50)
    lector.porcentaje_cpu_total()
    fuente.total = tiempos(60, 140)
    assert lector.leer()["cpu"]["pct"] == 10.0


def test_cpu_sin_delta_repite_el_ultimo_valor(fuente, lector):
    fuente.total = tiempos(25, 75)
    fuente.nucleos = [tiempos(10, 40), tiempos(40, 10)]
    lector.leer()

    cpu = lector.leer()["cpu"]

    assert cpu["pct"] == 25.0
    assert cpu["per_core"] == [20.0, 80.0]


def test_cambio_en_numero_de_nucleos_reinicia_referencia(fuente, lector):
    fuente.nucleos = [tiempos(5, 5), tiempos(5, 5), tiempos(5, 5)]

    assert lector.leer()["cpu"]["per_core"] == [0.0, 0.0, 0.0]


def test_sin_frecuencia_de_cpu(fuente, lector):
    fuente.frecuencia = None

    cpu = lector.leer()["cpu"]

    assert cpu["freq_mhz"] is None and cpu["freq_max_mhz"] is None


def test_frecuencia_que_lanza_error(fuente, lector):
    def falla():
        raise FileNotFoundError("/sys/devices/system/cpu/cpufreq")

    fuente.cpu_freq = falla

    assert lector.leer()["cpu"]["freq_mhz"] is None


# ── Disco ─────────────────────────────────────────────────────────────────────
def test_velocidad_disco_primera_lectura_sin_datos(lector):
    disco = lector.leer()["disk"]

    assert disco["read_bps"] is None and disco["write_bps"] is None
    assert disco["pct"] == 25.0


def test_velocidad_disco_entre_dos_lecturas(fuente, reloj, lector):
    lector.leer()
    fuente.io = DiskIo(4_000, 2_000)
    reloj.valor += 2

    disco = lector.leer()["disk"]

    assert disco["read_bps"] == 2_000
    assert disco["write_bps"] == 1_000


def test_contadores_de_disco_reiniciados_no_dan_velocidad_negativa(fuente, reloj, lector):
    fuente.io = DiskIo(5_000, 5_000)
    lector.leer()
    fuente.io = DiskIo(10, 10)
    reloj.valor += 1

    disco = lector.leer()["disk"]

    assert disco["read_bps"] is None and disco["write_bps"] is None


def test_sin_contadores_de_disco(fuente, lector):
    fuente.io = None

    assert lector.leer()["disk"]["read_bps"] is None


# ── Temperaturas ──────────────────────────────────────────────────────────────
def test_temperatura_cpu_es_la_mas_alta_del_sensor_de_cpu(fuente, lector):
    fuente.temperaturas = {
        "nvme": [Temp("Composite", 90.0, 80.0, 85.0)],
        "coretemp": [Temp("Package id 0", 55.0, 80.0, 100.0), Temp("Core 0", 61.24, 80.0, 100.0)],
    }

    metricas = lector.leer()

    assert metricas["cpu_temp"] == 61.2
    assert len(metricas["temperatures"]) == 3


def test_umbrales_absurdos_del_sensor_usan_los_configurados(fuente, lector):
    fuente.temperaturas = {"acpitz": [Temp("", 40.0, None, 0.0)]}

    temperatura = lector.leer()["temperatures"][0]

    assert temperatura["high"] == 70
    assert temperatura["critical"] == 85
    assert temperatura["label"] == "acpitz 1"


def test_sin_sensor_de_cpu_conocido(fuente, lector):
    fuente.temperaturas = {"nvme": [Temp("Composite", 40.0, 80.0, 85.0)]}

    assert lector.leer()["cpu_temp"] is None


def test_plataforma_sin_sensores(fuente, lector):
    # psutil no define sensors_temperatures ni sensors_fans en Windows ni macOS.
    del fuente.sensors_temperatures
    del fuente.sensors_fans

    metricas = lector.leer()

    assert metricas["temperatures"] == [] and metricas["fans"] == []
    assert metricas["cpu_temp"] is None


def test_error_leyendo_sensores_no_rompe_la_respuesta(fuente, lector):
    def falla():
        raise PermissionError("/sys/class/hwmon")

    fuente.sensors_temperatures = falla

    assert lector.temperatura_cpu() is None


def test_ventiladores(fuente, lector):
    fuente.ventiladores = {"nct6775": [Fan("", 1200), Fan("cpu_fan", 900)]}

    assert lector.leer()["fans"] == [
        {"sensor": "nct6775", "label": "nct6775 1", "rpm": 1200},
        {"sensor": "nct6775", "label": "cpu_fan", "rpm": 900},
    ]


# ── Memoria y arranque ────────────────────────────────────────────────────────
def test_memoria_swap_y_uptime(fuente, lector):
    fuente.swap = SimpleNamespace(total=200, used=50, percent=25.0)

    metricas = lector.leer()

    assert metricas["memory"] == {
        "total": 1000, "used": 400, "available": 600, "pct": 40.0, "cached": 100, "buffers": 50,
    }
    assert metricas["swap"] == {"total": 200, "used": 50, "pct": 25.0}
    assert metricas["uptime_seconds"] == 3_600
    assert metricas["boot_time"].startswith("1970-01-01T00:16:40")
    assert metricas["thresholds"] == {"temp_warn": 70, "temp_crit": 85}


def test_memoria_sin_campos_de_linux(fuente, lector):
    fuente.virtual_memory = lambda: SimpleNamespace(total=10, used=5, available=5, percent=50.0)

    memoria = lector.leer()["memory"]

    assert memoria["cached"] is None and memoria["buffers"] is None
