"""Retrieval eval harness: fixed queries with expected notes, per engine.

The eval file is YAML — a list of entries:

    - name: bioc-cloud-costs
      query: bioconductor cloud costs
      expect: [projects/Bioconductor_U24/cloud-costs.md]   # any hit counts
      modes: [ranked, exact]        # optional; default compares both

Personal query sets live in the vault (e.g. <vault>/.vault-mcp/eval.yaml —
dot-directories are never indexed), not in this repo. Run with:

    uv run python -m vault_mcp.eval [--vault PATH] [--eval PATH] [-k 5]

Scoring per entry and mode: rank of the first expected path in the top-k
results (None = miss). Summary per mode: hit rate, MRR, mean latency. This
is also the roadmap's embeddings gate: add embeddings only if these numbers
demonstrably miss.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
import yaml

from vault_mcp.service import VaultService

DEFAULT_EVAL_PATH = ".vault-mcp/eval.yaml"
DEFAULT_MODES = ("ranked", "exact")


@dataclass
class ModeResult:
    rank: int | None  # 1-based rank of first expected hit, None = miss
    ms: float


@dataclass
class EntryResult:
    name: str
    modes: dict[str, ModeResult]


async def run_entry(
    service: VaultService, entry: dict[str, Any], k: int = 5
) -> EntryResult:
    expect = set(entry["expect"])
    modes: dict[str, ModeResult] = {}
    for mode in entry.get("modes", DEFAULT_MODES):
        t0 = time.perf_counter()
        hits = await service.search(entry["query"], match=mode, limit=k)
        ms = (time.perf_counter() - t0) * 1000
        rank = next(
            (i + 1 for i, h in enumerate(hits) if h["path"] in expect), None
        )
        modes[mode] = ModeResult(rank=rank, ms=ms)
    return EntryResult(name=entry.get("name", entry["query"]), modes=modes)


async def run_eval(
    service: VaultService, entries: list[dict[str, Any]], k: int = 5
) -> list[EntryResult]:
    return [await run_entry(service, e, k=k) for e in entries]


def summarize(results: list[EntryResult]) -> dict[str, dict[str, float]]:
    """Per mode: hit rate, MRR, and mean latency over the entries that ran it."""
    summary: dict[str, dict[str, float]] = {}
    for mode in sorted({m for r in results for m in r.modes}):
        ran = [r.modes[mode] for r in results if mode in r.modes]
        hits = [m for m in ran if m.rank is not None]
        summary[mode] = {
            "n": len(ran),
            "hit_rate": len(hits) / len(ran) if ran else 0.0,
            "mrr": sum(1 / m.rank for m in hits) / len(ran) if ran else 0.0,
            "mean_ms": sum(m.ms for m in ran) / len(ran) if ran else 0.0,
        }
    return summary


@click.command()
@click.option("--vault", default=None, help="Vault root (default: VAULT_MCP_ROOT).")
@click.option("--eval", "eval_path", default=None,
              help=f"Eval YAML (default: <vault>/{DEFAULT_EVAL_PATH}).")
@click.option("-k", default=5, show_default=True, help="Judge the top-k results.")
def main(vault: str | None, eval_path: str | None, k: int) -> None:
    """Run the retrieval eval and print per-query and per-engine results."""
    service = VaultService(root=Path(vault).expanduser() if vault else None)
    path = Path(eval_path).expanduser() if eval_path else service.root / DEFAULT_EVAL_PATH
    entries = yaml.safe_load(path.read_text())
    results = asyncio.run(run_eval(service, entries, k=k))

    width = max(len(r.name) for r in results)
    modes = sorted({m for r in results for m in r.modes})
    click.echo(f"{'query'.ljust(width)}  " + "  ".join(f"{m:>14}" for m in modes))
    for r in results:
        cells = []
        for m in modes:
            mr = r.modes.get(m)
            cells.append(f"{'—':>14}" if mr is None
                         else f"{('rank ' + str(mr.rank)) if mr.rank else 'MISS':>8} {mr.ms:4.0f}ms")
        click.echo(f"{r.name.ljust(width)}  " + "  ".join(cells))

    click.echo()
    for mode, s in summarize(results).items():
        click.echo(f"{mode:>8}: hit {s['hit_rate']:.0%} ({int(s['hit_rate']*s['n'])}/{int(s['n'])})"
                   f"  MRR {s['mrr']:.2f}  mean {s['mean_ms']:.0f}ms")


if __name__ == "__main__":
    main()
