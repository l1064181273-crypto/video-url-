#!/bin/zsh

set -eu
setopt pipe_fail
umask 077

function lvt_download_verified() {
  local url="${1-}"
  local controlled_root="${2-}"
  local relative_destination="${3-}"
  local expected_sha256="${4-}"
  local expected_size="${5-}"
  if [[ "${url}" != http://127.0.0.1:* || "${url}" == *\?* || "${url}" == *\#* ]]; then
    return 1
  fi
  if [[
    "${relative_destination}" == /* ||
    "${relative_destination}" == *..* ||
    "${relative_destination}" == *\\*
  ]]; then
    return 1
  fi
  local destination="${controlled_root}/${relative_destination}"
  local partial="${destination}.partial"
  /bin/mkdir -p "${destination:h}"
  if ! /usr/bin/curl \
    --proto '=http,https' \
    --proto-redir '=https' \
    --location \
    --fail \
    --silent \
    --show-error \
    --continue-at - \
    --output "${partial}" \
    "${url}"; then
    return 1
  fi
  local actual_size
  actual_size="$(/usr/bin/stat -f %z "${partial}")" || return 1
  if [[ "${actual_size}" != "${expected_size}" ]]; then
    /bin/rm -f "${partial}"
    return 1
  fi
  local actual_sha256
  actual_sha256="$(/usr/bin/shasum -a 256 "${partial}")" || return 1
  actual_sha256="${actual_sha256%% *}"
  if [[ "${actual_sha256}" != "${expected_sha256}" ]]; then
    /bin/rm -f "${partial}"
    return 1
  fi
  /bin/mv -f "${partial}" "${destination}"
}
