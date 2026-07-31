FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py config.py config.ini ./
COPY static/ ./static/

EXPOSE 3000

CMD ["python", "main.py"]
