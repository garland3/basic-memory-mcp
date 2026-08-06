from __future__ import annotations

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
    topics, total = store.discover_topics(tmp_path, limit=10)
    assert total == 2
    ids = {t["id"] for t in topics}
    assert ids == {"a/first", "b/second"}


def test_discover_topics_by_query(tmp_path: Path) -> None:
    store.write_topic("first", "alpha", tmp_path, title="One")
    store.write_topic("second", "beta", tmp_path, title="Two")
    topics, total = store.discover_topics(tmp_path, query="beta", limit=10)
    assert total == 1
    assert topics[0]["id"] == "second"


def test_discover_topics_by_tag(tmp_path: Path) -> None:
    store.write_topic("first", "a", tmp_path, tags=["x"])
    store.write_topic("second", "b", tmp_path, tags=["y"])
    topics, _ = store.discover_topics(tmp_path, tag="x", limit=10)
    assert len(topics) == 1
    assert topics[0]["id"] == "first"


def test_discover_topics_limit_reports_truncation(tmp_path: Path) -> None:
    for i in range(5):
        store.write_topic(f"topic{i}", f"content {i}", tmp_path)
    topics, total = store.discover_topics(tmp_path, limit=2)
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
    topics, total = store.discover_topics(tmp_path, query="search", limit=10)
    assert total == 2
    ids = [t["id"] for t in topics]
    assert ids[0] == "search-results"
    assert ids[1] == "alpha"


def test_discover_topics_returns_outbound_links(tmp_path: Path) -> None:
    store.write_topic("hub", " Points to [[people/garland]] and [[projects/atlas-ports]].", tmp_path)
    topics, total = store.discover_topics(tmp_path, limit=10)
    assert total == 1
    assert topics[0]["outbound_links"] == ["people/garland", "projects/atlas-ports"]
