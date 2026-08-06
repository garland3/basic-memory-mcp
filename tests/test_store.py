from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from basic_memory_mcp import store


def test_resolve_simple_topic_id(tmp_path: Path) -> None:
    path = store.resolve("projects/atlas-ports", tmp_path)
    assert path == tmp_path / "projects" / "atlas-ports.md"


def test_resolve_rejects_absolute() -> None:
    with pytest.raises(ValueError, match="absolute"):
        store.resolve("/etc/passwd", Path("/tmp"))


def test_resolve_rejects_parent_escape(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=".."):
        store.resolve("../outside", tmp_path)


def test_resolve_rejects_symlink_outside(tmp_path: Path) -> None:
    inner = tmp_path / "inner"
    inner.mkdir()
    # Evil file lives outside the server root.
    outside = tmp_path.parent / "basic-memory-test-evil.md"
    outside.write_text("oops")
    try:
        link = inner / "link.md"
        link.symlink_to(outside)
        with pytest.raises(ValueError, match="outside"):
            store.resolve("inner/link", tmp_path)
    finally:
        outside.unlink(missing_ok=True)
        link.unlink(missing_ok=True)


def test_resolve_allows_symlink_inside(tmp_path: Path) -> None:
    inner = tmp_path / "inner"
    inner.mkdir()
    target = tmp_path / "real.md"
    target.write_text("hello")
    link = inner / "link.md"
    link.symlink_to(target)
    path = store.resolve("inner/link", tmp_path)
    assert path == (tmp_path / "inner" / "link.md").resolve()


def test_split_frontmatter_present() -> None:
    text = "---\ntitle: Foo\n---\nbody"
    fm, body = store._split_frontmatter(text)  # type: ignore[attr-defined]
    assert fm == "title: Foo"
    assert body == "body"


def test_split_frontmatter_absent() -> None:
    assert store._split_frontmatter("just body") is None  # type: ignore[attr-defined]


def test_write_topic_creates_file(tmp_path: Path) -> None:
    resolved, created = store.write_topic("hello", "world", tmp_path, title="Hello", tags=["a", "b"])
    assert created
    assert resolved == "hello"
    text = (tmp_path / "hello.md").read_text()
    assert text.startswith("---\n")
    assert "title: Hello" in text
    assert "tags:\n- a\n- b" in text
    assert "world" in text


def test_write_topic_replaces_keeps_created(tmp_path: Path) -> None:
    store.write_topic("note", "first", tmp_path, title="Note")
    resolved, created = store.write_topic("note", "second", tmp_path, title="Note")
    assert not created
    assert resolved == "note"
    text = (tmp_path / "note.md").read_text()
    assert "second" in text
    assert "created:" in text


def test_write_topic_size_guard(tmp_path: Path) -> None:
    big = "x" * (store.MAX_TOPIC_BYTES + 10)
    with pytest.raises(ValueError, match="exceeding"):
        store.write_topic("big", big, tmp_path)


def test_read_topic_roundtrip(tmp_path: Path) -> None:
    store.write_topic("round", "body text", tmp_path, title="Round")
    raw = store.read_topic("round", tmp_path)
    assert "title: Round" in raw
    assert "body text" in raw


def test_read_missing_topic(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="discover_topics"):
        store.read_topic("missing", tmp_path)


def test_edit_bumps_updated(tmp_path: Path) -> None:
    store.write_topic("editme", "alpha beta", tmp_path, title="Edit")
    first = (tmp_path / "editme.md").read_text()
    store.edit_topic("editme", "alpha", "ALPHA", tmp_path)
    second = (tmp_path / "editme.md").read_text()
    assert "ALPHA beta" in second
    assert "updated:" in second
    assert first != second


def test_edit_no_old_match(tmp_path: Path) -> None:
    store.write_topic("editme", "hello", tmp_path)
    with pytest.raises(ValueError, match="not found"):
        store.edit_topic("editme", "xyz", "abc", tmp_path)


def test_edit_multiple_requires_replace_all(tmp_path: Path) -> None:
    store.write_topic("editme", "a a a", tmp_path)
    with pytest.raises(ValueError, match="replace_all"):
        store.edit_topic("editme", "a", "b", tmp_path)


def test_discover_topics_no_filter(tmp_path: Path) -> None:
    store.write_topic("a/first", "one", tmp_path)
    store.write_topic("b/second", "two", tmp_path)
    topics, total, _hidden = store.discover_topics(tmp_path, limit=10)
    assert total == 2
    ids = {t["id"] for t in topics}
    assert ids == {"a/first", "b/second"}


def test_discover_topics_by_query(tmp_path: Path) -> None:
    store.write_topic("first", "alpha", tmp_path, title="One")
    store.write_topic("second", "beta", tmp_path, title="Two")
    topics, total, _hidden = store.discover_topics(tmp_path, query="beta", limit=10)
    assert total == 1
    assert topics[0]["id"] == "second"


def test_discover_topics_by_tag(tmp_path: Path) -> None:
    store.write_topic("first", "a", tmp_path, tags=["x"])
    store.write_topic("second", "b", tmp_path, tags=["y"])
    topics, _total, _hidden = store.discover_topics(tmp_path, tag="x", limit=10)
    assert len(topics) == 1
    assert topics[0]["id"] == "first"


def test_discover_topics_limit_reports_truncation(tmp_path: Path) -> None:
    for i in range(5):
        store.write_topic(f"topic{i}", f"content {i}", tmp_path)
    topics, total, _hidden = store.discover_topics(tmp_path, limit=2)
    assert len(topics) == 2
    assert total == 5


def test_delete_soft(tmp_path: Path) -> None:
    store.write_topic("gone", "bye", tmp_path)
    trash = store.delete_topic("gone", tmp_path)
    assert ".trash" in trash.as_posix()
    assert not (tmp_path / "gone.md").exists()


def test_delete_hard(tmp_path: Path) -> None:
    store.write_topic("gone", "bye", tmp_path)
    store.delete_topic("gone", tmp_path, hard_delete=True)
    assert not (tmp_path / "gone.md").exists()
    assert not (tmp_path / ".trash").exists()


def test_prune_empty_parents(tmp_path: Path) -> None:
    store.write_topic("a/b/c", "deep", tmp_path)
    store.delete_topic("a/b/c", tmp_path)
    assert not (tmp_path / "a").exists()


def test_handwritten_no_frontmatter_is_allowed(tmp_path: Path) -> None:
    (tmp_path / "plain.md").write_text("no frontmatter here")
    topic = store.load_topic("plain", tmp_path)
    assert topic["body"] == "no frontmatter here"
    assert topic["title"] == "plain"


def test_wiki_links_extracted(tmp_path: Path) -> None:
    store.write_topic("web", "See [[people/garland]] and [[projects/atlas-ports]].", tmp_path)
    assert store.parse_wiki_links("See [[people/garland]].") == ["people/garland"]
    topic = store.load_topic("web", tmp_path)
    assert "people/garland" in topic["outbound_links"]


# ---------------------------------------------------------------------------
# regression tests for spec gaps


def test_write_topic_strips_embedded_frontmatter(tmp_path: Path) -> None:
    """If the model puts a leading frontmatter block in content, merge it into the
    server's single block instead of writing two blocks.
    """
    content = "---\ntitle: FromContent\ncustom: keepme\n---\nbody here"
    store.write_topic("mixed", content, tmp_path, title="ToolTitle", tags=["t"])
    raw = (tmp_path / "mixed.md").read_text()
    # Should be one frontmatter block; the body frontmatter must not be duplicated.
    assert raw.startswith("---\n")
    assert raw.count("\n---\n") == 1
    assert "title: ToolTitle" in raw
    assert "custom: keepme" in raw
    fm, body = store._decode_file(tmp_path / "mixed.md")  # type: ignore[attr-defined]
    assert body.strip() == "body here"
    assert fm["tags"] == ["t"]


def test_write_topic_preserves_custom_frontmatter_keys(tmp_path: Path) -> None:
    """Replacing a topic must keep unknown frontmatter keys unless the new content
    explicitly overrides them.
    """
    store.write_topic("keep", "---\ncustom: secret\n---\nbody1", tmp_path, title="Keep")
    store.write_topic("keep", "body2", tmp_path)
    fm, _ = store._decode_file(tmp_path / "keep.md")  # type: ignore[attr-defined]
    assert fm["custom"] == "secret"
    assert "body2" in (tmp_path / "keep.md").read_text()


def test_resolve_preserves_inner_dots(tmp_path: Path) -> None:
    path = store.resolve("atlas-ui-v1.2.3", tmp_path)
    assert path.name == "atlas-ui-v1.2.3.md"


def test_resolve_rejects_trash_route(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"\.trash.*reserved"):
        store.resolve(".trash/secret", tmp_path)


def test_read_topic_rejects_trash(tmp_path: Path) -> None:
    # The resolve step must block the reserved .trash namespace before any read.
    (tmp_path / ".trash").mkdir()
    (tmp_path / ".trash" / "leak.md").write_text("oops")
    with pytest.raises(ValueError, match=r"\.trash.*reserved"):
        store.read_topic(".trash/leak", tmp_path)


def test_created_timestamp_is_stable_iso_z(tmp_path: Path) -> None:
    _, created = store.write_topic("stable", "body", tmp_path, title="Stable")
    assert created
    topic = store.load_topic("stable", tmp_path)
    assert topic["created"].endswith("Z")
    assert topic["updated"].endswith("Z")
    # A second rewrite must keep the original created value and format.
    created_before = topic["created"]
    store.write_topic("stable", "body2", tmp_path)
    topic2 = store.load_topic("stable", tmp_path)
    assert topic2["created"] == created_before


def test_existing_datetime_frontmatter_is_normalized(tmp_path: Path) -> None:
    (tmp_path / "legacy.md").write_text(
        "---\ntitle: Legacy\ncreated: 2026-08-05 12:00:00+00:00\nupdated: 2026-08-05 13:00:00+00:00\n---\nlegacy body"
    )
    store.write_topic("legacy", "new body", tmp_path)
    fm, _ = store._decode_file(tmp_path / "legacy.md")  # type: ignore[attr-defined]
    assert isinstance(fm["created"], str)
    assert fm["created"].endswith("Z")
    assert isinstance(fm["updated"], str)


def test_discover_topics_ranks_title_and_id_hits_first(tmp_path: Path) -> None:
    store.write_topic("alpha", "the search query appears here", tmp_path, title="A")
    store.write_topic("search-results", "irrelevant body", tmp_path, title="Search Results")
    # alpha was written second but is a body-only hit; it should still fall below
    # search-results because a title/id hit is ranked higher.
    topics, total, _hidden = store.discover_topics(tmp_path, query="search", limit=10)
    assert total == 2
    ids = [t["id"] for t in topics]
    assert ids[0] == "search-results"
    assert ids[1] == "alpha"


def test_discover_topics_returns_outbound_links(tmp_path: Path) -> None:
    store.write_topic("hub", " Points to [[people/garland]] and [[projects/atlas-ports]].", tmp_path)
    topics, total, _hidden = store.discover_topics(tmp_path, limit=10)
    assert total == 1
    assert topics[0]["outbound_links"] == ["people/garland", "projects/atlas-ports"]


# ---------------------------------------------------------------------------
# Phase 2 features


def test_append_topic_adds_text(tmp_path: Path) -> None:
    store.write_topic("note", "first line", tmp_path, title="Note")
    store.append_topic("note", "second line", tmp_path)
    raw = (tmp_path / "note.md").read_text()
    assert "first line" in raw
    assert "second line" in raw
    # exactly one blank line between original body and appended text
    assert "\n\nsecond line" in raw


def test_append_under_existing_heading(tmp_path: Path) -> None:
    store.write_topic("note", "## Log\nalpha", tmp_path)
    store.append_topic("note", "beta", tmp_path, heading="Log")
    raw = (tmp_path / "note.md").read_text()
    assert raw.index("alpha") < raw.index("beta")
    # beta is still under the Log heading
    assert "## Log" in raw


def test_append_creates_heading_when_missing(tmp_path: Path) -> None:
    store.write_topic("note", "alpha", tmp_path)
    store.append_topic("note", "beta", tmp_path, heading="Log")
    raw = (tmp_path / "note.md").read_text()
    assert "## Log" in raw
    assert "beta" in raw


def test_append_fails_missing_topic(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="discover_topics"):
        store.append_topic("missing", "text", tmp_path)


def test_append_bumps_updated(tmp_path: Path) -> None:
    store.write_topic("note", "body", tmp_path)
    before = store.load_topic("note", tmp_path)["updated"]
    import time
    time.sleep(1.2)
    store.append_topic("note", "more", tmp_path)
    after = store.load_topic("note", tmp_path)["updated"]
    assert after > before


def test_append_size_guard(tmp_path: Path) -> None:
    store.write_topic("note", "x", tmp_path)
    big = "x" * (store.MAX_TOPIC_BYTES + 10)
    with pytest.raises(ValueError, match="exceed"):
        store.append_topic("note", big, tmp_path)


def test_related_outbound_and_inbound(tmp_path: Path) -> None:
    store.write_topic("hub", "links to [[spoke/one]] and [[spoke/two]]", tmp_path)
    store.write_topic("spoke/one", "spoke one", tmp_path)
    store.write_topic("spoke/two", "back to [[hub]]", tmp_path)
    rel = store.related_topics("hub", tmp_path)
    assert rel["outbound"] == ["spoke/one", "spoke/two"]
    assert "spoke/two" in rel["inbound"]
    assert rel["id"] == "hub"


def test_rename_moves_file_and_rewrites_links(tmp_path: Path) -> None:
    store.write_topic("people/garland", "Anthony Garland", tmp_path)
    store.write_topic("hub", "link to [[people/garland]]", tmp_path)
    result = store.rename_topic("people/garland", "people/tony-garland", tmp_path)
    assert result["old_id"] == "people/garland"
    assert result["new_id"] == "people/tony-garland"
    assert result["link_rewrites"] == 1
    assert not (tmp_path / "people" / "garland.md").exists()
    assert (tmp_path / "people" / "tony-garland.md").exists()
    hub_text = (tmp_path / "hub.md").read_text()
    assert "[[people/tony-garland]]" in hub_text
    assert "[[people/garland]]" not in hub_text


def test_rename_rewrites_prefix_links(tmp_path: Path) -> None:
    store.write_topic("scratch/a", "note a", tmp_path)
    store.write_topic("scratch/b", "note b", tmp_path)
    store.write_topic("hub", "See [[scratch/a]] and [[scratch/b|b]].", tmp_path)
    result = store.rename_topic("scratch", "archive/scratch", tmp_path)
    hub_text = (tmp_path / "hub.md").read_text()
    assert result["link_rewrites"] == 2
    assert "[[archive/scratch/a]]" in hub_text
    assert "[[archive/scratch/b|b]]" in hub_text
    assert "[[scratch/" not in hub_text


def test_rename_refuses_existing_target(tmp_path: Path) -> None:
    store.write_topic("a", "a", tmp_path)
    store.write_topic("b", "b", tmp_path)
    with pytest.raises(FileExistsError, match="already exists"):
        store.rename_topic("a", "b", tmp_path)


def test_prime_catalog(tmp_path: Path) -> None:
    store.write_topic("projects/atlas", "Atlas", tmp_path, title="ATLAS project")
    store.write_topic("people/garland", "Garland", tmp_path, title="Anthony Garland")
    text, truncated = store.prime_catalog(tmp_path)
    assert "projects/atlas" in text
    assert "ATLAS project" in text
    assert not truncated


def test_prime_catalog_truncated_by_bytes(tmp_path: Path) -> None:
    store.write_topic("a", "x", tmp_path, title="A")
    store.write_topic("b", "x", tmp_path, title="B")
    text, truncated = store.prime_catalog(tmp_path, max_topics=10, max_bytes=6)
    # Should keep no more than fits in tiny budget and report truncation.
    assert len(text.encode("utf-8")) <= 6 or text == ""
    assert truncated


def test_prime_catalog_truncated_by_count(tmp_path: Path) -> None:
    for i in range(5):
        store.write_topic(f"topic{i}", f"body {i}", tmp_path, title=f"Title {i}")
    text, truncated = store.prime_catalog(tmp_path, max_topics=3, max_bytes=100000)
    assert truncated
    assert len(text.splitlines()) <= 3


def test_append_heading_preserves_blank_line_before_next_heading(tmp_path: Path) -> None:
    store.write_topic(
        "note",
        "## Log\nalpha\n\n## Other\nomega",
        tmp_path,
    )
    store.append_topic("note", "beta", tmp_path, heading="Log")
    body = store._decode_file(tmp_path / "note.md")[1]  # type: ignore[attr-defined]
    # There must be a blank line before the next heading.
    assert "\n\n## Other" in body
    assert "beta\n\n## Other" in body or "beta\n## Other" not in body


def test_discover_multi_term_and_ranking(tmp_path: Path) -> None:
    store.write_topic("atlas", "atlas ports map", tmp_path, title="Atlas ports")  # title hit + body
    store.write_topic("misc", "something atlas then ports", tmp_path, title="Other")  # body hit
    topics, total, _hidden = store.discover_topics(tmp_path, query="atlas ports", limit=10)
    assert total == 2
    ids = [t["id"] for t in topics]
    # atlas hits both terms in title; misc only in body, so atlas ranks first.
    assert ids[0] == "atlas"
    assert ids[1] == "misc"


def test_discover_quoted_phrase_only(tmp_path: Path) -> None:
    store.write_topic("atlas", "atlas ports map", tmp_path, title="Atlas")
    store.write_topic("other", "atlas and ports", tmp_path, title="Other")
    topics, total, _hidden = store.discover_topics(tmp_path, query='"atlas ports"', limit=10)
    assert total == 1
    assert topics[0]["id"] == "atlas"


def test_discover_match_centered_snippet(tmp_path: Path) -> None:
    long = "\n".join([f"line {i}" for i in range(50)])
    store.write_topic("note", long + "\nneedle here", tmp_path)
    topics, total, _hidden = store.discover_topics(tmp_path, query="needle", limit=10)
    assert total == 1
    snippet = topics[0]["snippet"]
    assert "needle" in snippet
    assert "line 0" not in snippet


# ---------------------------------------------------------------------------
# Phase 3 — retention (§12)


def test_write_topic_default_retention_is_permanent(tmp_path: Path) -> None:
    store.write_topic("note", "body", tmp_path, title="Note")
    fm, _ = store._decode_file(tmp_path / "note.md")  # type: ignore[attr-defined]
    assert fm.get("expires") == "never"


def test_write_topic_retention_session_is_24h(tmp_path: Path) -> None:
    before = datetime.now(timezone.utc)
    store.write_topic("note", "body", tmp_path, retention="session")
    fm, _ = store._decode_file(tmp_path / "note.md")  # type: ignore[attr-defined]
    expires = datetime.fromisoformat(str(fm["expires"]).replace("Z", "+00:00"))
    delta = expires - before
    assert timedelta(hours=23) < delta < timedelta(hours=25)


def test_write_topic_retention_iso_date(tmp_path: Path) -> None:
    store.write_topic("note", "body", tmp_path, retention="2026-08-10")
    fm, _ = store._decode_file(tmp_path / "note.md")  # type: ignore[attr-defined]
    assert str(fm["expires"]).startswith("2026-08-10")


def test_append_extends_expiry(tmp_path: Path) -> None:
    store.write_topic("note", "body", tmp_path, retention="2026-08-10T00:00:00Z")
    store.append_topic("note", "more", tmp_path, retention="2026-09-01T00:00:00Z")
    fm, _ = store._decode_file(tmp_path / "note.md")  # type: ignore[attr-defined]
    assert str(fm["expires"]).startswith("2026-09-01")


def test_append_does_not_shorten_expiry(tmp_path: Path) -> None:
    store.write_topic("note", "body", tmp_path, retention="2026-09-01T00:00:00Z")
    store.append_topic("note", "more", tmp_path, retention="2026-08-10T00:00:00Z")
    fm, _ = store._decode_file(tmp_path / "note.md")  # type: ignore[attr-defined]
    assert str(fm["expires"]).startswith("2026-09-01")


def test_append_to_permanent_does_not_shorten(tmp_path: Path) -> None:
    store.write_topic("note", "body", tmp_path)
    store.append_topic("note", "more", tmp_path, retention="today")
    fm, _ = store._decode_file(tmp_path / "note.md")  # type: ignore[attr-defined]
    assert fm.get("expires") == "never"


def test_discover_hides_expired_by_default(tmp_path: Path) -> None:
    store.write_topic("permanent", "perm", tmp_path, retention="permanent")
    store.write_topic("expired", "old", tmp_path, retention="2000-01-01T00:00:00Z")
    topics, total, hidden = store.discover_topics(tmp_path)
    assert total == 1
    assert hidden == 1
    assert {t["id"] for t in topics} == {"permanent"}


def test_discover_include_expired_marks_them(tmp_path: Path) -> None:
    store.write_topic("expired", "old", tmp_path, retention="2000-01-01T00:00:00Z")
    topics, total, hidden = store.discover_topics(tmp_path, include_expired=True)
    assert total == 1 and hidden == 0
    assert topics[0]["expired"] is True


def test_sweep_dry_run_then_delete(tmp_path: Path) -> None:
    store.write_topic("old", "old", tmp_path, retention="2000-01-01T00:00:00Z")
    ids = store.sweep_expired(tmp_path, dry_run=True)
    assert ids == ["old"]
    assert (tmp_path / "old.md").exists()
    deleted = store.sweep_expired(tmp_path, dry_run=False)
    assert "old" in deleted
    assert not (tmp_path / "old.md").exists()
    assert (tmp_path / ".trash").exists()


def test_prime_catalog_skips_expired(tmp_path: Path) -> None:
    store.write_topic("a", "a", tmp_path, retention="permanent")
    store.write_topic("b", "b", tmp_path, retention="2000-01-01T00:00:00Z")
    text, _ = store.prime_catalog(tmp_path)
    assert "a" in text
    assert "b" not in text


def test_malformed_expires_treated_as_permanent(tmp_path: Path) -> None:
    (tmp_path / "bad.md").write_text("---\nexpires: not-a-date\n---\nbody")
    topics, total, hidden = store.discover_topics(tmp_path)
    assert total == 1 and hidden == 0
    assert topics[0]["id"] == "bad"


def test_read_still_reads_expired(tmp_path: Path) -> None:
    store.write_topic("old", "old", tmp_path, retention="2000-01-01T00:00:00Z")
    raw = store.read_topic("old", tmp_path)
    assert "old" in raw


def test_invalid_retention_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid retention"):
        store.write_topic("x", "x", tmp_path, retention="nonsense")

