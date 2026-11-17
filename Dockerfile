FROM python:3.12-slim

# Sin .pyc: el sistema de archivos del contenedor es de solo lectura
# (read_only en docker-compose.yml). HOME apunta a /tmp (tmpfs) porque el
# usuario lo fija docker-compose.yml y no tiene entrada en /etc/passwd.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py config.py version.py actualizacion.py limpieza.py servidor.py telegrama.py avisos.py ajustes.py seguridad.py clienteDocker.py config.ini ./
COPY static/ ./static/

# Usuario sin privilegios por defecto. docker-compose.yml lo sustituye por el
# UID/GID de quien ejecuta docker-up.sh (para escribir en el buzón del vigilante)
# y añade el grupo del socket de Docker.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin dockermanager
USER 10001

EXPOSE 3000

# /api/health es público y comprueba la conexión con el daemon de Docker.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://localhost:%s/api/health' % os.environ.get('PORT', '3000'), timeout=3)"

CMD ["python", "main.py"]
