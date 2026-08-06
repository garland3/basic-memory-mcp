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

## Storage

Files are UTF-8 Markdown with YAML frontmatter; only `.md` files are visible. The server
manages `created` and `updated`; the model supplies `title` and `tags`. The memory folder is
plain files, so `git` and ordinary editors work.

## Development

```bash
uv sync
uv run pytest
```
