#!/bin/zsh

set -eu
setopt pipe_fail
umask 077

typeset script_dir="${0:A:h}"
typeset release_root="${script_dir:h}"
source "${script_dir}/lib/common.zsh"

typeset data_root=""
typeset expect_data_root=0
typeset argument
for argument in "$@"; do
  if (( expect_data_root )); then
    data_root="${argument}"
    expect_data_root=0
  elif [[ "${argument}" == "--data-root" ]]; then
    expect_data_root=1
  elif [[ "${argument}" == --data-root=* ]]; then
    data_root="${argument#--data-root=}"
  fi
done
if (( expect_data_root )); then
  lvt_log ERROR "INSTALL_ARGUMENT_INVALID：--data-root 缺少参数"
  exit 2
fi

typeset -a install_arguments
install_arguments=("$@")
if [[ -z "${data_root}" ]]; then
  data_root="$(lvt_default_data_root)"
  install_arguments+=("--data-root" "${data_root}")
fi

typeset python_path=""
if [[ -n "${LVT_TEST_ROOT-}" && -n "${LVT_PYTHON-}" ]]; then
  python_path="${LVT_PYTHON}"
elif [[
  "${data_root}" == /* &&
  "${data_root}" == "${data_root:A}" &&
  -x "${data_root}/app/tools/python/bin/python3" &&
  ! -L "${data_root}/app/tools/python/bin/python3"
]]; then
  python_path="${data_root}/app/tools/python/bin/python3"
elif (( ${+commands[python3]} )); then
  python_path="${commands[python3]}"
else
  lvt_log ERROR "PYTHON_MISSING：未找到可用的 Python 3"
  exit 1
fi

typeset install_tool="${release_root}/packaging/tools/install.py"
lvt_assert_within_root "${release_root}" "${install_tool}" || {
  lvt_log ERROR "INSTALL_PATH_UNSAFE：安装工具路径不安全"
  exit 2
}
if [[ ! -f "${install_tool}" || -L "${install_tool}" ]]; then
  lvt_log ERROR "INSTALL_MISSING：安装工具缺失"
  exit 2
fi

exec "${python_path}" "${install_tool}" "${install_arguments[@]}"
