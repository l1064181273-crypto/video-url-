#!/bin/zsh

set -eu
setopt pipe_fail
umask 077

typeset _lvt_process_lib="${${(%):-%N}:A:h}"
source "${_lvt_process_lib}/common.zsh"
unset _lvt_process_lib

function lvt_process_start_time() {
  local pid="${1-}"
  if [[ "${pid}" != <-> || "${pid}" -le 0 ]]; then
    return 1
  fi
  local value
  value="$(/bin/ps -o lstart= -p "${pid}" 2>/dev/null)" || return 1
  value="${${value}//[[:space:]]##/ }## }"
  [[ -n "${value}" ]] || return 1
  print -r -- "${value}"
}

function lvt_process_identity_matches() {
  local pid="${1-}"
  local expected_start="${2-}"
  local actual_start
  actual_start="$(lvt_process_start_time "${pid}")" || return 1
  [[ "${actual_start}" == "${expected_start}" ]]
}

function lvt_project_port_in_use() {
  local port="${1-11435}"
  [[ "${port}" == 11435 ]] || return 2
  /usr/bin/nc -z -w 1 127.0.0.1 "${port}" >/dev/null 2>&1
}
