# Changelog

El formato sigue [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/).
`docker-update.sh` imprime la primera sección al actualizar.

## [1.0.0] - 2026-10-06

### Añadido
- Versión de la aplicación (`version.py`), visible en la cabecera y en el modal «Sistema».
- Endpoint público `/api/health` y `HEALTHCHECK` en la imagen.
- `docker-update.sh`: actualización con comprobaciones previas, verificación del arranque y vuelta atrás automática a la imagen anterior.
- Las imágenes se etiquetan por versión (`dockermanager:1.0.0`).
- Contenedores agrupados por stack de Docker Compose o, si no lo hay, por el prefijo del nombre (`PorfolioManager-caddy` → `PorfolioManager`), con inicio, reinicio y parada del grupo completo.
- Imágenes y volúmenes clasificados en el grupo de su contenedor; los que no se usan se asignan por nombre (`porfoliomanager:3.0.0`) y el resto va a «Sin usar». Columna «Usada por».
- Tarjetas de resumen, filtro por estado, estado del healthcheck y grupos plegables.

### Corregido
- Los ficheros estáticos se revalidan siempre (`Cache-Control: no-cache`): tras actualizar ya no se sigue viendo la interfaz antigua.
- HTTP 500 al listar contenedores cuando la imagen de alguno ya no existía.
- Borrar una imagen o volumen en uso ya no se fuerza: Docker lo rechaza y se muestra el motivo.

### Cambiado
- `update.sh` sustituido por `docker-update.sh`.
