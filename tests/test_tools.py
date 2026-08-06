import json

import basic_memory_mcp.store as store  # type: ignore
import pytest
from fastmcp.exceptions import ToolError

from basic_memory_mcp.config import ServerConfig, set_config
from basic_memory_mcp.server import (append, delete, discover_topics, edit, mcp, read, register_tools, related, rename, sweep, write)


@pytest.fixture(autouse=True)
def cfg(tmp_path):
    """Install a fresh scratch-root config before every tool test."""
    c = ServerConfig(
        root=tmp_path.resolve(),
        read_only=False,
        hard_delete=False,
        host="127.0.0.1",
        port=0,
    )
    set_config(c)
    register_tools(c)
    return c


# ---------------------------------------------------------------------------
# discover_topics


def test_discover_topics_empty(cfg):
    out = discover_topics()
    assert "[]" in out


def test_discover_topics_lists_topic(cfg):
    write("projects/test", "hello memory")
    out = discover_topics()
    assert "projects/test" in out
    assert "hello memory" in out


def test_discover_topics_reports_truncation(cfg):
    for i in range(5):
        write(f"t{i}", f"c{i}")
    out = discover_topics(limit=2)
    assert "3 additional topics omitted" in out


def test_discover_topics_filter_by_query(cfg):
    write("alpha", "first note", tags=["x"])
    write("beta", "second note", tags=["y"])
    out = discover_topics(query="second")
    assert "beta" in out
    assert "alpha" not in out


def test_discover_topics_filter_by_tag(cfg):
    write("alpha", "a", tags=["shared"])
    write("beta", "b", tags=["other"])
    out = discover_topics(tag="shared")
    assert "alpha" in out
    assert "beta" not in out


# ---------------------------------------------------------------------------
# read


def test_read_existing(cfg):
    write("my/note", "body here")
    out = read(topic_id="my/note")
    assert "title: my/note" in out or "title:" in out
    assert "body here" in out


def test_read_missing_errors(cfg):
    with pytest.raises(ToolError, match="discover_topics"):
        read(topic_id="missing")


# ---------------------------------------------------------------------------
# write


def test_write_creates_and_reports(cfg):
    out = write(topic_id="a/b/c", content="hello world", title="Greeting")
    assert "a/b/c: created" == out
    raw = read(topic_id="a/b/c")
    assert "title: Greeting" in raw
    assert "hello world" in raw


def test_write_replaces_existing(cfg):
    write(topic_id="same", content="first")
    out = write(topic_id="same", content="second")
    assert "same: replaced" == out
    assert "second" in read(topic_id="same")


def test_write_rejects_oversize(cfg):
    big = "x" * (1_000_010)
    with pytest.raises(ToolError, match="exceeding"):
        write(topic_id="big", content=big)


# ---------------------------------------------------------------------------
# edit


def test_edit_single_occurrence(cfg):
    write(topic_id="editme", content="alpha beta gamma")
    out = edit(topic_id="editme", old="beta", new="BETA")
    assert "editme: edited" in out
    assert "BETA" in read(topic_id="editme")


def test_edit_old_not_found(cfg):
    write(topic_id="editme", content="hello")
    with pytest.raises(ToolError, match="not found"):
        edit(topic_id="editme", old="missing", new="nope")


def test_edit_multiple_without_replace_all(cfg):
    write(topic_id="editme", content="a a a")
    with pytest.raises(ToolError, match="replace_all"):
        edit(topic_id="editme", old="a", new="b")


def test_edit_replace_all(cfg):
    write(topic_id="editme", content="a a a")
    out = edit(topic_id="editme", old="a", new="b", replace_all=True)
    assert "editme: edited" in out
    assert read(topic_id="editme").count("b") == 3


# ---------------------------------------------------------------------------
# delete


def test_delete_moves_to_trash(cfg):
    write(topic_id="gone", content="bye")
    out = delete(topic_id="gone")
    assert ".trash" in out
    with pytest.raises(ToolError, match="discover_topics"):
        read(topic_id="gone")


def test_delete_prunes_empty_parents(cfg):
    write(topic_id="deep/nested/note", content="x")
    delete(topic_id="deep/nested/note")
    assert not (cfg.root / "deep").exists()


# ---------------------------------------------------------------------------
# read-only mode


async def test_read_only_registers_only_read_tools(tmp_path):
    c = ServerConfig(
        root=tmp_path.resolve(),
        read_only=True,
        hard_delete=False,
        host="127.0.0.1",
        port=0,
    )
    set_config(c)
    register_tools(c)
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert names == {"discover_topics", "read", "related"}


# ---------------------------------------------------------------------------
# Phase 2 tools


def test_append_adds_text_to_existing_topic(cfg):
    write(topic_id="journal", content="first entry")
    out = append(topic_id="journal", text="second entry")
    assert "journal: appended" == out
    raw = read(topic_id="journal")
    assert "second entry" in raw


def test_append_under_heading(cfg):
    write(topic_id="log", content="## Tasks\nalpha")
    append(topic_id="log", text="beta", heading="Tasks")
    raw = read(topic_id="log")
    body = store._decode_file(cfg.root / "log.md")[1]  # type: ignore[attr-defined]
    assert "alpha" in body and "beta" in body
    assert body.index("alpha") < body.index("beta")


def test_append_fails_missing_topic(cfg):
    with pytest.raises(ToolError, match="discover_topics"):
        append(topic_id="missing", text="nope")


def test_related_reports_link_graph(cfg):
    write(topic_id="hub", content="see [[spoke]]")
    write(topic_id="spoke", content="back to [[hub]]")
    out = related(topic_id="hub")
    assert "spoke" in out
    data = json.loads(out)
    assert data["outbound"] == ["spoke"]
    assert data["inbound"] == ["spoke"]


def test_rename_moves_and_rewrites_links(cfg):
    write(topic_id="people/garland", content="Anthony")
    write(topic_id="projects/team", content="Member: [[people/garland]]")
    out = rename(old_id="people/garland", new_id="people/anthony-garland")
    assert "people/garland -> people/anthony-garland" in out
    assert "1 wiki links" in out
    team_text = read(topic_id="projects/team")
    assert "[[people/anthony-garland]]" in team_text


async def test_memory_resource_reads_nested_topic(cfg):
    write(topic_id="users/anthony-garland", content="Anthony Garland")
    result = await mcp.read_resource("memory://users/anthony-garland")
    assert "Anthony Garland" in result.contents[0].content


async def test_memory_resources_listed_include_nested_topics(cfg):
    write(topic_id="claude/skills-directory", content="Skills")
    # register_tools/_refresh_catalog registers concrete memory:// resources on write.
    resources = await mcp.list_resources()
    uris = {str(r.uri) for r in resources}
    assert "memory://claude/skills-directory" in uris


# ---------------------------------------------------------------------------
# Phase 3 — retention (§12)


def test_write_retention_is_stored(cfg):
    write(topic_id="note", content="body", retention="today")
    raw = read(topic_id="note")
    assert "expires:" in raw
    assert "never" not in raw or "T" in raw  # today => absolute datetime


def test_write_permanent_default(cfg):
    write(topic_id="note", content="body")
    raw = read(topic_id="note")
    assert "expires: never" in raw


def test_discover_reports_expired_hidden(cfg):
    write(topic_id="old", content="expired body", retention="2000-01-01T00:00:00Z")
    out = discover_topics()
    assert "expired topics hidden" in out
    assert "pass include_expired=true" in out


def test_discover_include_expired_reveals_them(cfg):
    write(topic_id="old", content="expired body", retention="2000-01-01T00:00:00Z")
    out = discover_topics(include_expired=True)
    assert '"expired": true' in out


def test_sweep_dry_run_then_delete(cfg):
    write(topic_id="old", content="expired body", retention="2000-01-01T00:00:00Z")
    out = sweep(dry_run=True)
    assert "old" in out
    assert "dry run" in out
    assert read(topic_id="old")

    out = sweep(dry_run=False)
    assert "Swept topics" in out
    assert "old" in out
    with pytest.raises(ToolError, match="discover_topics"):
        read(topic_id="old")


def test_append_preserved_expiry_when_retention_omitted(cfg):
    write(topic_id="note", content="body", retention="2099-01-01T00:00:00Z")
    append(topic_id="note", text="more")
    raw = read(topic_id="note")
    assert "2099-01-01" in raw


def test_sweep_invalid_retention_raises(cfg):
    with pytest.raises(ToolError, match="invalid retention"):
        write(topic_id="bad", content="body", retention="nonsense")


async def test_read_only_does_not_register_sweep(tmp_path):
    c = ServerConfig(
        root=tmp_path.resolve(),
        read_only=True,
        hard_delete=False,
        host="127.0.0.1",
        port=0,
    )
    set_config(c)
    register_tools(c)
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert "sweep" not in names


