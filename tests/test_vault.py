"""Tests for vault-mcp against a temporary fixture vault.

The real vault is only ever touched by the read-only smoke test at the bottom.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from vault_mcp.index import normalize_tags, parse_frontmatter
from vault_mcp.service import VaultService, slugify

REAL_VAULT = Path("~/Documents/seandavis").expanduser()


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "notes").mkdir(parents=True)
    (root / "decisions").mkdir()
    (root / "templates").mkdir()
    (root / "raw").mkdir()
    (root / ".obsidian").mkdir()

    (root / "notes" / "alpha.md").write_text(
        "---\ndate: 2026-08-01\ntags: [memory, agents]\nsource: test\n---\n"
        "\n# Alpha Note\n\nLinks to [[beta]] and [[missing-note]].\n"
    )
    (root / "notes" / "gamma.md").write_text(
        "---\ndate: 2026-08-02\ntags: memory\n---\n\n# Gamma\n\nStandalone idea.\n"
    )
    (root / "decisions" / "beta.md").write_text(
        "---\ndate: 2026-08-03\nstatus: open\n---\n\n# Beta Decision\n\nSee [[alpha]].\n"
    )
    (root / "inbox.md").write_text(
        "# Inbox\n\n## Action needed\n\n- [ ] existing item\n\n## Waiting on others\n\n- [ ] waiting item\n"
    )
    # None of these should be indexed or searched.
    (root / "templates" / "note.md").write_text("# Template Alpha\n")
    (root / "raw" / "dump.md").write_text("raw Alpha dump\n")
    (root / ".obsidian" / "hidden.md").write_text("hidden Alpha\n")
    return root


@pytest.fixture()
def service(vault: Path) -> VaultService:
    return VaultService(root=vault)


# ---------------------------------------------------------------- pure helpers


def test_normalize_tags() -> None:
    assert normalize_tags(None) == []
    assert normalize_tags("memory") == ["memory"]
    assert normalize_tags("a, b , ") == ["a", "b"]
    assert normalize_tags(["a", " b "]) == ["a", "b"]


def test_parse_frontmatter_malformed_yaml() -> None:
    fm, body = parse_frontmatter("---\n: [unclosed\n---\nbody\n")
    assert fm == {}
    assert body == "body\n"


def test_slugify() -> None:
    assert slugify("My Great Idea!") == "my-great-idea"
    with pytest.raises(ValueError):
        slugify("!!!")


# ----------------------------------------------------------------------- index


def test_index_counts(service: VaultService) -> None:
    counts = service.refresh()
    # alpha, gamma, beta, inbox — templates/raw/.obsidian excluded
    assert counts == {"notes": 4, "links": 3}


def test_index_metadata(service: VaultService) -> None:
    result = service.query("SELECT name, type, tags, status FROM notes ORDER BY name")
    rows = {r[0]: r for r in result["rows"]}
    assert rows["alpha"][1] == "notes"
    assert rows["alpha"][2] == ["memory", "agents"]
    assert rows["beta"][1] == "decisions"
    assert rows["beta"][3] == "open"
    assert rows["inbox"][1] == "root"


# --------------------------------------------------------------- resolve/read


def test_resolve_and_read(service: VaultService) -> None:
    by_name = service.read("alpha")
    assert by_name["path"] == "notes/alpha.md"
    assert "# Alpha Note" in by_name["content"]
    by_path = service.read("decisions/beta.md")
    assert by_path["path"] == "decisions/beta.md"


def test_resolve_traversal_guard(service: VaultService, tmp_path: Path) -> None:
    (tmp_path / "outside.md").write_text("secret\n")
    with pytest.raises(FileNotFoundError):
        service.read("../outside.md")


def test_resolve_missing(service: VaultService) -> None:
    with pytest.raises(FileNotFoundError):
        service.read("no-such-note")


# --------------------------------------------------------------------- related


def test_related(service: VaultService) -> None:
    rel = service.related("alpha")
    assert [o["path"] for o in rel["outlinks"]] == ["decisions/beta.md"]
    assert rel["unresolved_outlinks"] == ["missing-note"]
    assert [b["path"] for b in rel["backlinks"]] == ["decisions/beta.md"]
    shared = {s["path"]: s["shared"] for s in rel["shared_tags"]}
    assert shared == {"notes/gamma.md": ["memory"]}


# ----------------------------------------------------------------------- query


def test_query_readonly_guard(service: VaultService) -> None:
    with pytest.raises(ValueError):
        service.query("UPDATE notes SET title = 'x'")
    with pytest.raises(ValueError):
        service.query("SELECT 1; SELECT 2")
    assert service.query("SELECT count(*) FROM links")["rows"] == [[3]]


# ---------------------------------------------------------------------- recent


def test_recent(service: VaultService) -> None:
    paths = [r["path"] for r in service.recent(limit=2, type="notes")]
    assert len(paths) == 2
    assert all(p.startswith("notes/") for p in paths)


# ---------------------------------------------------------------------- writes


def test_create_note(service: VaultService, vault: Path) -> None:
    created = service.create_note("My Great Idea", "Body text.", tags=["x"], source="test")
    assert created["path"] == "notes/my-great-idea.md"
    text = (vault / created["path"]).read_text()
    assert text.startswith("---\ndate: ")
    assert "tags: [x]" in text and "source: test" in text
    assert "# My Great Idea\n\nBody text.\n" in text
    with pytest.raises(FileExistsError):
        service.create_note("My Great Idea", "again")
    assert any(r["path"] == created["path"] for r in service.recent(limit=1))


def test_add_inbox_item(service: VaultService, vault: Path) -> None:
    service.add_inbox_item("do the thing")
    lines = (vault / "inbox.md").read_text().splitlines()
    heading = lines.index("## Action needed")
    waiting = lines.index("## Waiting on others")
    new = next(i for i, l in enumerate(lines) if "do the thing" in l)
    existing = lines.index("- [ ] existing item")
    assert heading < new < existing < waiting
    assert "(via vault-mcp," in lines[new]


def test_append_daily(service: VaultService, vault: Path) -> None:
    first = service.append_daily("first entry")
    path = vault / first["path"]
    assert path.exists()
    service.append_daily("second entry")
    lines = path.read_text().splitlines()
    log = lines.index("## Log")
    evening = next(i for i, l in enumerate(lines) if l.startswith("## Evening"))
    one = next(i for i, l in enumerate(lines) if "first entry" in l)
    two = next(i for i, l in enumerate(lines) if "second entry" in l)
    assert log < one < two < evening
    assert lines[log + 1] == ""  # blank line preserved after heading


# ---------------------------------------------------------------------- search


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
async def test_search(service: VaultService) -> None:
    hits = await service.search("Alpha")
    paths = [h["path"] for h in hits]
    assert "notes/alpha.md" in paths
    # excluded dirs never surface, even though they contain "Alpha"
    assert not any(p.startswith(("templates/", "raw/", ".obsidian/")) for p in paths)
    tagged = await service.search("idea", tag="memory")
    assert [h["path"] for h in tagged] == ["notes/gamma.md"]
    assert await service.search("no-such-string-anywhere") == []


# ----------------------------------------------- read-only smoke on real vault


@pytest.mark.skipif(not REAL_VAULT.is_dir(), reason="real vault not present")
async def test_real_vault_readonly_smoke() -> None:
    service = VaultService(root=REAL_VAULT)
    counts = service.refresh()
    assert counts["notes"] > 100
    assert counts["links"] > 100
    result = service.query("SELECT type, count(*) FROM notes GROUP BY type")
    assert len(result["rows"]) > 3
    hits = await service.search("Bioconductor", limit=3)
    assert hits
