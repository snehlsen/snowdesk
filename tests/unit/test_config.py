from __future__ import annotations

from pathlib import Path

import pytest

from snowdesk import config


@pytest.fixture
def cfg_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("SNOWFLAKE_DEFAULT_CONNECTION_NAME", raising=False)
    (tmp_path / "connections.toml").write_text(
        """
[prod]
account = "acct1"
user = "me"
authenticator = "externalbrowser"

[dev]
account = "acct2"
user = "me"
role = "DEV"
"""
    )
    (tmp_path / "config.toml").write_text('default_connection_name = "dev"\n')
    return tmp_path


def test_lists_connections_with_default_first(cfg_dir: Path) -> None:
    conns = config.list_connections(cfg_dir)
    assert [c.name for c in conns] == ["dev", "prod"]
    assert conns[0].is_default
    assert conns[0].role == "DEV"
    assert conns[1].authenticator == "externalbrowser"


def test_env_overrides_default_connection(cfg_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SNOWFLAKE_DEFAULT_CONNECTION_NAME", "prod")
    assert config.default_connection_name(cfg_dir) == "prod"
    assert config.list_connections(cfg_dir)[0].name == "prod"


def test_missing_file_yields_no_connections(tmp_path: Path) -> None:
    assert config.list_connections(tmp_path) == []


def test_connections_nested_in_config_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The layout produced by `snow connection add`: no connections.toml at all."""
    monkeypatch.delenv("SNOWFLAKE_DEFAULT_CONNECTION_NAME", raising=False)
    (tmp_path / "config.toml").write_text(
        """
default_connection_name = "myconnection"

[connections.myconnection]
account = "acct"
user = "me"
authenticator = "SNOWFLAKE_JWT"
private_key_file = "/keys/rsa_key.p8"
role = "ACCOUNTADMIN"
warehouse = "COMPUTE_WH"
database = "SF_TUTS"
schema = "PUBLIC"

[cli.logs]
save_logs = true
"""
    )
    conns = config.list_connections(tmp_path)
    assert [c.name for c in conns] == ["myconnection"]
    entry = conns[0]
    assert entry.is_default
    assert entry.authenticator == "SNOWFLAKE_JWT"
    assert entry.warehouse == "COMPUTE_WH"
    assert entry.database == "SF_TUTS"


def test_unrelated_config_sections_are_not_connections(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text("[cli.logs]\nlevel = 'info'\n")
    assert config.list_connections(tmp_path) == []


def test_both_files_are_merged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SNOWFLAKE_DEFAULT_CONNECTION_NAME", raising=False)
    (tmp_path / "config.toml").write_text('[connections.nested]\naccount = "a1"\n')
    (tmp_path / "connections.toml").write_text('[separate]\naccount = "a2"\n')
    assert [c.name for c in config.list_connections(tmp_path)] == ["nested", "separate"]


def test_connections_toml_wins_on_a_name_collision(tmp_path: Path) -> None:
    """Matches the connector's own precedence between the two files."""
    (tmp_path / "config.toml").write_text('[connections.dup]\naccount = "from-config"\n')
    (tmp_path / "connections.toml").write_text('[dup]\naccount = "from-connections"\n')
    conns = config.list_connections(tmp_path)
    assert len(conns) == 1
    assert conns[0].account == "from-connections"


def test_default_from_config_toml_applies_to_nested_connections(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        'default_connection_name = "b"\n[connections.a]\naccount = "a"\n'
        '[connections.b]\naccount = "b"\n'
    )
    assert [c.name for c in config.list_connections(tmp_path)] == ["b", "a"]


def test_unreadable_file_raises_config_error(tmp_path: Path) -> None:
    (tmp_path / "connections.toml").write_text("this is not toml = = =")
    with pytest.raises(config.ConfigError):
        config.list_connections(tmp_path)


def test_summary_for_picker(cfg_dir: Path) -> None:
    conns = {c.name: c for c in config.list_connections(cfg_dir)}
    assert conns["prod"].summary == "me @ acct1"


def test_snowflake_home_is_honoured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SNOWFLAKE_HOME", str(tmp_path))
    assert config.connections_file() == tmp_path / "connections.toml"
