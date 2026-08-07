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

## 11. Phase 2 — after §4b passes

Not part of the first build. Every item here assumes the five core tools and the
§4b checklist are green; none of them are worth starting while `write` can still
emit two frontmatter blocks. Ordered by value per line of code.

### 11.1 `append(topic_id: str, text: str, heading: str | None = None)`

The missing verb. Adding one fact to an existing memory is the most common
operation there is, and today it forces a choice between `write` (round-trips the
whole body through the model, clobbers on any mistake) and `edit` (needs an exact
anchor string the model has to guess). `append` removes the largest remaining way
to destroy a memory by accident.

- Appends to the body, never the frontmatter. Bumps `updated`.
- Ensures exactly one blank line between the old body and `text` — no
  `\n\n\n\n` accretion after twenty appends.
- With `heading`: append under that `##` heading if it exists, else create it at
  the end. This is what keeps a long-lived memory organized instead of becoming a
  chronological pile.
- Fails on a missing topic rather than creating one. `write` is the create verb;
  an `append` that silently creates hides a mistyped id.
- Same ~1 MB guard as `write`, measured against the *resulting* file — the guard
  exists to bound `discover_topics`, and appends are how a file gets big.

### 11.2 Backlinks and `rename`

`parse_wiki_links` (`store.py:169`) already exists and nothing consumes it. Two
tools fall out of wiring it up:

**`related(topic_id)`** → `{outbound, inbound}`. Outbound is a parse of one file;
inbound is a scan for `[[topic_id]]` across the folder, the same walk
`discover_topics` already does. This is what turns `[[…]]` from decoration into
navigation — a model that finds one relevant memory can then follow the graph
instead of re-searching.

**`rename(old_id, new_id)`** — moves the file *and* rewrites `[[old_id]]` →
`[[new_id]]` everywhere. Without it, every reorganization silently breaks every
link that pointed into the moved subtree, and reorganization is exactly what a
growing memory folder needs.

- Rewrite prefixes too: renaming `scratch/` → `archive/scratch/` must fix
  `[[scratch/x]]` links, not just exact-id matches.
- Refuse if `new_id` exists — this is a move, not a merge.
- Prune empty parents like `delete` does; create new ones like `write` does.
- Report the count of rewritten links in the result. A rename that touched 14
  files should say so.

Also add `links` to the `discover_topics` return shape (still missing — see the
defect list; `load_topic` computes `outbound_links` but discover drops it).

### 11.3 Match-centered snippets

`store.py:228` always returns the first ~200 bytes of the body, even when the
query matched on line 40. So a search result shows a preamble that does not
contain the thing that was searched for, and the model must `read` every
candidate to find out which one is relevant.

Center the snippet on the first match instead: ~80 bytes either side, `…` on
whichever end is truncated, and keep the head-of-body behavior only for the
no-query catalog call. This is the change that lets `discover_topics` answer a
question by itself rather than acting as a router to N `read` calls — the single
biggest lever on tokens-per-recall in the whole server.

If a topic matched on title/id/tag but not in the body, keep the head snippet and
say why it matched. A snippet that shows nothing relevant reads as a bad hit.

### 11.4 Catalog priming

The plan assumes the model calls `discover_topics` first, but nothing enforces
that, and a model with no reason to suspect memories exist will not ask. A memory
that is never queried is the same as no memory at all — this is the item that
decides whether the whole server gets used.

Two mechanisms, not exclusive:

- **Dynamic instructions.** `server.py:26` sets `instructions` once at import.
  Recompute them at startup (and cheaply on change) to include the current topic
  id list — ids and titles only, which is small even at a few hundred notes. The
  model then sees what is known without spending a tool call.
- **MCP resources.** Expose each topic as a resource (`memory://<topic-id>`) so a
  client can attach one directly. FastMCP supports this alongside tools; it costs
  a second registration pass over the same `_iter_topics` walk.

Cap the primed catalog by count and total bytes, and say in the text when it was
truncated — the §3 no-silent-caps rule applies here too. If the folder outgrows
the cap, prime with the most recently updated N and let `discover_topics` cover
the tail.

### 11.5 Multi-term search

`query in text` (`store.py:220`) is a single substring test, so `"atlas ports"`
matches only that exact adjacent phrase — a note titled "ATLAS port map" does not
come back. That is a surprising miss for the most natural way to phrase a query.

- Split the query on whitespace and require **all** terms to be present (AND),
  each still a case-insensitive substring so partial words keep working.
- Rank by number of distinct terms matched, then by the existing title/id-before-
  body rule, then by `updated` descending. (Note the current sort is inverted —
  fix that defect first or this ranking inherits the bug.)
- Quoted `"exact phrase"` falls back to today's single-substring behavior, so
  there is still a way to ask for adjacency.
- Still no stemming, no fuzzy matching, no ranking cleverness beyond term count.
  §1's rule holds: predictable beats clever, because the model retries a bad
  query far better than it second-guesses a mysterious ranking.

### Deferred from this list

Git-backed history, a `restore` tool, a mutation journal, `review(older_than_days)`,
and a SQLite FTS5 index were all considered and left out of phase 2 — worth
revisiting once real usage shows whether recall or rot is the actual problem.
§9's exclusions (embeddings, semantic search, multi-user namespacing) still stand.

## 12. Retention — ephemeral vs long-term

The problem this solves is not disk. It is that §11.4's primed catalog is the
model's first impression of what it knows, and without expiry that list fills
with `scratch/2026-08-05-tunnel-debug` entries forever until the signal is buried
in yesterday's debugging. Retention is what keeps priming useful at 500 notes.

### 12.1 One axis, not two categories

"Just now" and "always" are the ends of a single axis, and most real memories sit
in the middle ("relevant for this project", "true until we migrate off k3s"). So:
one frontmatter field, `expires`, holding an ISO-8601 date, plus the sentinel
`never`.

```yaml
---
title: tunnel debug scratch
expires: 2026-08-06T00:00:00Z     # or: never
---
```

- **Default when unset is `never`.** A memory written by a model that has never
  heard of this feature, or by a human in an editor, must not silently evaporate.
  Permanence is the safe default; ephemerality is opt-in.
- `never` is stored explicitly rather than by omission when the model asserts it,
  so "deliberately permanent" is distinguishable from "didn't say."

### 12.2 Why time and not "session"

The tempting spelling is `retention: session`, and it is wrong here: **the server
cannot observe conversation boundaries.** Over stdio its lifetime is ATLAS's
lifetime, not the chat's; over HTTP it may serve several clients at once. A
`session` value would either never fire or fire at an unrelated moment, which is
worse than not having the feature.

So the model declares a *duration* and the server stamps an absolute time. Tools
accept a friendly `retention` argument and convert:

| argument | becomes |
|---|---|
| `"session"` / `"today"` | now + 24h |
| `"week"`, `"month"` | now + 7d / 30d |
| `"permanent"` (default) | `never` |
| an ISO-8601 date | itself |

Storing absolute, not relative, is deliberate — a `+24h` in the file would mean
something different every time it is read, and the folder has to be interpretable
without the server.

### 12.3 Tool surface

- `write(..., retention: str = "permanent")` and `append(..., retention: str | None = None)`.
  On `append`, `None` leaves the existing value alone; passing one **extends**
  the expiry (never shortens it — an append is evidence the memory is still
  live, and silently shortening the life of a note someone just added to is
  surprising).
- `discover_topics(include_expired: bool = False)` — expired topics are hidden by
  default and shown with an `expired: true` marker when asked for.
- `sweep(dry_run: bool = True)` → soft-deletes expired topics into `.trash/`,
  returning the list. `dry_run` defaults **true** so the destructive form is
  always a deliberate second call.

### 12.4 Expiry is visibility, not deletion

**Nothing disappears on its own.** An expired topic stays on disk, readable by
`read` at its id, and greppable — it is only dropped from discovery and from
priming. §4's rule against automatic hard-delete on a timer applies with full
force here; the whole design leans on the folder still being a folder.

Deletion happens only when a human or a model explicitly calls `sweep`, and even
then it goes to `.trash/`, which is exactly what soft delete was built for. A
`--sweep-on-start` flag is acceptable (it is a human running a command), an
in-process timer is not.

### 12.5 Interaction with priming and search

- **Priming skips expired topics entirely.** This is the point of the feature.
- `discover_topics` de-ranks *near*-expiry topics below permanent ones at equal
  relevance, so decay is gradual rather than a cliff. Do not let this override a
  strong term match — §11.5's ranking stays primary.
- Report expired-and-hidden counts the same way truncation is reported: "3
  expired topics hidden; pass include_expired=true". Silence here reads as "that
  is everything," the same failure the §3 cap rule exists to prevent.

### 12.6 Done means

- A file with no `expires` is treated as permanent and is never hidden or swept.
- A malformed `expires` value is treated as permanent, not as expired — parse
  failure must never cause disappearance.
- `sweep(dry_run=True)` writes nothing; `sweep(dry_run=False)` lands files in
  `.trash/` recoverable by the §4 `mv`.
- An expired topic is absent from priming and default discovery, present with
  `include_expired=true`, and still readable by id.
- `append` to an expiring topic extends its life and never shortens it.

## 13. Phase 3 — optional multi-tenancy via `_atlas_user` (off by default)

§9 excluded multi-user namespacing from the core build, and that stays true for
the default configuration. This section adds it as an **opt-in flag** for the
ATLAS deployment specifically, because ATLAS already has a clean, spoof-proof way
to tell the server who is calling.

### 13.1 How ATLAS injects the user (looked up, not guessed)

The mechanism lives in `atlas-ui-3` and is *schema-driven*:

- A tool opts in by declaring a parameter literally named `_atlas_user` in its
  schema. `tool_accepts_atlas_user()`
  (`atlas/application/chat/utilities/tool_executor.py:301`) inspects the tool's
  JSON schema `properties` for that key.
- If present, the backend sets `parsed_args["_atlas_user"] = user_email`
  (`tool_executor.py:744-745`), where `user_email` is the authenticated user's
  email from the session context (`X-User-Email` header upstream).
- Anything the **model** supplied for `_atlas_user` is stripped and re-injected
  server-side (`atlas/modules/mcp_tools/mcp_execution.py:630-633`), so the LLM
  cannot impersonate another user. The working demo is
  `atlas-ui-3/atlas/mcp/username-override-demo/main.py`.

So the contract is simply: *declare the parameter and ATLAS fills it*. No
handshake, no auth code in this server.

### 13.2 Design: `--multi-tenant` flag

```
basic-memory-mcp --stdio --root ... --multi-tenant
```

- **Off (default):** tool signatures are exactly the §3 shapes — no
  `_atlas_user` parameter exists anywhere. This matters for the common case:
  most clients are a local Claude Code / Cursor / whatever running the server on
  their own machine. They have no injector, would see a weird underscore
  parameter they don't understand, and might try to fill it. Absent-by-default
  means the schema-driven injection simply never triggers and local behavior is
  byte-identical to today.
- **On:** every tool grows a trailing
  `_atlas_user: str | None = None` parameter (docstring: "injected by the Atlas
  backend; do not supply"). The store then resolves all topic ids under
  `<root>/<tenant>/` instead of `<root>/`.

The flag changes *registered schemas*, not runtime branching inside one schema —
same pattern as `--read-only` in §6, which unregisters tools rather than
registering-and-failing.

### 13.3 Tenant → folder mapping

- Tenant key is the injected email, sanitized to a filesystem-safe slug:
  lowercase, `@` and `.` → `_`, reject anything that doesn't match
  `[a-z0-9_-]+` after sanitizing. `garland3@gmail.com` →
  `garland3_gmail_com/`.
- The slug becomes a path segment **prepended by the server**, never taken from
  a topic id — the §3 `resolve()` escape rules apply after prepending, so one
  tenant can never name another tenant's files.
- Each tenant gets their own `.trash/` under their folder; `sweep`, priming
  (§11.4), and `discover_topics` are all scoped to the calling tenant.
- `_atlas_user` missing or `None` while `--multi-tenant` is on → the call is
  **refused** with a clear error, not silently mapped to a shared/default
  tenant. A misconfigured deployment should fail loudly rather than mingle
  users' memories.

### 13.4 What this deliberately is not

- Not security against a hostile client — stdio gives whoever spawns the
  process the whole folder anyway. It is *namespacing* for the trusted-ATLAS
  case, where the backend injection is the only source of the tenant id.
- Not enabled in the §8 ATLAS registration until there is actually a second
  user; single-user ATLAS keeps the flat layout so existing topic ids and the
  hand-editability story (§1) are untouched.
- No migration tool. Turning the flag on over an existing flat root leaves old
  topics invisible; moving them under a tenant folder is a one-time `mv` the
  README documents.

### 13.5 Done means

- With the flag off, `list_tools` schemas contain no `_atlas_user` anywhere.
- With the flag on, every tool declares it, and a call without it errors.
- Two different injected emails see disjoint catalogs, trashes, and primed
  instructions; a `[[link]]` written by tenant A resolves only within A.
- A sanitized-slug collision test: `a.b@c.d` and `a_b@c_d` map to the same slug
  — document this as accepted (emails are the trust boundary, ATLAS
  authenticates them; the collision requires two *authenticated* users with
  pathological addresses).
