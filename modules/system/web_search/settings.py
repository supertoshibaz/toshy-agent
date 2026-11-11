"""Web Search's explicit proxy settings, shared by the UI and core process."""

import hashlib
import json
import math
import os
import uuid
from urllib.parse import urlsplit, urlunsplit

from core.paths import APP_PATHS

SETTINGS_PATH = APP_PATHS.runtime / "web_search.json"
SECRET_SERVICE = "Toshy Web Search proxy"

# default, minimum, maximum; integer defaults identify integer settings.
CRAWLER_LIMITS = {
    "concurrency": (4, 1, 16),
    "delay": (0.25, 0.0, 30.0),
    "timeout": (15.0, 1.0, 120.0),
    "retries": (2, 0, 5),
    "retry_backoff": (1.0, 0.1, 30.0),
    "max_page_mb": (8, 1, 64),
    "max_pages": (1500, 1, 100000),
    "search_results": (8, 1, 30),
    "search_pages": (2, 0, 30),
}


def normalize_crawler(value=None):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("Некорректные настройки краулера.")
    result = {}
    for key, (default, minimum, maximum) in CRAWLER_LIMITS.items():
        if key == 'search_pages':
            result[key] = 0  # Старую настройку принимаем, автозагрузки поиска больше нет.
            continue
        number = value.get(key, default)
        if (
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not math.isfinite(number)
            or not minimum <= number <= maximum
            or (isinstance(default, int) and int(number) != number)
        ):
            raise ValueError(f"Параметр краулера {key}: от {minimum} до {maximum}.")
        result[key] = int(number) if isinstance(default, int) else float(number)
    if result["search_pages"] > result["search_results"]:
        raise ValueError(
            "Количество скачиваемых страниц не может превышать число результатов поиска."
        )
    return result


def normalize_browser(value=None):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("Некорректные настройки браузера.")
    path = value.get("path", "")
    if not isinstance(path, str) or len(path) > 4096 or any(ord(c) < 32 for c in path):
        raise ValueError("Некорректный путь к браузеру.")
    timeout = value.get("timeout", 30)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or not 5 <= timeout <= 120
    ):
        raise ValueError("Таймаут браузера: от 5 до 120 секунд.")
    return {
        "enabled": value.get("enabled") is True,
        "path": path.strip(),
        "timeout": float(timeout),
    }


def _normalize_address(value, schemes):
    address = str(value or "").strip()
    if not address:
        return ""
    if len(address) > 4096 or any(c.isspace() or ord(c) < 32 for c in address):
        raise ValueError("Некорректный адрес прокси.")
    try:
        parsed = urlsplit(address)
        host, port = parsed.hostname, parsed.port
        invalid = (
            parsed.scheme not in schemes
            or not host
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        )
        userinfo = parsed.username is not None or parsed.password is not None
    except ValueError:
        raise ValueError("Некорректный адрес или порт прокси.") from None
    if invalid:
        raise ValueError("Укажите адрес прокси без пути и параметров.")
    if userinfo:
        raise ValueError("Логин и пароль прокси вводятся в отдельных полях.")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Порт прокси: от 1 до 65535.")
    host = f"[{host}]" if ":" in host else host.encode("idna").decode("ascii")
    return urlunsplit(
        (parsed.scheme, host + (f":{port}" if port is not None else ""), "", "", "")
    )


def normalize_settings(value):
    if not isinstance(value, dict):
        raise ValueError("Некорректные настройки Web Search.")
    username = str(value.get("username") or "").strip()
    if len(username) > 1024 or any(ord(c) < 32 for c in username):
        raise ValueError("Некорректный логин прокси.")
    result = {
        "version": 2,
        "enabled": value.get("enabled") is True,
        "http": "",
        "https": "",
        "socks5": "",
        "use_http_for_https": True,
        "socks_remote_dns": True,
        "username": username,
        "password_saved": value.get("password_saved") is True,
        "password_id": "",
        "crawler": normalize_crawler(value.get("crawler")),
        "browser": normalize_browser(value.get("browser")),
    }
    if "address" in value and not any(
        key in value for key in ("http", "https", "socks5")
    ):
        address = _normalize_address(
            value.get("address"), {"http", "https", "socks5", "socks5h"}
        )
        if address.startswith("socks"):
            result.update(
                socks5=address, socks_remote_dns=address.startswith("socks5h:")
            )
        else:
            result.update(http=address, https=address)
        if result["password_saved"]:
            result["password_id"] = hashlib.sha256(
                (address + "\0" + username).encode()
            ).hexdigest()
    else:
        result.update(
            http=_normalize_address(value.get("http"), {"http", "https"}),
            https=_normalize_address(value.get("https"), {"http", "https"}),
            socks5=_normalize_address(value.get("socks5"), {"socks5", "socks5h"}),
            use_http_for_https=value.get("use_http_for_https", True) is True,
            socks_remote_dns=value.get("socks_remote_dns", True) is True,
        )
        if result["socks5"]:
            parsed = urlsplit(result["socks5"])
            result["socks5"] = urlunsplit(
                parsed._replace(
                    scheme="socks5h" if result["socks_remote_dns"] else "socks5"
                )
            )
        identity = str(value.get("password_id") or "")
        if result["password_saved"]:
            result["password_id"] = (
                identity
                if len(identity) == 64
                and all(c in "0123456789abcdef" for c in identity)
                else connection_identity(result)
            )
    https = result["http"] if result["use_http_for_https"] else result["https"]
    if result["enabled"] and not any((result["http"], https, result["socks5"])):
        raise ValueError("Укажите хотя бы один прокси.")
    return result


def connection_identity(settings):
    https = settings["http"] if settings["use_http_for_https"] else settings["https"]
    endpoints = set()
    for address in (settings["http"], https, settings["socks5"]):
        if not address:
            continue
        parsed = urlsplit(address)
        scheme = "socks5" if parsed.scheme.startswith("socks") else parsed.scheme
        port = parsed.port or {"http": 80, "https": 443, "socks5": 1080}[scheme]
        endpoints.add((scheme, parsed.hostname, port))
    return hashlib.sha256(
        json.dumps([sorted(endpoints), settings["username"]]).encode()
    ).hexdigest()


def load_settings():
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return normalize_settings({})
    except (OSError, ValueError):
        raise ValueError(
            "Не удалось прочитать настройки Web Search. Проверьте их в Startup."
        ) from None
    return normalize_settings(data)


def credential_id(settings):
    return settings.get("password_id") or connection_identity(settings)


def resolve_password(settings):
    if not settings.get("password_saved"):
        return ""
    try:
        from core.model_connections import _secure_keyring

        password = _secure_keyring().get_password(
            SECRET_SERVICE, credential_id(settings)
        )
    except Exception:
        password = None
    if not password:
        raise ValueError(
            "Сохранённый пароль прокси недоступен. Введите и сохраните его в Startup → Web Search."
        )
    return password


def save_settings(value, password=None):
    settings = normalize_settings(value)
    try:
        previous = load_settings()
    except ValueError:
        previous = normalize_settings({})
    same_credentials = connection_identity(previous) == connection_identity(settings)
    settings["password_saved"] = (
        previous["password_saved"]
        if same_credentials and password is None
        else bool(password)
    )
    settings["password_id"] = (
        (
            credential_id(previous)
            if same_credentials and password is None
            else connection_identity(settings)
        )
        if settings["password_saved"]
        else ""
    )
    if password is not None and len(password) > 4096:
        raise ValueError("Пароль прокси слишком длинный.")
    changes = []
    if password:
        changes.append((SECRET_SERVICE, credential_id(settings), password))
    rollback = []
    try:
        if changes:
            from core.model_connections import _secure_keyring

            backend = _secure_keyring()
            for service, identity, secret in changes:
                old = backend.get_password(service, identity)
                rollback.append((service, identity, old))
                backend.set_password(service, identity, secret)
        _write_settings(settings)
    except Exception:
        for service, identity, old in reversed(rollback):
            try:
                if old is None:
                    backend.delete_password(service, identity)
                else:
                    backend.set_password(service, identity, old)
            except Exception:
                pass
        raise ValueError(
            "Не удалось сохранить настройки или секреты Web Search. Прежняя конфигурация сохранена."
        ) from None
    if previous["password_saved"] and (
        credential_id(previous) != credential_id(settings)
        or not settings["password_saved"]
    ):
        try:
            from core.model_connections import _secure_keyring

            _secure_keyring().delete_password(SECRET_SERVICE, credential_id(previous))
        except Exception:
            pass
    return settings


def _write_settings(settings):
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_PATH.with_name(".web-search-" + uuid.uuid4().hex + ".tmp")
    try:
        fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(settings, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, SETTINGS_PATH)
    finally:
        temporary.unlink(missing_ok=True)
