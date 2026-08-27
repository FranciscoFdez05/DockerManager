#!/usr/bin/env python3
# Script para lanzar comandos Docker comunes desde un menu interactivo
# Uso: python3 dockerManager.py

import subprocess
import sys
import os


def clearScreen():
    # Limpia la pantalla de la terminal, compatible con Linux/Mac (clear) y Windows (cls)
    os.system("cls" if os.name == "nt" else "clear")


def runCommand(commandStr):
    # Ejecuta un comando de shell y muestra la salida en tiempo real
    print(f"\n>>> Ejecutando: {commandStr}\n")
    resultCode = subprocess.run(commandStr, shell=True)
    if resultCode.returncode != 0:
        print(f"\n[!] El comando termino con codigo de salida {resultCode.returncode}")
    else:
        print("\n[OK] Comando completado")


def confirmAction(promptText):
    # Pide confirmacion antes de ejecutar acciones destructivas
    respuesta = input(f"{promptText} (s/N): ").strip().lower()
    return respuesta == "s"


def stopAllContainers():
    runCommand("docker stop $(docker ps -q)")


def removeAllContainers():
    if confirmAction("Esto eliminara TODOS los contenedores (parados y corriendo si se fuerzan). Continuar?"):
        runCommand("docker rm $(docker ps -aq)")


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
    runCommand("docker rm $(docker ps -aq)")
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