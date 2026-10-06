#!/usr/bin/env sh
# Actualiza la instalación en marcha y la deja comprobada.
#
# La actualización a mano era `git pull && ./docker-up.sh`, con cosas que solo
# se descubrían tarde:
#
#   1. Si hay cambios locales en ficheros versionados (típicamente `config.ini`,
#      que se edita en el servidor para cambiar el puerto), el pull da un
#      conflicto en mitad de la actualización. Aquí se comprueba ANTES de tocar
#      nada y se explica cómo salir.
#   2. Nada verificaba que la versión nueva arrancase. El contenedor se quedaba
#      arriba con `restart: unless-stopped` reiniciándose en bucle, y te
#      enterabas al abrir la web. Aquí se espera a que `/api/health` responda,
#      que es una comprobación de verdad: consulta el daemon de Docker.
#   3. `docker compose build` sin etiqueta deja solo la imagen nueva, así que
#      volver atrás era reconstruir desde el código anterior. Ahora cada versión
#      queda etiquetada (dockermanager:X.Y.Z) y la vuelta atrás es inmediata.
#   4. Este script se actualiza a sí mismo. El `git pull` reemplaza el fichero
#      que el intérprete está leyendo, y `sh` guarda un desplazamiento dentro de
#      él: si el fichero nuevo tiene otro tamaño, las órdenes que quedan por leer
#      se descolocan y la actualización se queda a medias por un error de
#      sintaxis absurdo. Por eso lo primero que hace es ejecutarse desde una
#      copia (ver abajo).
#
# Si el arranque no responde, se vuelve solo a la imagen anterior. Los datos
# (usuario y contraseña) viven en el volumen dockermanager-data y esta
# actualización no los toca.
#
# No necesita python en el host: solo sh, sed, awk, git y docker.
#
# Uso: ./docker-update.sh [--sin-pull]
set -e

# El directorio del proyecto se fija antes que nada: la copia de la que se
# reejecuta vive en /tmp, así que allí `dirname "$0"` ya no sirve.
DM_PROYECTO="${DM_PROYECTO:-$(cd "$(dirname "$0")" && pwd)}"
export DM_PROYECTO
cd "$DM_PROYECTO"

# Reejecutarse desde una copia: lo que corre es una foto del script, y el pull
# de más abajo puede reemplazar el original sin descolocar esta ejecución. Si no
# se pudiera crear la copia se sigue igualmente: es una protección, no un
# requisito.
if [ -z "$DM_UPDATE_COPIA" ]; then
    copia=$(mktemp "${TMPDIR:-/tmp}/docker-update.XXXXXX" 2>/dev/null) || copia=""
    if [ -n "$copia" ] && cp "$0" "$copia" 2>/dev/null; then
        DM_UPDATE_COPIA="$copia"
        export DM_UPDATE_COPIA
        codigo=0
        sh "$copia" "$@" || codigo=$?
        rm -f "$copia"
        exit "$codigo"
    fi
fi

SIN_PULL=0
[ "$1" = "--sin-pull" ] && SIN_PULL=1

# Con sudo, el git pull deja los ficheros como root. Docker no necesita sudo
# cuando el usuario pertenece al grupo docker.
if [ "$(id -u)" -eq 0 ]; then
    echo "ERROR: no ejecutes docker-update.sh con sudo." >&2
    echo "       Ejecútalo como tu usuario normal: ./docker-update.sh" >&2
    exit 1
fi

SERVICIO="dockermanager"
IMAGEN="dockermanager"
ESPERA_SALUD=60   # segundos que se le dan a la versión nueva para responder

version_del_codigo() {
    sed -n 's/^__version__ = "\(.*\)"/\1/p' version.py 2>/dev/null | head -n 1
}

aviso()  { printf '\n\033[33m%s\033[0m\n' "$*"; }
error()  { printf '\n\033[31m%s\033[0m\n' "$*" >&2; }
paso()   { printf '\n\033[36m── %s\033[0m\n' "$*"; }

# ── 1. Comprobaciones previas ─────────────────────────────────────────────────
paso "Comprobando el estado local"

if [ ! -f .env ]; then
    error "No hay .env. Esto es una instalación nueva: usa ./docker-up.sh."
    exit 1
fi

# El chmod +x que se hace en el servidor cambia el modo del fichero (100644 →
# 100755) y git lo cuenta como modificación: aparecía como «cambio local» y
# bloqueaba cada actualización. Se le dice a git que en este clon ignore los
# permisos; solo afecta a esta copia del repositorio.
git config core.fileMode false 2>/dev/null || true

# Solo importan los ficheros versionados modificados: son los que chocarían con
# el pull. Los no versionados (.env, __pycache__...) no molestan.
if [ "$SIN_PULL" -eq 0 ]; then
    cambios=$(git status --porcelain --untracked-files=no 2>/dev/null || true)
    if [ -n "$cambios" ]; then
        error "Hay cambios locales en ficheros versionados; el pull podría chocar con ellos:"
        printf '%s\n' "$cambios" >&2
        cat <<'FIN'

  Lo habitual es haber editado config.ini (por ejemplo, el puerto). Para
  conservar esos cambios durante la actualización:

      git stash && ./docker-update.sh && git stash pop

  Para descartarlos:  git checkout -- <fichero> && ./docker-update.sh
  Para ver qué cambió: git diff

FIN
        exit 1
    fi
fi

VERSION_ANTERIOR=$(version_del_codigo)
[ -n "$VERSION_ANTERIOR" ] || VERSION_ANTERIOR="desconocida"
echo "Versión instalada: $VERSION_ANTERIOR"

# ── 2. Traer los cambios ──────────────────────────────────────────────────────
if [ "$SIN_PULL" -eq 0 ]; then
    paso "Descargando la versión nueva"
    git pull --ff-only
fi

VERSION_NUEVA=$(version_del_codigo)
[ -n "$VERSION_NUEVA" ] || VERSION_NUEVA="desconocida"

if [ "$VERSION_NUEVA" = "$VERSION_ANTERIOR" ]; then
    aviso "Ya estabas en la $VERSION_NUEVA. Se reconstruye igualmente."
else
    echo "Actualizando: $VERSION_ANTERIOR → $VERSION_NUEVA"
    if [ -f CHANGELOG.md ]; then
        paso "Novedades de la $VERSION_NUEVA"
        awk '/^## \[/{n++} n==1{print} n==2{exit}' CHANGELOG.md
    fi
fi

# ── 3. Puerto ─────────────────────────────────────────────────────────────────
# Misma fuente que docker-up.sh: [server] port de config.ini.
PORT=$(awk -F= '
    /^[[:space:]]*[;#]/  { next }
    /^[[:space:]]*\[/    { section=$0; gsub(/[][[:space:]]/, "", section); next }
    section == "server" && $1 ~ /^[[:space:]]*port[[:space:]]*$/ {
        gsub(/[[:space:]]/, "", $2); print $2; exit
    }
' config.ini)
case "$PORT" in
    ''|*[!0-9]*)
        aviso "No pude leer [server] port de config.ini, uso 3000."
        PORT=3000
        ;;
esac
export PORT

# Si falta la SECRET_KEY (un .env antiguo), se avisa: sin ella, cada reinicio
# cierra las sesiones abiertas. docker-up.sh sabe generarla.
if [ -z "$(awk -F= '/^SECRET_KEY=/{print $2; exit}' .env)" ]; then
    aviso "El .env no tiene SECRET_KEY. Ejecuta ./docker-up.sh una vez para generarla."
fi

# La etiqueta de la imagen sale de la versión: dockermanager:1.0.0
if [ "$VERSION_NUEVA" != "desconocida" ]; then
    DOCKERMANAGER_VERSION="$VERSION_NUEVA"
else
    DOCKERMANAGER_VERSION="latest"
fi
export DOCKERMANAGER_VERSION

# ── 4. Construir y levantar ───────────────────────────────────────────────────
paso "Construyendo la imagen $DOCKERMANAGER_VERSION"
docker compose build

paso "Levantando"
docker compose up -d

# ── 5. Comprobar que arranca de verdad ────────────────────────────────────────
# /api/health devuelve 503 si la app no consigue hablar con Docker, así que
# esperar aquí distingue «el contenedor está arriba» de «la aplicación funciona».
# Se consulta desde dentro del contenedor: no hace falta curl en el host ni
# depende del puerto publicado ni del firewall.
paso "Esperando a que responda (hasta ${ESPERA_SALUD}s)"

salud() {
    docker compose exec -T "$SERVICIO" python -c \
"import os,sys,urllib.request
r = urllib.request.urlopen('http://localhost:%s/api/health' % os.environ.get('PORT', '3000'), timeout=3)
sys.stdout.write(r.read().decode())" 2>/dev/null
}

sano=0
i=0
while [ "$i" -lt "$ESPERA_SALUD" ]; do
    if salud >/dev/null 2>&1; then
        sano=1
        break
    fi
    i=$((i + 1))
    printf '.'
    sleep 1
done
printf '\n'

if [ "$sano" -eq 1 ]; then
    LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
    [ -n "$LAN_IP" ] || LAN_IP=$(ip route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')
    [ -n "$LAN_IP" ] || LAN_IP="IP_DEL_SERVIDOR"
    paso "Actualización correcta"
    salud || true
    printf '\n\nDockerManager %s en marcha en el puerto %s.\n' "$VERSION_NUEVA" "$PORT"
    printf '  Local:        http://localhost:%s\n  Desde la LAN: http://%s:%s\n' "$PORT" "$LAN_IP" "$PORT"
    exit 0
fi

# ── 6. Vuelta atrás ───────────────────────────────────────────────────────────
error "La versión $VERSION_NUEVA no responde tras ${ESPERA_SALUD}s. Volviendo atrás."

echo
echo "Últimas líneas del log:"
docker compose logs --tail 40 "$SERVICIO" 2>&1 || true

if [ "$VERSION_ANTERIOR" != "desconocida" ] \
   && [ "$VERSION_ANTERIOR" != "$VERSION_NUEVA" ] \
   && docker image inspect "${IMAGEN}:${VERSION_ANTERIOR}" >/dev/null 2>&1; then
    paso "Levantando de nuevo la $VERSION_ANTERIOR"
    DOCKERMANAGER_VERSION="$VERSION_ANTERIOR" docker compose up -d --no-build
    aviso "Se ha vuelto a la $VERSION_ANTERIOR. El código del repositorio SÍ está actualizado:
para dejarlo también como estaba, ejecuta  git checkout v${VERSION_ANTERIOR}"
else
    error "No hay una imagen anterior etiquetada distinta de la nueva; no se puede volver sola."
fi

cat <<'FIN'

  Los datos (usuario y contraseña) están en el volumen dockermanager-data y esta
  actualización no los modifica, así que volver a una imagen anterior es seguro.

FIN
exit 1
