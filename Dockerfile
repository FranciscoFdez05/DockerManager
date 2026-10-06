FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py config.py version.py config.ini ./
COPY static/ ./static/

EXPOSE 3000

# /api/health es público y comprueba la conexión con el daemon de Docker.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://localhost:%s/api/health' % os.environ.get('PORT', '3000'), timeout=3)"

CMD ["python", "main.py"]
