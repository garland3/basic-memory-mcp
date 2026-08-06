# plan.md — `basic-memory-mcp`

A small MCP server that gives a model a persistent notebook. The database is a
folder of Markdown files; there is no index, no embedding store, no SQL. Tools:
`discover_topics`, `read`, `write`, `edit`, `delete`.

Built with `uv` + `fastmcp`, modeled directly on the sibling `basic-fns-mcp/`
(same layout, same dual stdio/HTTP entrypoint, same flag style). Registered in
`atlas-ui-3/config/mcp.json` as **stdio** by default.

---

## 1. Why a folder is the right database here

The whole value of this server is that memories stay legible and editable without
it. A folder means: `grep` works, `git` works, the user can open a memory in an
editor, and a corrupted server loses nothing. The cost is that discovery is a
filesystem walk — fine at the scale this is for (hundreds of notes, not
millions). If it ever outgrows that, the fix is an *added* index that is
rebuildable from the files, never a move away from them.

## 2. Storage layout

```
<root>/                       # --root, default ~/ATLAS-GROUP/basic-memory/memory
  index.md                    # optional, human-maintained; server never writes it
  projects/atlas-ports.md
  people/garland.md
  scratch/2026-08-05-tunnel-debug.md
```

- **Topic id = path relative to root, without `.md`.** `projects/atlas-ports`.
  That is the only handle tools take. Slashes are the folder structure; the model
  creates folders implicitly by writing a nested id.
- One memory per file. Files are UTF-8 Markdown with YAML frontmatter:

```markdown
---
title: ATLAS port map
tags: [atlas, networking]
created: 2026-08-05T21:37:00Z
updated: 2026-08-05T21:37:00Z
---

Body text. Links to other memories use [[people/garland]].
```

- **Frontmatter is server-managed for `created`/`updated`, model-supplied for
  `title`/`tags`.** If a file has no frontmatter (a hand-written note), it is
  still valid — treat missing fields as empty rather than erroring. Do not
  rewrite a hand-authored file just to add frontmatter on read.
- `[[topic-id]]` wiki links are a *convention*, surfaced by `discover_topics` as
  outbound links. The server does not validate that the target exists; a dangling
  link marks something worth writing later.

## 3. Tools

Argument shapes are what the model sees, so they are terse and hard to misuse.
Every path argument goes through one `resolve(topic_id) -> Path` helper that
rejects absolute paths, `..` escapes, and symlinks pointing outside root. No tool
takes a filesystem path.

### `discover_topics(query: str | None = None, tag: str | None = None, limit: int = 50)`

The entry point — the model calls this before anything else. Returns a list of
`{id, title, tags, updated, size, snippet, links}`, newest-updated first. `links`
is the `[[topic-id]]` targets parsed out of the body — that is what makes the
wiki-link convention in §2 actually navigable rather than decorative.

- No `query`: the whole catalog (ids + titles + tags), capped at `limit`. This is
  the "what do I know" call, so it must stay cheap — title/tag come from
  frontmatter, and only the first ~200 bytes of body are read for the snippet.
- With `query`: case-insensitive substring match against id, title, tags, and
  body. A plain `str.lower() in` scan over the files. Not fuzzy, not ranked
  beyond "title/id hits before body hits" — predictable beats clever here.
- With `tag`: exact tag match, combinable with `query`.
- Always report when results were truncated by `limit`, in the response text. A
  silent cap reads as "that's everything" when it isn't.

### `read(topic_id: str)`

Returns the full file content including frontmatter. Errors with a clear
"no such topic; call discover_topics" message rather than an empty string — an
empty result gets confused with an empty memory.

### `write(topic_id: str, content: str, title: str | None = None, tags: list[str] | None = None)`

Create-or-replace. Creates parent folders. Stamps `created` (first write) and
`updated` (every write). **Overwrites without asking** — that is the documented
contract, and `edit` exists for the non-destructive case. Returns the resolved id
and whether it created or replaced.

`content` is the **body only**. Frontmatter is composed by the server from
`title`/`tags` plus its own timestamps. If `content` nonetheless starts with a
`---` block — models will do this — parse it, merge it under the explicit
`title`/`tags` arguments, and strip it from the body. Never emit two frontmatter
blocks; a file with two is unreadable by every other tool that touches the folder.
On replace, `created` and any frontmatter keys the server does not own are carried
over from the existing file rather than dropped.

### `edit(topic_id: str, old: str, new: str, replace_all: bool = False)`

Exact string replacement, same semantics as the Claude Code `Edit` tool: fails if
`old` is absent, fails if `old` appears more than once and `replace_all` is
false. Refuses to run on a topic that does not exist. Bumps `updated`.

**Matches against the body only, never the frontmatter block.** Otherwise an
`old` of `updated: ...` or a stray `---` lets an edit corrupt the header, and the
match-count check silently means something different than the model expects. To
change `title`/`tags`, use `write`.

This is the tool that keeps memories from being clobbered by a model that only
wanted to change a line, so it should be the one described most precisely in the
tool docstring.

### `delete(topic_id: str)`

Soft-removes the topic. Prunes now-empty parent folders up to (not including)
root. Returns the trash path it moved to, so the model can tell the user exactly
what to `mv` back.

**Deletes are soft — decided, not optional.** `delete` moves the file to
`<root>/.trash/<timestamp>/<topic-id>.md`; it never calls `unlink`. A model
deleting the wrong memory is a plausible failure and the recovery cost is
otherwise total.

- `.trash/` is excluded from `discover_topics` and from `read`.
- The timestamp folder preserves the relative path, so recovery is a plain `mv`
  back — no tooling needed, which is the point of a folder-as-database.
- Deleting the same topic twice leaves both copies under different timestamps;
  nothing is ever overwritten inside `.trash/`.
- No tool empties the trash and no automatic expiry runs. Cleanup is the user's
  `rm -rf`, deliberately — a server that can hard-delete on a timer has the same
  failure mode this design exists to prevent.
- `--hard-delete` exists for a throwaway/test root only. It is **not** the
  default and should not be set in the ATLAS registration.

## 4. Concurrency and safety

- Writes are atomic: write to a temp file in the same directory, `os.replace`.
  Prevents a truncated memory if the process dies mid-write.
- No cross-process locking. Single-user, single-client assumption — state it in
  the README instead of pretending otherwise with a lockfile that will rot.
- Size guard: reject a `write` over ~1 MB with a message suggesting the content
  be split across topics. Keeps one bad call from making `discover_topics` slow
  forever.
- Only `.md` files are visible. Anything else in the folder is ignored, so the
  user can keep attachments alongside without polluting the catalog.
- Dot-folders are skipped entirely on walk — `.trash/`, and `.git/` if the user
  versions the memory folder as §9 suggests. Walking `.git/` would be slow and
  would surface loose-object paths as topics.

## 4b. Done means

The build is finished when all of these hold, not when the tools exist:

- `discover_topics` on an empty root returns an empty list with a "no memories
  yet" note, not an error.
- Round-trip: `write` → `read` → `edit` → `read` preserves `created`, bumps
  `updated`, and leaves exactly one frontmatter block.
- Every path-escape attempt is refused: `../x`, `/etc/passwd`, `a/../../b`,
  `.trash/anything`, and a symlink inside root pointing out of it.
- A hand-written `.md` with no frontmatter is discoverable and readable, and
  `read` returns it byte-identical.
- `delete` then `mv` back from the reported trash path restores the topic and it
  reappears in `discover_topics`.
- The §7 step-5 handshake lists exactly five tools (three under `--read-only`).
- ATLAS starts with no `DISCOVERY FAILED` in the log.

## 5. Layout and packaging

```
basic-memory/
  pyproject.toml            # hatchling, requires-python >=3.10, fastmcp>=3.1,<4
  uv.lock
  README.md
  plan.md
  src/basic_memory_mcp/
    __init__.py
    __main__.py             # arg parsing, transport selection
    server.py               # FastMCP instance + the five tools
    store.py                # resolve(), read/write/list, frontmatter, atomic write
    config.py               # dataclass: root, read_only, hard_delete, host, port
  tests/
    test_store.py           # path escapes, frontmatter round-trip, atomic write
    test_tools.py           # each tool via an in-process fastmcp Client
```

```toml
[project.scripts]
basic-memory-mcp = "basic_memory_mcp.__main__:main"
```

## 6. CLI

```
basic-memory-mcp --stdio --root ~/ATLAS-GROUP/basic-memory/memory
basic-memory-mcp --http --host 127.0.0.1 --port 8101 --root ...
```

- `--stdio` (default) | `--http`
- `--root PATH` — created on first run if missing
- `--read-only` — discover/read only; write/edit/delete are not registered at all
  rather than registered-and-failing, so the model never sees a tool it can't use
- `--hard-delete` — skip `.trash/` and really unlink. Escape hatch for test roots
  only; soft delete is the default and stays on in the real deployment
- `--port` defaults to **8101**. Per `AGENTS.md`: new servers live in 8100+, and
  **never 8080** on this host — k3s Traefik hijacks it including on loopback, and
  the symptom (empty 500s, nothing in your log) costs an hour to diagnose. 8100
  is called out in the docs as an example, so take 8101 to avoid a collision.

## 7. Build order

1. `uv init` + `uv add fastmcp`, `uv add --dev pytest pytest-asyncio`.
2. `store.py` first, with tests — path resolution and frontmatter are where the
   real bugs live, and they are testable without any MCP machinery.
3. `server.py` wrapping store in the five tools; docstrings matter here since they
   are the model's only spec. Spell out: ids are slash-paths without `.md`,
   `write` overwrites, `edit` is exact-match, `discover_topics` comes first.
4. `__main__.py` transport wiring.
5. Standalone handshake check before wiring into ATLAS:

```bash
cd basic-memory && .venv/bin/python -c "
import asyncio; from fastmcp import Client
from fastmcp.client.transports import StdioTransport
async def m():
    async with Client(StdioTransport(command='.venv/bin/basic-memory-mcp', args=['--stdio'])) as c:
        print([t.name for t in await c.list_tools()])
asyncio.run(m())"
```

## 8. Registering with ATLAS

Add to `atlas-ui-3/config/mcp.json` — absolute paths, the server's own venv
entrypoint (not `uv run`, which resolves a different project and adds startup
latency to every ATLAS boot):

```json
"basic_memory": {
  "command": ["/home/garlan/ATLAS-GROUP/basic-memory/.venv/bin/basic-memory-mcp",
              "--stdio", "--root", "/home/garlan/ATLAS-GROUP/basic-memory/memory"],
  "cwd": "/home/garlan/ATLAS-GROUP/basic-memory",
  "transport": "stdio",
  "groups": ["users"],
  "description": "Persistent notes: discover_topics, read, write, edit, delete"
}
```

Then restart ATLAS so discovery picks it up. `config/mcp.json` currently holds
exactly one server (`basic_fns`) and a clean startup log is the baseline — any
`DISCOVERY FAILED` after this is this server's fault.

Stdio is the right default: no port, no systemd unit, no boot ordering, lifetime
tied to the app. Switch to HTTP only if a second client needs the same memory
folder — in which case add a `systemctl --user` unit modeled on `atlas.service`
(user scope, explicit `PATH`, **no `KillMode=mixed`**) and keep it on loopback.

## 9. Deliberately out of scope

Named so they don't get half-built: no embeddings/semantic search, no SQLite
index, no automatic summarization or memory consolidation, no multi-user
namespacing, no versioning beyond whatever `git init` in the memory folder gives
you (which is worth doing by hand and costs the server nothing).

## 10. Open question

**Does this replace `~/.claude/.../memory/`, or sit beside it?** That directory
already holds a working memory convention — one fact per file, frontmatter with
`name`/`description`/`type`, an `MEMORY.md` index. If the intent is for this
server to serve *that* folder, point `--root` at it and match its frontmatter
schema (`name`/`description`/`metadata.type`) instead of the
`title`/`tags` shape above; the code is identical either way. Worth deciding
before step 2, since it fixes the frontmatter model.
