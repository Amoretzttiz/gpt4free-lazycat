#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GHCR_PREFLIGHT_SOURCE_ONLY=1 source "${repo_root}/.github/scripts/check-ghcr-tag.sh"

tmpdir="$(mktemp -d)"
trap 'rm -rf "${tmpdir}"' EXIT

printf '{}' >"${tmpdir}/ok.json"
printf '{"errors":[{"code":"MANIFEST_UNKNOWN"}]}' >"${tmpdir}/missing.json"
printf '{"errors":[{"code":"NAME_UNKNOWN"}]}' >"${tmpdir}/wrong-404.json"

assert_status() {
  local expected="$1"
  local status="$2"
  local body="$3"
  local actual

  set +e
  classify_manifest_response "${status}" "${body}"
  actual=$?
  set -e
  if [[ "${actual}" -ne "${expected}" ]]; then
    printf 'status %s: expected %s, got %s\n' "${status}" "${expected}" "${actual}" >&2
    exit 1
  fi
}

assert_status 10 200 "${tmpdir}/ok.json"
assert_status 0 404 "${tmpdir}/missing.json"
assert_status 20 404 "${tmpdir}/wrong-404.json"
assert_status 20 401 "${tmpdir}/ok.json"
assert_status 20 503 "${tmpdir}/ok.json"

printf 'GHCR manifest response classification tests passed\n'
