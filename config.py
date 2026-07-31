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

CDN_JSDELIVR = _get("cdn", "jsdelivr", "https://cdn.jsdelivr.net")

# Si DOCKER_HOST no esta ya definido, usar el socket configurado en el .ini
if "DOCKER_HOST" not in os.environ and DOCKER_SOCKET:
    os.environ["DOCKER_HOST"] = f"unix://{DOCKER_SOCKET}"
