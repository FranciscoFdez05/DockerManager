#!/bin/bash
# Instalador de Docker Gestor para Ubuntu
set -e

APP_DIR="/opt/dockergestor"
SERVICE="dockergestor"
CURRENT_USER=$(whoami)

echo "========================================"
echo "  Docker Gestor — Instalador"
echo "========================================"
echo ""

# Verificar Docker
if ! command -v docker &>/dev/null; then
  echo "ERROR: Docker no está instalado. Instálalo primero."
  exit 1
fi

# Verificar Python 3
if ! command -v python3 &>/dev/null; then
  echo "Instalando Python3..."
  sudo apt-get update -qq
  sudo apt-get install -y python3 python3-venv python3-pip
fi

# Verificar que python3-venv esté disponible
python3 -c "import venv" 2>/dev/null || {
  echo "Instalando python3-venv..."
  sudo apt-get install -y python3-venv
}

# Copiar archivos
echo "Instalando archivos en $APP_DIR..."
sudo mkdir -p "$APP_DIR/static"
sudo cp main.py config.py requirements.txt "$APP_DIR/"
[ -f "$APP_DIR/config.ini" ] || sudo cp config.ini "$APP_DIR/"
sudo cp static/index.html static/style.css static/app.js "$APP_DIR/static/"
sudo chown -R "$CURRENT_USER:$CURRENT_USER" "$APP_DIR"

# Puerto leido de config.ini (usado solo para mostrar la URL final)
PORT=$(grep -E '^port\s*=' "$APP_DIR/config.ini" | head -1 | sed -E 's/^port\s*=\s*//')
PORT=${PORT:-3000}

# Crear entorno virtual e instalar dependencias
echo "Instalando dependencias Python..."
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
echo "Dependencias instaladas."

# Agregar usuario al grupo docker si hace falta
if ! groups "$CURRENT_USER" | grep -qw docker; then
  echo "Agregando $CURRENT_USER al grupo docker..."
  sudo usermod -aG docker "$CURRENT_USER"
  NEEDS_RELOGIN=1
fi

# Instalar servicio systemd
echo "Configurando servicio systemd..."
sudo tee /etc/systemd/system/${SERVICE}.service > /dev/null <<EOF
[Unit]
Description=Docker Gestor - Interfaz Web para Docker
After=network.target docker.service
Wants=docker.service

[Service]
Type=simple
User=${CURRENT_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/venv/bin/python main.py
Restart=on-failure
RestartSec=5s
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE"

if [ "${NEEDS_RELOGIN}" = "1" ]; then
  echo ""
  echo "AVISO: Se añadió $CURRENT_USER al grupo docker."
  echo "Debes cerrar sesión y volver a entrar para activar el permiso."
  echo "Después ejecuta:  sudo systemctl start $SERVICE"
else
  sudo systemctl start "$SERVICE"
  sleep 1
  STATUS=$(sudo systemctl is-active "$SERVICE" 2>/dev/null || true)
  if [ "$STATUS" = "active" ]; then
    IP=$(hostname -I | awk '{print $1}')
    echo ""
    echo "========================================"
    echo "  Instalacion completada!"
    echo "  Accede en: http://${IP}:${PORT}"
    echo "========================================"
  else
    echo "El servicio no arrancó. Revisa logs con:"
    echo "  sudo journalctl -u $SERVICE -e"
  fi
fi

echo ""
echo "Comandos útiles:"
echo "  sudo systemctl status  $SERVICE"
echo "  sudo systemctl restart $SERVICE"
echo "  sudo systemctl stop    $SERVICE"
echo "  sudo journalctl -u     $SERVICE -f"
