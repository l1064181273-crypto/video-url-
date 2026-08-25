#!/bin/zsh

set -eu
setopt pipe_fail
umask 077

typeset _lvt_download_lib="${${(%):-%N}:A:h}"
source "${_lvt_download_lib}/common.zsh"
unset _lvt_download_lib

function lvt_verify_file() {
  local path="${1-}"
  local expected_sha256="${2-}"
  local expected_size="${3-}"
  if [[ -z "${path}" || -L "${path}" || ! -f "${path}" ]]; then
    return 1
  fi
  if [[ ! "${expected_sha256}" =~ ^[0-9a-f]{64}$ ]]; then
    return 1
  fi
  if [[ "${expected_size}" != <-> || "${expected_size}" -le 0 ]]; then
    return 1
  fi
  local actual_size
  actual_size="$(/usr/bin/stat -f %z "${path}")" || return 1
  [[ "${actual_size}" == "${expected_size}" ]] || return 1
  local actual_sha256
  actual_sha256="$(/usr/bin/shasum -a 256 "${path}")" || return 1
  actual_sha256="${actual_sha256%% *}"
  [[ "${actual_sha256}" == "${expected_sha256}" ]]
}

function lvt_download_verified() {
  local url="${1-}"
  local controlled_root="${2-}"
  local relative_destination="${3-}"
  local expected_sha256="${4-}"
  local expected_size="${5-}"
  if [[ "${url}" != https://* || "${url}" == *\?* || "${url}" == *\#* ]]; then
    return 1
  fi
  lvt_validate_relative_path "${relative_destination}" || return 1
  if [[ "${controlled_root}" != /* || -L "${controlled_root}" || ! -d "${controlled_root}" ]]; then
    return 1
  fi
  local destination="${controlled_root}/${relative_destination}"
  lvt_assert_within_root "${controlled_root}" "${destination}" || return 1
  local parent="${destination:h}"
  lvt_assert_within_root "${controlled_root}" "${parent}" || return 1
  /bin/mkdir -p "${parent}"
  local partial="${destination}.partial.$$"
  {
    /usr/bin/curl \
      --proto '=https' \
      --tlsv1.2 \
      --location \
      --fail \
      --silent \
      --show-error \
      --output "${partial}" \
      "${url}"
    lvt_verify_file "${partial}" "${expected_sha256}" "${expected_size}"
    /bin/mv -f "${partial}" "${destination}"
  } always {
    if [[ -e "${partial}" || -L "${partial}" ]]; then
      /bin/rm -f "${partial}"
    fi
  }
}
