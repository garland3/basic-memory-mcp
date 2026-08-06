from __future__ import annotations

import os
import re
import shutil
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

MAX_TOPIC_BYTES = 1_000_000  # ~1 MB; keeps a bad call from slowing discovery.

_TRASH_DIR = ".trash"
_SERVER_KEYS = {"created", "updated"}
_EXPIRES_NEVER = "never"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def resolve(topic_id: str, root: Path) -> Path:
    """Return the filesystem path for a topic id, or raise if invalid.

    Topic ids are slash-paths relative to root, without the ``.md`` extension.
    Rejects absolute paths, parent-directory escapes, the reserved ``.trash``
    namespace, and symlinks that point outside the root.
    """
    if topic_id.startswith("/"):
        raise ValueError(f"topic id must be relative, got absolute path: {topic_id!r}")
    if "\\" in topic_id:
        raise ValueError(f"topic id must use '/' separators, got: {topic_id!r}")
    if any(part == ".." for part in topic_id.split("/")):
        raise ValueError(f"topic id cannot contain '..' : {topic_id!r}")
    if any(part == _TRASH_DIR for part in topic_id.split("/")):
        raise ValueError(f"{_TRASH_DIR!r} is reserved and not a valid topic id")

    # Build the final file path without swallowing existing dots. Using
    # ``with_suffix`` would turn ``atlas-v1.2-config`` into ``atlas-v1.md``.
    raw = root / topic_id
    path = raw.parent / (raw.name + ".md")
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
    normalized = {k: _normalize_fm_value(v) for k, v in data.items()}
    return "---\n" + yaml.safe_dump(normalized, default_flow_style=False, sort_keys=False, allow_unicode=True) + "---\n"


def _format_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_fm_value(value: Any) -> Any:
    """Convert datetime/date objects to stable strings; leave other values alone."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return _format_iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, list):
        return [_normalize_fm_value(v) for v in value]
    return value


def _parse_tags(value: Any) -> list[str]:
    """Coerce a tag value (str, list, None) into a list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    if isinstance(value, list):
        return [str(p).strip() for p in value if str(p).strip()]
    return []


def _format_expires(dt: datetime | None) -> str:
    """Serialize an expiry value for frontmatter storage."""
    if dt is None:
        return _EXPIRES_NEVER
    return _format_iso(dt)


def parse_expires(value: Any) -> datetime | None:
    """Return a UTC datetime for an expiry value, or None for permanent/malformed.

    ``None``, the string ``never``, and unparseable values all mean "permanent".
    ISO-8601 dates are treated as midnight UTC on that day.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    s = str(value).strip()
    low = s.lower()
    if low in ("", "never", "permanent", "none", "null", "false"):
        return None
    if low.endswith("z"):
        s = low[:-1] + "+00:00"
    else:
        s = low
    if "t" in s or " " in s:
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            return None
    try:
        d = date.fromisoformat(s)
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_retention(value: str | None, now: datetime | None = None) -> datetime | None:
    """Convert a retention argument to an absolute UTC expiry.

    Returns ``None`` when the retention means "permanent" or "never".
    """
    if value is None:
        return None
    now = now or _now_utc()
    v = value.strip().lower()
    mapping = {
        "session": timedelta(days=1),
        "today": timedelta(days=1),
        "week": timedelta(days=7),
        "month": timedelta(days=30),
        "permanent": None,
        "never": None,
    }
    if v in mapping:
        delta = mapping[v]
        return None if delta is None else now + delta
    parsed = parse_expires(value)
    if parsed is None:
        raise ValueError(
            f"invalid retention value {value!r}; expected one of "
            "permanent, session, today, week, month, never, or an ISO-8601 date/datetime"
        )
    return parsed


def _decode_file(path: Path) -> tuple[dict[str, Any], str]:
    """Return (frontmatter dict, body) for a file, handling missing frontmatter."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    split = _split_frontmatter(text)
    if split is None:
        return {}, text
    fm, body = split
    return _parse_frontmatter(fm), body


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


def load_topic(topic_id: str, root: Path) -> dict[str, Any]:
    """Return structured metadata and body for a topic.

    Keys: id, title, tags, created, updated, body, size, outbound_links, expires.
    Missing frontmatter fields are filled with sensible empties.
    """
    path = resolve(topic_id, root)
    if not path.is_file():
        raise FileNotFoundError(f"no such topic: {topic_id!r}; call discover_topics first")

    fm, body = _decode_file(path)
    stat = path.stat()
    mtime_utc = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)

    created = _normalize_fm_value(fm.get("created")) or _format_iso(mtime_utc)
    updated = _normalize_fm_value(fm.get("updated")) or _format_iso(mtime_utc)

    topic: dict[str, Any] = {
        "id": topic_id,
        "title": fm.get("title") or topic_id,
        "tags": _parse_tags(fm.get("tags")),
        "created": created,
        "updated": updated,
        "body": body,
        "size": stat.st_size,
        "outbound_links": parse_wiki_links(body),
    }
    if "expires" in fm:
        topic["expires"] = _normalize_fm_value(fm.get("expires"))
    return topic


def parse_wiki_links(body: str) -> list[str]:
    """Return link-target ids from ``[[topic-id]]`` / ``[[topic-id|alias]]`` in body text."""
    return [m.group(1) for m in re.finditer(r"\[\[([^\[\]|]+)(?:\|[^\[\]|]*)?\]\]", body)]


def _topic_id_from_path(path: Path, root: Path) -> str:
    """Convert a path back to its slash-style topic id without ``.md`` suffix."""
    rel = path.relative_to(root.resolve())
    return rel.with_suffix("").as_posix()


def _strip_markdown_links(text: str) -> str:
    """Remove wiki-link brackets so searches match the raw text inside them."""
    return re.sub(r"\[\[([^\[]+)\]\]", r"\1", text)


def _extract_content_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """If a model supplies content with a leading ``---`` block, parse it and
    return the remaining body so the server can write a single merged frontmatter
    block instead of two.
    """
    split = _split_frontmatter(content)
    if split is None:
        return {}, content
    fm_text, body = split
    return _parse_frontmatter(fm_text), body


# ---------------------------------------------------------------------------
# Query parsing and snippet helpers (§11.3, §11.5)


def _query_terms(query: str | None) -> tuple[list[str], bool]:
    """Split a query into terms.

    Returns ``(terms, phrase)`` where ``phrase`` is true when the query is a
    single quoted string that should be matched exactly.
    """
    if not query:
        return [], False
    q = query.strip()
    if len(q) >= 2 and q.startswith('"') and q.endswith('"') and q.count('"') == 2:
        return [q[1:-1]], True
    return [t for t in q.split() if t], False


def _term_matches(topic_id: str, title: str, tags: list[str], body: str, term: str) -> dict[str, bool]:
    """Return a map of where ``term`` appears in the topic fields."""
    t = term.lower()
    in_id = t in topic_id.lower()
    in_title = t in title.lower()
    in_tags = any(t in tag.lower() for tag in tags)
    in_body = t in body.lower()
    return {
        "id": in_id,
        "title": in_title,
        "tags": in_tags,
        "body": in_body,
        "any": in_id or in_title or in_tags or in_body,
    }


def _head_snippet(body: str) -> str:
    """Return a snippet from the start of the body, capped at ~200 bytes."""
    text = body.lstrip().replace("\n", " ")
    if len(text.encode("utf-8")) > 200:
        while len(text.encode("utf-8")) > 200 and len(text) > 1:
            text = text[:-1]
        text = text.rstrip() + "…"
    return text


def _centered_snippet_around(body: str, term: str, side_chars: int = 80) -> str:
    """Return a snippet centered on the first occurrence of ``term`` in ``body``."""
    lower = body.lower()
    t = term.lower()
    pos = lower.find(t)
    if pos == -1:
        return _head_snippet(body)
    start = max(0, pos - side_chars)
    end = min(len(body), pos + len(term) + side_chars)
    snippet = body[start:end]
    if start > 0:
        snippet = "…" + snippet.lstrip()
    if end < len(body):
        snippet = snippet.rstrip() + "…"
    return snippet.replace("\n", " ")


# ---------------------------------------------------------------------------
# Discovery and search


def _expiry_rank(expires_dt: datetime | None, now: datetime) -> int:
    """Return a sort rank for expiry: permanent=2, finite=1, expired=0."""
    if expires_dt is None:
        return 2
    if expires_dt < now:
        return 0
    return 1


def discover_topics(
    root: Path,
    query: str | None = None,
    tag: str | None = None,
    limit: int = 50,
    include_expired: bool = False,
) -> tuple[list[dict[str, Any]], int, int]:
    """List topics, optionally filtered.

    Returns ``(topics, total_visible, expired_hidden)`` so the caller can report
    truncation and hidden expired topics. Each topic dict contains: id, title,
    tags, updated, size, snippet, outbound_links, expires (when present), and
    match_reason (when a query is supplied).
    """
    root = root.resolve()
    now = _now_utc()
    terms, phrase = _query_terms(query)
    tag_lower = tag.strip().lower() if tag else None

    # Recent-first at the filesystem level; final sort applies ranking on top.
    all_paths = sorted(_iter_topics(root), key=lambda p: p.stat().st_mtime, reverse=True)
    matches: list[tuple[tuple[int, int, int, str], dict[str, Any]]] = []
    expired_hidden = 0

    for path in all_paths:
        topic_id = _topic_id_from_path(path, root)
        fm, body = _decode_file(path)
        title = fm.get("title") or topic_id
        tags = _parse_tags(fm.get("tags"))
        updated = (
            _normalize_fm_value(fm.get("updated"))
            or _normalize_fm_value(fm.get("created"))
            or _format_iso(datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc))
        )
        expires_raw = fm.get("expires")
        expires_dt = parse_expires(expires_raw)

        tag_ok = True
        if tag_lower is not None:
            tag_ok = tag_lower in [t.lower() for t in tags]

        # Multi-term AND search; a quoted phrase is treated as a single term.
        term_results: list[dict[str, bool]] = []
        if terms:
            term_results = [_term_matches(topic_id, title, tags, body, term) for term in terms]
            if phrase:
                all_terms_present = term_results[0]["any"]
            else:
                all_terms_present = all(r["any"] for r in term_results)
        else:
            all_terms_present = True

        if not (tag_ok and all_terms_present):
            continue

        expired = expires_dt is not None and expires_dt < now
        if expired and not include_expired:
            expired_hidden += 1
            continue

        if terms:
            distinct_matched = sum(1 for r in term_results if r["any"])
            title_id_tag_hit = any(r["id"] or r["title"] or r["tags"] for r in term_results)
            body_hit = any(r["body"] for r in term_results)
            if body_hit:
                # Center the snippet on the first body match of any term.
                body_lower = body.lower()
                positions = [(body_lower.find(term.lower()), term) for term in terms]
                positions = [(idx, term) for idx, term in positions if idx != -1]
                if positions:
                    _, chosen_term = min(positions, key=lambda x: x[0])
                    snippet = _centered_snippet_around(body, chosen_term)
                else:
                    snippet = _head_snippet(body)
            else:
                snippet = _head_snippet(body)
            match_reason = "title/id/tag" if title_id_tag_hit and not body_hit else "body"
            rank = (distinct_matched, int(title_id_tag_hit), _expiry_rank(expires_dt, now), updated)
        else:
            snippet = _head_snippet(body)
            match_reason = None
            rank = (0, 0, _expiry_rank(expires_dt, now), updated)

        topic = {
            "id": topic_id,
            "title": title,
            "tags": tags,
            "updated": updated,
            "size": path.stat().st_size,
            "snippet": snippet,
            "outbound_links": parse_wiki_links(body),
        }
        if expires_raw is not None:
            topic["expires"] = _normalize_fm_value(expires_raw)
        if include_expired and expired:
            topic["expired"] = True
        if match_reason:
            topic["match_reason"] = match_reason

        matches.append((rank, topic))

    # Rank by: distinct terms matched (desc), title/id/tag hit before body-only,
    # expiry permanence (desc), updated (desc).
    matches.sort(key=lambda t: t[0], reverse=True)
    topics = [m[1] for m in matches]
    total_visible = len(topics)
    return topics[:limit], total_visible, expired_hidden


# ---------------------------------------------------------------------------
# Writing and editing


def write_topic(
    topic_id: str,
    content: str,
    root: Path,
    title: str | None = None,
    tags: list[str] | None = None,
    retention: str = "permanent",
) -> tuple[str, bool]:
    """Create or replace a topic.

    Returns ``(resolved_id, created)`` where ``created`` is True when the topic did
    not previously exist.
    """
    path = resolve(topic_id, root)
    now = _format_iso(_now_utc())
    expires_dt = parse_retention(retention)

    content_fm, body = _extract_content_frontmatter(content)
    # For size guard, count only the body the server will actually store.
    byte_count = len(body.encode("utf-8"))
    if byte_count > MAX_TOPIC_BYTES:
        raise ValueError(
            f"topic {topic_id!r} body is {byte_count} bytes, exceeding the {MAX_TOPIC_BYTES} byte limit; "
            "split it across multiple topics."
        )

    existed = path.exists()
    existing_fm: dict[str, Any] = {}
    created: str | None = None
    if existed:
        existing_fm, _ = _decode_file(path)
        created = _normalize_fm_value(existing_fm.get("created"))

    # Precedence: explicit tool args > content frontmatter > existing frontmatter > defaults.
    resolved_title = title
    if resolved_title is None:
        resolved_title = content_fm.get("title") or existing_fm.get("title")
    if not resolved_title:
        resolved_title = topic_id

    resolved_tags: list[str]
    if tags is not None:
        resolved_tags = tags
    elif content_fm.get("tags") is not None:
        resolved_tags = _parse_tags(content_fm.get("tags"))
    elif existing_fm.get("tags") is not None:
        resolved_tags = _parse_tags(existing_fm.get("tags"))
    else:
        resolved_tags = []

    # Preserve custom frontmatter keys from both existing and content, with content overriding.
    owned = _SERVER_KEYS | {"title", "tags", "expires"}
    new_fm: dict[str, Any] = {}
    for k, v in existing_fm.items():
        if k not in owned:
            new_fm[k] = v
    for k, v in content_fm.items():
        if k not in owned:
            new_fm[k] = v

    new_fm["title"] = resolved_title
    new_fm["tags"] = resolved_tags
    new_fm["created"] = created or now
    new_fm["updated"] = now
    new_fm["expires"] = _format_expires(expires_dt)

    body = body.rstrip() + ("\n" if body and not body.endswith("\n") else "")
    full_text = _format_frontmatter(new_fm) + body

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

    Frontmatter is preserved; to change title, tags, or expiry use ``write_topic`` instead.
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

    fm["updated"] = _format_iso(_now_utc())
    # Ensure title exists so a previously frontmatter-less file gets a valid header.
    if "title" not in fm:
        fm["title"] = topic_id
    result = _format_frontmatter(fm) + new_body

    _atomic_write(path, result)
    return result, fm


# ---------------------------------------------------------------------------
# §11.1 append


def _append_text_to_body(body: str, text: str, heading: str | None = None) -> str:
    """Append ``text`` to ``body`` with exactly one blank line before it.

    If ``heading`` is supplied and a matching ``## heading`` exists, append under
    that section; otherwise create the heading at the end of the body.
    """
    text = text.rstrip() + "\n"

    if heading:
        pattern = re.compile(rf"^##\s*{re.escape(heading)}\s*$", re.MULTILINE | re.IGNORECASE)
        match = pattern.search(body)
        if match:
            # Move past the heading line itself so we insert after it.
            section_end = match.end()
            if section_end < len(body) and body[section_end] == "\n":
                section_end += 1
            next_heading = re.search(r"^(?:#{1,2})\s", body[section_end:], re.MULTILINE)
            if next_heading:
                section_end += next_heading.start()
            else:
                section_end = len(body)
            prefix = body[:section_end].rstrip()
            suffix = body[section_end:]
            # Ensure a blank line before the following heading.
            if suffix and re.match(r"^#+\s", suffix.lstrip("\n"), re.MULTILINE):
                suffix = "\n" + suffix.lstrip("\n")
            return prefix + "\n\n" + text + suffix
        else:
            prefix = body.rstrip()
            gap = "\n\n" if prefix else ""
            return prefix + gap + f"## {heading}\n\n" + text

    prefix = body.rstrip()
    gap = "\n\n" if prefix else ""
    return prefix + gap + text


def append_topic(
    topic_id: str,
    text: str,
    root: Path,
    heading: str | None = None,
    retention: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Append ``text`` to an existing topic's body.

    Refuses to create a missing topic. Bumps the ``updated`` timestamp. The optional
    ``heading`` appends under an existing ``## heading`` or creates it at the end.
    A ``retention`` argument extends an existing expiry but never shortens it.
    """
    path = resolve(topic_id, root)
    if not path.is_file():
        raise FileNotFoundError(f"no such topic: {topic_id!r}; call discover_topics first")

    fm, body = _decode_file(path)
    now_dt = _now_utc()
    now = _format_iso(now_dt)
    created = _normalize_fm_value(fm.get("created")) or _format_iso(
        datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    )
    existing_expires_raw = fm.get("expires")
    existing_expires_dt = parse_expires(existing_expires_raw)

    # Preserve custom frontmatter keys while composing the managed block.
    owned = _SERVER_KEYS | {"title", "tags", "expires"}
    new_fm: dict[str, Any] = {k: v for k, v in fm.items() if k not in owned}
    new_fm["title"] = fm.get("title") or topic_id
    new_fm["tags"] = _parse_tags(fm.get("tags"))
    new_fm["created"] = created
    new_fm["updated"] = now

    if retention is not None:
        requested_dt = parse_retention(retention, now_dt)
        if requested_dt is None:
            # Explicitly permanent: extends any finite expiry to never.
            new_fm["expires"] = _EXPIRES_NEVER
        elif existing_expires_dt is not None and requested_dt > existing_expires_dt:
            new_fm["expires"] = _format_iso(requested_dt)
        # Otherwise keep the existing (longer) expiry by copying below.

    if "expires" not in new_fm and existing_expires_raw is not None:
        new_fm["expires"] = existing_expires_raw

    new_body = _append_text_to_body(body, text, heading)
    if len((_format_frontmatter(new_fm) + new_body).encode("utf-8")) > MAX_TOPIC_BYTES:
        raise ValueError(
            f"appending to {topic_id!r} would exceed the {MAX_TOPIC_BYTES} byte limit; "
            "split it across multiple topics."
        )

    result = _format_frontmatter(new_fm) + new_body
    _atomic_write(path, result)
    return result, new_fm


# ---------------------------------------------------------------------------
# §11.2 related and rename


def related_topics(topic_id: str, root: Path) -> dict[str, Any]:
    """Return outbound and inbound wiki-link relationships for a topic."""
    path = resolve(topic_id, root)
    if not path.is_file():
        raise FileNotFoundError(f"no such topic: {topic_id!r}; call discover_topics first")

    fm, body = _decode_file(path)
    outbound = parse_wiki_links(body)
    inbound: list[str] = []
    root = root.resolve()
    target_id = topic_id

    for other_path in _iter_topics(root):
        if other_path == path:
            continue
        other_id = _topic_id_from_path(other_path, root)
        _, other_body = _decode_file(other_path)
        if target_id in parse_wiki_links(other_body):
            inbound.append(other_id)

    return {
        "id": topic_id,
        "outbound": outbound,
        "inbound": inbound,
    }


def _rewrite_wiki_links(body: str, old_id: str, new_id: str) -> tuple[str, int]:
    """Rewrite ``[[old_id]]`` and ``[[old_id/...]]`` links to use ``new_id``."""
    pattern = re.compile(rf"\[\[{re.escape(old_id)}(?:/([^\[\]|]*))?(?:\|([^\[\]]*))?\]\]")

    def repl(match: re.Match) -> str:
        rest = match.group(1)
        alias = match.group(2)
        target = new_id if rest is None else f"{new_id}/{rest}"
        if alias is not None:
            return f"[[{target}|{alias}]]"
        return f"[[{target}]]"

    return pattern.subn(repl, body)


def rename_topic(old_id: str, new_id: str, root: Path) -> dict[str, Any]:
    """Move a topic file (or a whole prefix subtree) and rewrite wiki links.

    Also rewrites prefix links: renaming ``scratch/`` → ``archive/scratch/`` fixes
    ``[[scratch/x]]`` → ``[[archive/scratch/x]]``. Refuses if any target topic
    already exists.
    """
    root = root.resolve()
    old_id = old_id.strip().rstrip("/")
    new_id = new_id.strip().rstrip("/")
    if old_id == new_id:
        raise ValueError("old_id and new_id are identical; no rename requested")

    exact_old_path = resolve(old_id, root)
    prefix = old_id + "/"

    old_paths: list[Path] = []
    if exact_old_path.is_file():
        old_paths.append(exact_old_path)

    for path in _iter_topics(root):
        topic_id = _topic_id_from_path(path, root)
        if topic_id == old_id:
            continue
        if topic_id.startswith(prefix):
            old_paths.append(path)

    if not old_paths:
        raise FileNotFoundError(f"no such topic: {old_id!r}; call discover_topics first")

    def _target_path(path: Path) -> Path:
        topic_id = _topic_id_from_path(path, root)
        if topic_id == old_id:
            new_topic_id = new_id
        else:
            new_topic_id = new_id + "/" + topic_id.removeprefix(prefix)
        return resolve(new_topic_id, root)

    targets = {_target_path(p) for p in old_paths}
    if len(targets) != len(old_paths):
        raise FileExistsError(
            f"rename would create a collision under {new_id!r}; target topic already exists"
        )

    for p in old_paths:
        np = _target_path(p)
        if np.exists():
            raise FileExistsError(f"target topic already exists: {_topic_id_from_path(np, root)!r}")
        np.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), str(np))

    parents = sorted({p.parent for p in old_paths}, key=lambda d: -len(d.parts))
    for parent in parents:
        if parent.exists():
            _prune_empty_parents(parent, root)

    link_rewrites = 0
    edited_files = 0
    for path in _iter_topics(root):
        topic_id = _topic_id_from_path(path, root)
        _, body = _decode_file(path)
        new_body, count = _rewrite_wiki_links(body, old_id, new_id)
        if count:
            edit_topic(topic_id, body, new_body, root)
            link_rewrites += count
            edited_files += 1

    return {
        "old_id": old_id,
        "new_id": new_id,
        "link_rewrites": link_rewrites,
        "edited_files": edited_files,
    }


# ---------------------------------------------------------------------------
# §11.4 catalog priming


def prime_catalog(
    root: Path,
    max_topics: int = 50,
    max_bytes: int = 4000,
) -> tuple[str, bool]:
    """Return a compact ``id — title`` list of the most recently updated topics.

    Returns ``(text, truncated)``. Truncation is reported both when the count
    cap or the byte cap is hit, in line with the no-silent-caps rule. Expired
    topics are skipped entirely.
    """
    root = root.resolve()
    now = _now_utc()
    all_paths = sorted(_iter_topics(root), key=lambda p: p.stat().st_mtime, reverse=True)
    paths = all_paths[:max_topics]
    truncated = len(all_paths) > max_topics

    entries: list[str] = []
    for path in paths:
        fm, _ = _decode_file(path)
        if parse_expires(fm.get("expires")) is not None and parse_expires(fm.get("expires")) < now:
            continue
        topic_id = _topic_id_from_path(path, root)
        title = fm.get("title") or topic_id
        line = f"- {topic_id} — {title}"
        entries.append(line)
        if len("\n".join(entries).encode("utf-8")) > max_bytes:
            entries.pop()
            return "\n".join(entries), True
    return "\n".join(entries), truncated


# ---------------------------------------------------------------------------
# Deletion and retention sweep


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


def sweep_expired(root: Path, dry_run: bool = True) -> list[str]:
    """Soft-delete topics whose expiry has passed.

    With ``dry_run=True`` (the default) the matching topic ids are returned and
    nothing is moved. With ``dry_run=False`` files are moved to ``.trash`` just
    like ``delete_topic``.
    """
    root = root.resolve()
    now = _now_utc()
    removed: list[str] = []
    for path in _iter_topics(root):
        fm, _ = _decode_file(path)
        expires_dt = parse_expires(fm.get("expires"))
        if expires_dt is not None and expires_dt < now:
            topic_id = _topic_id_from_path(path, root)
            if dry_run:
                removed.append(topic_id)
            else:
                delete_topic(topic_id, root, hard_delete=False)
                removed.append(topic_id)
    return removed


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
