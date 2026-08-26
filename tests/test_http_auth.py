from __future__ import annotations

import sys

import pytest

import basic_memory_mcp.__main__ as cli


def test_build_auth_uses_atlas_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "DEBUG", False)
    monkeypatch.setenv("ATLAS_MCP_TOKEN", "test-token")

    auth = cli._build_auth()

    assert auth is not None
    assert auth.tokens == {
        "test-token": {
            "user_id": "atlas-ui",
            "client_id": "atlas-ui-backend",
            "scopes": ["read", "write"],
        }
    }


def test_build_auth_requires_atlas_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "DEBUG", False)
    monkeypatch.delenv("ATLAS_MCP_TOKEN", raising=False)

    with pytest.raises(ValueError, match="ATLAS_MCP_TOKEN environment variable is not set"):
        cli._build_auth()


def test_build_auth_is_disabled_in_debug_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "DEBUG", True)
    monkeypatch.delenv("ATLAS_MCP_TOKEN", raising=False)

    assert cli._build_auth() is None


def test_stdio_does_not_require_atlas_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    run_calls = []
    monkeypatch.setattr(cli, "DEBUG", False)
    monkeypatch.delenv("ATLAS_MCP_TOKEN", raising=False)
    monkeypatch.setattr(sys, "argv", ["basic-memory-mcp", "--stdio", "--root", str(tmp_path)])
    monkeypatch.setattr(cli.mcp, "run", lambda **kwargs: run_calls.append(kwargs))

    cli.main()

    assert run_calls == [{"transport": "stdio", "show_banner": False}]


def test_http_enables_static_token_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    run_calls = []
    monkeypatch.setattr(cli, "DEBUG", False)
    monkeypatch.setenv("ATLAS_MCP_TOKEN", "test-token")
    monkeypatch.setattr(
        sys,
        "argv",
        ["basic-memory-mcp", "--http", "--root", str(tmp_path)],
    )
    monkeypatch.setattr(cli.mcp, "run", lambda **kwargs: run_calls.append(kwargs))

    cli.main()

    assert cli.mcp.auth is not None
    assert cli.mcp.auth.tokens["test-token"]["client_id"] == "atlas-ui-backend"
    assert run_calls == [{"transport": "http", "host": "127.0.0.1", "port": 8101}]
