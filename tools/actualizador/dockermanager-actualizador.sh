#!/usr/bin/env sh
# Vigilante de actualizaciones de DockerManager.
#
# Recoge la petición que deja el botón «Actualizar» del desplegable de la
# versión y ejecuta ./docker-update.sh, que ya hace pull, build, comprobación de
# salud y vuelta atrás. Lo lanza cada 30 s el temporizador
# dockermanager-actualizador.timer (ver instalar-vigilante.sh).
#
# La app no puede hacerlo ella misma: docker-update.sh recrea su contenedor y se
# mataría a mitad de la operación. Este script corre en el host, fuera del
# contenedor, y sobrevive al reinicio.
#
# Como docker-update.sh, no necesita python en el host: solo sh, sed, awk y tr.
#
# Uso: ./tools/actualizador/dockermanager-actualizador.sh  (también a mano)
set -e
cd "$(dirname "$0")/../.."   # tools/actualizador/ -> raíz del repo

BUZON="data/actualizacion"
SOLICITADA="$BUZON/solicitada.json"
CERROJO="$BUZON/.cerrojo"

# Texto -> cadena JSON: sin colores ANSI ni caracteres de control, con las
# comillas y barras escapadas y los saltos de línea como \n.
json_texto() {
    printf '%s' "$1" \
        | sed 's/\x1b\[[0-9;]*[A-Za-z]//g' \
        | tr -d '\000-\010\013-\037' \
        | sed 's/\\/\\\\/g; s/"/\\"/g; s/\t/\\t/g' \
        | awk 'BEGIN { ORS = "" } NR > 1 { print "\\n" } { print }'
}

# estado.json se escribe en un temporal y se renombra: la app nunca lee un JSON
# a medio escribir.
escribir_estado() {
    temporal="$BUZON/estado.json.tmp"
    printf '{"estado":"%s","codigo":%s,"solicitadaEn":"%s","actualizadoEn":"%s","detalle":"%s"}\n' \
        "$1" "$2" "$(json_texto "$SOLICITADA_EN")" "$(date +%Y-%m-%dT%H:%M:%S)" "$(json_texto "$3")" > "$temporal"
    mv -f "$temporal" "$BUZON/estado.json"
}

# Todo va dentro de una función y la llamada en la última línea: docker-update.sh
# hace git pull, que puede reescribir este fichero mientras sh lo lee. Así sh lo
# ha leído entero antes de empezar y no se descoloca.
main() {
    mkdir -p "$BUZON"
    # Se toca en cada pasada, haya o no petición: así la app sabe que el vigilante
    # está instalado y vivo, en vez de dejar el botón esperando a nadie.
    touch "$BUZON/latido"

    # Lo normal: nadie ha pulsado nada.
    [ -f "$SOLICITADA" ] || return 0

    # Cerrojo atómico con mkdir: si una actualización larga se solapa con el
    # siguiente disparo del timer, esta pasada se retira sin tocar nada.
    mkdir "$CERROJO" 2>/dev/null || return 0
    trap 'rmdir "$CERROJO" 2>/dev/null' EXIT

    SOLICITADA_EN=$(sed -n 's/.*"solicitadaEn"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$SOLICITADA" | head -n 1)

    # Se consume ANTES de actualizar: si se va la luz a mitad, al volver no se
    # relanza sola.
    rm -f "$SOLICITADA"

    escribir_estado en_marcha null ""

    if [ ! -f docker-compose.yml ] || ! command -v docker >/dev/null 2>&1; then
        escribir_estado fallo null "No hay docker-compose.yml o docker en este host: solo se sabe actualizar un despliegue con Docker (docker-update.sh)."
        return 0
    fi

    codigo=0
    salida=$(./docker-update.sh 2>&1) || codigo=$?

    # Es para el desplegable, no un log: bastan las últimas líneas. El log entero
    # queda en journalctl -u dockermanager-actualizador.
    printf '%s\n' "$salida"
    salida=$(printf '%s\n' "$salida" | tail -n 40)

    if [ "$codigo" -eq 0 ]; then
        escribir_estado ok 0 "$salida"
    else
        escribir_estado fallo "$codigo" "$salida"
    fi
}

main "$@"; exit $?
