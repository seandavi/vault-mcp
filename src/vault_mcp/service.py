"""Vault operations behind the MCP tools.

All reads go through ripgrep and the DuckDB index; all writes go through
constrained, convention-enforcing verbs (never arbitrary file writes).
"""

from __future__ import annotations

import asyncio
import functools
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TypeVar

from vault_mcp.index import EXCLUDED_TOP_DIRS, VaultIndex

DEFAULT_ROOT = "~/Documents/seandavis"
ROOT_ENV_VAR = "VAULT_MCP_ROOT"

SLUG_RE = re.compile(r"[^a-z0-9]+")
READONLY_SQL_RE = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


def slugify(title: str) -> str:
    slug = SLUG_RE.sub("-", title.lower()).strip("-")
    if not slug:
        raise ValueError(f"cannot derive a slug from title {title!r}")
    return slug


F = TypeVar("F", bound=Callable[..., Any])


def locked(fn: F) -> F:
    """Serialize a VaultService method under the instance lock.

    The DuckDB connection is a single shared object (a rebuild closes the old
    connection mid-query otherwise), and the write verbs do read-modify-write
    on shared files — both unsafe once concurrent clients arrive over HTTP.
    """

    @functools.wraps(fn)
    def wrapper(self: VaultService, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return fn(self, *args, **kwargs)

    return wrapper  # type: ignore[return-value]


class VaultService:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or Path(os.environ.get(ROOT_ENV_VAR, DEFAULT_ROOT)).expanduser()).resolve()
        if not self.root.is_dir():
            raise FileNotFoundError(f"vault root does not exist: {self.root}")
        self.index = VaultIndex(self.root)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ reads

    async def search(
        self,
        query: str,
        type: str | None = None,
        tag: str | None = None,
        match: str = "ranked",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Search the vault: BM25 ranking by default, ripgrep for exact/regex."""
        if match == "ranked":
            return self._search_ranked(query, type=type, tag=tag, limit=limit)
        if match not in ("exact", "regex"):
            raise ValueError("match must be one of: ranked, exact, regex")
        args = [
            "rg", "--no-heading", "--with-filename", "--line-number",
            "--smart-case", "--max-count", "5", "--glob", "*.md",
            *(f"--glob=!{d}/**" for d in sorted(EXCLUDED_TOP_DIRS)),
        ]
        if match == "exact":
            args.append("--fixed-strings")
        args.extend(["--", query, "."])

        proc = await asyncio.create_subprocess_exec(
            *args, cwd=self.root,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode not in (0, 1):  # 1 = no matches
            raise RuntimeError(f"ripgrep failed: {stderr.decode().strip()}")

        by_file: dict[str, list[dict[str, Any]]] = {}
        for line in stdout.decode(errors="replace").splitlines():
            parts = line.split(":", 2)
            if len(parts) != 3:
                continue
            path, lineno, text = parts
            path = path.removeprefix("./")
            by_file.setdefault(path, []).append({"line": int(lineno), "text": text.strip()[:300]})

        if not by_file:
            return []

        placeholders = ",".join("?" for _ in by_file)
        sql = f"SELECT path, title, type, tags, date FROM notes WHERE path IN ({placeholders})"
        params: list[Any] = list(by_file)
        if type:
            sql += " AND type = ?"
            params.append(type)
        if tag:
            sql += " AND list_contains(tags, ?)"
            params.append(tag)
        with self._lock:
            con = self.index.connection()
            meta = {r[0]: r for r in con.execute(sql, params).fetchall()}

        results = [
            {
                "path": path,
                "title": meta[path][1],
                "type": meta[path][2],
                "tags": meta[path][3],
                "date": meta[path][4],
                "matches": matches,
            }
            for path, matches in by_file.items()
            if path in meta
        ]
        results.sort(key=lambda r: len(r["matches"]), reverse=True)
        return results[:limit]

    def _search_ranked(
        self, query: str, *, type: str | None, tag: str | None, limit: int
    ) -> list[dict[str, Any]]:
        """BM25 over title+body. Scores must be filtered via a subquery alias —
        filtering match_bm25 directly in WHERE returns wrong rows (duckdb-fts
        1.5.5; see docs/research/duckdb-fts.md)."""
        sql = """
            SELECT path, title, type, tags, date, score, body FROM (
                SELECT *, fts_main_notes.match_bm25(path, ?) AS score FROM notes
            ) WHERE score IS NOT NULL
        """
        params: list[Any] = [query]
        if type:
            sql += " AND type = ?"
            params.append(type)
        if tag:
            sql += " AND list_contains(tags, ?)"
            params.append(tag)
        sql += " ORDER BY score DESC LIMIT ?"
        params.append(limit)

        with self._lock:
            con = self.index.connection()
            self.index.ensure_fts()
            rows = con.execute(sql, params).fetchall()

        terms = [t for t in re.split(r"[^a-z0-9]+", query.lower()) if t]
        results = []
        for path, title, ntype, tags, date, score, body in rows:
            snippet = [
                line.strip()[:300]
                for line in body.splitlines()
                if line.strip() and any(t in line.lower() for t in terms)
            ][:3]
            results.append(
                {"path": path, "title": title, "type": ntype, "tags": tags,
                 "date": date, "score": round(score, 3), "snippet": snippet}
            )
        return results

    @locked
    def resolve(self, name_or_path: str) -> Path:
        """Resolve a vault-relative path, note name, or frontmatter alias.

        Typos auto-resolve only above high similarity (0.95 Jaro-Winkler);
        below that the error carries did-you-mean candidates instead of
        silently guessing."""
        candidate = (self.root / name_or_path).resolve()
        if candidate.is_file() and candidate.is_relative_to(self.root):
            return candidate
        stem = Path(name_or_path).stem
        con = self.index.connection()
        rows = con.execute(
            """
            SELECT path FROM notes
            WHERE lower(name) = lower(?)
               OR list_contains(list_transform(aliases, a -> lower(a)), lower(?))
            ORDER BY modified DESC
            """,
            [stem, stem],
        ).fetchall()
        if rows:
            return self.root / rows[0][0]

        fuzzy = con.execute(
            """
            SELECT path, name, greatest(
                jaro_winkler_similarity(lower(name), lower(?)),
                coalesce(list_max(list_transform(
                    aliases, a -> jaro_winkler_similarity(lower(a), lower(?)))), 0.0)
            ) AS sim
            FROM notes ORDER BY sim DESC, modified DESC LIMIT 3
            """,
            [stem, stem],
        ).fetchall()
        if fuzzy and fuzzy[0][2] >= 0.95:
            return self.root / fuzzy[0][0]
        suggestions = ", ".join(f"{name}" for _, name, sim in fuzzy if sim >= 0.7)
        hint = f" — did you mean: {suggestions}?" if suggestions else ""
        raise FileNotFoundError(f"no note matching {name_or_path!r}{hint}")

    @locked
    def read(self, name_or_path: str) -> dict[str, str]:
        path = self.resolve(name_or_path)
        return {"path": str(path.relative_to(self.root)), "content": path.read_text(encoding="utf-8")}

    @locked
    def related(self, name_or_path: str) -> dict[str, Any]:
        """Graph neighborhood of a note: outlinks, backlinks, shared-tag neighbors."""
        path = self.resolve(name_or_path)
        rel = str(path.relative_to(self.root))
        con = self.index.connection()
        name, tags = con.execute("SELECT name, tags FROM notes WHERE path = ?", [rel]).fetchone()

        outlinks, unresolved = [], []
        for (target,) in con.execute("SELECT DISTINCT target FROM links WHERE source = ?", [rel]).fetchall():
            hit = con.execute(
                "SELECT path, title FROM notes WHERE lower(name) = lower(?) LIMIT 1", [target]
            ).fetchone()
            (outlinks.append({"name": target, "path": hit[0], "title": hit[1]}) if hit
             else unresolved.append(target))

        backlinks = [
            {"path": p, "title": t}
            for p, t in con.execute(
                """
                SELECT DISTINCT n.path, n.title FROM links l
                JOIN notes n ON n.path = l.source
                WHERE lower(l.target) = lower(?) AND l.source != ?
                """,
                [name, rel],
            ).fetchall()
        ]
        shared_tags = [
            {"path": p, "title": t, "shared": s}
            for p, t, s in con.execute(
                """
                SELECT path, title, list_intersect(tags, ?) AS shared FROM notes
                WHERE path != ? AND len(list_intersect(tags, ?)) > 0
                ORDER BY len(shared) DESC, modified DESC LIMIT 10
                """,
                [tags, rel, tags],
            ).fetchall()
        ] if tags else []

        return {
            "path": rel,
            "outlinks": outlinks,
            "unresolved_outlinks": unresolved,
            "backlinks": backlinks,
            "shared_tags": shared_tags,
        }

    @locked
    def query(self, sql: str, limit: int = 200) -> dict[str, Any]:
        """Read-only SQL over the index. The index is a disposable cache, so the
        guard protects against accidents, not adversaries."""
        if not READONLY_SQL_RE.match(sql):
            raise ValueError("only SELECT/WITH queries are allowed")
        if ";" in sql.rstrip().rstrip(";"):
            raise ValueError("multiple statements are not allowed")
        cur = self.index.connection().execute(sql)
        columns = [d[0] for d in cur.description]
        rows = cur.fetchmany(limit)
        return {"columns": columns, "rows": [list(r) for r in rows]}

    @locked
    def recent(self, limit: int = 10, type: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT path, title, type, modified FROM notes"
        params: list[Any] = []
        if type:
            sql += " WHERE type = ?"
            params.append(type)
        sql += " ORDER BY modified DESC LIMIT ?"
        params.append(limit)
        return [
            {"path": p, "title": t, "type": ty, "modified": str(m)}
            for p, t, ty, m in self.index.connection().execute(sql, params).fetchall()
        ]

    # ----------------------------------------------------------------- writes

    @locked
    def create_note(
        self, title: str, content: str, tags: list[str] | None = None, source: str = ""
    ) -> dict[str, str]:
        """Create an atomic note in notes/ following the vault's note template."""
        path = self.root / "notes" / f"{slugify(title)}.md"
        if path.exists():
            raise FileExistsError(f"note already exists: {path.relative_to(self.root)}")
        tag_line = f"[{', '.join(tags)}]" if tags else "[]"
        path.write_text(
            f"---\ndate: {datetime.now():%Y-%m-%d}\ntags: {tag_line}\nsource: {source}\n---\n"
            f"\n# {title}\n\n{content.rstrip()}\n",
            encoding="utf-8",
        )
        self.index.invalidate()
        return {"path": str(path.relative_to(self.root))}

    @locked
    def edit_note(self, name_or_path: str, old_text: str, new_text: str) -> dict[str, str]:
        """Replace exactly one occurrence of old_text anywhere in a note."""
        if not old_text:
            raise ValueError("old_text must be non-empty")
        path = self.resolve(name_or_path)
        rel = path.relative_to(self.root)
        text = path.read_text(encoding="utf-8")
        count = text.count(old_text)
        if count == 0:
            raise ValueError(
                f"old_text not found in {rel} — read the note and copy the passage exactly"
            )
        if count > 1:
            raise ValueError(
                f"old_text occurs {count} times in {rel} — "
                "include surrounding lines to make it unique"
            )
        path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        self.index.invalidate()
        return {"path": str(rel)}

    @locked
    def rename_note(self, name_or_path: str, new_title: str) -> dict[str, Any]:
        """Rename a note and rewrite [[wiki-links]] to it across the vault."""
        old_path = self.resolve(name_or_path)
        new_path = old_path.with_name(f"{slugify(new_title)}.md")
        if new_path != old_path and new_path.exists():
            raise FileExistsError(f"note already exists: {new_path.relative_to(self.root)}")
        old_rel = old_path.relative_to(self.root)

        # Links may be written as the bare stem or the vault-relative path,
        # optionally carrying #heading or |alias suffixes; match the target
        # part case-insensitively, the way Obsidian resolves them.
        targets = {old_path.stem, str(old_rel), str(old_rel.with_suffix(""))}
        link_re = re.compile(
            r"\[\[(?:" + "|".join(re.escape(t) for t in sorted(targets)) + r")(?=[\]#|])",
            re.IGNORECASE,
        )
        replacement = f"[[{new_path.stem}"
        rewritten = 0
        con = self.index.connection()
        sources = con.execute(
            "SELECT DISTINCT source FROM links WHERE lower(target) IN "
            f"({', '.join('?' for _ in targets)})",
            [t.lower() for t in targets],
        ).fetchall()
        for (source,) in sources:
            source_path = self.root / source
            text = source_path.read_text(encoding="utf-8")
            new_text = link_re.sub(replacement, text)
            if new_text != text:
                source_path.write_text(new_text, encoding="utf-8")
                rewritten += 1

        # The vault template keeps the first H1 in sync with the title.
        lines = old_path.read_text(encoding="utf-8").splitlines(keepends=True)
        for i, line in enumerate(lines):
            if line.startswith("# "):
                lines[i] = f"# {new_title}\n"
                break
        old_path.write_text("".join(lines), encoding="utf-8")
        if new_path != old_path:
            old_path.rename(new_path)
        self.index.invalidate()
        return {
            "old_path": str(old_rel),
            "path": str(new_path.relative_to(self.root)),
            "links_rewritten": rewritten,
        }

    @locked
    def update_note(self, name_or_path: str, content: str) -> dict[str, str]:
        """Replace a note's entire body, preserving its frontmatter block verbatim."""
        path = self.resolve(name_or_path)
        text = path.read_text(encoding="utf-8")
        frontmatter = ""
        if text.startswith("---\n"):
            end = text.find("\n---\n", 4)
            if end != -1:
                frontmatter = text[: end + 5] + "\n"
        path.write_text(f"{frontmatter}{content.strip()}\n", encoding="utf-8")
        self.index.invalidate()
        return {"path": str(path.relative_to(self.root))}

    @locked
    def add_inbox_item(self, text: str) -> dict[str, str]:
        """Append an open action item under '## Action needed' in inbox.md."""
        inbox = self.root / "inbox.md"
        item = f"- [ ] {text.strip()} (via vault-mcp, {datetime.now():%Y-%m-%d})"
        lines = inbox.read_text(encoding="utf-8").splitlines() if inbox.exists() else ["# Inbox", "", "## Action needed"]
        for i, line in enumerate(lines):
            if line.strip() == "## Action needed":
                insert_at = i + 1
                while insert_at < len(lines) and not lines[insert_at].strip():
                    insert_at += 1
                if insert_at >= len(lines) or lines[insert_at].lstrip().startswith("#"):
                    lines.insert(insert_at, "")
                lines.insert(insert_at, item)
                if lines[i + 1].strip():
                    lines.insert(i + 1, "")
                break
        else:
            lines.extend(["", "## Action needed", "", item])
        inbox.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.index.invalidate()
        return {"path": "inbox.md", "item": item}

    @locked
    def append_daily(self, text: str) -> dict[str, str]:
        """Append a timestamped entry to today's journal Log, creating the file
        from the daily template structure if needed."""
        now = datetime.now()
        path = self.root / "journal" / f"{now:%Y}" / f"{now:%m}" / f"{now:%Y-%m-%d}.md"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                f"# {now:%A, %B} {now.day}, {now:%Y}\n\n"
                '## Morning (Franklin: "What good shall I do this day?")\n\n'
                "## Log\n\n"
                '## Evening (Franklin: "What good have I done today?")\n',
                encoding="utf-8",
            )
        entry = f"- **{now:%H:%M}** — {text.strip()}"
        lines = path.read_text(encoding="utf-8").splitlines()
        try:
            log_at = next(i for i, l in enumerate(lines) if l.strip().startswith("## Log"))
        except StopIteration:
            lines.extend(["", "## Log", "", entry])
        else:
            end = next(
                (i for i in range(log_at + 1, len(lines)) if lines[i].startswith("## ")),
                len(lines),
            )
            while end > log_at + 1 and not lines[end - 1].strip():
                end -= 1
            if end == log_at + 1:  # empty Log section — keep a blank after the heading
                lines.insert(end, "")
                end += 1
            lines.insert(end, entry)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.index.invalidate()
        return {"path": str(path.relative_to(self.root)), "entry": entry}

    # ------------------------------------------------------------------ admin

    @locked
    def refresh(self) -> dict[str, int]:
        return self.index.rebuild()
