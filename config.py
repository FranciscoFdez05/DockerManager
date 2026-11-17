import configparser
import os

_INI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")

_parser = configparser.ConfigParser()
_parser.read(_INI_PATH)


def _get(section: str, key: str, default: str, env: str | None = None) -> str:
    if env and os.getenv(env) is not None:
        return os.getenv(env)
    return _parser.get(section, key, fallback=default)


def _get_int(section: str, key: str, default: int, env: str | None = None) -> int:
    return int(_get(section, key, str(default), env))


PORT = _get_int("server", "port", 3000, env="PORT")
BIND_HOST = _get("server", "bind_host", "0.0.0.0", env="HOST")

DATA_DIR = _get("docker", "data_dir", "./data", env="DATA_DIR")
DOCKER_SOCKET = _get("docker", "docker_socket", "/var/run/docker.sock")

SESSION_COOKIE_NAME = _get("security", "session_cookie_name", "dm_session")
SESSION_MAX_AGE_DAYS = _get_int("security", "session_max_age_days", 30)
SESSION_MAX_AGE_SECONDS = SESSION_MAX_AGE_DAYS * 86400
BCRYPT_COST = _get_int("security", "bcrypt_cost", 12)
MAX_LOGIN_ATTEMPTS = _get_int("security", "max_login_attempts", 5)
LOCKOUT_MINUTES = _get_int("security", "lockout_minutes", 15)
# IPs o redes CIDR de proxies inversos de confianza. Solo de ellas se acepta
# X-Forwarded-For; vacío = se usa siempre la IP de la conexión.
proxiesConfiables = _get("security", "trusted_proxies", "")

CDN_JSDELIVR = _get("cdn", "jsdelivr", "https://cdn.jsdelivr.net")

UPDATE_CHECK = _get("actualizacion", "comprobar", "true").strip().lower() in ("1", "true", "yes", "si", "on")
UPDATE_REPO = _get("actualizacion", "repositorio", "FranciscoFdez05/DockerManager")
UPDATE_BRANCH = _get("actualizacion", "rama", "main")
UPDATE_TIMEOUT_SECONDS = _get_int("actualizacion", "timeout_segundos", 5)
UPDATE_CACHE_HOURS = _get_int("actualizacion", "cache_horas", 6)
UPDATE_DIR = _get("actualizacion", "directorio", "./data/actualizacion", env="ACTUALIZACION_DIR")

# Panel «Servidor» (servidor.py). Umbrales de color de la temperatura cuando el
# sensor no publica los suyos, y ruta cuyo sistema de ficheros se mide como disco.
servidorTempAviso = _get_int("servidor", "temperatura_aviso", 70)
servidorTempCritica = _get_int("servidor", "temperatura_critica", 85)
servidorRutaDisco = _get("servidor", "ruta_disco", "/")

CLEANUP_DEFAULT_DAYS = _get_int("limpieza", "dias_por_defecto", 30)
CLEANUP_MAX_DAYS = _get_int("limpieza", "dias_maximos", 3650)

# Telegram (telegrama.py / avisos.py). El token y los IDs autorizados se leen
# solo del entorno (.env), nunca de config.ini: ese archivo se publica en el repo.
telegramToken = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
telegramIdsPermitidos = os.getenv("TELEGRAM_IDS_PERMITIDOS", "").strip()
telegramCategorias = _get("telegram", "notificar", "sistema, contenedores, seguridad, actualizaciones, recursos")
telegramTimeout = _get_int("telegram", "timeout_segundos", 10)
telegramIntervalo = _get_int("telegram", "intervalo_segundos", 60)
telegramDiscoAviso = _get_int("telegram", "disco_aviso_pct", 90)
telegramReiniciosBucle = _get_int("telegram", "reinicios_bucle", 3)
telegramVentanaBucleMinutos = _get_int("telegram", "ventana_bucle_minutos", 10)

# Si DOCKER_HOST no esta ya definido, usar el socket configurado en el .ini
if "DOCKER_HOST" not in os.environ and DOCKER_SOCKET:
    os.environ["DOCKER_HOST"] = f"unix://{DOCKER_SOCKET}"
