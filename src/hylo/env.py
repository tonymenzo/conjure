"""``.env`` file management — borrowed pattern from agenTeX.

Two locations are searched:

- ``~/.config/hylo/.env`` (user-global, set via ``hylo
  config set``).
- ``./.env`` in the current working directory (project-local).

Loading rules:

- Shell environment always wins. Anything already in ``os.environ`` at
  process start is not overwritten.
- The user file is loaded first; the project file is loaded after and
  may override user values (so per-project settings beat global
  defaults) but never shell values.

``python-dotenv`` is an optional dependency — when absent, env file
loading is a no-op and only shell ``os.environ`` is used.
"""

from __future__ import annotations

import os
from pathlib import Path


USER_ENV_PATH = Path.home() / ".config" / "hylo" / ".env"
# Before 0.3.0 hylo was named conjure; its user .env is read when hylo's is absent.
LEGACY_USER_ENV_PATH = Path.home() / ".config" / "conjure" / ".env"
PROJECT_ENV_FILENAME = ".env"

_SECRET_KEY_HINTS = ("API_KEY", "TOKEN", "SECRET")


def adopt_legacy_variables(environ: "os._Environ[str] | dict[str, str] | None" = None) -> list[str]:
    """Copy each ``CONJURE_*`` variable to its ``HYLO_*`` name when that is unset.

    hylo was named conjure before 0.3.0; settings exported under the old
    names keep working. Returns the names adopted.
    """
    env = os.environ if environ is None else environ
    adopted = []
    for key in [k for k in env if k.startswith("CONJURE_")]:
        new = "HYLO_" + key[len("CONJURE_"):]
        if new not in env:
            env[new] = env[key]
            adopted.append(new)
    return adopted


def user_env_path() -> Path:
    """hylo's user ``.env``, or conjure's when only that one exists."""
    if not USER_ENV_PATH.exists() and LEGACY_USER_ENV_PATH.exists():
        return LEGACY_USER_ENV_PATH
    return USER_ENV_PATH


def _carry_legacy_env() -> None:
    """Before hylo first writes its user .env, copy conjure's into place, so
    a write does not hide the keys stored under the old name."""
    if not USER_ENV_PATH.exists() and LEGACY_USER_ENV_PATH.exists():
        import shutil

        USER_ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(LEGACY_USER_ENV_PATH, USER_ENV_PATH)


def _shell_env_keys_snapshot() -> set[str]:
    """Snapshot of ``os.environ`` keys at the moment of capture."""
    return set(os.environ.keys())


def load_env_files(*, project_dir: Path | None = None) -> None:
    """Pull values from user + project ``.env`` files into ``os.environ``.

    Shell env always wins. User values are loaded first; project values
    may override user but not shell. No-op when ``python-dotenv`` is not
    installed.
    """
    try:
        from dotenv import dotenv_values  # type: ignore
    except ImportError:
        return

    shell_keys = _shell_env_keys_snapshot()
    project_path = (project_dir or Path.cwd()) / PROJECT_ENV_FILENAME

    for path, allow_override_user in (
        (user_env_path(), False),
        (project_path, True),
    ):
        if not path.exists():
            continue
        for k, v in dotenv_values(path).items():
            if v is None:
                continue
            if k in shell_keys:
                continue
            if not allow_override_user and k in os.environ:
                continue
            os.environ[k] = v


def redact(key: str, value: str) -> str:
    """Redact ``value`` if ``key`` looks secret-shaped."""
    if not any(h in key for h in _SECRET_KEY_HINTS) or not value:
        return value
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}…{value[-4:]}"


def list_user_env() -> dict[str, str]:
    """Return the user .env file contents (empty dict if absent or
    python-dotenv is missing)."""
    try:
        from dotenv import dotenv_values  # type: ignore
    except ImportError:
        return {}
    if not user_env_path().exists():
        return {}
    return {k: (v or "") for k, v in dotenv_values(user_env_path()).items()}


def set_user_env(key: str, value: str) -> Path:
    """Set ``key=value`` in the user .env file (creating it if needed).
    Raises ImportError if python-dotenv is not installed."""
    from dotenv import set_key  # type: ignore

    _carry_legacy_env()
    USER_ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    USER_ENV_PATH.touch(exist_ok=True)
    set_key(str(USER_ENV_PATH), key, value)
    return USER_ENV_PATH


def unset_user_env(key: str) -> Path:
    """Remove ``key`` from the user .env file."""
    from dotenv import unset_key  # type: ignore

    _carry_legacy_env()
    if not USER_ENV_PATH.exists():
        return USER_ENV_PATH
    unset_key(str(USER_ENV_PATH), key)
    return USER_ENV_PATH
