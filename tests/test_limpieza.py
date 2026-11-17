"""Tests de limpieza.py con un cliente de Docker falso (sin daemon)."""

import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import docker.errors
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import limpieza  # noqa: E402

AHORA = datetime(2026, 11, 17, tzinfo=timezone.utc)


class FakeImages:
    def __init__(self, imagenes, fallan=()):
        self.imagenes = imagenes
        self.fallan = set(fallan)
        self.borradas = []

    def list(self):
        return self.imagenes

    def remove(self, imageId, force=False):
        if imageId in self.fallan:
            raise docker.errors.APIError("conflict", explanation="image has dependent child images")
        self.borradas.append((imageId, force))


class FakeVolume:
    def __init__(self, nombre, creado, registro):
        self.name = nombre
        self.attrs = {"CreatedAt": creado, "Driver": "local"}
        self._registro = registro

    def remove(self):
        self._registro.append(self.name)


class FakeVolumes:
    def __init__(self, datos):
        self.borrados = []
        self.volumenes = [FakeVolume(n, c, self.borrados) for n, c in datos]

    def list(self):
        return self.volumenes

    def get(self, nombre):
        return next(v for v in self.volumenes if v.name == nombre)


def imagen(sufijo, creada, tags=("app:1",), size=10 * 1024**2):
    return SimpleNamespace(id=f"sha256:{sufijo * 12}", tags=list(tags),
                           attrs={"Created": creada, "Size": size})


def contenedor(imageId, volumenes=()):
    return SimpleNamespace(attrs={
        "Image": imageId,
        "Mounts": [{"Type": "volume", "Name": n} for n in volumenes] + [{"Type": "bind", "Source": "/x"}],
    })


@pytest.fixture
def client():
    imagenes = [
        imagen("a", "2026-01-01T00:00:00.123456789Z", tags=("vieja:1",)),
        imagen("b", "2026-11-10T00:00:00Z", tags=("reciente:1",)),
        imagen("c", "2025-01-01T00:00:00Z", tags=("usada:1",)),
        imagen("d", "2025-01-01T00:00:00Z", tags=("multi:1", "multi:latest")),
        imagen("e", "fecha-rota", tags=()),
    ]
    volumenes = [
        ("viejo", "2026-01-01T10:00:00+02:00"),
        ("reciente", "2026-11-16T00:00:00Z"),
        ("montado", "2025-01-01T00:00:00Z"),
        ("sin_fecha", ""),
    ]
    return SimpleNamespace(
        images=FakeImages(imagenes),
        volumes=FakeVolumes(volumenes),
        containers=SimpleNamespace(list=lambda all=False: [contenedor("sha256:" + "c" * 12, ["montado"])]),
    )


def test_parsearFecha_formatos():
    assert limpieza.parsearFecha("2026-01-01T00:00:00.123456789Z") == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert limpieza.parsearFecha("2026-01-01T10:00:00+02:00") == datetime(2026, 1, 1, 10, tzinfo=timezone.utc)
    assert limpieza.parsearFecha("") is None
    assert limpieza.parsearFecha(None) is None
    assert limpieza.parsearFecha("basura") is None


@pytest.mark.parametrize("dias", [-1, 101])
def test_validarDias_fuera_de_rango(dias):
    with pytest.raises(limpieza.LimpiezaInvalida):
        limpieza.validarDias(dias, 100)


def test_validarDias_limites():
    assert limpieza.validarDias(0, 100) == 0
    assert limpieza.validarDias(100, 100) == 100


def test_candidatos_excluye_usados_recientes_y_sin_fecha(client):
    resultado = limpieza.candidatos(client, dias=30, ahora=AHORA)
    assert [i["id"] for i in resultado["imagenes"]] == ["a" * 12, "d" * 12]
    assert [v["name"] for v in resultado["volumenes"]] == ["viejo"]
    assert resultado["imagenes"][0]["size_mb"] == 10.0
    assert resultado["dias"] == 30


def test_candidatos_cero_dias_incluye_recientes(client):
    resultado = limpieza.candidatos(client, dias=0, ahora=AHORA)
    assert "b" * 12 in [i["id"] for i in resultado["imagenes"]]
    assert {v["name"] for v in resultado["volumenes"]} == {"viejo", "reciente"}


def test_limpiar_solo_imagenes_fuerza_solo_multitag(client):
    resultado = limpieza.limpiar(client, dias=30, tipos={"imagenes"}, ahora=AHORA)
    assert client.images.borradas == [("a" * 12, False), ("d" * 12, True)]
    assert client.volumes.borrados == []
    assert resultado["volumes_deleted"] == []
    assert resultado["freed_mb"] == 20.0
    assert resultado["errors"] == []


def test_limpiar_sigue_tras_un_error(client):
    client.images.fallan = {"a" * 12}
    resultado = limpieza.limpiar(client, dias=30, tipos={"imagenes", "volumenes"}, ahora=AHORA)
    assert resultado["images_deleted"] == ["d" * 12]
    assert resultado["volumes_deleted"] == ["viejo"]
    assert resultado["errors"] == [{"item": "a" * 12, "error": "image has dependent child images"}]
    assert resultado["freed_mb"] == 10.0


@pytest.mark.parametrize("tipos", [set(), {"redes"}, {"imagenes", "contenedores"}])
def test_limpiar_tipos_invalidos(client, tipos):
    with pytest.raises(limpieza.LimpiezaInvalida):
        limpieza.limpiar(client, dias=30, tipos=tipos, ahora=AHORA)
