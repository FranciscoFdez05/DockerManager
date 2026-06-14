#!/bin/bash
# Actualiza Docker Gestor con los últimos archivos del directorio actual
set -e

APP_DIR="/opt/dockergestor"
SERVICE="dockergestor"

echo "Actualizando Docker Gestor..."
sudo cp main.py requirements.txt "$APP_DIR/"
sudo cp static/index.html static/style.css static/app.js "$APP_DIR/static/"

CURRENT_USER=$(whoami)
sudo chown -R "$CURRENT_USER:$CURRENT_USER" "$APP_DIR"

"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
sudo systemctl restart "$SERVICE"

sleep 1
sudo systemctl is-active "$SERVICE" && echo "Servicio reiniciado correctamente." || echo "Error al reiniciar."
