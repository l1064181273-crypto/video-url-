#!/bin/zsh

set -eu
setopt pipe_fail
umask 077

typeset script_dir="${0:A:h}"
typeset release_root="${script_dir:h}"
source "${script_dir}/lib/common.zsh"

typeset python_path=""
if [[ -n "${LVT_TEST_ROOT-}" && -n "${LVT_PYTHON-}" ]]; then
  python_path="${LVT_PYTHON}"
elif [[ -x "${release_root}/.venv/bin/python" ]]; then
  python_path="${release_root}/.venv/bin/python"
elif [[ -x "${release_root}/backend/.venv/bin/python" ]]; then
  python_path="${release_root}/backend/.venv/bin/python"
elif (( ${+commands[python3]} )); then
  python_path="${commands[python3]}"
else
  lvt_log ERROR "PYTHON_MISSING：未找到可用的 Python 3"
  exit 1
fi

typeset doctor_tool="${release_root}/packaging/tools/doctor.py"
lvt_assert_within_root "${release_root}" "${doctor_tool}" || {
  lvt_log ERROR "DOCTOR_PATH_UNSAFE：检查工具路径不安全"
  exit 2
}
if [[ ! -f "${doctor_tool}" || -L "${doctor_tool}" ]]; then
  lvt_log ERROR "DOCTOR_MISSING：检查工具缺失"
  exit 2
fi

exec "${python_path}" "${doctor_tool}" "$@"
