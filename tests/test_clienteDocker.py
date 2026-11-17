"""Tests de clienteDocker.py sin daemon de Docker."""

import os
import sys

import docker.errors
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import clienteDocker  # noqa: E402


class FakeCliente:
    def __init__(self) -> None:
        self.cerrado = False

    def close(self) -> None:
        self.cerrado = True


def test_reutiliza_el_mismo_cliente():
    creados = []
    proveedor = clienteDocker.ProveedorCliente(lambda: creados.append(FakeCliente()) or creados[-1])
    assert proveedor.obtener() is proveedor.obtener()
    assert len(creados) == 1


def test_un_fallo_no_se_cachea():
    intentos = []

    def fabrica():
        intentos.append(1)
        if len(intentos) == 1:
            raise docker.errors.DockerException("daemon caído")
        return FakeCliente()

    proveedor = clienteDocker.ProveedorCliente(fabrica)
    with pytest.raises(docker.errors.DockerException):
        proveedor.obtener()
    assert isinstance(proveedor.obtener(), FakeCliente)


def test_cerrar_cierra_y_permite_recrear():
    proveedor = clienteDocker.ProveedorCliente(FakeCliente)
    primero = proveedor.obtener()
    proveedor.cerrar()
    assert primero.cerrado
    assert proveedor.obtener() is not primero


def test_cerrar_sin_cliente_no_falla():
    clienteDocker.ProveedorCliente(FakeCliente).cerrar()
