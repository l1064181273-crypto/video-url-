#!/bin/zsh

set -eu
setopt pipe_fail
umask 077

typeset script_dir="${0:A:h}"
source "${script_dir}/lib/common.zsh"
source "${script_dir}/lib/process.zsh"

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
  else
    lvt_log ERROR "START_ARGUMENT_INVALID：启动参数无效"
    exit 2
  fi
done
if (( expect_data_root )); then
  lvt_log ERROR "START_ARGUMENT_INVALID：--data-root 缺少值"
  exit 2
fi
if [[ -z "${data_root}" ]]; then
  data_root="$(lvt_default_data_root)"
fi

typeset release_root
release_root="$(lvt_current_release "${data_root}")" || {
  lvt_log ERROR "START_INSTALL_UNSAFE：当前安装版本不存在或路径不安全"
  exit 2
}
typeset python_path
python_path="$(lvt_release_python "${release_root}")" || {
  lvt_log ERROR "START_PYTHON_MISSING：应用 Python 不可用"
  exit 1
}
typeset process_tool="${release_root}/packaging/tools/process_state.py"
lvt_assert_within_root "${release_root}" "${process_tool}" || {
  lvt_log ERROR "START_PATH_UNSAFE：生命周期工具路径不安全"
  exit 2
}
if [[ ! -f "${process_tool}" || -L "${process_tool}" ]]; then
  lvt_log ERROR "START_TOOL_MISSING：生命周期工具缺失"
  exit 2
fi

set +e
typeset result
result="$("${python_path}" "${process_tool}" start \
  --data-root "${data_root}" \
  --release-root "${release_root}")"
typeset command_status=$?
set -e
print -r -- "${result}"
if (( command_status == 0 )); then
  lvt_log INFO "START_READY：本地服务已启动"
  lvt_log INFO "START_HEALTH：http://127.0.0.1:8765/health"
  lvt_log INFO "START_EXTENSION：${data_root/#${HOME}/\~}/extension"
elif (( command_status == 1 )); then
  lvt_log ERROR "START_PREREQUISITE_MISSING：请先完成安装依赖"
else
  lvt_log ERROR "START_UNSAFE：服务状态无法安全收敛"
fi
exit "${command_status}"
