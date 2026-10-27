#!/bin/sh
# launchd/systemd wrapper: bind the vault-mcp HTTP server to this machine's
# Tailscale IP, so the tailnet — and only the tailnet — can reach it.
# No app-level auth: Tailscale is the auth layer (see README → Security model).
#
# The IP is resolved at start rather than hardcoded, waiting for tailscaled
# to come up after boot.
set -eu

REPO=${VAULT_MCP_REPO:-$HOME/Documents/git/vault-mcp}
PORT=${VAULT_MCP_PORT:-9321}

if command -v tailscale >/dev/null 2>&1; then
    TAILSCALE=tailscale
else
    TAILSCALE=/Applications/Tailscale.app/Contents/MacOS/Tailscale
fi

HOST=""
for _ in $(seq 1 60); do
    HOST=$("$TAILSCALE" ip -4 2>/dev/null | head -n1) || true
    if [ -n "$HOST" ]; then
        break
    fi
    sleep 2
done
if [ -z "$HOST" ]; then
    echo "vault-mcp-tailnet: no Tailscale IP after 120s; is tailscaled running?" >&2
    exit 1
fi

# Exec the venv entry point, not `uv run`: under launchd, macOS privacy (TCC)
# checks the exec'd binary, and a Homebrew uv upgrade changes uv's path and
# silently drops its Full Disk Access (the server then sees an empty vault).
# The venv's Python is stable across uv upgrades. Run `uv sync` after pulling.
exec "$REPO/.venv/bin/vault-mcp" --http --host "$HOST" --port "$PORT"
