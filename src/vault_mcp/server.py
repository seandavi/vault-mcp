"""FastMCP server exposing the vault as a shared memory substrate.

Storage truth lives in the markdown files (versioned by jj/git); the DuckDB
index is a disposable cache. Writes go through constrained verbs only.
"""

from __future__ import annotations

from typing import Any

import click
from fastmcp import FastMCP

from vault_mcp.auth import PROVIDERS, build_auth
from vault_mcp.service import VaultService

INSTRUCTIONS = """\
This server exposes a personal knowledge vault as a memory substrate.

The vault is a collection of durable, human-readable artifacts — not an agent
transcript store. Do not create memories merely because information appeared
in a conversation. Write a memory only when it represents a durable fact,
decision, idea, relationship, or useful piece of project context.

Write verbs and their stores:
- append_daily: episodic capture — timestamped log line in today's journal.
- add_inbox_item: prospective memory — an open action item.
- create_note: promoted semantic memory — one atomic idea per note, linked
  generously with [[wiki-links]] to related notes.
- edit_note: surgical correction of an existing note — exact, unique
  string replacement.
- update_note: wholesale rewrite of a note's body (frontmatter preserved).

Read before you write: use search/related to check whether a memory already
exists, and prefer linking to it over duplicating it. Before editing, always
read_note first and quote the current text exactly; prefer edit_note over
update_note so text you did not re-send cannot be lost.
"""

mcp = FastMCP("vault-memory", instructions=INSTRUCTIONS)
_service: VaultService | None = None


def get_service() -> VaultService:
    global _service
    if _service is None:
        _service = VaultService()
    return _service


@mcp.tool
async def search(
    query: str,
    type: str | None = None,
    tag: str | None = None,
    match: str = "ranked",
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Search the vault's markdown files.

    Args:
        query: What to find.
        type: Filter by note type — the frontmatter `type` field, else the
            top-level directory (e.g. notes, decisions, journal, people, wiki).
        tag: Filter to notes carrying this frontmatter tag.
        match: "ranked" (default) — BM25 relevance over title + body; best for
            topical recall ("what do I know about X"). Terms are stemmed and
            OR'd; there is no phrase or boolean syntax. "exact" — literal
            substring line-match via ripgrep, with line numbers; use for
            identifiers and exact phrases. "regex" — ripgrep regular
            expression.
        limit: Maximum number of notes to return.

    Ranked hits carry a bm25 `score` and up to 3 `snippet` lines; exact/regex
    hits carry up to 5 matching lines with line numbers.
    """
    return await get_service().search(query, type=type, tag=tag, match=match, limit=limit)


@mcp.tool
def read_note(name_or_path: str) -> dict[str, str]:
    """Read a note by wiki-link name (e.g. 'icegate') or vault-relative path
    (e.g. 'notes/icegate.md'). Returns the resolved path and full content."""
    return get_service().read(name_or_path)


@mcp.tool
def related(name_or_path: str) -> dict[str, Any]:
    """Graph neighborhood of a note: resolved outgoing [[wiki-links]],
    unresolved link targets, backlinks (notes linking here), and up to 10
    notes sharing frontmatter tags."""
    return get_service().related(name_or_path)


@mcp.tool
def query(sql: str, limit: int = 200) -> dict[str, Any]:
    """Run read-only SQL (DuckDB dialect; SELECT/WITH only) over the vault
    metadata index. The index is rebuilt from the files — it is never a
    source of truth.

    Schema:
        notes(path, name, title, type, tags VARCHAR[], aliases VARCHAR[],
              date, status, frontmatter JSON, modified TIMESTAMP, size,
              body /* full note text — SELECT specific columns, not * */)
        links(source /* path */, target /* wiki-link name as written */)

    Examples:
        SELECT type, count(*) FROM notes GROUP BY type ORDER BY 2 DESC
        SELECT path, title FROM notes WHERE list_contains(tags, 'memory')
        SELECT target, count(*) n FROM links GROUP BY target ORDER BY n DESC LIMIT 20
        SELECT path, json_extract_string(frontmatter, '$.status') FROM notes WHERE type = 'projects'
    """
    return get_service().query(sql, limit=limit)


@mcp.tool
def recent(limit: int = 10, type: str | None = None) -> list[dict[str, Any]]:
    """Most recently modified notes, optionally filtered by type."""
    return get_service().recent(limit=limit, type=type)


@mcp.tool
def create_note(
    title: str, content: str, tags: list[str] | None = None, source: str = ""
) -> dict[str, str]:
    """Create an atomic idea note in notes/ (one idea per note, template
    frontmatter enforced). Use for durable ideas/insights worth keeping —
    search first to avoid duplicating an existing note; link related notes
    in the content with [[wiki-links]]. Fails if the note already exists."""
    return get_service().create_note(title, content, tags=tags, source=source)


@mcp.tool
def edit_note(name_or_path: str, old_text: str, new_text: str) -> dict[str, str]:
    """Surgically correct an existing note by exact string replacement.

    old_text must occur exactly once in the note's current text — read_note
    first and copy the passage verbatim, adding surrounding lines if it is
    not unique. Works anywhere in the file, frontmatter included. Prefer
    this over update_note for corrections: it cannot drop text you did not
    quote."""
    return get_service().edit_note(name_or_path, old_text, new_text)


@mcp.tool
def update_note(name_or_path: str, content: str) -> dict[str, str]:
    """Rewrite a note's entire body. The frontmatter block is preserved
    verbatim (use edit_note to change frontmatter). Everything below it is
    replaced with content — read_note first and carry forward anything that
    should survive. For targeted corrections use edit_note instead."""
    return get_service().update_note(name_or_path, content)


@mcp.tool
def add_inbox_item(text: str) -> dict[str, str]:
    """Add an open action item to the vault inbox (the source of truth for
    all open action items). Use for tasks/follow-ups, not ideas or status."""
    return get_service().add_inbox_item(text)


@mcp.tool
def append_daily(text: str) -> dict[str, str]:
    """Append a timestamped entry to today's journal Log — for decisions,
    conclusions, work accomplished, and key insights as they happen."""
    return get_service().append_daily(text)


@mcp.tool
def refresh_index() -> dict[str, int]:
    """Force a rebuild of the metadata index from the files on disk.
    Returns note and link counts."""
    return get_service().refresh()


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--http",
    "use_http",
    is_flag=True,
    envvar="VAULT_MCP_HTTP",
    help="Serve streamable HTTP at /mcp instead of stdio.",
)
@click.option(
    "--host",
    default="127.0.0.1",
    envvar="VAULT_MCP_HOST",
    show_default=True,
    help="HTTP bind address. Keep 127.0.0.1 behind `tailscale serve`, or bind "
    "the machine's Tailscale IP to serve the tailnet directly.",
)
@click.option(
    "--port",
    default=8787,
    envvar="VAULT_MCP_PORT",
    show_default=True,
    type=int,
    help="HTTP port.",
)
@click.option(
    "--auth",
    "auth_provider",
    default="none",
    envvar="VAULT_MCP_AUTH",
    show_default=True,
    type=click.Choice(["none", *sorted(PROVIDERS)]),
    help="OAuth provider for the HTTP transport. Credentials come from "
    "VAULT_MCP_OAUTH_CLIENT_ID / VAULT_MCP_OAUTH_CLIENT_SECRET.",
)
@click.option(
    "--base-url",
    envvar="VAULT_MCP_BASE_URL",
    default=None,
    help="Public URL clients reach the server at (required with --auth), "
    "e.g. https://mac.tailnet.ts.net",
)
def main(use_http: bool, host: str, port: int, auth_provider: str, base_url: str | None) -> None:
    """Expose the markdown vault as an MCP server (stdio by default)."""
    if auth_provider != "none":
        if not use_http:
            raise click.UsageError("--auth requires --http (stdio has no auth layer)")
        try:
            mcp.auth = build_auth(auth_provider, base_url)
        except ValueError as exc:
            raise click.UsageError(str(exc)) from exc
    if use_http:
        mcp.run(transport="http", host=host, port=port)
    else:
        mcp.run()
