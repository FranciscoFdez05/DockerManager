#!/usr/bin/env sh
# Instala el vigilante de actualizaciones de DockerManager: un temporizador de
# systemd que cada 30 s mira si el botón «Actualizar» del desplegable de la
# versión ha dejado una petición y, si la hay, ejecuta ./docker-update.sh.
#
# Solo hace falta una vez por servidor. Se puede repetir cuantas veces se
# quiera: reescribe las unidades y reinicia el timer, sin tocar nada más.
#
# Uso: sudo ./tools/actualizador/instalar-vigilante.sh
set -e

RUTA="$(cd "$(dirname "$0")/../.." && pwd)"
SERVICIO="dockermanager-actualizador"
UNIDAD_SERVICIO="/etc/systemd/system/${SERVICIO}.service"
UNIDAD_TIMER="/etc/systemd/system/${SERVICIO}.timer"
SCRIPT="$RUTA/tools/actualizador/dockermanager-actualizador.sh"

aviso()  { printf '\n\033[33m%s\033[0m\n' "$*"; }
error()  { printf '\n\033[31m%s\033[0m\n' "$*" >&2; }
paso()   { printf '\n\033[36m── %s\033[0m\n' "$*"; }

if [ "$(id -u)" -ne 0 ]; then
    error "Hace falta sudo: sudo $0"
    exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
    error "No hay systemd (systemctl) en este host: el vigilante no se puede registrar."
    echo "       Sin él, el botón «Actualizar» deja la petición y nadie la recoge." >&2
    exit 1
fi

if [ ! -f "$SCRIPT" ]; then
    error "No se encuentra $SCRIPT"
    exit 1
fi
chmod +x "$SCRIPT" "$RUTA/docker-update.sh"

# El vigilante corre como el dueño del clon, nunca como root: docker-update.sh
# se niega a ejecutarse con sudo, y el git pull dejaría ficheros de root.
USUARIO="${SUDO_USER:-}"
[ -n "$USUARIO" ] || USUARIO="$(stat -c '%U' "$RUTA" 2>/dev/null || true)"
if [ -z "$USUARIO" ] || [ "$USUARIO" = "root" ]; then
    error "No se pudo saber qué usuario es el dueño del clon (o es root)."
    echo "       Ejecútalo con sudo desde tu usuario normal, no desde una sesión de root." >&2
    exit 1
fi
GRUPO="$(id -gn "$USUARIO")"

if ! id -nG "$USUARIO" | tr ' ' '\n' | grep -qx docker; then
    aviso "$USUARIO no está en el grupo docker: docker-update.sh no podrá hablar con Docker.
Arréglalo con:  sudo usermod -aG docker $USUARIO"
fi

# El buzón lo monta docker-compose.yml en el contenedor. Si Docker lo creó antes
# que nadie, es de root y el vigilante no podría escribir en él.
mkdir -p "$RUTA/data/actualizacion"
chown "$USUARIO:$GRUPO" "$RUTA/data" "$RUTA/data/actualizacion"

paso "Registrando $SERVICIO como $USUARIO en $RUTA"

tee "$UNIDAD_SERVICIO" >/dev/null <<FIN
# Generado por tools/actualizador/instalar-vigilante.sh de DockerManager.
# Se sobrescribe cada vez que se vuelve a ejecutar ese script.
[Unit]
Description=Vigilante de actualizaciones de DockerManager
After=docker.service

[Service]
Type=oneshot
User=${USUARIO}
Group=${GRUPO}
WorkingDirectory=${RUTA}
ExecStart=${SCRIPT}
FIN

tee "$UNIDAD_TIMER" >/dev/null <<FIN
# Generado por tools/actualizador/instalar-vigilante.sh de DockerManager.
[Unit]
Description=Comprueba cada 30 s si se ha pedido actualizar DockerManager

[Timer]
OnBootSec=30s
OnUnitActiveSec=30s
AccuracySec=5s

[Install]
WantedBy=timers.target
FIN

systemctl daemon-reload
systemctl enable --quiet --now "${SERVICIO}.timer"

cat <<FIN

Vigilante instalado: cada 30 s mira si se ha pulsado «Actualizar» en el
desplegable de la versión de DockerManager.

Gestión:
  sudo systemctl status ${SERVICIO}.timer    última y próxima pasada
  sudo journalctl -u ${SERVICIO} -f          salida de cada actualización
  sudo systemctl disable --now ${SERVICIO}.timer   desactivarlo
FIN
