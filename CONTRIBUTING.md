# Contributing

Issues and PRs welcome. This started as a personal memory substrate, so the
defaults encode one vault's conventions — generalization PRs are interesting,
but read the Design section of the README first: the constrained-verbs write
model is deliberate, not an oversight.

## Dev setup

```sh
git clone https://github.com/seandavi/vault-mcp
cd vault-mcp
uv sync            # Python >= 3.11
uv run pytest      # everything runs against tmp fixture vaults
```

[ripgrep](https://github.com/BurntSushi/ripgrep) (`rg`) must be on PATH for
the exact/regex search modes (their tests skip without it). The one test that
touches a real vault is read-only and skips itself when the vault isn't
present, so the suite is safe to run anywhere.

## Making changes

- Branch from `main`; open a PR. CI (pytest) must be green to merge.
- New behavior comes with tests against the fixture vault in
  `tests/test_vault.py`.
- Write verbs must keep the existing discipline: hold the service lock,
  enforce a convention (never arbitrary file writes), invalidate the index,
  and fail loudly rather than guess.
- Type hints throughout; docstrings on the MCP tool functions are the tool
  descriptions agents see — write them for the agent, not the linter.
