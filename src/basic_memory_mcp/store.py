from __future__ import annotations

import os
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

MAX_TOPIC_BYTES = 1_000_000  # ~1 MB; keeps a bad call from slowing discovery.

_TRASH_DIR = ".trash"


def resolve(topic_id: str, root: Path) -> Path:
    """Return the filesystem path for a topic id, or raise if invalid.

    Topic ids are slash-paths relative to root, without the ``.md`` extension.
    Rejects absolute paths, parent-directory escapes, and symlinks that point
    outside the root.
    """
    if topic_id.startswith("/"):
        raise ValueError(f"topic id must be relative, got absolute path: {topic_id!r}")
    if "\\" in topic_id:
        raise ValueError(f"topic id must use '/' separators, got: {topic_id!r}")
    if any(part == ".." for part in topic_id.split("/")):
        raise ValueError(f"topic id cannot contain '..' : {topic_id!r}")

    # Build the final file path first, then resolve, so symlinks in the leaf are
    # actually followed before the root check.
    path = (root / topic_id).with_suffix(".md")
    resolved = path.resolve()

    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"topic id resolves outside server root: {topic_id!r}") from exc

    return resolved


def _iter_topics(root: Path) -> list[Path]:
    """Return every .md file under root except those inside .trash and root index.md.

    ``index.md`` is a human-maintained table of contents; the server never
    treats it as a topic.
    """
    results: list[Path] = []
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != _TRASH_DIR and not d.startswith(".")]
        for name in filenames:
            if not name.endswith(".md"):
                continue
            path = Path(dirpath) / name
            try:
                rel = path.relative_to(root)
            except ValueError:
                continue
            if str(rel) == "index.md":
                continue
            # Skip hidden directories cleanly by checking resolved path.
            results.append(path)
    return results


def _split_frontmatter(text: str) -> tuple[str, str] | None:
    """Split ``text`` into (frontmatter_block, body). Returns None if no frontmatter.

    Only the classic YAML frontmatter fence ``---`` at the very start is recognized.
    """
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end == -1:
        return None
    return text[4:end], text[end + 5 :]


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Parse YAML frontmatter, returning an empty dict on malformed data.

    Missing fields are treated as empty rather than raising.
    """
    if not text.strip():
        return {}
    try:
        return yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return {}


def _format_frontmatter(data: dict[str, Any]) -> str:
    """Dump frontmatter as YAML with a trailing ``---`` separator."""
    return "---\n" + yaml.safe_dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True) + "---\n"


def _format_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_tags(value: Any) -> list[str]:
    """Coerce a tag value (str, list, None) into a list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    if isinstance(value, list):
        return [str(p).strip() for p in value if str(p).strip()]
    return []


def read_topic(topic_id: str, root: Path) -> str:
    """Return the full raw content of a topic file."""
    path = resolve(topic_id, root)
    if not path.is_file():
        raise FileNotFoundError(f"no such topic: {topic_id!r}; call discover_topics first")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError as exc:
        raise OSError(f"failed to read topic {topic_id!r}: {exc}") from exc


def _decode_file(path: Path) -> tuple[dict[str, Any], str]:
    """Return (frontmatter dict, body) for a file, handling missing frontmatter."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    split = _split_frontmatter(text)
    if split is None:
        return {}, text
    fm, body = split
    return _parse_frontmatter(fm), body


def load_topic(topic_id: str, root: Path) -> dict[str, Any]:
    """Return structured metadata and body for a topic.

    Keys: id, title, tags, created, updated, body, size, outbound_links.
    Missing frontmatter fields are filled with sensible empties.
    """
    path = resolve(topic_id, root)
    if not path.is_file():
        raise FileNotFoundError(f"no such topic: {topic_id!r}; call discover_topics first")

    fm, body = _decode_file(path)
    stat = path.stat()
    mtime_utc = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)

    created = fm.get("created") or _format_iso(mtime_utc)
    updated = fm.get("updated") or _format_iso(mtime_utc)

    return {
        "id": topic_id,
        "title": fm.get("title") or topic_id,
        "tags": _parse_tags(fm.get("tags")),
        "created": created,
        "updated": updated,
        "body": body,
        "size": stat.st_size,
        "outbound_links": parse_wiki_links(body),
    }


def parse_wiki_links(body: str) -> list[str]:
    """Return ``[[topic-id]]`` links found in body text."""
    return re.findall(r"\[\[([^\[\]]+)\]]", body)


def _topic_id_from_path(path: Path, root: Path) -> str:
    """Convert a path back to its slash-style topic id without ``.md`` suffix."""
    rel = path.relative_to(root.resolve())
    return rel.with_suffix("").as_posix()


def _strip_markdown_links(text: str) -> str:
    """Remove wiki-link brackets so searches match the raw text inside them."""
    return re.sub(r"\[\[([^\[\]]+)\]]", r"\1", text)


def discover_topics(
    root: Path,
    query: str | None = None,
    tag: str | None = None,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], int]:
    """List topics, optionally filtered.

    Returns ``(topics, total_before_limit)`` so the caller can report truncation.
    Each topic dict contains: id, title, tags, updated, size, snippet.
    """
    root = root.resolve()
    query = query.strip().lower() if query else None
    tag = tag.strip().lower() if tag else None

    all_paths = sorted(_iter_topics(root), key=lambda p: p.stat().st_mtime, reverse=True)
    matches: list[tuple[bool, dict[str, Any]]] = []  # (title_or_id_hit, topic)

    for path in all_paths:
        topic_id = _topic_id_from_path(path, root)
        fm, body = _decode_file(path)
        title = (fm.get("title") or topic_id)
        tags = _parse_tags(fm.get("tags"))
        updated = fm.get("updated") or fm.get("created") or _format_iso(datetime.utcfromtimestamp(path.stat().st_mtime))

        tag_match = True
        if tag is not None:
            tag_match = tag in [t.lower() for t in tags]

        query_match = True
        title_or_id_hit = False
        if query is not None:
            body_text = (title + "\n" + _strip_markdown_links(body)).lower()
            in_title_or_id = query in topic_id.lower() or query in title.lower()
            in_tags = any(query in t.lower() for t in tags)
            in_body = query in body_text
            query_match = in_title_or_id or in_tags or in_body
            title_or_id_hit = in_title_or_id or in_tags

        if not (tag_match and query_match):
            continue

        # Snippet is first ~200 bytes of body, normalized.
        snippet = body.lstrip().replace("\n", " ")
        if len(snippet.encode("utf-8")) > 200:
            while len(snippet.encode("utf-8")) > 200 and len(snippet) > 1:
                snippet = snippet[:-1]
            snippet = snippet.rstrip() + "…"

        matches.append((
            title_or_id_hit,
            {
                "id": topic_id,
                "title": title,
                "tags": tags,
                "updated": updated,
                "size": path.stat().st_size,
                "snippet": snippet,
            },
        ))

    # Sort: title/id hits first (True < False), then by updated-time descending.
    # Reversing both fields puts newest first and keeps hits above non-hits.
    matches.sort(key=lambda t: (not t[0], t[1]["updated"]))
    matches.reverse()
    topics = [m[1] for m in matches]
    total = len(topics)
    return topics[:limit], total


def write_topic(
    topic_id: str,
    content: str,
    root: Path,
    title: str | None = None,
    tags: list[str] | None = None,
) -> tuple[str, bool]:
    """Create or replace a topic.

    Returns ``(resolved_id, created)`` where ``created`` is True when the topic did
    not previously exist.
    """
    path = resolve(topic_id, root)
    now = _format_iso(datetime.now(timezone.utc))
    created: str | None = None

    byte_count = len(content.encode("utf-8"))
    if byte_count > MAX_TOPIC_BYTES:
        raise ValueError(
            f"topic {topic_id!r} is {byte_count} bytes, exceeding the {MAX_TOPIC_BYTES} byte limit; "
            "split it across multiple topics."
        )

    existed = path.exists()
    if existed:
        existing_fm, _ = _decode_file(path)
        created = existing_fm.get("created")

    if title is None:
        title = topic_id
    if tags is None:
        tags = []

    fm = {
        "title": title,
        "tags": tags,
        "created": created or now,
        "updated": now,
    }

    body = content.rstrip() + ("\n" if content and not content.endswith("\n") else "")
    full_text = _format_frontmatter(fm) + body

    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, full_text)

    return _topic_id_from_path(path, root), not existed


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically via a temp file and ``os.replace``."""
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise


def edit_topic(
    topic_id: str,
    old: str,
    new: str,
    root: Path,
    replace_all: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Exact-string edit of a topic's body, then update ``updated`` timestamp.

    Frontmatter is preserved; to change title or tags, use ``write_topic`` instead.
    Returns ``(full_new_text, frontmatter_dict)``.
    """
    path = resolve(topic_id, root)
    if not path.is_file():
        raise FileNotFoundError(f"no such topic: {topic_id!r}; call discover_topics first")

    if old == new:
        raise ValueError("old and new strings are identical; no change requested")

    with open(path, "r", encoding="utf-8", newline="") as fh:
        full_text = fh.read()

    split = _split_frontmatter(full_text)
    if split is None:
        fm: dict[str, Any] = {}
        body = full_text
    else:
        fm_text, body = split
        fm = _parse_frontmatter(fm_text)

    count = body.count(old)
    if count == 0:
        raise ValueError(f"old string not found in topic {topic_id!r}")
    if count > 1 and not replace_all:
        raise ValueError(
            f"found {count} occurrences of old string in topic {topic_id!r}; "
            "add more surrounding context or set replace_all=True"
        )

    new_body = body.replace(old, new, -1 if replace_all else 1)

    fm["updated"] = _format_iso(datetime.now(timezone.utc))
    result = _format_frontmatter(fm) + new_body

    _atomic_write(path, result)
    return result, fm


def delete_topic(topic_id: str, root: Path, hard_delete: bool = False) -> Path:
    """Soft-delete a topic into ``<root>/.trash/<timestamp>/<topic-id>.md``.

    Prunes now-empty parent folders. With ``hard_delete=True`` it unlinks the file
    directly (test root escape hatch only).
    """
    path = resolve(topic_id, root)
    if not path.is_file():
        raise FileNotFoundError(f"no such topic: {topic_id!r}; call discover_topics first")

    if hard_delete:
        path.unlink()
        _prune_empty_parents(path.parent, root)
        return path

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    trash_base = root.resolve() / _TRASH_DIR / timestamp
    rel = path.relative_to(root.resolve())
    trash_path = trash_base / rel
    trash_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(trash_path))

    _prune_empty_parents(path.parent, root)
    return trash_path


def _prune_empty_parents(start: Path, root: Path) -> None:
    """Remove empty directories from start up to (but not including) root."""
    root = root.resolve()
    current = start.resolve()
    while current != root:
        if current.exists() and current.is_dir() and not any(current.iterdir()):
            current.rmdir()
            current = current.parent
        else:
            break
