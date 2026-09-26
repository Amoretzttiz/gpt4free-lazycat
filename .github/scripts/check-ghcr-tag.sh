#!/usr/bin/env bash
set -euo pipefail

classify_manifest_response() {
  local status="$1"
  local response_body="$2"

  case "${status}" in
    2??)
      return 10
      ;;
    404)
      if python3 -c 'import json, sys; data = json.load(open(sys.argv[1])); sys.exit(0 if any(item.get("code") == "MANIFEST_UNKNOWN" for item in data.get("errors", [])) else 1)' "${response_body}"; then
        return 0
      fi
      return 20
      ;;
    *)
      return 20
      ;;
  esac
}

if [[ "${GHCR_PREFLIGHT_SOURCE_ONLY:-}" == "1" ]]; then
  return 0 2>/dev/null || exit 0
fi

if [[ "$#" -ne 2 ]]; then
  printf 'usage: %s ghcr.io/OWNER/IMAGE TAG\n' "$0" >&2
  exit 64
fi

image="${1%/}"
tag="$2"
: "${GHCR_USERNAME:?GHCR_USERNAME is required}"
: "${GHCR_TOKEN:?GHCR_TOKEN is required}"

case "${image}" in
  ghcr.io/*) repository="${image#ghcr.io/}" ;;
  *)
    printf 'refusing non-GHCR image: %s\n' "${image}" >&2
    exit 64
    ;;
esac

if [[ -z "${repository}" || -z "${tag}" || "${repository}" == */ || "${tag}" == */* ]]; then
  printf 'invalid GHCR repository or tag\n' >&2
  exit 64
fi

workdir="$(mktemp -d)"
trap 'rm -rf "${workdir}"' EXIT
auth_body="${workdir}/auth.json"
manifest_body="${workdir}/manifest.json"

if ! auth_status="$(curl --silent --show-error \
  --output "${auth_body}" \
  --write-out '%{http_code}' \
  --user "${GHCR_USERNAME}:${GHCR_TOKEN}" \
  --get \
  --data-urlencode 'service=ghcr.io' \
  --data-urlencode "scope=repository:${repository}:pull" \
  'https://ghcr.io/token')"; then
  printf 'GHCR authentication request failed; refusing to build or push\n' >&2
  exit 20
fi

if [[ "${auth_status}" != "200" ]]; then
  printf 'GHCR authentication failed with HTTP %s; refusing to build or push\n' "${auth_status}" >&2
  exit 20
fi

if ! registry_token="$(python3 -c 'import json, sys; data = json.load(open(sys.argv[1])); token = data.get("token") or data.get("access_token"); print(token) if token else sys.exit(1)' "${auth_body}")"; then
  printf 'GHCR authentication response contained no registry token; refusing to build or push\n' >&2
  exit 20
fi

if ! manifest_status="$(curl --silent --show-error \
  --output "${manifest_body}" \
  --write-out '%{http_code}' \
  --header "Authorization: Bearer ${registry_token}" \
  --header 'Accept: application/vnd.oci.image.index.v1+json' \
  --header 'Accept: application/vnd.oci.image.manifest.v1+json' \
  --header 'Accept: application/vnd.docker.distribution.manifest.list.v2+json' \
  --header 'Accept: application/vnd.docker.distribution.manifest.v2+json' \
  "https://ghcr.io/v2/${repository}/manifests/${tag}")"; then
  printf 'GHCR manifest request failed; refusing to build or push\n' >&2
  exit 20
fi

set +e
classify_manifest_response "${manifest_status}" "${manifest_body}"
outcome=$?
set -e

case "${outcome}" in
  0)
    printf 'GHCR tag is absent (authenticated MANIFEST_UNKNOWN): %s:%s\n' "${image}" "${tag}"
    ;;
  10)
    printf 'immutable GHCR tag already exists; refusing to overwrite: %s:%s\n' "${image}" "${tag}" >&2
    exit 10
    ;;
  *)
    printf 'GHCR manifest check returned HTTP %s without MANIFEST_UNKNOWN; refusing to build or push\n' "${manifest_status}" >&2
    exit 20
    ;;
esac
