from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .config import ServerConfig, set_config
from .server import mcp, register_tools


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser().resolve() if value else default


def main() -> None:
    default_root = Path("~/ATLAS-GROUP/basic-memory/memory").expanduser().resolve()
    defaults = {
        "root": _env_path("BASIC_MEMORY_ROOT", default_root),
        "host": os.environ.get("BASIC_MEMORY_HOST", "127.0.0.1"),
        "port": _env_int("BASIC_MEMORY_PORT", 8101),
        "read_only": _env_bool("BASIC_MEMORY_READ_ONLY", False),
        "hard_delete": _env_bool("BASIC_MEMORY_HARD_DELETE", False),
    }

    parser = argparse.ArgumentParser(
        prog="basic-memory-mcp",
        description="MCP server that gives a model a persistent notebook backed by a folder of Markdown files.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=defaults["root"],
        help="Memory root directory (created if missing)",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Disable write/edit/delete tools",
    )
    parser.add_argument(
        "--hard-delete",
        action="store_true",
        help="Permanently delete files instead of moving to .trash (test roots only)",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=defaults["host"],
        help="HTTP bind address",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=defaults["port"],
        help="HTTP bind port",
    )
    parser.add_argument(
        "--transport",
        choices=("http", "stdio"),
        default=os.environ.get("BASIC_MEMORY_TRANSPORT", "stdio"),
        help="Transport to use: 'stdio' (default) or 'http'",
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="Shorthand for --transport http",
    )
    parser.add_argument(
        "--stdio",
        action="store_true",
        help="Shorthand for --transport stdio",
    )

    args = parser.parse_args()

    transport = "http" if args.http else ("stdio" if args.stdio else args.transport)
    if transport not in ("http", "stdio"):
        parser.error(f"invalid transport {transport!r} (expected 'http' or 'stdio')")

    root = args.root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    cfg = ServerConfig(
        root=root,
        read_only=defaults["read_only"] or args.read_only,
        hard_delete=defaults["hard_delete"] or args.hard_delete,
        host=args.host,
        port=args.port,
    )

    set_config(cfg)
    register_tools(cfg)

    if transport == "http":
        mcp.run(transport="http", host=cfg.host, port=cfg.port)
    else:
        mcp.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
