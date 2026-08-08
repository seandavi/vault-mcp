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
| `search(query, type?, tag?, match?, limit?)` | read | `ranked` (default): BM25 over title+body with score + snippet lines; `exact`/`regex`: ripgrep line matches with line numbers |
| `read_note(name_or_path)` | read | resolve a vault-relative path, note name, or frontmatter alias (typos auto-correct above 0.95 similarity; below that the error carries did-you-mean candidates) |
| `related(name_or_path)` | read | graph neighborhood: outlinks, backlinks, unresolved links, shared-tag neighbors |
| `query(sql, limit?)` | read | read-only SQL (DuckDB dialect, SELECT/WITH only) over `notes` and `links` tables |
| `recent(limit?, type?)` | read | most recently modified notes |
| `create_note(title, content, tags?, source?)` | write | atomic idea note in `notes/` with template frontmatter; refuses overwrite |
| `edit_note(name_or_path, old_text, new_text)` | write | exact string replacement anywhere in a note (frontmatter included); `old_text` must occur exactly once, else the error says why |
| `update_note(name_or_path, content)` | write | replace a note's entire body; the frontmatter block is preserved verbatim |
| `rename_note(name_or_path, new_title)` | write | rename file + first H1 to the new title and rewrite `[[wiki-links]]` vault-wide (`\|alias`/`#heading` forms preserved); refuses overwrite |
| `add_inbox_item(text)` | write | open action item under `## Action needed` in `inbox.md` |
| `append_daily(text)` | write | timestamped line in today's journal `## Log`, creating the file if needed |
| `refresh_index()` | admin | force index rebuild; returns note/link counts |

Index schema for `query`:

```sql
notes(path, name, title, type, tags VARCHAR[], aliases VARCHAR[], date, status,
      frontmatter JSON, modified TIMESTAMP, size, body /* SELECT columns, not * */)
links(source /* note path */, target /* wiki-link name as written */)
```

Ranked search is DuckDB's FTS extension (BM25; digits searchable, stopwords
disabled — see `docs/research/duckdb-fts.md` for the extension's real
constraints). The FTS index builds lazily, once per rebuild, on first ranked
query.

`templates/`, `raw/`, and dot-directories are excluded from indexing and search.

## Configuration

The vault root defaults to `~/Documents/seandavis`; override with the
`VAULT_MCP_ROOT` environment variable.

Requires [ripgrep](https://github.com/BurntSushi/ripgrep) (`rg`) on PATH.

### Transports

`vault-mcp` speaks stdio by default. Pass `--http` to serve streamable HTTP
at `/mcp` (`--host`/`--port`, default `127.0.0.1:8787`; also settable via
`VAULT_MCP_HTTP`, `VAULT_MCP_HOST`, `VAULT_MCP_PORT`).

### Claude Code

```sh
# stdio (local spawn)
claude mcp add vault-memory -- uv run --directory ~/Documents/git/vault-mcp vault-mcp

# HTTP (shared server, e.g. over the tailnet)
claude mcp add --transport http vault-memory https://<machine>.<tailnet>.ts.net/mcp
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

## Serving the tailnet

One HTTP server co-located with the vault gives every dev machine the same
memory substrate — one canonical index, one writer (which also keeps
Obsidian-sync conflicts down, since remote machines write through the API
instead of writing files and hoping sync merges them).

### macOS (launchd)

On the (Mac) machine that owns the vault, run the server as a LaunchAgent so
it starts at login and restarts if it dies:

```sh
mkdir -p ~/.local/state   # log destination
cp deploy/com.seandavis.vault-mcp.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.seandavis.vault-mcp.plist
```

The agent runs `deploy/vault-mcp-tailnet.sh`, which waits for tailscaled,
resolves the machine's Tailscale IP, and binds it directly on port 9321 —
no `tailscale serve` layer needed. Verify and manage it with:

```sh
launchctl print gui/$(id -u)/com.seandavis.vault-mcp | head   # state
tail -f ~/.local/state/vault-mcp-http.log                     # logs
launchctl kickstart -k gui/$(id -u)/com.seandavis.vault-mcp   # restart (e.g. after git pull)
launchctl bootout gui/$(id -u)/com.seandavis.vault-mcp        # stop + unload
```

Clients on the tailnet connect to `http://<tailscale-ip>:9321/mcp`, e.g.:

```sh
claude mcp add --transport http vault-memory http://100.72.62.9:9321/mcp
```

### Linux (systemd)

The same wrapper script works as a systemd user service on a Linux tailnet
member — `deploy/vault-mcp.service` carries the install steps in its header
(copy to `~/.config/systemd/user/`, `systemctl --user enable --now vault-mcp`,
and `loginctl enable-linger` so it survives logout).

### Security model

On the tailnet the server runs with no auth; Tailscale is the auth layer.
That holds only while it binds the machine's Tailscale IP (what the wrapper
does) or loopback behind `tailscale serve` — never bind `0.0.0.0`. If you
want TLS and a stable DNS name instead of the raw IP, the loopback +
`tailscale serve --bg --https=443 127.0.0.1:8787` arrangement still works;
the direct bind is just fewer moving parts.

## OAuth (optional)

For any deployment where network trust isn't enough (the public Bioconductor
layer, or defense-in-depth on the tailnet), turn on OAuth:

```sh
export VAULT_MCP_OAUTH_CLIENT_ID=$(gcloud secrets versions access latest --secret=vault-mcp-oauth-client-id)
export VAULT_MCP_OAUTH_CLIENT_SECRET=$(gcloud secrets versions access latest --secret=vault-mcp-oauth-client-secret)

vault-mcp --http --auth google --base-url https://<machine>.<tailnet>.ts.net
```

This is the MCP spec's OAuth 2.1 flow (via FastMCP's OAuth proxy): clients
like Claude Code discover the server's auth metadata and pop the browser
login on their own — the `claude mcp add --transport http ...` line doesn't
change. Register `<base-url>/auth/callback` as an authorized redirect URI on
the OAuth client (Google Cloud console → Credentials).

Providers are a registry in `src/vault_mcp/auth.py` — `google` and `github`
are wired; adding another is one entry (all FastMCP providers take
`client_id` / `client_secret` / `base_url`). For launchd, use
`deploy/vault-mcp-http.sh`, which pulls the credentials from Google Secret
Manager at boot so secrets never sit in the plist.

## Development

```sh
uv run pytest                    # fixture-vault tests + a read-only smoke test on the real vault
uv run vault-mcp                 # run the server on stdio
uv run python -m vault_mcp.eval  # retrieval eval (query set: <vault>/.vault-mcp/eval.yaml)
```

## Retrieval benchmark

`vault_mcp.eval` runs a fixed query set against each search engine and reports
rank-of-first-expected-hit, hit rate, MRR, and latency per engine
(`uv run python -m vault_mcp.eval`). Query sets reference real note paths, so
they live inside the vault (`<vault>/.vault-mcp/eval.yaml`), never in this repo.

Representative results on a ~2,200-note vault, 11 queries spanning topical
paraphrases, substring/exact-phrase/regex lookups, and digit-bearing
identifiers:

| engine | hit@5 | hit@10 | mean latency |
| --- | --- | --- | --- |
| `ranked` (BM25) | 70% | 90% | ~100 ms |
| `exact` (ripgrep) | 40% | 40% | ~75 ms |

The engines are complementary, not redundant: substring, exact-phrase, and
regex queries all miss in ranked mode and hit in exact mode (BM25 tokenizes
and has no phrase syntax), while topical paraphrases do the reverse (literal
matching can't cross word gaps). The first ranked query after a rebuild pays
the lazy BM25 index build (~200 ms at this size); a warm full index rebuild is
~630 ms.

Known failure mode: natural-language questions against long notes — the FTS
extension normalizes even title-restricted matches by whole-document length,
so short notes outrank long ones with exact title matches. A title-term bonus
is the planned fix (tracked on the wayfinder map).

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
