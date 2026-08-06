from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class ServerConfig:
    root: Path
    read_only: bool = False
    hard_delete: bool = False
    host: str = "127.0.0.1"
    port: int = 8101


# Module-level singleton set by __main__.py and read by the server.
_config: ServerConfig | None = None


def set_config(cfg: ServerConfig) -> None:
    global _config
    _config = cfg


def get_config() -> ServerConfig:
    if _config is None:
        # Provide a safe import-time default so the module can be inspected or
        # served directly. Production/__main__ overrides this before handling requests.
        return ServerConfig(root=Path.cwd().resolve())
    return _config
