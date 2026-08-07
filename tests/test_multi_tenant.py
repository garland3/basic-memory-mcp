"""Phase 3 (§13): optional multi-tenancy via _atlas_user, off by default."""

from __future__ import annotations

import json
import os
import sys

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from fastmcp.exceptions import ToolError

from basic_memory_mcp import store
from basic_memory_mcp.config import ServerConfig, set_config
from basic_memory_mcp.server import (
    append_mt,
    delete_mt,
    discover_topics_mt,
    edit_mt,
    mcp,
    read_mt,
    register_tools,
    related_mt,
    rename_mt,
    sweep_mt,
    write_mt,
)

ALICE = "alice@example.com"  # -> alice_example_com
BOB = "bob@example.org"  # -> bob_example_org


@pytest.fixture
def mt_cfg(tmp_path):
    """Install a fresh multi-tenant scratch-root config."""
    c = ServerConfig(
        root=tmp_path.resolve(),
        read_only=False,
        hard_delete=False,
        multi_tenant=True,
        host="127.0.0.1",
        port=0,
    )
    set_config(c)
    register_tools(c)
    return c


@pytest.fixture
def plain_cfg(tmp_path):
    """Install a fresh single-tenant (default) scratch-root config."""
    c = ServerConfig(
        root=tmp_path.resolve(),
        read_only=False,
        hard_delete=False,
        multi_tenant=False,
        host="127.0.0.1",
        port=0,
    )
    set_config(c)
    register_tools(c)
    return c


# ---------------------------------------------------------------------------
# §13.3 slug mapping


def test_sanitize_tenant_basic() -> None:
    assert store.sanitize_tenant("garland3@gmail.com") == "garland3_gmail_com"


def test_sanitize_tenant_lowercases() -> None:
    assert store.sanitize_tenant("Garland3@Gmail.COM") == "garland3_gmail_com"
    assert store.sanitize_tenant("  Alice@Example.com  ") == "alice_example_com"


@pytest.mark.parametrize(
    "bad",
    [
        "user+tag@gmail.com",  # '+' is not in [a-z0-9_-]
        "a/b",  # path separator
        "../..",  # dots become '_' but the slash survives -> rejected
        "a\\b",  # backslash
        "o'brien@x.com",  # apostrophe
        "üser@example.com",  # non-ascii
        "",  # empty
        "   ",  # whitespace only
    ],
)
def test_sanitize_tenant_rejects_unusable_values(bad: str) -> None:
    with pytest.raises(ValueError, match="not usable as a tenant id"):
        store.sanitize_tenant(bad)


def test_sanitize_tenant_neutralizes_dot_escapes() -> None:
    # '.' maps to '_' before validation, so ".." can never survive as a
    # parent-directory segment; it becomes a harmless slug instead.
    assert store.sanitize_tenant("..") == "__"


def test_sanitize_tenant_slug_collision_is_accepted() -> None:
    # §13.5: a.b@c.d and a_b@c_d map to the same slug. Accepted: emails are the
    # trust boundary and ATLAS authenticates them, so the collision requires two
    # authenticated users with pathological addresses.
    assert store.sanitize_tenant("a.b@c.d") == store.sanitize_tenant("a_b@c_d") == "a_b_c_d"


def test_tenant_root_off_is_passthrough(tmp_path) -> None:
    assert store.tenant_root(tmp_path, ALICE, multi_tenant=False) == tmp_path
    assert store.tenant_root(tmp_path, None, multi_tenant=False) == tmp_path


def test_tenant_root_appends_slug(tmp_path) -> None:
    assert store.tenant_root(tmp_path, ALICE, multi_tenant=True) == tmp_path / "alice_example_com"


@pytest.mark.parametrize("missing", [None, "", "   "])
def test_tenant_root_refuses_missing_user(missing, tmp_path) -> None:
    with pytest.raises(ValueError, match="_atlas_user is required"):
        store.tenant_root(tmp_path, missing, multi_tenant=True)


# ---------------------------------------------------------------------------
# §13.2 / §13.5 registered schemas


def _tool_props(tool) -> dict:
    return tool.parameters.get("properties", {})


async def test_flag_off_schemas_have_no_atlas_user_anywhere(plain_cfg) -> None:
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert names == {"discover_topics", "read", "related", "write", "edit", "append", "rename", "delete", "sweep"}
    for t in tools:
        assert "_atlas_user" not in _tool_props(t), f"{t.name} unexpectedly declares _atlas_user"


async def test_flag_on_every_tool_declares_atlas_user(mt_cfg) -> None:
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert names == {"discover_topics", "read", "related", "write", "edit", "append", "rename", "delete", "sweep"}
    for t in tools:
        props = _tool_props(t)
        assert "_atlas_user" in props, f"{t.name} is missing _atlas_user"
        # Trailing parameter.
        assert list(props.keys())[-1] == "_atlas_user", f"{t.name}: _atlas_user is not trailing"
        # Optional, with the documented description.
        assert "_atlas_user" not in t.parameters.get("required", [])
        assert props["_atlas_user"].get("description") == "injected by the Atlas backend; do not supply"


async def test_flag_on_read_only_registers_three_tenant_tools(tmp_path) -> None:
    c = ServerConfig(
        root=tmp_path.resolve(),
        read_only=True,
        hard_delete=False,
        multi_tenant=True,
        host="127.0.0.1",
        port=0,
    )
    set_config(c)
    register_tools(c)
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert names == {"discover_topics", "read", "related"}
    for t in tools:
        assert "_atlas_user" in _tool_props(t)


# ---------------------------------------------------------------------------
# §13.3: missing/None _atlas_user while --multi-tenant is on -> refuse


def test_every_tool_refuses_missing_atlas_user(mt_cfg) -> None:
    calls = [
        lambda: discover_topics_mt(),
        lambda: read_mt(topic_id="x"),
        lambda: related_mt(topic_id="x"),
        lambda: write_mt(topic_id="x", content="y"),
        lambda: edit_mt(topic_id="x", old="y", new="z"),
        lambda: append_mt(topic_id="x", text="y"),
        lambda: rename_mt(old_id="x", new_id="y"),
        lambda: delete_mt(topic_id="x"),
        lambda: sweep_mt(),
    ]
    for call in calls:
        with pytest.raises(ToolError, match="_atlas_user is required"):
            call()


def test_every_tool_refuses_none_atlas_user(mt_cfg) -> None:
    with pytest.raises(ToolError, match="_atlas_user is required"):
        discover_topics_mt(_atlas_user=None)
    with pytest.raises(ToolError, match="_atlas_user is required"):
        write_mt(topic_id="x", content="y", _atlas_user=None)


def test_refusal_is_not_silently_mapped_to_shared_tenant(mt_cfg) -> None:
    with pytest.raises(ToolError, match="_atlas_user is required"):
        write_mt(topic_id="x", content="y")
    # Nothing was written anywhere: no shared/default folder appeared.
    assert list(mt_cfg.root.iterdir()) == []


def test_unusable_atlas_user_value_refused(mt_cfg) -> None:
    with pytest.raises(ToolError, match="not usable as a tenant id"):
        write_mt(topic_id="x", content="y", _atlas_user="user+tag@gmail.com")


# ---------------------------------------------------------------------------
# §13.5: disjoint catalogs, trashes, priming, and link graphs


def test_two_tenants_see_disjoint_catalogs(mt_cfg) -> None:
    write_mt(topic_id="projects/alpha", content="alice note", _atlas_user=ALICE)
    write_mt(topic_id="projects/beta", content="bob note", _atlas_user=BOB)

    out_a = discover_topics_mt(_atlas_user=ALICE)
    out_b = discover_topics_mt(_atlas_user=BOB)
    assert "projects/alpha" in out_a and "projects/beta" not in out_a
    assert "projects/beta" in out_b and "projects/alpha" not in out_b

    # Files physically live under each tenant's slug folder.
    assert (mt_cfg.root / "alice_example_com" / "projects" / "alpha.md").is_file()
    assert (mt_cfg.root / "bob_example_org" / "projects" / "beta.md").is_file()
    # The flat root holds no topic files of its own.
    assert not (mt_cfg.root / "projects").exists()


def test_tenant_reads_own_content_only(mt_cfg) -> None:
    write_mt(topic_id="spoke", content="alice's spoke", _atlas_user=ALICE)
    write_mt(topic_id="spoke", content="bob's spoke", _atlas_user=BOB)
    assert "alice's spoke" in read_mt(topic_id="spoke", _atlas_user=ALICE)
    assert "bob's spoke" in read_mt(topic_id="spoke", _atlas_user=BOB)


def test_disjoint_trashes(mt_cfg) -> None:
    write_mt(topic_id="gone", content="alice trash", _atlas_user=ALICE)
    out = delete_mt(topic_id="gone", _atlas_user=ALICE)

    # Trash path is inside alice's own folder.
    assert "alice_example_com/.trash" in out
    assert list((mt_cfg.root / "alice_example_com" / ".trash").rglob("*.md"))
    # Bob has no trash and an empty catalog.
    assert not (mt_cfg.root / "bob_example_org" / ".trash").exists()
    assert "[]" in discover_topics_mt(_atlas_user=BOB)
    # Alice's deleted topic is gone from her catalog too.
    assert "gone" not in discover_topics_mt(_atlas_user=ALICE)


def test_sweep_is_scoped_to_calling_tenant(mt_cfg) -> None:
    write_mt(topic_id="old", content="alice expired", retention="2000-01-01T00:00:00Z", _atlas_user=ALICE)
    write_mt(topic_id="old", content="bob expired", retention="2000-01-01T00:00:00Z", _atlas_user=BOB)

    out = sweep_mt(dry_run=False, _atlas_user=ALICE)
    assert "old" in out

    # Alice's expired topic was swept into her own trash...
    with pytest.raises(ToolError, match="discover_topics"):
        read_mt(topic_id="old", _atlas_user=ALICE)
    assert list((mt_cfg.root / "alice_example_com" / ".trash").rglob("*.md"))
    # ...while Bob's identical topic is untouched.
    assert "bob expired" in read_mt(topic_id="old", _atlas_user=BOB)
    assert not (mt_cfg.root / "bob_example_org" / ".trash").exists()


def test_primed_instructions_do_not_leak_across_tenants(mt_cfg) -> None:
    write_mt(topic_id="secret/alice-only", content="alice secret", _atlas_user=ALICE)
    write_mt(topic_id="secret/bob-only", content="bob secret", _atlas_user=BOB)

    # Server instructions are shared, so in multi-tenant mode they must carry
    # no tenant's catalog at all.
    assert "alice-only" not in mcp.instructions
    assert "bob-only" not in mcp.instructions
    assert "Multi-tenant mode is on" in mcp.instructions

    # Priming scoped to a tenant root shows only that tenant's topics.
    prime_a, _ = store.prime_catalog(mt_cfg.root / "alice_example_com")
    prime_b, _ = store.prime_catalog(mt_cfg.root / "bob_example_org")
    assert "secret/alice-only" in prime_a and "secret/bob-only" not in prime_a
    assert "secret/bob-only" in prime_b and "secret/alice-only" not in prime_b


def test_wiki_links_resolve_only_within_tenant(mt_cfg) -> None:
    write_mt(topic_id="hub", content="see [[spoke]]", _atlas_user=ALICE)
    write_mt(topic_id="spoke", content="alice's spoke, back to [[hub]]", _atlas_user=ALICE)
    write_mt(topic_id="spoke", content="bob's unrelated spoke", _atlas_user=BOB)

    data = json.loads(related_mt(topic_id="hub", _atlas_user=ALICE))
    assert data["outbound"] == ["spoke"]
    assert data["inbound"] == ["spoke"]  # alice's spoke only; bob's is invisible

    # Bob has no hub at all.
    with pytest.raises(ToolError, match="discover_topics"):
        related_mt(topic_id="hub", _atlas_user=BOB)


def test_rename_rewrites_links_only_within_tenant(mt_cfg) -> None:
    write_mt(topic_id="people/garland", content="Anthony", _atlas_user=ALICE)
    write_mt(topic_id="projects/team", content="Member: [[people/garland]]", _atlas_user=ALICE)
    write_mt(topic_id="projects/team", content="Member: [[people/garland]]", _atlas_user=BOB)

    out = rename_mt(old_id="people/garland", new_id="people/anthony-garland", _atlas_user=ALICE)
    assert "1 wiki links" in out

    # Alice's link was rewritten...
    assert "[[people/anthony-garland]]" in read_mt(topic_id="projects/team", _atlas_user=ALICE)
    # ...Bob's identical file was not touched.
    assert "[[people/garland]]" in read_mt(topic_id="projects/team", _atlas_user=BOB)


def test_tenant_cannot_escape_into_another_tenant(mt_cfg) -> None:
    write_mt(topic_id="bob-secret", content="bob only", _atlas_user=BOB)
    # The §3 resolve() escape rules apply after the tenant segment is prepended.
    with pytest.raises(ToolError, match="cannot contain '..'"):
        read_mt(topic_id="../bob_example_org/bob-secret", _atlas_user=ALICE)
    with pytest.raises(ToolError, match="cannot contain '..'"):
        write_mt(topic_id="a/../../bob_example_org/bob-secret", content="pwned", _atlas_user=ALICE)
    # A topic id that merely *names* another slug stays inside alice's folder.
    write_mt(topic_id="bob_example_org/fake", content="alice-local", _atlas_user=ALICE)
    assert (mt_cfg.root / "alice_example_com" / "bob_example_org" / "fake.md").is_file()
    assert "bob only" in read_mt(topic_id="bob-secret", _atlas_user=BOB)


def test_slug_collision_tenants_share_folder_accepted(mt_cfg) -> None:
    # §13.5: documented as accepted — both emails sanitize to a_b_c_d.
    write_mt(topic_id="shared-note", content="written via a.b@c.d", _atlas_user="a.b@c.d")
    out = discover_topics_mt(_atlas_user="a_b@c_d")
    assert "shared-note" in out
    assert "written via a.b@c.d" in read_mt(topic_id="shared-note", _atlas_user="a_b@c_d")


def test_memory_resource_refused_in_multi_tenant_mode(mt_cfg) -> None:
    write_mt(topic_id="x", content="alice", _atlas_user=ALICE)
    from basic_memory_mcp.server import memory_resource

    out = memory_resource("x")
    assert "disabled in multi-tenant mode" in out


# ---------------------------------------------------------------------------
# §13.2: CLI flag and env var, end to end over stdio


def _stdio_transport(tmp_path, extra_args=(), extra_env=None) -> StdioTransport:
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    return StdioTransport(
        command=sys.executable,
        args=["-m", "basic_memory_mcp", "--stdio", "--root", str(tmp_path), *extra_args],
        env=env,
    )


async def test_cli_default_schemas_have_no_atlas_user(tmp_path) -> None:
    async with Client(_stdio_transport(tmp_path)) as c:
        tools = await c.list_tools()
    assert tools
    for t in tools:
        assert "_atlas_user" not in t.inputSchema.get("properties", {})


async def test_cli_multi_tenant_flag_adds_atlas_user(tmp_path) -> None:
    async with Client(_stdio_transport(tmp_path, extra_args=["--multi-tenant"])) as c:
        tools = await c.list_tools()
    assert tools
    for t in tools:
        assert "_atlas_user" in t.inputSchema.get("properties", {}), t.name


async def test_cli_multi_tenant_env_var_adds_atlas_user(tmp_path) -> None:
    async with Client(_stdio_transport(tmp_path, extra_env={"BASIC_MEMORY_MULTI_TENANT": "1"})) as c:
        tools = await c.list_tools()
    assert tools
    for t in tools:
        assert "_atlas_user" in t.inputSchema.get("properties", {}), t.name


async def test_cli_end_to_end_tenant_write_and_refusal(tmp_path) -> None:
    async with Client(_stdio_transport(tmp_path, extra_args=["--multi-tenant"])) as c:
        # A call with an injected user lands under the tenant folder.
        await c.call_tool("write", {"topic_id": "notes/hi", "content": "hello", "_atlas_user": ALICE})
        assert (tmp_path / "alice_example_com" / "notes" / "hi.md").is_file()

        # A call without the injected user is refused, not mapped to a default.
        with pytest.raises(Exception, match="_atlas_user is required"):
            await c.call_tool("discover_topics", {})
