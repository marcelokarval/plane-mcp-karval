"""Tests for the Plane MCP command-line transport selection."""

from __future__ import annotations

import sys
import os
from pathlib import Path

import pytest

import plane_mcp_karval.cli as cli


def test_project_env_is_loaded_without_overriding_process_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "PLANE_API_URL=https://from-project.invalid\n"
        "PLANE_API_TOKEN=project-token\n"
    )
    monkeypatch.setattr(cli, "PROJECT_ENV", env_file)
    monkeypatch.setenv("PLANE_API_TOKEN", "process-token")
    monkeypatch.delenv("PLANE_API_URL", raising=False)

    loaded = cli._load_project_env()

    assert loaded == env_file
    assert os.environ["PLANE_API_URL"] == "https://from-project.invalid"
    assert os.environ["PLANE_API_TOKEN"] == "process-token"


def test_installed_cli_falls_back_to_env_in_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("PLANE_API_URL=https://working-directory.invalid\n")
    monkeypatch.setattr(cli, "PROJECT_ENV", tmp_path / "missing.env")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PLANE_API_URL", raising=False)

    loaded = cli._load_project_env()

    assert loaded == env_file
    assert os.environ["PLANE_API_URL"] == "https://working-directory.invalid"


class FakeServer:
    def __init__(self) -> None:
        self.run_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def run(self, *args: object, **kwargs: object) -> None:
        self.run_calls.append((args, kwargs))


def install_fake_servers(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[FakeServer, list[object], FakeServer, list[object]]:
    full_server = FakeServer()
    full_creations: list[object] = []
    offline_server = FakeServer()
    offline_creations: list[object] = []

    def create_full_server() -> FakeServer:
        full_creations.append(object())
        return full_server

    def create_offline() -> FakeServer:
        offline_creations.append(object())
        return offline_server

    monkeypatch.setattr(cli, "create_server", create_full_server)
    monkeypatch.setattr(cli, "create_offline_server", create_offline)
    monkeypatch.setattr(cli, "PROJECT_ENV", Path("/nonexistent/plane-mcp-karval/.env"))
    return full_server, full_creations, offline_server, offline_creations


def test_stdio_keeps_the_existing_transport_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    full_server, full_creations, offline_server, offline_creations = install_fake_servers(
        monkeypatch
    )
    monkeypatch.setattr(sys, "argv", ["plane-mcp-karval", "stdio"])

    cli.main()

    assert len(full_creations) == 1
    assert full_server.run_calls == [((), {"transport": "stdio"})]
    assert offline_creations == []
    assert offline_server.run_calls == []


def test_streamable_http_defaults_to_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    full_server, full_creations, offline_server, offline_creations = install_fake_servers(
        monkeypatch
    )
    monkeypatch.setattr(sys, "argv", ["plane-mcp-karval", "streamable-http"])

    cli.main()

    assert full_creations == []
    assert full_server.run_calls == []
    assert len(offline_creations) == 1
    assert offline_server.run_calls == [
        ((), {"transport": "streamable-http", "host": "127.0.0.1", "port": 8000})
    ]


def test_streamable_http_accepts_an_explicit_loopback_host_and_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    full_server, full_creations, offline_server, offline_creations = install_fake_servers(
        monkeypatch
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["plane-mcp-karval", "streamable-http", "--host", "::1", "--port", "9000"],
    )

    cli.main()

    assert full_creations == []
    assert full_server.run_calls == []
    assert len(offline_creations) == 1
    assert offline_server.run_calls == [
        ((), {"transport": "streamable-http", "host": "::1", "port": 9000})
    ]


@pytest.mark.parametrize("port", ["0", "65536"])
def test_invalid_http_port_rejects_before_server_creation(
    monkeypatch: pytest.MonkeyPatch, port: str
) -> None:
    _, full_creations, _, offline_creations = install_fake_servers(monkeypatch)
    monkeypatch.setattr(
        sys, "argv", ["plane-mcp-karval", "streamable-http", "--port", port]
    )

    with pytest.raises(SystemExit, match="2"):
        cli.main()

    assert full_creations == []
    assert offline_creations == []


@pytest.mark.parametrize("host", ["0.0.0.0", "192.0.2.1", "localhost"])
def test_non_loopback_http_host_rejects_before_server_creation(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    _, full_creations, _, offline_creations = install_fake_servers(monkeypatch)
    monkeypatch.setattr(
        sys, "argv", ["plane-mcp-karval", "streamable-http", "--host", host]
    )

    with pytest.raises(SystemExit, match="2"):
        cli.main()

    assert full_creations == []
    assert offline_creations == []


@pytest.mark.parametrize("option", ["--host", "--port"])
def test_stdio_rejects_http_options_before_server_creation(
    monkeypatch: pytest.MonkeyPatch, option: str
) -> None:
    _, full_creations, _, offline_creations = install_fake_servers(monkeypatch)
    value = "127.0.0.1" if option == "--host" else "9000"
    monkeypatch.setattr(sys, "argv", ["plane-mcp-karval", "stdio", option, value])

    with pytest.raises(SystemExit, match="2"):
        cli.main()

    assert full_creations == []
    assert offline_creations == []


def test_help_explains_that_http_is_offline_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, full_creations, _, offline_creations = install_fake_servers(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["plane-mcp-karval", "--help"])

    with pytest.raises(SystemExit, match="0"):
        cli.main()

    help_text = " ".join(capsys.readouterr().out.split())
    assert "streamable-http is offline-only with no provider access" in help_text
    assert full_creations == []
    assert offline_creations == []
