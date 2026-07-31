#!/bin/bash
set -e

cd "$(dirname "$0")"

echo "Actualizando DockerManager..."
docker compose down
# Vía docker-up.sh para que el puerto siga saliendo de config.ini
./docker-up.sh
echo ""
docker compose ps
