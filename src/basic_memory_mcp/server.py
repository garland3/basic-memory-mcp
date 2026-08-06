from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from . import store
from .config import ServerConfig, get_config

_DEFAULT_INSTRUCTIONS = (
    "Persistent notebook tools. Topic ids are slash-paths relative to the memory root, "
    "without '.md', e.g. 'projects/atlas-ports'. Always call discover_topics first "
    "to see what is known. read returns the full Markdown file with frontmatter. "
    "write creates or replaces a topic; it overwrites without asking, so use edit "
    "when you only need to change a few lines. edit is exact-string replacement, "
    "modeled on the Claude Code Edit tool: the old string must match exactly, and "
    "it fails if it appears more than once unless replace_all is true. "
    "delete moves topics to a .trash folder and never unlinks them. "
    "Internal wiki links use [[topic-id]]."
)

mcp = FastMCP("basic-memory-mcp", instructions=_DEFAULT_INSTRUCTIONS)


def _as_tool_error(exc: Exception) -> ToolError:
    """Normalize store exceptions to ToolError for the model."""
    if isinstance(exc, ToolError):
        return exc
    return ToolError(str(exc))


@mcp.tool(name="discover_topics", annotations={"readOnlyHint": True})
def discover_topics(
    query: Annotated[str | None, Field(description="Optional case-insensitive substring search across id, title, tags, and body")] = None,
    tag: Annotated[str | None, Field(description="Optional exact tag match")] = None,
    limit: Annotated[int, Field(description="Maximum topics to return", ge=1, le=500)] = 50,
) -> str:
    """List available topics. Call this before read/write/edit/delete.

    Returns a JSON list of {id, title, tags, updated, size, snippet}, newest first.
    If query or tag filters are given, only matching topics are returned. Reports
    when results are truncated by limit.
    """
    cfg = get_config()
    try:
        topics, total = store.discover_topics(cfg.root, query, tag, limit)
    except Exception as exc:
        raise _as_tool_error(exc) from exc

    lines = [json.dumps(topic, ensure_ascii=False) for topic in topics]
    if lines:
        out = "[\n" + ",\n".join(lines) + "\n]"
    else:
        out = "[]"
    if total > limit:
        out += f"\n({total - limit} additional topics omitted due to limit={limit})"
    return out


@mcp.tool(annotations={"readOnlyHint": True})
def read(
    topic_id: Annotated[str, Field(description="Topic id, e.g. 'projects/atlas-ports'")],
) -> str:
    """Read a single topic in full, including its YAML frontmatter.

    Errors with a clear message if the topic does not exist; call discover_topics first.
    """
    cfg = get_config()
    try:
        return store.read_topic(topic_id, cfg.root)
    except Exception as exc:
        raise _as_tool_error(exc) from exc


def write(
    topic_id: Annotated[str, Field(description="Topic id, e.g. 'scratch/2026-08-05-note'")],
    content: Annotated[str, Field(description="Markdown body text (frontmatter is managed by the server)")],
    title: Annotated[str | None, Field(description="Optional display title; defaults to the topic id")] = None,
    tags: Annotated[list[str] | None, Field(description="Optional list of tags")] = None,
) -> str:
    """Create or replace a topic. Overwrites existing topics without asking; use edit for targeted changes.

    Parent folders are created implicitly. Returns the resolved id and whether the topic was created or replaced.
    Rejects content over approximately 1 MB; split large memories into multiple topics.
    """
    cfg = get_config()
    if tags is None:
        tags = []
    try:
        resolved_id, created = store.write_topic(topic_id, content, cfg.root, title=title, tags=tags)
    except Exception as exc:
        raise _as_tool_error(exc) from exc
    action = "created" if created else "replaced"
    return f"{resolved_id}: {action}"


def edit(
    topic_id: Annotated[str, Field(description="Topic id, e.g. 'projects/atlas-ports'")],
    old: Annotated[str, Field(description="Exact text to replace. Must match character-for-character, including whitespace")],
    new: Annotated[str, Field(description="Replacement text")],
    replace_all: Annotated[bool, Field(description="Replace every occurrence of old; default is exactly one occurrence")] = False,
) -> str:
    """Exact-string replacement inside an existing topic, like the Claude Code Edit tool.

    Fails if the topic does not exist. Fails if 'old' is not found. Fails if 'old'
    appears more than once and replace_all is false. Bumps the 'updated' timestamp.
    """
    cfg = get_config()
    try:
        new_text, _ = store.edit_topic(topic_id, old, new, cfg.root, replace_all=replace_all)
    except Exception as exc:
        raise _as_tool_error(exc) from exc

    old_idx = new_text.find(new) if new else 0
    snippet_start = max(0, old_idx - 120)
    snippet_end = min(len(new_text), old_idx + len(new) + 120)
    snippet = new_text[snippet_start:snippet_end]
    return f"{topic_id}: edited\n--- snippet ---\n{snippet}"


def delete(
    topic_id: Annotated[str, Field(description="Topic id, e.g. 'projects/atlas-ports'")],
) -> str:
    """Soft-delete a topic, moving it to <root>/.trash/<timestamp>/<topic-id>.md.

    Returns the trash path so it can be manually restored. Parent folders are
    pruned if empty. Topics are never unlinked.
    """
    cfg = get_config()
    try:
        trash_path = store.delete_topic(topic_id, cfg.root, hard_delete=cfg.hard_delete)
    except Exception as exc:
        raise _as_tool_error(exc) from exc
    return f"{topic_id}: moved to {trash_path.as_posix()}"


_READ_WRITE_TOOLS = {
    "write": write,
    "edit": edit,
    "delete": delete,
}


def register_tools(cfg: ServerConfig | None = None) -> None:
    """Register tools on the FastMCP instance, respecting config flags."""
    if cfg is None:
        cfg = get_config()

    mcp.instructions = _DEFAULT_INSTRUCTIONS

    # Remove conditional tools first so re-registration is idempotent.
    for name in _READ_WRITE_TOOLS:
        try:
            mcp.local_provider.remove_tool(name)
        except Exception:
            pass

    if not cfg.read_only:
        for name, fn in _READ_WRITE_TOOLS.items():
            mcp.tool(fn, name=name, annotations={"destructiveHint": True})


# Register with the default config at import time so the module can be served directly.
# __main__ re-runs this after applying CLI flags.
register_tools()
