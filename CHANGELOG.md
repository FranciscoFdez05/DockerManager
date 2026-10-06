# Changelog

El formato sigue [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/).
`docker-update.sh` imprime la primera sección al actualizar.

## [1.0.0] - 2026-10-06

### Añadido
- Versión de la aplicación (`version.py`), visible en la cabecera y en el modal «Sistema».
- Endpoint público `/api/health` y `HEALTHCHECK` en la imagen.
- `docker-update.sh`: actualización con comprobaciones previas, verificación del arranque y vuelta atrás automática a la imagen anterior.
- Las imágenes se etiquetan por versión (`dockermanager:1.0.0`).
- Contenedores agrupados por stack de Docker Compose, con inicio, reinicio y parada de un stack completo.
- Imágenes y volúmenes clasificados por contenedor o stack propietario, con grupo «Sin usar» y columna «Usada por».
- Tarjetas de resumen, filtro por estado, estado del healthcheck y grupos plegables.

### Corregido
- HTTP 500 al listar contenedores cuando la imagen de alguno ya no existía.
- Borrar una imagen o volumen en uso ya no se fuerza: Docker lo rechaza y se muestra el motivo.

### Cambiado
- `update.sh` sustituido por `docker-update.sh`.
