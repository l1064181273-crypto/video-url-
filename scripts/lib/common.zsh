#!/bin/zsh

set -eu
setopt pipe_fail
umask 077

function lvt_validate_relative_path() {
  local value="${1-}"
  if [[ -z "${value}" || "${value}" == /* || "${value}" == *\\* || "${value}" == *//* ]]; then
    return 1
  fi
  if [[ "${value}" == [A-Za-z]:/* || "${value}" == */ || "${value}" == . || "${value}" == .. ]]; then
    return 1
  fi
  local part
  for part in "${(@s:/:)value}"; do
    if [[ -z "${part}" || "${part}" == . || "${part}" == .. ]]; then
      return 1
    fi
  done
}

function lvt_assert_within_root() {
  local root="${1-}"
  local candidate="${2-}"
  if [[ -z "${root}" || -z "${candidate}" || "${root}" != /* || "${candidate}" != /* ]]; then
    return 1
  fi
  if [[ -L "${root}" || -L "${candidate}" ]]; then
    return 1
  fi
  local resolved_root="${root:A}"
  local resolved_candidate="${candidate:A}"
  [[ "${resolved_candidate}" == "${resolved_root}" || "${resolved_candidate}" == "${resolved_root}/"* ]]
}

function lvt_redact() {
  local value="${1-}"
  print -r -- "${value}" | /usr/bin/sed -E \
    -e 's/(X-LVT-Token:[[:space:]]*)[^[:space:]]+/\1[REDACTED]/g' \
    -e 's#(https?://[^?[:space:]]+)\?[^[:space:]]*#\1?[REDACTED]#g' \
    -e 's#(^|[[:space:]])/[^[:space:]]+#\1[path]#g'
}

function lvt_log() {
  local level="${1-INFO}"
  shift || true
  local message
  message="$(lvt_redact "$*")"
  print -r -- "[${level}] ${message}"
}

function lvt_default_data_root() {
  if [[ -n "${LVT_TEST_ROOT-}" ]]; then
    print -r -- "${LVT_TEST_ROOT}/LocalVideoTranscriber"
  else
    print -r -- "${HOME}/Library/Application Support/LocalVideoTranscriber"
  fi
}
