#!/bin/zsh

set -eu
setopt pipe_fail
umask 077

typeset script_dir="${0:A:h}"
typeset release_root="${script_dir:h}"
source "${script_dir}/lib/common.zsh"
source "${script_dir}/lib/download.zsh"

typeset -r bootstrap_python_url="https://releases.astral.sh/github/python-build-standalone/releases/download/20260623/cpython-3.11.15%2B20260623-aarch64-apple-darwin-install_only_stripped.tar.gz"
typeset -r bootstrap_python_sha256="2318799eaf104f8a29bc09a93b0851b05dbbcb4ce9a5f045ddea169c0c7ff3a5"
typeset -r bootstrap_python_size="27115717"
typeset bootstrap_temp=""
typeset bootstrap_parent=""

function lvt_cleanup_python_bootstrap() {
  if [[
    -n "${bootstrap_temp}" &&
    -n "${bootstrap_parent}" &&
    "${bootstrap_temp:h}" == "${bootstrap_parent}" &&
    "${bootstrap_temp:t}" == lvt-python-bootstrap.* &&
    -d "${bootstrap_temp}" &&
    ! -L "${bootstrap_temp}"
  ]]; then
    /bin/chmod -R u+w "${bootstrap_temp}" 2>/dev/null || true
    /bin/rm -rf "${bootstrap_temp}"
  fi
  bootstrap_temp=""
}

function TRAPEXIT() {
  lvt_cleanup_python_bootstrap
}

function lvt_bootstrap_python() {
  bootstrap_parent="${TMPDIR:-/tmp}"
  if [[ ! -d "${bootstrap_parent}" || -L "${bootstrap_parent}" ]]; then
    lvt_log ERROR "PYTHON_BOOTSTRAP_FAILED：临时目录不安全"
    return 1
  fi
  bootstrap_parent="${bootstrap_parent:A}"
  bootstrap_temp="$(/usr/bin/mktemp -d "${bootstrap_parent}/lvt-python-bootstrap.XXXXXXXX")" || {
    lvt_log ERROR "PYTHON_BOOTSTRAP_FAILED：无法创建临时目录"
    return 1
  }
  if ! lvt_assert_within_root "${bootstrap_parent}" "${bootstrap_temp}"; then
    lvt_log ERROR "PYTHON_BOOTSTRAP_FAILED：临时路径不安全"
    return 1
  fi

  typeset archive="${bootstrap_temp}/python-runtime.tar.gz"
  typeset expected_sha256="${bootstrap_python_sha256}"
  typeset expected_size="${bootstrap_python_size}"
  if [[
    -n "${LVT_TEST_ROOT-}" &&
    "${LVT_TEST_FORCE_PYTHON_BOOTSTRAP-}" == "1" &&
    -n "${LVT_TEST_BOOTSTRAP_PYTHON_ARCHIVE-}" &&
    -n "${LVT_TEST_BOOTSTRAP_PYTHON_SHA256-}" &&
    -n "${LVT_TEST_BOOTSTRAP_PYTHON_SIZE-}"
  ]]; then
    typeset test_archive="${LVT_TEST_BOOTSTRAP_PYTHON_ARCHIVE}"
    expected_sha256="${LVT_TEST_BOOTSTRAP_PYTHON_SHA256}"
    expected_size="${LVT_TEST_BOOTSTRAP_PYTHON_SIZE}"
    if [[ -L "${test_archive}" || ! -f "${test_archive}" ]]; then
      lvt_log ERROR "PYTHON_BOOTSTRAP_FAILED：测试运行时不可用"
      return 1
    fi
    /bin/cp "${test_archive}" "${archive}"
    lvt_verify_file "${archive}" "${expected_sha256}" "${expected_size}" || {
      lvt_log ERROR "PYTHON_BOOTSTRAP_FAILED：测试运行时校验失败"
      return 1
    }
  else
    lvt_download_verified \
      "${bootstrap_python_url}" \
      "${bootstrap_temp}" \
      "python-runtime.tar.gz" \
      "${expected_sha256}" \
      "${expected_size}" || {
      lvt_log ERROR "PYTHON_BOOTSTRAP_FAILED：Python 下载或校验失败"
      return 1
    }
  fi

  typeset extract_root="${bootstrap_temp}/extract"
  /bin/mkdir "${extract_root}"
  /usr/bin/tar -xzf "${archive}" -C "${extract_root}" || {
    lvt_log ERROR "PYTHON_BOOTSTRAP_FAILED：Python 解压失败"
    return 1
  }
  bootstrap_python_root="${extract_root}/python"
  typeset python_executable="${bootstrap_python_root}/bin/python3"
  if [[
    -L "${bootstrap_python_root}" ||
    ! -d "${bootstrap_python_root}" ||
    ! -x "${python_executable}" ||
    "${python_executable:A}" != "${bootstrap_python_root:A}/"*
  ]]; then
    lvt_log ERROR "PYTHON_BOOTSTRAP_FAILED：Python 运行时结构无效"
    return 1
  fi
}

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
typeset bootstrap_python_root=""
typeset -a bootstrap_install_arguments
bootstrap_install_arguments=()
if [[ -n "${LVT_TEST_ROOT-}" && -n "${LVT_PYTHON-}" ]]; then
  python_path="${LVT_PYTHON}"
elif [[
  "${data_root}" == /* &&
  "${data_root}" == "${data_root:A}" &&
  -x "${data_root}/app/tools/python/bin/python3" &&
  ! -L "${data_root}/app/tools/python/bin/python3"
]]; then
  python_path="${data_root}/app/tools/python/bin/python3"
elif (( ${+commands[python3]} )) && [[ "${LVT_TEST_FORCE_PYTHON_BOOTSTRAP-}" != "1" ]]; then
  python_path="${commands[python3]}"
else
  lvt_log INFO "PYTHON_BOOTSTRAP_START：正在准备内置 Python 运行时"
  lvt_bootstrap_python || exit 1
  python_path="${bootstrap_python_root}/bin/python3"
  bootstrap_install_arguments=(--bootstrap-python-root "${bootstrap_python_root}")
  lvt_log INFO "PYTHON_BOOTSTRAP_READY：Python 运行时已校验"
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
  "${python_path}" "${install_tool}" \
    --phase staging-core \
    --data-root "${data_root}" \
    "${bootstrap_install_arguments[@]}"
  lvt_cleanup_python_bootstrap
  exit 0
fi

if [[ -z "${phase}" ]]; then
  "${python_path}" "${install_tool}" \
    --phase staging-core \
    --data-root "${data_root}" \
    "${bootstrap_install_arguments[@]}"
  if [[ -n "${bootstrap_python_root}" ]]; then
    python_path="${data_root}/app/tools/python/bin/python3"
    lvt_cleanup_python_bootstrap
  fi
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
