# vault-mcp

An MCP server that exposes a markdown vault (Obsidian-style: YAML frontmatter +
`[[wiki-links]]`) as a **shared memory substrate** for coding/research agents —
Claude Code, Gemini CLI, Codex, or anything else that speaks MCP.

## Design

- **The vault is the source of truth.** Durable, human-readable markdown files,
  versioned by git/jj. The server never stores state anywhere else.
- **The DuckDB index is disposable infrastructure.** Frontmatter and wiki-links
  are parsed into an in-memory DuckDB database that backs search filters,
  `related`, and `query`. It is rebuilt from the files on demand (30s TTL,
  invalidated on every write) and is never authoritative.
- **Writes are constrained verbs, never arbitrary file writes.** Each write
  tool targets one convention-enforced location (an atomic note, an inbox
  item, a journal log line). Version control is the safety net.

The rule that matters more than the machinery, baked into the server's MCP
instructions so every connected agent receives it:

> The vault is a collection of durable, human-readable artifacts — not an
> agent transcript store. Do not create memories merely because information
> appeared in a conversation. Write a memory only when it represents a durable
> fact, decision, idea, relationship, or useful piece of project context.

## Tool surface

| Tool | Kind | What it does |
| --- | --- | --- |
| `search(query, type?, tag?, regex?, limit?)` | read | ripgrep full-text search, filtered by indexed metadata, ranked by match count |
| `read_note(name_or_path)` | read | resolve a wiki-link name or vault-relative path; return content |
| `related(name_or_path)` | read | graph neighborhood: outlinks, backlinks, unresolved links, shared-tag neighbors |
| `query(sql, limit?)` | read | read-only SQL (DuckDB dialect, SELECT/WITH only) over `notes` and `links` tables |
| `recent(limit?, type?)` | read | most recently modified notes |
| `create_note(title, content, tags?, source?)` | write | atomic idea note in `notes/` with template frontmatter; refuses overwrite |
| `add_inbox_item(text)` | write | open action item under `## Action needed` in `inbox.md` |
| `append_daily(text)` | write | timestamped line in today's journal `## Log`, creating the file if needed |
| `refresh_index()` | admin | force index rebuild; returns note/link counts |

Index schema for `query`:

```sql
notes(path, name, title, type, tags VARCHAR[], date, status, frontmatter JSON, modified TIMESTAMP, size)
links(source /* note path */, target /* wiki-link name as written */)
```

`templates/`, `raw/`, and dot-directories are excluded from indexing and search.

## Configuration

The vault root defaults to `~/Documents/seandavis`; override with the
`VAULT_MCP_ROOT` environment variable.

Requires [ripgrep](https://github.com/BurntSushi/ripgrep) (`rg`) on PATH.

### Claude Code

```sh
claude mcp add vault-memory -- uv run --directory ~/Documents/git/vault-mcp vault-mcp
```

### Gemini CLI (`~/.gemini/settings.json`)

```json
{
  "mcpServers": {
    "vault-memory": {
      "command": "uv",
      "args": ["run", "--directory", "/Users/davsean/Documents/git/vault-mcp", "vault-mcp"]
    }
  }
}
```

### Codex (`~/.codex/config.toml`)

```toml
[mcp_servers.vault-memory]
command = "uv"
args = ["run", "--directory", "/Users/davsean/Documents/git/vault-mcp", "vault-mcp"]
```

## Development

```sh
uv run pytest        # fixture-vault tests + one read-only smoke test on the real vault
uv run vault-mcp     # run the server on stdio
```

## Roadmap

- **v0.2 — consolidation agent.** Nightly promotion pass modeled on memory
  consolidation: scan the episodic tier (journal, inbox), search existing
  memories, then *propose* creates/merges/updates for human approval — never
  silent rewrites of the long-term store.
- **Public community layer.** Anonymous-read project memory for a community
  (first target: Bioconductor) — same primitives (files + index + MCP), plus a
  curated `INDEX.md` as the human orientation layer, served without exposing
  private state.
- **Embeddings — only if needed.** Added as another disposable index, and only
  once keyword + metadata + link retrieval demonstrably misses; not part of
  the ontology.
