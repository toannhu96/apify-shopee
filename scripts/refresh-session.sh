#!/usr/bin/env bash
# Refresh Shopee through the operator API using the current Chrome session.
set +x
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd -- "$script_dir/.." && pwd)"

usage() {
  cat <<'HELP'
Usage: refresh-session.sh [ACCOUNT_ALIAS] [OPTIONS]

Refresh once using account primary, https://shopee-api.gcp.nudgen.net,
Chrome at http://127.0.0.1:9222, and product 24995347301.

Examples:
  ./scripts/refresh-session.sh
  ./scripts/refresh-session.sh --watch
  ./scripts/refresh-session.sh another-account --signing-mode standalone-seed
  ./scripts/refresh-session.sh --cdp-url http://127.0.0.1:9223

Options are forwarded to shopee_service.browser_refresh, including --watch,
--once, --api-url, --cdp-url, --item-id, and --signing-mode.

Set SH_SESSION_REFRESH_TOKEN in your environment, or enter it at the hidden
prompt. The token is not saved. For noninteractive use, set it beforehand.

Requires the updated API with its operator token configured, the project's
.venv dependencies installed, and signed-in Chrome with loopback debugging.
HELP
}

for argument in "$@"; do
  case "$argument" in
    -h|--help) usage; exit 0 ;;
  esac
done

account_alias=primary
if [[ $# -gt 0 && "$1" != -* ]]; then
  account_alias="$1"
  shift
fi

cd -- "$project_dir"
if [[ ! -x .venv/bin/python ]]; then
  printf '%s\n' 'Missing .venv/bin/python. Create the project virtual environment first.' >&2
  exit 1
fi
if ! .venv/bin/python -c 'import httpx; import playwright.sync_api' >/dev/null 2>&1; then
  printf '%s\n' 'Missing dependencies. Run: .venv/bin/python -m pip install -r requirements-dev.txt' >&2
  exit 1
fi

if [[ -z "${SH_SESSION_REFRESH_TOKEN:-}" ]]; then
  if [[ ! -t 0 ]]; then
    printf '%s\n' 'Set SH_SESSION_REFRESH_TOKEN before running without an interactive terminal.' >&2
    exit 1
  fi
  if ! IFS= read -r -s -p 'Session refresh token: ' SH_SESSION_REFRESH_TOKEN; then
    printf '\n%s\n' 'No token entered; refresh cancelled.' >&2
    exit 1
  fi
  printf '\n' >&2
  if [[ -z "$SH_SESSION_REFRESH_TOKEN" ]]; then
    printf '%s\n' 'No token entered; refresh cancelled.' >&2
    exit 1
  fi
fi
export SH_SESSION_REFRESH_TOKEN

exec .venv/bin/python -m shopee_service.browser_refresh "$account_alias" \
  --api-url https://shopee-api.gcp.nudgen.net \
  --cdp-url http://127.0.0.1:9222 \
  --item-id 24995347301 "$@"
