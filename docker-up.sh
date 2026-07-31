#!/usr/bin/env sh
# ============================================================================
# DockerManager - arranque en el servidor
#
#   ./docker-up.sh [args extra para docker compose up]
#
# Hace tres cosas:
#   1. Crea el .env si no existe y genera una SECRET_KEY aleatoria.
#   2. Lee el puerto de config.ini ([server] port) -> unica fuente de verdad.
#   3. Levanta el stack con docker compose, publicado en 0.0.0.0 para que
#      cualquier equipo de la LAN pueda entrar por http://IP_DEL_SERVIDOR:PUERTO
#
# No necesita python3 en el host: solo sh, awk y docker.
# ============================================================================
set -e
cd "$(dirname "$0")"

# ── 1. SECRET_KEY / .env ────────────────────────────────────────────────────
gen_secret() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 32
    elif [ -r /dev/urandom ]; then
        od -An -tx1 -N32 /dev/urandom | tr -d ' \n'
    else
        echo "ERROR: no encuentro openssl ni /dev/urandom para generar la SECRET_KEY." >&2
        exit 1
    fi
}

if [ ! -f .env ]; then
    [ -f .env.example ] || { echo "ERROR: falta .env.example" >&2; exit 1; }
    cp .env.example .env
    echo "Creado .env a partir de .env.example."
fi

# Rellena SECRET_KEY si esta vacia o ausente (cubre tambien un .env viejo
# creado a mano). Nunca sobreescribe una clave existente: cambiarla
# invalidaria las sesiones abiertas.
CURRENT_KEY=$(awk -F= '/^SECRET_KEY=/{print $2; exit}' .env)
if [ -z "$CURRENT_KEY" ]; then
    NEW_KEY=$(gen_secret)
    if grep -q '^SECRET_KEY=' .env; then
        # Delimitador | : la clave es hex, nunca lo contiene
        sed "s|^SECRET_KEY=.*|SECRET_KEY=$NEW_KEY|" .env > .env.tmp && mv .env.tmp .env
    else
        printf 'SECRET_KEY=%s\n' "$NEW_KEY" >> .env
    fi
    echo "SECRET_KEY generada automaticamente."
fi

# ── 2. Puerto desde config.ini ──────────────────────────────────────────────
# Recorre el .ini quedandose con [server] port. Ignora comentarios (; o #).
PORT=$(awk -F= '
    /^[[:space:]]*[;#]/  { next }
    /^[[:space:]]*\[/    { section=$0; gsub(/[][[:space:]]/, "", section); next }
    section == "server" && $1 ~ /^[[:space:]]*port[[:space:]]*$/ {
        gsub(/[[:space:]]/, "", $2); print $2; exit
    }
' config.ini)

case "$PORT" in
    ''|*[!0-9]*)
        echo "AVISO: no pude leer [server] port de config.ini, uso 3000." >&2
        PORT=3000
        ;;
esac

# Se exporta para docker-compose.yml: publica el puerto y se lo pasa a la app.
export PORT

# ── 3. Arranque ─────────────────────────────────────────────────────────────
docker compose up -d --build "$@"

# ── 4. Como entrar desde la LAN ─────────────────────────────────────────────
LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
[ -n "$LAN_IP" ] || LAN_IP=$(ip route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')
[ -n "$LAN_IP" ] || LAN_IP="IP_DEL_SERVIDOR"

echo
echo "DockerManager levantado en el puerto $PORT (config.ini)."
echo "  Local:        http://localhost:$PORT"
echo "  Desde la LAN: http://$LAN_IP:$PORT"
echo
echo "Si otro equipo de la red no conecta, abre el puerto en el firewall del servidor."
