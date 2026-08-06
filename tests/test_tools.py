import pytest

from basic_memory_mcp.config import ServerConfig, set_config
from basic_memory_mcp.server import delete, discover_topics, edit, mcp, read, register_tools, write
from fastmcp.exceptions import ToolError


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
    assert names == {"discover_topics", "read"}
