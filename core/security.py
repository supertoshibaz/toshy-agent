import hmac
import os
import secrets
from pathlib import Path
from typing import Dict

from core.paths import APP_PATHS


TOKEN_ENV = "TOSHY_CORE_TOKEN"


def load_or_create_core_token(token_path: Path = APP_PATHS.runtime / "core.token") -> str:
    env_token = os.environ.get(TOKEN_ENV, "").strip()
    if env_token:
        return env_token

    from core.private_files import file_lock, atomic_private, protect_private
    token_path=Path(token_path)
    with file_lock(token_path.with_name(token_path.name+'.lock')):
        try:
            protect_private(token_path)
            existing=token_path.read_text(encoding='utf-8').strip()
        except FileNotFoundError:existing=''
        except UnicodeError:existing=''
        if existing and not any(char.isspace() for char in existing):return existing
        token=secrets.token_urlsafe(32)
        atomic_private(token_path,token.encode('utf-8'))
        return token


def auth_headers(token: str | None = None) -> Dict[str, str]:
    active_token = token or load_or_create_core_token()
    return {"Authorization": f"Bearer {active_token}"}


def token_matches(candidate: str, expected: str) -> bool:
    return bool(candidate) and hmac.compare_digest(candidate, expected)
