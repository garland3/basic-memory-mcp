from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.resources import TextResource
from pydantic import Field

from . import store
from .config import ServerConfig, get_config

# Tracks concrete memory:// resources that have been registered via _refresh_catalog.
# Map: topic_id -> URI string.
_memory_resources: dict[str, str] = {}

# Updated for Phase 2 and retention tools.
_DEFAULT_INSTRUCTIONS = (
    "Persistent notebook tools. Topic ids are slash-paths relative to the memory root, "
    "without '.md', e.g. 'projects/atlas-ports'. Always call discover_topics first "
    "to see what is known, or use related to follow wiki-link relationships. "
    "read returns the full Markdown file with frontmatter. "
    "write creates or replaces a topic; it overwrites without asking, so use edit "
    "when you only need to change a few lines. append adds one fact to the end of an "
    "existing topic, optionally under a heading, and can extend an expiry. "
    "edit is exact-string replacement, modeled on the Claude Code Edit tool: the old "
    "string must match exactly, and it fails if it appears more than once unless "
    "replace_all is true. rename moves a topic and rewrites wiki links that pointed to it. "
    "delete moves topics to a .trash folder and never unlinks them. sweep lists or soft-deletes "
    "expired topics. "
    "write and append accept a retention argument: permanent (default), session/today, week, month, "
    "never, or an ISO-8601 date/time. Expired topics are hidden from discover_topics and priming "
    "by default; pass include_expired=true to see them. "
    "Internal wiki links use [[topic-id]]. "
    "If write content includes frontmatter, the server merges it into its own managed block."
)

# §13.2: in multi-tenant mode the catalog cannot be primed into the shared
# instructions (the tenant is only known per-call), so say so instead.
_MT_INSTRUCTIONS_NOTE = (
    "Multi-tenant mode is on: memories are namespaced per authenticated user. "
    "Every call is scoped to the injected _atlas_user and can only see that "
    "user's topics; call discover_topics to list them."
)

mcp = FastMCP("basic-memory-mcp", instructions=_DEFAULT_INSTRUCTIONS)


# §11.4 catalog priming parameters.
_MAX_PRIME_TOPICS = 50
_MAX_PRIME_BYTES = 4000

# §13.2: trailing parameter on every tool when --multi-tenant is on. The Atlas
# backend fills it from the authenticated session; the model must not supply it.
_AtlasUser = Annotated[
    str | None,
    Field(description="injected by the Atlas backend; do not supply"),
]


def _build_instructions(root: Path) -> str:
    """Compose server instructions, including the most recently updated topic list."""
    catalog, truncated = store.prime_catalog(root, _MAX_PRIME_TOPICS, _MAX_PRIME_BYTES)
    if catalog:
        prime = f"Known topics ({'partial list; truncated' if truncated else 'most recent'}):\n{catalog}"
    else:
        prime = "No memories yet."
    return _DEFAULT_INSTRUCTIONS + "\n\n" + prime


def _register_topic_resources(root: Path) -> None:
    """Expose each topic as a concrete MCP resource at memory://<topic-id>."""
    root = root.resolve()
    current_ids = {
        store._topic_id_from_path(path, root)  # type: ignore[attr-defined]
        for path in store._iter_topics(root)
    }

    # Remove resources for topics that no longer exist.
    for tid, uri in list(_memory_resources.items()):
        if tid not in current_ids:
            try:
                mcp.local_provider.remove_resource(uri)
            except Exception:
                pass
            del _memory_resources[tid]

    # Add resources for new topics.
    for tid in current_ids:
        if tid in _memory_resources:
            continue
        uri = f"memory://{tid}"
        try:
            text = store.read_topic(tid, root)
        except Exception as exc:
            text = f"ERROR: {exc}"
        try:
            mcp.local_provider.add_resource(
                TextResource(
                    uri=uri,
                    name=tid,
                    title=tid,
                    text=text,
                    mime_type="text/markdown",
                )
            )
            _memory_resources[tid] = uri
        except Exception:
            pass


def _refresh_catalog() -> None:
    """Recompute server instructions and enumerate resources after a mutation."""
    try:
        cfg = get_config()
        if cfg.multi_tenant:
            # No global priming or resource registration in multi-tenant mode:
            # the tenant is only known per-call, and a shared catalog or
            # resource list would leak one tenant's topics to another.
            return
        mcp.instructions = _build_instructions(cfg.root)
        _register_topic_resources(cfg.root)
    except Exception:
        pass


def _as_tool_error(exc: Exception) -> ToolError:
    """Normalize store exceptions to ToolError for the model."""
    if isinstance(exc, ToolError):
        return exc
    return ToolError(str(exc))


def _tenant_root(atlas_user: str | None) -> Path:
    """Resolve the calling tenant's memory root, refusing when injection is absent."""
    try:
        return store.tenant_root(get_config().root, atlas_user, multi_tenant=True)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Tool implementations. Each takes an explicit root so the plain tools and the
# multi-tenant (``*_mt``) tools can share one body; the only difference between
# the two families is how the root is resolved.


def _discover_topics_impl(
    root: Path,
    query: str | None,
    tag: str | None,
    limit: int,
    include_expired: bool,
) -> str:
    try:
        topics, total, expired_hidden = store.discover_topics(root, query, tag, limit, include_expired)
    except Exception as exc:
        raise _as_tool_error(exc) from exc

    lines = [json.dumps(topic, ensure_ascii=False) for topic in topics]
    if lines:
        out = "[\n" + ",\n".join(lines) + "\n]"
    else:
        out = "[]"
    extra: list[str] = []
    if total > limit:
        extra.append(f"{total - limit} additional topics omitted due to limit={limit}")
    if expired_hidden:
        extra.append(f"{expired_hidden} expired topics hidden; pass include_expired=true")
    if extra:
        out += "\n(" + "; ".join(extra) + ")"
    return out


def _read_impl(root: Path, topic_id: str) -> str:
    try:
        return store.read_topic(topic_id, root)
    except Exception as exc:
        raise _as_tool_error(exc) from exc


def _related_impl(root: Path, topic_id: str) -> str:
    try:
        result = store.related_topics(topic_id, root)
    except Exception as exc:
        raise _as_tool_error(exc) from exc
    return json.dumps(result, ensure_ascii=False)


def _write_impl(
    root: Path,
    topic_id: str,
    content: str,
    title: str | None,
    tags: list[str] | None,
    retention: str,
) -> str:
    if tags is None:
        tags = []
    try:
        resolved_id, created = store.write_topic(topic_id, content, root, title=title, tags=tags, retention=retention)
    except Exception as exc:
        raise _as_tool_error(exc) from exc
    _refresh_catalog()
    action = "created" if created else "replaced"
    return f"{resolved_id}: {action}"


def _edit_impl(root: Path, topic_id: str, old: str, new: str, replace_all: bool) -> str:
    try:
        new_text, _ = store.edit_topic(topic_id, old, new, root, replace_all=replace_all)
    except Exception as exc:
        raise _as_tool_error(exc) from exc

    _refresh_catalog()
    old_idx = new_text.find(new) if new else 0
    snippet_start = max(0, old_idx - 120)
    snippet_end = min(len(new_text), old_idx + len(new) + 120)
    snippet = new_text[snippet_start:snippet_end]
    return f"{topic_id}: edited\n--- snippet ---\n{snippet}"


def _append_impl(
    root: Path,
    topic_id: str,
    text: str,
    heading: str | None,
    retention: str | None,
) -> str:
    try:
        store.append_topic(topic_id, text, root, heading=heading, retention=retention)
    except Exception as exc:
        raise _as_tool_error(exc) from exc

    _refresh_catalog()
    return f"{topic_id}: appended"


def _rename_impl(root: Path, old_id: str, new_id: str) -> str:
    try:
        result = store.rename_topic(old_id, new_id, root)
    except Exception as exc:
        raise _as_tool_error(exc) from exc

    _refresh_catalog()
    return (
        f"{result['old_id']} -> {result['new_id']}: topic moved; "
        f"{result['link_rewrites']} wiki links rewritten across {result['edited_files']} file(s)"
    )


def _delete_impl(root: Path, topic_id: str) -> str:
    try:
        trash_path = store.delete_topic(topic_id, root, hard_delete=get_config().hard_delete)
    except Exception as exc:
        raise _as_tool_error(exc) from exc

    _refresh_catalog()
    return f"{topic_id}: moved to {trash_path.as_posix()}"


def _sweep_impl(root: Path, dry_run: bool) -> str:
    try:
        expired = store.sweep_expired(root, dry_run=dry_run)
    except Exception as exc:
        raise _as_tool_error(exc) from exc

    if dry_run:
        _refresh_catalog()
        return "No expired topics." if not expired else "\n".join(expired) + "\n(dry run; call sweep(dry_run=False) to delete)"

    _refresh_catalog()
    if not expired:
        return "No expired topics to sweep."
    return "Swept topics:\n" + "\n".join(expired)


# ---------------------------------------------------------------------------
# Plain tools (default). These schemas are exactly the §3 shapes — no
# _atlas_user parameter exists anywhere unless --multi-tenant is on.


def discover_topics(
    query: Annotated[str | None, Field(description="Optional case-insensitive AND search across id, title, tags, and body. Quoted \"exact phrase\" matches as a single term.")] = None,
    tag: Annotated[str | None, Field(description="Optional exact tag match")] = None,
    limit: Annotated[int, Field(description="Maximum topics to return", ge=1, le=500)] = 50,
    include_expired: Annotated[bool, Field(description="Include topics whose expiry has passed")] = False,
) -> str:
    """List available topics. Call this before read/write/edit/delete/append/rename/sweep.

    Returns a JSON list of {id, title, tags, updated, size, snippet, outbound_links,
    expires, match_reason}, newest first. With a query, snippets are centered on the first
    body match. Expired topics are hidden by default; pass include_expired=true to reveal
    them, marked with "expired": true.
    Reports when results are truncated by limit or when expired topics were hidden.
    """
    return _discover_topics_impl(get_config().root, query, tag, limit, include_expired)


def read(
    topic_id: Annotated[str, Field(description="Topic id, e.g. 'projects/atlas-ports'")],
) -> str:
    """Read a single topic in full, including its YAML frontmatter.

    Errors with a clear message if the topic does not exist; call discover_topics first.
    Expired topics are still readable by id.
    """
    return _read_impl(get_config().root, topic_id)


def related(
    topic_id: Annotated[str, Field(description="Topic id whose link graph to report")],
) -> str:
    """Report outbound and inbound wiki-link relationships for a topic.

    Returns {id, outbound, inbound}. outbound lists [[topic-id]] links in this
    topic; inbound lists topics that link to this topic. Errors if the topic
    does not exist.
    """
    return _related_impl(get_config().root, topic_id)


@mcp.resource("memory://{topic_id*}")
def memory_resource(topic_id: str) -> str:
    """Expose each topic as a wildcard MCP resource at memory://<topic-id>."""
    cfg = get_config()
    if cfg.multi_tenant:
        # Resources get no _atlas_user injection, so they cannot be scoped to
        # the caller; refuse rather than expose the flat (or another tenant's)
        # namespace.
        return "ERROR: memory:// resources are disabled in multi-tenant mode; use the read tool instead"
    try:
        return store.read_topic(topic_id, cfg.root)
    except Exception as exc:
        return f"ERROR: {exc}"


def write(
    topic_id: Annotated[str, Field(description="Topic id, e.g. 'scratch/2026-08-05-note'")],
    content: Annotated[str, Field(description="Markdown body text (frontmatter is managed by the server)")],
    title: Annotated[str | None, Field(description="Optional display title; defaults to the topic id")] = None,
    tags: Annotated[list[str] | None, Field(description="Optional list of tags")] = None,
    retention: Annotated[str, Field(description="Expiry: permanent (default), session/today, week, month, never, or an ISO-8601 date/time.")] = "permanent",
) -> str:
    """Create or replace a topic. Overwrites existing topics without asking; use edit for targeted changes.

    Parent folders are created implicitly. Returns the resolved id and whether the topic was created or replaced.
    Rejects content over approximately 1 MB; split large memories into multiple topics.
    Retention controls expiry: permanent means never; session/today means 24 hours; week means 7 days; month means 30 days.
    """
    return _write_impl(get_config().root, topic_id, content, title, tags, retention)


def edit(
    topic_id: Annotated[str, Field(description="Topic id, e.g. 'projects/atlas-ports'")],
    old: Annotated[str, Field(description="Exact text to replace. Must match character-for-character, including whitespace")],
    new: Annotated[str, Field(description="Replacement text")],
    replace_all: Annotated[bool, Field(description="Replace every occurrence of old; default is exactly one occurrence")] = False,
) -> str:
    """Exact-string replacement inside an existing topic, like the Claude Code Edit tool.

    Fails if the topic does not exist. Fails if 'old' is not found. Fails if 'old'
    appears more than once and replace_all is false. Bumps the 'updated' timestamp.
    To change title, tags, or expiry, use write instead.
    """
    return _edit_impl(get_config().root, topic_id, old, new, replace_all)


def append(
    topic_id: Annotated[str, Field(description="Topic id to append to")],
    text: Annotated[str, Field(description="Markdown text to append")],
    heading: Annotated[str | None, Field(description="Optional ## heading under which to append")] = None,
    retention: Annotated[str | None, Field(description="If given, extends the topic's expiry (never shortens it). Same values as write().")] = None,
) -> str:
    """Append text to an existing topic without rewriting the whole body.

    Inserts with exactly one blank line between the old body and the new text.
    If heading is supplied, appends under an existing ## heading or creates it at
    the end. Fails if the topic does not exist. Bumps the 'updated' timestamp.
    A retention argument only extends the current expiry; it never shortens it.
    """
    return _append_impl(get_config().root, topic_id, text, heading, retention)


def rename(
    old_id: Annotated[str, Field(description="Existing topic id to move")],
    new_id: Annotated[str, Field(description="Destination topic id")],
) -> str:
    """Move a topic and rewrite wiki links that pointed to it.

    Also rewrites prefix links: renaming `scratch/` → `archive/scratch/` fixes
    `[[scratch/x]]` links. Refuses if new_id already exists. Returns the count of
    rewritten links so the model knows the blast radius.
    """
    return _rename_impl(get_config().root, old_id, new_id)


def delete(
    topic_id: Annotated[str, Field(description="Topic id, e.g. 'projects/atlas-ports'")],
) -> str:
    """Soft-delete a topic, moving it to <root>/.trash/<timestamp>/<topic-id>.md.

    Returns the trash path so it can be manually restored. Parent folders are
    pruned if empty. Topics are never unlinked.
    """
    return _delete_impl(get_config().root, topic_id)


def sweep(
    dry_run: Annotated[bool, Field(description="When true (default), lists expired topics without moving them.")] = True,
) -> str:
    """List or soft-delete topics whose expiry has passed.

    dry_run=true returns the list without changing anything; the model should
    review it and then call sweep(dry_run=false) to move the topics into .trash.
    """
    return _sweep_impl(get_config().root, dry_run)


# ---------------------------------------------------------------------------
# Multi-tenant tools (--multi-tenant). Identical schemas plus a trailing
# _atlas_user parameter; every call resolves under <root>/<tenant>/ and refuses
# loudly when the backend did not inject a user. Docstrings are shared with the
# plain variants so the model's spec does not drift between modes.


def discover_topics_mt(
    query: Annotated[str | None, Field(description="Optional case-insensitive AND search across id, title, tags, and body. Quoted \"exact phrase\" matches as a single term.")] = None,
    tag: Annotated[str | None, Field(description="Optional exact tag match")] = None,
    limit: Annotated[int, Field(description="Maximum topics to return", ge=1, le=500)] = 50,
    include_expired: Annotated[bool, Field(description="Include topics whose expiry has passed")] = False,
    _atlas_user: _AtlasUser = None,
) -> str:
    return _discover_topics_impl(_tenant_root(_atlas_user), query, tag, limit, include_expired)


def read_mt(
    topic_id: Annotated[str, Field(description="Topic id, e.g. 'projects/atlas-ports'")],
    _atlas_user: _AtlasUser = None,
) -> str:
    return _read_impl(_tenant_root(_atlas_user), topic_id)


def related_mt(
    topic_id: Annotated[str, Field(description="Topic id whose link graph to report")],
    _atlas_user: _AtlasUser = None,
) -> str:
    return _related_impl(_tenant_root(_atlas_user), topic_id)


def write_mt(
    topic_id: Annotated[str, Field(description="Topic id, e.g. 'scratch/2026-08-05-note'")],
    content: Annotated[str, Field(description="Markdown body text (frontmatter is managed by the server)")],
    title: Annotated[str | None, Field(description="Optional display title; defaults to the topic id")] = None,
    tags: Annotated[list[str] | None, Field(description="Optional list of tags")] = None,
    retention: Annotated[str, Field(description="Expiry: permanent (default), session/today, week, month, never, or an ISO-8601 date/time.")] = "permanent",
    _atlas_user: _AtlasUser = None,
) -> str:
    return _write_impl(_tenant_root(_atlas_user), topic_id, content, title, tags, retention)


def edit_mt(
    topic_id: Annotated[str, Field(description="Topic id, e.g. 'projects/atlas-ports'")],
    old: Annotated[str, Field(description="Exact text to replace. Must match character-for-character, including whitespace")],
    new: Annotated[str, Field(description="Replacement text")],
    replace_all: Annotated[bool, Field(description="Replace every occurrence of old; default is exactly one occurrence")] = False,
    _atlas_user: _AtlasUser = None,
) -> str:
    return _edit_impl(_tenant_root(_atlas_user), topic_id, old, new, replace_all)


def append_mt(
    topic_id: Annotated[str, Field(description="Topic id to append to")],
    text: Annotated[str, Field(description="Markdown text to append")],
    heading: Annotated[str | None, Field(description="Optional ## heading under which to append")] = None,
    retention: Annotated[str | None, Field(description="If given, extends the topic's expiry (never shortens it). Same values as write().")] = None,
    _atlas_user: _AtlasUser = None,
) -> str:
    return _append_impl(_tenant_root(_atlas_user), topic_id, text, heading, retention)


def rename_mt(
    old_id: Annotated[str, Field(description="Existing topic id to move")],
    new_id: Annotated[str, Field(description="Destination topic id")],
    _atlas_user: _AtlasUser = None,
) -> str:
    return _rename_impl(_tenant_root(_atlas_user), old_id, new_id)


def delete_mt(
    topic_id: Annotated[str, Field(description="Topic id, e.g. 'projects/atlas-ports'")],
    _atlas_user: _AtlasUser = None,
) -> str:
    return _delete_impl(_tenant_root(_atlas_user), topic_id)


def sweep_mt(
    dry_run: Annotated[bool, Field(description="When true (default), lists expired topics without moving them.")] = True,
    _atlas_user: _AtlasUser = None,
) -> str:
    return _sweep_impl(_tenant_root(_atlas_user), dry_run)


for _mt_fn, _plain_fn in [
    (discover_topics_mt, discover_topics),
    (read_mt, read),
    (related_mt, related),
    (write_mt, write),
    (edit_mt, edit),
    (append_mt, append),
    (rename_mt, rename),
    (delete_mt, delete),
    (sweep_mt, sweep),
]:
    _mt_fn.__doc__ = _plain_fn.__doc__
del _mt_fn, _plain_fn


_READ_ONLY_TOOLS = {"discover_topics", "read", "related"}
_READ_WRITE_TOOLS = {
    "write": write,
    "edit": edit,
    "append": append,
    "rename": rename,
    "delete": delete,
    "sweep": sweep,
}
_READ_WRITE_TOOLS_MT = {
    "write": write_mt,
    "edit": edit_mt,
    "append": append_mt,
    "rename": rename_mt,
    "delete": delete_mt,
    "sweep": sweep_mt,
}
_ALL_TOOL_NAMES = _READ_ONLY_TOOLS | set(_READ_WRITE_TOOLS)


def register_tools(cfg: ServerConfig | None = None) -> None:
    """Register tools on the FastMCP instance, respecting config flags.

    --multi-tenant swaps every tool for a variant whose schema carries a
    trailing _atlas_user parameter (same pattern as --read-only swapping the
    tool set): the flag changes registered schemas, not runtime branching
    inside one schema.
    """
    if cfg is None:
        cfg = get_config()

    if cfg.multi_tenant:
        mcp.instructions = _DEFAULT_INSTRUCTIONS + "\n\n" + _MT_INSTRUCTIONS_NOTE
    else:
        mcp.instructions = _build_instructions(cfg.root)

    # Remove all tools first so re-registration is idempotent.
    for name in _ALL_TOOL_NAMES:
        try:
            mcp.local_provider.remove_tool(name)
        except Exception:
            pass

    if cfg.multi_tenant:
        read_tools = {"discover_topics": discover_topics_mt, "read": read_mt, "related": related_mt}
        write_tools = _READ_WRITE_TOOLS_MT
    else:
        read_tools = {"discover_topics": discover_topics, "read": read, "related": related}
        write_tools = _READ_WRITE_TOOLS

    for name, fn in read_tools.items():
        mcp.tool(fn, name=name, annotations={"readOnlyHint": True})

    if not cfg.read_only:
        for name, fn in write_tools.items():
            mcp.tool(fn, name=name, annotations={"destructiveHint": True})


# Register with the default config at import time so the module can be served directly.
# __main__ re-runs this after applying CLI flags.
register_tools()
