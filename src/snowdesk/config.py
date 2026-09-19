"""Connection discovery and application paths.

SnowDesk does not invent its own connection format: it reads the same files the
``snow`` CLI and the Python connector use, purely to populate the connection
picker.  Actual connection parameters are left to the connector to resolve from
``connection_name`` (see :mod:`snowdesk.db.session`).

The connector accepts connections in two places, and people use both:

* ``connections.toml``, one ``[name]`` table per connection;
* ``config.toml``, under a ``[connections.name]`` table.

Both are read here and merged.  ``connections.toml`` wins on a name collision,
matching the connector's own precedence.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_DIR = Path.home() / ".snowflake"
SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "SnowDesk"
LOG_DIR = Path.home() / "Library" / "Logs" / "SnowDesk"

#: Default page size for incremental result fetching (R2).
DEFAULT_PAGE_SIZE = 500
#: Default cap on rows held in memory per result (R4).
DEFAULT_ROW_CAP = 100_000


@dataclass(frozen=True, slots=True)
class ConnectionInfo:
    """A connection entry as shown in the picker."""

    name: str
    account: str | None = None
    user: str | None = None
    role: str | None = None
    warehouse: str | None = None
    database: str | None = None
    schema: str | None = None
    authenticator: str | None = None
    is_default: bool = False

    @property
    def summary(self) -> str:
        bits = [b for b in (self.user, self.account) if b]
        return " @ ".join(bits) if bits else ""


def config_dir() -> Path:
    """The Snowflake config directory, honouring ``SNOWFLAKE_HOME``."""
    override = os.environ.get("SNOWFLAKE_HOME")
    return Path(override).expanduser() if override else DEFAULT_CONFIG_DIR


def connections_file() -> Path:
    return config_dir() / "connections.toml"


def config_file() -> Path:
    return config_dir() / "config.toml"


def _load_toml(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Could not read {path}: {exc}") from exc


class ConfigError(Exception):
    """Raised when the connection configuration cannot be read."""


def default_connection_name(cfg_dir: Path | None = None) -> str | None:
    """Resolve the default connection name (C2).

    ``SNOWFLAKE_DEFAULT_CONNECTION_NAME`` wins over ``default_connection_name``
    in ``config.toml``.
    """
    env = os.environ.get("SNOWFLAKE_DEFAULT_CONNECTION_NAME")
    if env:
        return env
    data = _load_toml((cfg_dir or config_dir()) / "config.toml")
    value = data.get("default_connection_name")
    return value if isinstance(value, str) else None


def _str_or_none(section: dict[str, object], key: str) -> str | None:
    value = section.get(key)
    return value if isinstance(value, str) else None


def _connection_sections(directory: Path) -> dict[str, dict[str, object]]:
    """Collect ``name -> parameters`` from both config files.

    ``config.toml`` is read first so entries with the same name in
    ``connections.toml`` override it, which is the connector's precedence.
    """
    found: dict[str, dict[str, object]] = {}

    nested = _load_toml(directory / "config.toml").get("connections")
    if isinstance(nested, dict):
        for name, section in nested.items():
            if isinstance(section, dict):
                found[name] = section

    for name, section in _load_toml(directory / "connections.toml").items():
        if isinstance(section, dict):
            found[name] = section

    return found


def _to_info(name: str, section: dict[str, object], default: str | None) -> ConnectionInfo:
    return ConnectionInfo(
        name=name,
        account=_str_or_none(section, "account"),
        user=_str_or_none(section, "user"),
        role=_str_or_none(section, "role"),
        warehouse=_str_or_none(section, "warehouse"),
        database=_str_or_none(section, "database"),
        schema=_str_or_none(section, "schema"),
        authenticator=_str_or_none(section, "authenticator"),
        is_default=(name == default),
    )


def list_connections(cfg_dir: Path | None = None) -> list[ConnectionInfo]:
    """List every configured connection (C1).

    Sorted with the default connection first, then alphabetically.  Secrets in
    the files (passwords, passphrases, private keys) are deliberately never
    read — the connector resolves those itself at connect time.
    """
    directory = cfg_dir or config_dir()
    default = default_connection_name(directory)
    out = [
        _to_info(name, section, default)
        for name, section in _connection_sections(directory).items()
    ]
    out.sort(key=lambda c: (not c.is_default, c.name.lower()))
    return out


def support_dir() -> Path:
    """Application support directory, created on demand."""
    SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    return SUPPORT_DIR


def log_dir() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR


def history_db_path() -> Path:
    return support_dir() / "history.db"
