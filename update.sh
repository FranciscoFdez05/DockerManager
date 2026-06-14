#!/bin/bash
set -e

cd "$(dirname "$0")"

echo "Actualizando DockerManager..."
docker compose down
docker compose up -d --build
echo ""
docker compose ps
