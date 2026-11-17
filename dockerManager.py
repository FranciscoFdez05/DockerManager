#!/usr/bin/env python3
# Script para lanzar comandos Docker comunes desde un menu interactivo
# Uso: python3 dockerManager.py

import shlex
import subprocess
import sys
import os


def clearScreen():
    # Limpia la pantalla de la terminal, compatible con Linux/Mac (clear) y Windows (cls)
    os.system("cls" if os.name == "nt" else "clear")


def runCommand(commandStr):
    # Ejecuta un comando de shell y muestra la salida en tiempo real
    # Se trocea con shlex y se ejecuta sin shell=True: ningun texto se
    # interpreta como codigo de shell. Las sustituciones $(...) que antes
    # resolvia la shell las hace dockerIds().
    print(f"\n>>> Ejecutando: {commandStr}\n")
    resultCode = subprocess.run(shlex.split(commandStr))
    if resultCode.returncode != 0:
        print(f"\n[!] El comando termino con codigo de salida {resultCode.returncode}")
    else:
        print("\n[OK] Comando completado")


def dockerIds(*psArgs: str) -> list[str]:
    """Devuelve los IDs que lista `docker ps -q` con los argumentos dados.

    Args:
        psArgs: Argumentos extra para `docker ps -q` (por ejemplo "-a").

    Returns:
        Lista de IDs; vacia si no hay contenedores o si docker falla.
    """
    resultado = subprocess.run(["docker", "ps", "-q", *psArgs], capture_output=True, text=True)
    if resultado.returncode != 0:
        print(f"\n[!] docker ps fallo: {resultado.stderr.strip()}")
        return []
    return resultado.stdout.split()


def runOnContainers(action: str, *psArgs: str) -> None:
    """Ejecuta `docker <action>` sobre los contenedores de `docker ps -q <psArgs>`.

    Equivale a `docker <action> $(docker ps -q <psArgs>)` sin pasar por la shell.
    Sin contenedores no se ejecuta nada (docker fallaria por falta de argumentos).

    Args:
        action: Subcomando de docker, por ejemplo "stop" o "rm".
        psArgs: Argumentos extra para `docker ps -q`.
    """
    ids = dockerIds(*psArgs)
    psCommand = " ".join(["docker", "ps", "-q", *psArgs])
    print(f"\n>>> Ejecutando: docker {action} $({psCommand})\n")
    if not ids:
        print("[OK] No hay contenedores a los que aplicarlo")
        return
    resultCode = subprocess.run(["docker", action, *ids])
    if resultCode.returncode != 0:
        print(f"\n[!] El comando termino con codigo de salida {resultCode.returncode}")
    else:
        print("\n[OK] Comando completado")


def confirmAction(promptText):
    # Pide confirmacion antes de ejecutar acciones destructivas
    respuesta = input(f"{promptText} (s/N): ").strip().lower()
    return respuesta == "s"


def stopAllContainers():
    runOnContainers("stop")


def removeAllContainers():
    if confirmAction("Esto eliminara TODOS los contenedores (parados y corriendo si se fuerzan). Continuar?"):
        runOnContainers("rm", "-a")


def pruneImages():
    if confirmAction("Esto eliminara TODAS las imagenes no usadas por ningun contenedor. Continuar?"):
        runCommand("docker image prune -a -f")


def pruneVolumes():
    if confirmAction("Esto eliminara volumenes no usados. Puede borrar datos si no estan en uso. Continuar?"):
        runCommand("docker volume prune -f")


def pruneNetworks():
    runCommand("docker network prune -f")


def pruneSystemAll():
    if confirmAction("ATENCION: esto borra contenedores parados, imagenes no usadas, redes y VOLUMENES. Es IRREVERSIBLE. Continuar?"):
        runCommand("docker system prune -a -f --volumes")


def cleanAll():
    # Ejecuta la secuencia completa de limpieza, en orden logico
    print("\n=== LIMPIEZA COMPLETA DE DOCKER ===")
    if not confirmAction("Esto va a parar y eliminar TODO (contenedores, imagenes, volumenes, redes). Seguro?"):
        print("Cancelado.")
        return

    stopAllContainers()
    runOnContainers("rm", "-a")
    runCommand("docker image prune -a -f")
    runCommand("docker volume prune -f")
    runCommand("docker network prune -f")
    runCommand("docker system prune -a -f --volumes")
    print("\n=== LIMPIEZA COMPLETA FINALIZADA ===")


def showStatus():
    runCommand("docker ps -a")
    runCommand("docker images")


def showMenu():
    menuText = """
========================================
        DOCKER QUICK MANAGER
========================================
 1) Ver contenedores e imagenes (docker ps -a / docker images)
 2) Parar todos los contenedores corriendo (docker stop $(docker ps -q))
 3) Eliminar todos los contenedores (docker rm $(docker ps -aq))
 4) Eliminar imagenes no usadas (image prune -a -f)
 5) Eliminar volumenes no usados (volume prune -f)
 6) Eliminar redes no usadas (network prune -f)
 7) Limpieza total del sistema (system prune -a -f --volumes)
 8) LIMPIAR TODO (ejecuta 2-7 en orden, con confirmacion)
 0) Salir
========================================
"""
    print(menuText)


def main():
    opciones = {
        "1": showStatus,
        "2": stopAllContainers,
        "3": removeAllContainers,
        "4": pruneImages,
        "5": pruneVolumes,
        "6": pruneNetworks,
        "7": pruneSystemAll,
        "8": cleanAll,
    }

    while True:
        clearScreen()
        showMenu()
        eleccion = input("Elige una opcion: ").strip()

        if eleccion == "0":
            print("Saliendo.")
            sys.exit(0)

        accion = opciones.get(eleccion)
        if accion:
            accion()
        else:
            print("Opcion no valida.")

        input("\nPulsa Enter para continuar...")


if __name__ == "__main__":
    main()