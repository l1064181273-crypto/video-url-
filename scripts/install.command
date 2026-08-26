#!/bin/zsh

set -eu
setopt pipe_fail
umask 077

typeset script_dir="${0:A:h}"
typeset release_root="${script_dir:h}"
source "${script_dir}/lib/common.zsh"

typeset data_root=""
typeset expect_data_root=0
typeset phase=""
typeset expect_phase=0
typeset skip_models=0
typeset argument
for argument in "$@"; do
  if (( expect_data_root )); then
    data_root="${argument}"
    expect_data_root=0
  elif (( expect_phase )); then
    phase="${argument}"
    expect_phase=0
  elif [[ "${argument}" == "--data-root" ]]; then
    expect_data_root=1
  elif [[ "${argument}" == --data-root=* ]]; then
    data_root="${argument#--data-root=}"
  elif [[ "${argument}" == "--phase" ]]; then
    expect_phase=1
  elif [[ "${argument}" == --phase=* ]]; then
    phase="${argument#--phase=}"
  elif [[ "${argument}" == "--skip-models" ]]; then
    skip_models=1
  else
    lvt_log ERROR "INSTALL_ARGUMENT_INVALID：安装参数无效"
    exit 2
  fi
done
if (( expect_data_root || expect_phase )); then
  lvt_log ERROR "INSTALL_ARGUMENT_INVALID：安装参数缺少值"
  exit 2
fi
if [[ -z "${data_root}" ]]; then
  data_root="$(lvt_default_data_root)"
fi
if [[ -n "${phase}" && "${phase}" != "staging-core" && "${phase}" != "dependencies" ]]; then
  lvt_log ERROR "INSTALL_ARGUMENT_INVALID：安装阶段无效"
  exit 2
fi
if [[ "${phase}" == "staging-core" ]] && (( skip_models )); then
  lvt_log ERROR "INSTALL_ARGUMENT_INVALID：核心阶段不接受 --skip-models"
  exit 2
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

if [[ "${phase}" == "staging-core" ]]; then
  exec "${python_path}" "${install_tool}" \
    --phase staging-core \
    --data-root "${data_root}"
fi

if [[ -z "${phase}" ]]; then
  "${python_path}" "${install_tool}" \
    --phase staging-core \
    --data-root "${data_root}"
fi

typeset provision_tool="${release_root}/packaging/tools/provision.py"
lvt_assert_within_root "${release_root}" "${provision_tool}" || {
  lvt_log ERROR "INSTALL_PATH_UNSAFE：供应工具路径不安全"
  exit 2
}
if [[ ! -f "${provision_tool}" || -L "${provision_tool}" ]]; then
  lvt_log ERROR "INSTALL_MISSING：供应工具缺失"
  exit 2
fi

typeset version_file="${release_root}/VERSION"
if [[ ! -f "${version_file}" || -L "${version_file}" ]]; then
  lvt_log ERROR "INSTALL_MISSING：版本文件缺失"
  exit 2
fi
typeset version
version="$(<"${version_file}")"
if [[ -z "${version}" || "${version}" == *[^0-9.]* ]]; then
  lvt_log ERROR "INSTALL_ARGUMENT_INVALID：版本信息无效"
  exit 2
fi
typeset candidate="${data_root}/app/releases/${version}"
typeset -a provision_arguments
provision_arguments=(
  --phase dependencies
  --data-root "${data_root}"
  --release-root "${candidate}"
)
if (( skip_models )); then
  provision_arguments+=("--skip-models")
fi
set +e
"${python_path}" "${provision_tool}" "${provision_arguments[@]}"
typeset provision_status=$?
set -e
if (( provision_status != 0 )); then
  exit "${provision_status}"
fi
if [[ "${phase}" == "dependencies" ]]; then
  exit 0
fi

typeset publish_tool="${release_root}/packaging/tools/publish_install.py"
lvt_assert_within_root "${release_root}" "${publish_tool}" || {
  lvt_log ERROR "INSTALL_PATH_UNSAFE：发布工具路径不安全"
  exit 2
}
if [[ ! -f "${publish_tool}" || -L "${publish_tool}" ]]; then
  lvt_log ERROR "INSTALL_MISSING：发布工具缺失"
  exit 2
fi
exec "${python_path}" "${publish_tool}" \
  --data-root "${data_root}" \
  --release-root "${candidate}"
