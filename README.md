# basic-memory-mcp

An MCP server that gives a model a persistent notebook backed by a folder of Markdown files.

## Tools

- `discover_topics(query=None, tag=None, limit=50)` — list topics, optionally filtered.
- `read(topic_id)` — return the full Markdown file including frontmatter.
- `write(topic_id, content, title=None, tags=[])` — create or replace a topic.
- `edit(topic_id, old, new, replace_all=False)` — exact string replacement.
- `delete(topic_id)` — soft-delete into `<root>/.trash/<timestamp>/...`.

Topic ids are slash-paths relative to `--root` without `.md`, e.g. `projects/atlas-ports`.

## Usage

```bash
# stdio (default)
basic-memory-mcp --stdio --root ~/ATLAS-GROUP/basic-memory/memory

# HTTP
basic-memory-mcp --http --host 127.0.0.1 --port 8101 --root ...
```

Set `BASIC_MEMORY_READ_ONLY=1` or `--read-only` to expose only `discover_topics` and `read`.
Set `BASIC_MEMORY_HARD_DELETE=1` or `--hard-delete` to unlink files instead of moving them
(only for throwaway/test roots).

## Multi-tenancy (ATLAS deployments only, off by default)

`--multi-tenant` (or `BASIC_MEMORY_MULTI_TENANT=1`) namespaces every topic per
authenticated user, for the ATLAS deployment where the backend injects the caller's
email into tool calls.

- **Off (default):** tool schemas are exactly as above — no `_atlas_user` parameter
  exists anywhere, and behavior is byte-identical to a single-user server.
- **On:** every tool grows a trailing `_atlas_user: str | None = None` parameter
  ("injected by the Atlas backend; do not supply"). ATLAS fills it from the
  authenticated session and strips anything the model supplies, so the LLM cannot
  impersonate another user. A call arriving without it is **refused** with a clear
  error — never silently mapped to a shared/default tenant.
- The email is sanitized to a filesystem-safe slug (lowercase, `@` and `.` → `_`,
  must match `[a-z0-9_-]+` afterward): `garland3@gmail.com` → `garland3_gmail_com/`.
  All topic ids then resolve under `<root>/<slug>/`, and the usual path-escape rules
  apply after prepending, so one tenant can never name another tenant's files.
- Each tenant gets their own `.trash/`; `sweep`, catalog priming, and
  `discover_topics` are scoped to the calling tenant. `memory://` resources are
  disabled in this mode (resources get no `_atlas_user` injection, so they cannot be
  scoped to the caller).
- **Accepted slug collision:** `a.b@c.d` and `a_b@c_d` sanitize to the same slug and
  share a folder. Emails are the trust boundary and ATLAS authenticates them, so this
  requires two authenticated users with pathological addresses.
- This is namespacing for the trusted-ATLAS case, **not** security against a hostile
  client — stdio gives whoever spawns the process the whole folder anyway.
- **No migration tool.** Turning the flag on over an existing flat root leaves old
  topics invisible; move them under the tenant folder with a one-time
  `mv <root>/projects <root>/<slug>/` (etc.) if you enable it later.

## Storage

Files are UTF-8 Markdown with YAML frontmatter; only `.md` files are visible. The server
manages `created` and `updated`; the model supplies `title` and `tags`. The memory folder is
plain files, so `git` and ordinary editors work.

## Development

```bash
uv sync
uv run pytest
```
