"""Vault scanning and indexing.

Parses markdown files (YAML frontmatter + [[wiki-links]]) into an in-memory
DuckDB database that backs search filters, `related`, and the `query` tool.

The index is a disposable cache: it is rebuilt from the files on demand and
is never a source of truth. Files win, always.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import yaml

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
WIKILINK_RE = re.compile(r"\[\[([^\]|#\n]+)")
H1_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)

# Top-level vault directories that hold support material, not memories.
EXCLUDED_TOP_DIRS = frozenset({"templates", "raw", "node_modules"})


def is_indexable(rel: Path) -> bool:
    """True if a vault-relative markdown path belongs in the index."""
    parts = rel.parts
    if any(p.startswith(".") for p in parts):
        return False
    return parts[0] not in EXCLUDED_TOP_DIRS if len(parts) > 1 else True


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split *text* into (frontmatter dict, body). Malformed YAML → empty dict."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        fm = None
    if not isinstance(fm, dict):
        fm = {}
    return fm, text[m.end() :]


def normalize_tags(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [t.strip() for t in raw.split(",") if t.strip()]
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if str(t).strip()]
    return [str(raw)]


@dataclass
class NoteRow:
    path: str
    name: str
    title: str
    type: str
    tags: list[str]
    date: str | None
    status: str | None
    frontmatter: str  # JSON
    modified: datetime
    size: int


def parse_note(root: Path, abspath: Path) -> tuple[NoteRow, list[str]]:
    """Parse one markdown file into a NoteRow and its outgoing wiki-link targets."""
    rel = abspath.relative_to(root)
    text = abspath.read_text(encoding="utf-8", errors="replace")
    fm, body = parse_frontmatter(text)

    h1 = H1_RE.search(body)
    title = h1.group(1).strip() if h1 else abspath.stem
    note_type = str(fm.get("type") or (rel.parts[0] if len(rel.parts) > 1 else "root"))
    date = fm.get("date")
    status = fm.get("status")
    stat = abspath.stat()

    row = NoteRow(
        path=str(rel),
        name=abspath.stem,
        title=title,
        type=note_type,
        tags=normalize_tags(fm.get("tags")),
        date=str(date) if date is not None else None,
        status=str(status) if status is not None else None,
        frontmatter=json.dumps(fm, default=str),
        modified=datetime.fromtimestamp(stat.st_mtime),
        size=stat.st_size,
    )
    targets = sorted({t.strip() for t in WIKILINK_RE.findall(text) if t.strip()})
    return row, targets


class VaultIndex:
    """TTL-cached in-memory DuckDB index over the vault's markdown files."""

    def __init__(self, root: Path, ttl_seconds: float = 30.0) -> None:
        self.root = root
        self.ttl_seconds = ttl_seconds
        self._con: duckdb.DuckDBPyConnection | None = None
        self._built_at: float = 0.0

    def invalidate(self) -> None:
        self._built_at = 0.0

    def connection(self) -> duckdb.DuckDBPyConnection:
        if self._con is None or (time.monotonic() - self._built_at) > self.ttl_seconds:
            self.rebuild()
        assert self._con is not None
        return self._con

    def rebuild(self) -> dict[str, int]:
        con = duckdb.connect(":memory:")
        con.execute(
            """
            CREATE TABLE notes (
                path VARCHAR PRIMARY KEY,   -- vault-relative path
                name VARCHAR,               -- filename stem (wiki-link target)
                title VARCHAR,              -- first H1, else stem
                type VARCHAR,               -- frontmatter `type`, else top-level dir
                tags VARCHAR[],
                date VARCHAR,
                status VARCHAR,
                frontmatter JSON,           -- full frontmatter
                modified TIMESTAMP,
                size BIGINT
            )
            """
        )
        con.execute("CREATE TABLE links (source VARCHAR, target VARCHAR)")

        note_rows: list[tuple[Any, ...]] = []
        link_rows: list[tuple[str, str]] = []
        for abspath in sorted(self.root.rglob("*.md")):
            if not abspath.is_file() or not is_indexable(abspath.relative_to(self.root)):
                continue
            try:
                row, targets = parse_note(self.root, abspath)
            except OSError:
                continue
            note_rows.append(
                (row.path, row.name, row.title, row.type, row.tags, row.date,
                 row.status, row.frontmatter, row.modified, row.size)
            )
            link_rows.extend((row.path, t) for t in targets)

        if note_rows:
            con.executemany("INSERT INTO notes VALUES (?,?,?,?,?,?,?,?,?,?)", note_rows)
        if link_rows:
            con.executemany("INSERT INTO links VALUES (?,?)", link_rows)

        if self._con is not None:
            self._con.close()
        self._con = con
        self._built_at = time.monotonic()
        return {"notes": len(note_rows), "links": len(link_rows)}
