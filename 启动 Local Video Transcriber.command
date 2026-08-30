#!/bin/zsh

set -eu
setopt pipe_fail
umask 077

typeset release_root="${0:A:h}"
typeset scripts_root="${release_root}/scripts"
source "${scripts_root}/lib/common.zsh"
source "${scripts_root}/lib/process.zsh"

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
    lvt_log ERROR "LAUNCH_ARGUMENT_INVALID：启动参数无效"
    exit 2
  fi
done
if (( expect_data_root )); then
  lvt_log ERROR "LAUNCH_ARGUMENT_INVALID：--data-root 缺少值"
  exit 2
fi
if [[ -z "${data_root}" ]]; then
  data_root="$(lvt_default_data_root)"
fi

typeset release_version_file="${release_root}/VERSION"
if [[ ! -f "${release_version_file}" || -L "${release_version_file}" ]]; then
  lvt_log ERROR "LAUNCH_VERSION_MISSING：发行版本信息缺失"
  exit 2
fi
typeset release_version
release_version="$(<"${release_version_file}")"
if [[ -z "${release_version}" || "${release_version}" == *[^0-9.]* ]]; then
  lvt_log ERROR "LAUNCH_VERSION_INVALID：发行版本信息无效"
  exit 2
fi

typeset current_release=""
typeset launch_action="first_run"
if current_release="$(lvt_current_release "${data_root}" 2>/dev/null)"; then
  typeset installed_version_file="${current_release}/VERSION"
  lvt_assert_within_root "${current_release}" "${installed_version_file}" || {
    lvt_log ERROR "LAUNCH_VERSION_UNSAFE：已安装版本路径不安全"
    exit 2
  }
  if [[ ! -f "${installed_version_file}" || -L "${installed_version_file}" ]]; then
    lvt_log ERROR "LAUNCH_VERSION_MISSING：已安装版本信息缺失"
    exit 2
  fi
  typeset installed_version
  installed_version="$(<"${installed_version_file}")"
  if [[ -z "${installed_version}" || "${installed_version}" == *[^0-9.]* ]]; then
    lvt_log ERROR "LAUNCH_VERSION_INVALID：已安装版本信息无效"
    exit 2
  fi
  if [[ "${installed_version}" != "${release_version}" ]]; then
    autoload -Uz is-at-least
    if is-at-least "${release_version}" "${installed_version}"; then
      lvt_log ERROR "LAUNCH_DOWNGRADE_REFUSED：不会用旧发行包覆盖较新版本"
      exit 2
    fi
    launch_action="upgrade"
    lvt_log INFO "LAUNCH_UPGRADE：检测到旧版本，开始安全升级"
  else
    typeset installed_start="${current_release}/scripts/start.command"
    lvt_assert_within_root "${current_release}" "${installed_start}" || {
      lvt_log ERROR "LAUNCH_PATH_UNSAFE：已安装启动脚本路径不安全"
      exit 2
    }
    if [[ ! -x "${installed_start}" || -L "${installed_start}" ]]; then
      lvt_log ERROR "LAUNCH_START_MISSING：已安装启动脚本缺失"
      exit 2
    fi
    exec "${installed_start}" --data-root "${data_root}"
  fi
fi

typeset installer="${scripts_root}/install.command"
lvt_assert_within_root "${release_root}" "${installer}" || {
  lvt_log ERROR "LAUNCH_PATH_UNSAFE：安装脚本路径不安全"
  exit 2
}
if [[ ! -x "${installer}" || -L "${installer}" ]]; then
  lvt_log ERROR "LAUNCH_INSTALL_MISSING：安装脚本缺失"
  exit 2
fi
if [[ "${launch_action}" == "first_run" ]]; then
  lvt_log INFO "LAUNCH_FIRST_RUN：首次运行将安装并启动本地服务"
fi
exec "${installer}" --data-root "${data_root}"
