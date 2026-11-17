# Changelog

El formato sigue [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/).
`docker-update.sh` imprime la primera sección al actualizar.

## [1.2.0] - 2026-11-17

### Añadido
- Telegram se configura desde **Ajustes › Avisos** sin reiniciar (`configTelegram.py`). El token se comprueba con Telegram (`getMe`) antes de guardarlo y nunca vuelve al navegador. También se editan los IDs autorizados, las categorías y los umbrales. Se guarda en `data/telegram-config.json` (`0600`). Prioridad: `.env` > web > `config.ini`; lo que venga del `.env` aparece bloqueado en la web. Cambiar el bot o los IDs avisa a los IDs anteriores con la IP que hizo el cambio. Endpoints `PUT` y `DELETE /api/ajustes/telegram`.
- Botón **Borrar sin usar** en cada grupo de imágenes: borra, una a una, las imágenes del grupo que no usa ningún contenedor (versiones antiguas de una app).

### Cambiado
- Scrollbars finas y oscuras en toda la app. Los modales hacen scroll en su cuerpo en lugar de desbordar la pantalla, y los títulos largos se recortan con puntos suspensivos.
- Hoja de estilos: colores derivados y fuentes en variables, media queries agrupadas al final de mayor a menor ancho, estilos en línea del HTML sustituidos por clases y logo SVG definido una sola vez.
- Anillo de foco visible al navegar con teclado en botones y campos, y sin animaciones si el sistema pide reducir el movimiento.
- Portada del README con logo, título centrado e insignias de versión, Python, FastAPI, Docker y licencia.

### Corregido
- **Comprobar versiones** seguía mostrando la versión anterior hasta 5 minutos después de publicar una nueva: GitHub cachea los ficheros de cada rama en su CDN. Ahora `version.py` y `CHANGELOG.md` se leen del último commit de la rama, que se consulta a la API de GitHub; si la API falla, se leen de la rama como antes.
- La clase `hidden` solo ocultaba en algunos contextos: el modal **Nuevo contenedor** mostraba siempre una barra roja vacía y los formularios de login, configuración inicial y cambio de contraseña dejaban un hueco de más.
- Sin `DM_UID` en el `.env` (modo root de compatibilidad), `dockermanager-permisos` pasaba el buzón de actualizaciones a root y el vigilante del host se quedaba sin permisos. Ahora no toca nada.

## [1.1.0] - 2026-11-17

### Añadido
- La versión de la cabecera abre un desplegable de actualización: versión instalada, última publicada (se lee `version.py` de la rama en GitHub, con caché de 6 h), **Comprobar versiones** y **Actualizar ahora**. Si hay versión nueva, la versión se pone ámbar con un punto.
- Vigilante de actualizaciones (`tools/actualizador/`): un temporizador de systemd en el host que recoge la petición del botón y ejecuta `docker-update.sh`. La app no puede hacerlo ella misma porque recrearía su propio contenedor a mitad de la operación. Se instala una vez con `sudo ./tools/actualizador/instalar-vigilante.sh`; sin él, el desplegable lo dice y no deja pulsar «Actualizar».
- El desplegable sigue la actualización, recarga la página al volver la versión nueva y, si falla, muestra la salida de `docker-update.sh`.
- Sección `[actualizacion]` en `config.ini` y buzón `data/actualizacion/` montado en el contenedor.
- Avisos por Telegram (opcional, `telegrama.py` y `avisos.py`): contenedores caídos, OOM, `unhealthy` y bucles de reinicios; login correcto, IP bloqueada, cambio de contraseña y configuración inicial; versión nueva y resultado de la actualización; temperatura crítica y disco casi lleno; arranque de la app. Comandos de solo lectura `/estado`, `/contenedores` y `/ayuda`. Se configura desde **Ajustes › Avisos** sin reiniciar: el token se comprueba con Telegram antes de guardarlo y nunca vuelve al navegador. También se pueden editar las categorías y los umbrales. Se guarda en `data/telegram-config.json` (`0600`). `TELEGRAM_BOT_TOKEN` y `TELEGRAM_IDS_PERMITIDOS` en el `.env`, si están, tienen prioridad y aparecen bloqueados en la web. Cambiar el bot o los IDs avisa a los IDs anteriores. Endpoints `PUT` y `DELETE /api/ajustes/telegram`. Solo responde y avisa a esos IDs por chat privado, y descarta sin procesar los mensajes de cualquier otro. Sección `[telegram]` en `config.ini`.
- Botón **Limpiar** en la cabecera: muestra las imágenes y volúmenes que no usa ningún contenedor (ni siquiera uno parado) y tienen más de N días, y borra solo los tipos elegidos. Los fallos individuales (imagen con hijas, volumen recién montado) se informan sin detener el resto.
- Endpoints `GET` y `POST /api/limpieza` y sección `[limpieza]` en `config.ini`.
- «Actualizar» del desplegable de versión abre una ventana de confirmación (instalada → publicada, estado y cuándo se comprobó) con las novedades de cada versión intermedia, leídas del `CHANGELOG.md` de la rama configurada. Sustituye al `confirm()` del navegador.
- `GET /api/actualizacion` devuelve también `remota.novedades` y `remota.errorNovedades`.
- Botón **Servidor** en la cabecera: estado del host en vivo (CPU total y por núcleo, frecuencia, carga media, RAM, swap, uso y velocidad del disco, temperaturas, ventiladores y tiempo encendido) con gráfica histórica de CPU, RAM y temperatura. Red y procesos del host no se muestran: en modo bridge el contenedor solo ve los suyos.
- La barra de la cabecera muestra la temperatura de la CPU cuando hay sensor.
- Endpoint `GET /api/system/metrics` (`servidor.py`) y sección `[servidor]` en `config.ini`.
- Opción `[security] trusted_proxies` en `config.ini` para usar la app detrás de un proxy inverso.

### Cambiado
- El contenedor corre sin root: con el UID/GID de quien ejecuta `docker-up.sh` y el grupo del socket de Docker (`DM_UID`, `DM_GID` y `DOCKER_GID`, que los scripts escriben en el `.env`). Además, sistema de archivos de solo lectura, `cap_drop: ALL` y `no-new-privileges`. El servicio de un solo uso `dockermanager-permisos` cede el volumen de datos al usuario nuevo. La primera actualización desde una versión anterior aún arranca como root, porque la hace el `docker-update.sh` viejo; la siguiente ejecución de `docker-up.sh` o `docker-update.sh` lo pasa a no-root.
- La sesión se guarda en `session.json` (solo su HMAC con `SECRET_KEY`) y caduca en el servidor: sobrevive a los reinicios, y el logout y el cambio de contraseña la invalidan aunque se reinicie el contenedor.

### Seguridad
- El bloqueo por intentos fallidos se podía saltar enviando un `X-Forwarded-For` distinto en cada intento. Ahora solo se acepta de los proxies de `trusted_proxies`, y uvicorn ya no reescribe la IP del cliente.
- El login tardaba menos con un usuario inexistente; ahora siempre ejecuta bcrypt.
- Los WebSocket (logs, stats y terminal) comprueban `Origin` (Cross-Site WebSocket Hijacking).
- `config.json` y `session.json` se escriben de forma atómica y con permisos `0600`.
- Subresource Integrity en xterm.js, xterm-addon-fit y Chart.js.
- `X-XSS-Protection: 0`, en lugar del modo `block`, que está obsoleto.
- `dockerManager.py` ya no usa `shell=True`.

### Rendimiento
- Un único cliente de Docker compartido en vez de crear uno por petición.
- El listado de contenedores hace una sola llamada al daemon en lugar de un `inspect` por contenedor. Afecta a contenedores, imágenes, volúmenes y `/contenedores` de Telegram.
- `/api/system/host` ya no bloquea un hilo 300 ms en cada refresco.
- `config.json` se cachea en memoria en lugar de leerse en cada petición.
- Compresión gzip de las respuestas (`app.js`, `style.css`, JSON grandes).

### Corregido
- `install.sh` no copiaba `version.py` y la instalación nativa no arrancaba.
- Crear un contenedor con un puerto no numérico o una política de reinicio desconocida devolvía un 500; ahora devuelve un 400 con el motivo.
- `install.sh` no copiaba `servidor.py`, `telegrama.py`, `avisos.py` ni `ajustes.py`, y la instalación nativa no arrancaba.

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
