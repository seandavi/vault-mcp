#!/bin/sh
# launchd wrapper: pull OAuth credentials from Google Secret Manager at boot,
# then exec the server. Secrets never live in the plist or on disk.
#
# To use with auth, point the LaunchAgent's ProgramArguments at this script
# and adjust the secret names / base URL below. Without auth, the plist can
# keep running `vault-mcp --http` directly.
set -eu

GCLOUD=${GCLOUD:-/opt/homebrew/bin/gcloud}
BASE_URL=${VAULT_MCP_BASE_URL:?set VAULT_MCP_BASE_URL, e.g. https://mac.tailnet.ts.net}

VAULT_MCP_OAUTH_CLIENT_ID="$("$GCLOUD" secrets versions access latest --secret=vault-mcp-oauth-client-id)"
VAULT_MCP_OAUTH_CLIENT_SECRET="$("$GCLOUD" secrets versions access latest --secret=vault-mcp-oauth-client-secret)"
export VAULT_MCP_OAUTH_CLIENT_ID VAULT_MCP_OAUTH_CLIENT_SECRET

exec /opt/homebrew/bin/uv run --directory /Users/davsean/Documents/git/vault-mcp \
    vault-mcp --http --auth google --base-url "$BASE_URL"
