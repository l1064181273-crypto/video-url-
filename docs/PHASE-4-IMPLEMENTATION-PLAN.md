# Local Video Transcriber v0.1 Phase 4 实施计划

- 状态：待实施
- 计划基线：`641563bc9789d8bc3de3141d44d06a22cb54fca2`
- 目标平台：macOS 13+、Apple Silicon arm64
- 目标版本：`0.1.0`
- 最终产物：`dist/LocalVideoTranscriber-mac-arm64-v0.1.0.zip`

## 1. 目标与边界

Phase 4 只负责把已通过验收的 Phase 1–3 交付为可安装、可启动、可诊断、可升级和
可回滚的本地产品。不得重新设计流水线、队列、HTTP API、Chrome UI 或 Phase 3
浏览器边界。

本阶段必须交付：

1. Apple Silicon 安装前检查和首次联网依赖安装。
2. `install.command`、`start.command`、`stop.command`、`doctor.command`、
   `package.command`；为满足卸载要求，另提供 `uninstall.command`。
3. 稳定的应用安装目录、数据目录、模型目录和 unpacked extension 目录。
4. 用户从 ZIP 解压后，经过少量明确步骤即可完成安装、启动和 Chrome 连接。
5. 幂等重装、版本升级、数据库备份、失败回滚和保留数据的卸载。
6. arm64 ZIP、内部文件清单、外部 SHA-256、解压烟雾测试和完整中文文档。
7. 根目录统一质量门，最终 `make verify` 必须串行执行并正确传递非零退出码。

本阶段明确不做：

- 不支持 Intel Mac、Windows 或 Linux。
- 不制作完全离线且内含大模型的 ZIP。
- 不实现自动更新、LaunchAgent 登录自启或后台静默升级。
- 不实现 Apple Developer ID 签名、公证、DMG/PKG。
- 不上架 Chrome Web Store，不生成商店发布包。
- 不把 Python、FFmpeg、Ollama 或模型二进制未经许可直接塞入 ZIP。
- 不修改 Phase 3 业务逻辑；只有安装路径、启动环境等真实阻塞可做最小兼容修改。

冻结边界：

- `extension/src/**`、`extension/public/manifest.json` 和 Phase 3 浏览器/API 安全边界
  全部冻结；Phase 4 只复制已经构建并通过白名单检查的 `extension/dist`，不新增权限、
  host permission、UI、消息或浏览器生命周期逻辑。
- Phase 1 strict-token、翻译、exporter 和真实模型语义冻结。
- Phase 2 Job 状态机、Repository CAS、重试、恢复、控制 API 和现有进程
  TERM/KILL/wait 语义冻结。
- 本计划仅允许三类窄接口变更：安装路径/本地引擎配置、外部进程 ownership observer、
  migration-only/precommit claim barrier。每项必须独立 checkpoint、保持默认开发模式
  兼容并跑完 Phase 1–3 全量回归；不得借 Phase 4 扩展 Chrome 或业务功能。

## 2. 当前基线与实施前缺口

当前已确认：

- 后端版本为 `0.1.0`，支持 Python `>=3.11,<3.13`。
- 默认服务地址为 `127.0.0.1:8765`。
- 数据根目录为
  `~/Library/Application Support/LocalVideoTranscriber/`。
- Token 已由后端使用 48-byte URL-safe 随机值生成，文件权限为 `0600`。
- ASR 默认模型为 `mlx-community/whisper-small-mlx`。
- diarization 使用 sherpa-onnx，模型约 47 MB，不需要 HF Token。
- 翻译使用 Ollama：
  `hy-mt2:1.8b-q4km-fixed` 为主模型，`qwen2.5:1.5b` 为 fallback。
- Phase 3 extension 已构建、验收并冻结。

当前缺口：

- 没有后端锁文件；`pyproject.toml` 中仍有范围依赖。
- 没有 `README.md`、`docs/KNOWN_LIMITATIONS.md`、项目 `LICENSE` 和
  `THIRD_PARTY_NOTICES.md`。
- 五个要求的 `.command` 均不存在。
- `packaging/` 只有 Hy-MT2 Ollama Modelfile。
- 后端 diarization 模型路径仍依赖源码树中的 `vendor/`，不适合无模型 ZIP。
- 没有稳定 extension 安装路径、版本化 release 目录或升级回滚机制。
- 没有可复现 ZIP、SHA-256、解压烟雾测试和统一 `make verify`。

这些缺口必须按 checkpoint 顺序处理。未完成许可证和锁文件门禁前不得发布 ZIP。

## 3. 固定分发决策

### 3.1 ZIP 结构

ZIP 只包含运行代码、已构建 extension、脚本、锁文件和文档：

```text
LocalVideoTranscriber-v0.1.0/
  VERSION
  README.md
  LICENSE
  THIRD_PARTY_NOTICES.md
  MANIFEST.sha256
  backend/
    pyproject.toml
    uv.lock
    src/
  extension/
    dist/
  scripts/
    install.command
    start.command
    stop.command
    doctor.command
    package.command
    uninstall.command
    lib/
  packaging/
    release-manifest.json
    dependencies.json
    ollama/
      Modelfile.hy-mt2-1.8b-q4km
  docs/
    INSTALL.zh-CN.md
    USER_GUIDE.zh-CN.md
    TROUBLESHOOTING.zh-CN.md
    KNOWN_LIMITATIONS.md
    LICENSES/
```

`package.command` 作为发布可复现入口随 ZIP 保留，但只面向维护者；最终用户安装和
运行不依赖 Node、npm、Make 或 `package.command`。

ZIP 严禁包含：

- `models/`、GGUF、ONNX、Hugging Face cache 或 Ollama blobs。
- Token、`.env`、配置文件、数据库、WAL/SHM、PID 文件。
- 日志、work、exports、临时媒体、测试媒体和 Playwright 产物。
- `.git`、`node_modules`、venv、cache、`.DS_Store`、source map。
- 未确认可再分发的 FFmpeg、Ollama、Python 或模型二进制。

### 3.2 安装后目录

保持代码、extension 和用户数据隔离：

```text
~/Library/Application Support/
  .LocalVideoTranscriber.lifecycle/
    lock
    bootstrap.lock/
    uninstall-journal/
      slot-a.json
      slot-b.json
  LocalVideoTranscriber/
    app/
      tools/
        uv/<uv-version>/uv
        python/
      releases/
        0.1.0/
          backend/
          .venv/
          scripts/
          packaging/
          docs/
          VERSION
      current -> releases/0.1.0
    extension/
      manifest.json
      sidepanel.html
      assets...
    runtime/
      backend.pid
      ollama.pid
      install-state.json
      transaction-journal/
        slot-a.json
        slot-b.json
      processes/
    config/
      api-token
      runtime.env
    db/
      lvt.sqlite3
    models/
      huggingface/
      diarization/
      ollama/
      downloads/
    work/
    exports/
    logs/
    backups/
```

约束：

- Chrome 始终加载稳定路径 `.../LocalVideoTranscriber/extension`，升级不能改变该路径。
- `app/current` 只在新 release 完整安装并通过自检后原子切换。
- uv 固定为 `<data-root>/app/tools/uv/<version>/uv`；uv 管理的 CPython 固定在
  `<data-root>/app/tools/python`；每个 release 的 venv 固定为
  `<data-root>/app/releases/<version>/.venv`。
- 安装器设置 `UV_PYTHON_INSTALL_DIR` 和 `UV_PROJECT_ENVIRONMENT`，不得解析或复用
  开发机 `.venv-smoke`、系统 Python site-packages 或其他 release 的 venv。
- `config`、`db`、`models`、`exports` 和 `logs` 不属于任何 release。
- Token 升级时保持不变；只有用户显式执行轮换才生成新 Token。
- `runtime.env` 只保存端口、目录和模型 cache 等非秘密值，不保存 Token。
- 所有临时目录使用 `mktemp -d`、`umask 077` 和 `trap` 清理。

生命周期排他锁：

- lifecycle inode 和 bootstrap lease 永久位于待安装/删除目标的 parent 下：
  `~/Library/Application Support/.LocalVideoTranscriber.lifecycle/`。purge、rollback
  或重装均不得 unlink、rename 或替换该 `lock` inode。
- 第一次安装尚无 app-owned Python 时，先通过同一文件系统上的原子
  `mkdir .LocalVideoTranscriber.lifecycle/bootstrap.lock` 取得 bootstrap lease；
  目录记录 PID、start time 和 release nonce，只有 ownership 全部匹配或确认进程
  不存在时才允许清理 stale lease。
- app-owned Python 可用后，在仍持有 bootstrap lease 时取得
  `fcntl.flock(LOCK_EX)` 的 parent-scoped `lifecycle/lock`，再释放 bootstrap lease，
  锁切换不得出现无锁窗口。lock file 首次创建后永久保留，mode 固定 `0600`。
- 后续 install/upgrade/start/stop/uninstall 全程由 app-owned Python lock runner
  持有同一 flock FD；子进程继承关闭该 FD，不能意外延长锁生命周期。
- `doctor` 默认只读且不取排他锁；执行 DB quick check 时取共享锁。`package.command`
  只操作源码 staging，不取得用户 runtime 锁。

### 3.3 依赖策略

| 组件 | v0.1 策略 |
| --- | --- |
| uv | 安装器下载固定版本的官方 arm64 standalone archive，校验 SHA-256；禁止 `curl | sh` |
| Python | 由固定版本 uv 安装 app-owned CPython 3.11；不修改系统 Python |
| venv | 每个 release 独立 venv，使用 `uv sync --frozen --no-dev` |
| Node/npm | 只用于发布构建；最终用户机器不需要 Node |
| FFmpeg | 通过锁定的 `static-ffmpeg` 在安装期下载到隔离 staging，校验后复制到 app-owned tools；生产运行禁止 `static_ffmpeg.add_paths()` |
| Ollama | 使用已安装的官方 CLI 启动项目专用 daemon；缺失时给出官方安装链接，不静默安装 `.app` 或请求 sudo |
| ASR 模型 | 安装期下载到 `models/huggingface`，默认 whisper-small-mlx |
| Diarization | 安装期从固定 HTTPS URL 下载两个 ONNX，逐文件校验 SHA-256 |
| Hy-MT2 | 下载固定 GGUF、校验 SHA-256，使用项目 Modelfile 创建固定本地模型 |
| qwen fallback | `dependencies.json` 预先固定 Ollama manifest digest、全部 blob digest/size/media type；下载前后均验证，禁止按实际收到内容回填 |

`packaging/dependencies.json` 必须固定 URL、版本、架构、SHA-256、许可证、来源和
下载后预期文件。任何缺少 SHA-256 或许可证的外部二进制不得进入安装流程。

### 3.4 Ollama 所有权

- Local Video Transcriber 不复用用户可能运行在 `127.0.0.1:11434` 的 Ollama。
  项目默认使用专用 `127.0.0.1:11435` 和
  `OLLAMA_MODELS=<data-root>/models/ollama`；backend 的本地 Ollama URL 必须通过
  Phase 4 配置接线到该地址。
- 11435 已被任何非本项目 ownership 的进程占用时，`doctor` 和 `start` 必须失败；
  不自动换端口，不发送信号，不影响 11434 上的用户 Ollama。
- provisioning 在 11435 上启动临时项目-owned daemon。qwen 必须按预置 manifest
  digest 拉取，并逐个检查 app-owned store 中的 `blobs/sha256-*`；manifest 或任一
  blob 不匹配时立即隔离该下载、删除未验证 tag 并失败。
- 禁止把 `ollama pull qwen2.5:1.5b` 返回的 digest 作为新的可信值写回 manifest；
  更新 digest 必须是独立的依赖升级 commit，并经过许可证与真实模型 smoke 审查。
- `stop.command` 只有在 PID、PGID、start time、executable、端口和 ownership nonce
  全部匹配时才停止项目-owned Ollama。

### 3.5 生产 FFmpeg 契约

- provisioning 可在隔离 staging venv 中调用 `static_ffmpeg.add_paths()`，但必须把
  下载后的 `ffmpeg`、`ffprobe` 复制到
  `<data-root>/app/tools/ffmpeg/<version>/bin/`，校验预置 SHA-256、arm64 架构和
  executable mode 后原子发布。
- `runtime.env` 固定 `LVT_FFMPEG_DIR`；生产 backend 只接受该目录中的两个文件，并
  校验 realpath 位于 app root、版本和安装状态中的 digest。
- installed mode 下 `discover_ffmpeg_binaries()` 找不到或校验失败时只返回
  `FFMPEG_NOT_FOUND/UNAVAILABLE`，不得调用 `static_ffmpeg.add_paths()`、不得联网、
  不得回退到 PATH 中未经验证的用户 FFmpeg。
- 开发模式可保留现有 fallback，但必须由明确的非生产配置开启，并有测试证明安装
  launcher 永远启用 strict installed mode。

### 3.6 Token 和 Chrome 连接

- 安装器调用现有安全 helper 生成 `config/api-token`，权限固定为 `0600`。
- Token 不进入命令参数、环境变量、日志、URL、JSON 报告或 ZIP。
- 首次安装完成后通过 `/usr/bin/pbcopy < api-token` 复制到剪贴板，不打印 Token。
- 用户在 `chrome://extensions` 开启开发者模式，加载稳定 `extension` 目录。
- Side Panel 中填写端口 `8765`，粘贴 Token，保存后密码输入立即清空。
- `doctor --json` 不得提供读取或复制 Token 的字段。

### 3.7 版本、签名和发布范围

- 根 `VERSION` 是 release 单一来源，值为 `0.1.0`。
- 测试必须保证 `VERSION`、backend metadata、`/health` 和 extension manifest 一致。
- ZIP 名称固定为 `LocalVideoTranscriber-mac-arm64-v0.1.0.zip`。
- 同目录生成 `.sha256`；ZIP 内生成 `MANIFEST.sha256`。
- v0.1 必需：无签名 ZIP + SHA-256 + 中文 Gatekeeper 打开说明。
- v0.1 非必需、后续项：Developer ID 签名、公证、DMG/PKG、Chrome Web Store、
  CRX/自动更新。文档不得暗示 unsigned ZIP 已签名或公证。
- 不建议用户全局关闭 Gatekeeper；只说明对已核对 SHA-256 的 ZIP 右键“打开”。

## 4. `.command` 契约

### 4.1 通用规则

所有 `.command`：

- 使用 macOS 自带 shell 能力，不依赖 Bash 4。
- 通过脚本自身真实路径定位 release，支持空格、中文和非 ASCII 解压目录。
- 文件模式为 `100755`；ZIP 解压后保持 executable bit。
- 开头设置严格模式和 `umask 077`。
- 不使用 `eval`，不拼接 shell 命令，不静默 `sudo`。
- 删除、rename、symlink 前验证 canonical path 位于应用根或受控临时根。
- 同时输出简体中文人类信息和稳定错误码；秘密操作不进入 tee/log 区域。
- 支持 `LVT_TEST_ROOT`、临时 `HOME` 和 fake executable 注入，用于确定性测试；
  生产默认值不能因测试模式而改变。

### 4.2 `install.command`

执行顺序：

1. 只读 preflight：`uname -m=arm64`、macOS >=13、非 Rosetta、磁盘、内存、网络、
   解压目录和文件完整性。
2. 校验 `MANIFEST.sha256`，确认 release 版本和架构。
3. 取得 bootstrap/lifecycle 排他锁，创建 staging release，不修改 `app/current` 或
   stable extension。
4. 安装固定 uv、app-owned Python 和 release venv；执行 frozen dependency sync。
5. 创建数据目录，不覆盖已有 config/db/models/exports；生成或复用 Token。
6. 执行 `verify_install --phase staging-core --release-root <staging>`：
   - 只验证 manifest、版本、app-owned Python、venv imports、脚本、extension
     candidate 和 Token 文件权限；
   - 明确禁止解析 `app/current`、连接 backend、要求模型或调用默认 full doctor。
7. 下载并校验 FFmpeg、ASR、diarization 和 Ollama 模型；支持安全断点续传。
8. 执行 `verify_install --phase dependencies --release-root <staging>`：
   - 显式检查 app-owned FFmpeg digest、Python packages、ASR cache、两个
     diarization 文件及预固定 Ollama manifest/blob digests；
   - 可以启动临时项目-owned Ollama，但仍禁止依赖 `app/current` 和 backend health。
9. staging/core 与 dependencies 均通过后，准备 stable extension candidate 和
   current candidate；通过同目录 rename 原子切换各自路径，并记录可回滚旧值。
10. 切换后启动 precommit backend，再执行
    `verify_install --phase runtime-full`：
    - 此阶段才允许解析 `app/current`、stable extension、backend health 和完整
      capabilities；
    - 首次安装没有旧版本时，失败必须停止 candidate service、移除 current 和
      stable extension candidate，但保留可复用的已校验 downloads。
11. runtime/full 通过并完成事务 commit 后激活 worker claim，复制 Token 到剪贴板。
12. 输出下一步：Chrome 加载稳定 extension 路径、端口和 Token 已复制提示。

四类验证器必须显式接收 phase，不允许根据文件是否存在自动降级：

| phase | 可依赖 current | 可依赖 backend | 可要求模型 | 可启动 worker |
| --- | --- | --- | --- | --- |
| staging-core | 否 | 否 | 否 | 否 |
| dependencies | 否 | 否 | 是 | 否 |
| installed-prerequisites | 是 | 否 | 是 | 否 |
| runtime-full | 是 | 是，precommit/normal 均有明确状态 | 是 | commit 前否 |

幂等要求：

- 同版本重复安装不得重建 Token、删除任务、重拉已有且 checksum 正确的文件。
- 未完成 download 使用 `.partial`，只在 checksum 通过后 rename。
- 任一 commit 前阶段失败时保持旧 `current`、stable extension 和 service；
  staging 可安全删除或下次恢复。
- 所有 failpoint 都必须在 journal 中有确定状态，不能通过“重新跑 doctor 看结果”
  猜测安装到哪一步。

### 4.3 `start.command`

- 取得 parent-scoped lifecycle lock 并完成 transaction/uninstall journal 对账后，先运行
  `verify_install --phase installed-prerequisites`；该阶段检查 current、stable
  extension、app-owned Python/venv、Token metadata、模型、strict FFmpeg、11435
  端口 ownership、DB/export/log 目录，但明确禁止要求 backend health。
- prestart 退出契约固定：
  - `exit 0` 且 JSON `status=ready_to_start`：唯一允许创建 Ollama/backend 进程的状态；
  - `exit 10` 且 `status=already_running`：不创建任何进程，改为执行 runtime-full；
    runtime-full 通过后 start 整体返回 0；
  - `exit 1` 且 `status=missing_prerequisite`：显示固定中文修复建议并中止，不启动
    部分服务；backend stopped 本身不是缺失项；
  - `exit 2` 且 `status=unsafe_or_corrupt`：fail closed，中止且不得修改安装；
  - 其他退出码或 status/exit 不匹配视为内部错误，退出 2。
- 处理 stale PID 和 PID reuse，不能仅靠 `kill -0`。
- 先确认/启动 11435 上的项目-owned Ollama，再启动当前 release backend；不探测、
  复用或停止用户 11434 daemon。
- backend 不通过环境变量传 Token，读取现有 `config/api-token`。
- 日志写入 `logs/backend.log`；输出使用 `~/...` 形式的日志和 extension 路径。
- 使用有界 health 重试，不用任意 sleep 猜测；成功后显示
  `http://127.0.0.1:8765/health`。
- backend health 成功后才执行默认 `doctor --phase runtime-full`；runtime-full 通过
  才向用户报告启动成功。失败时停止本次创建的 backend/Ollama，保留已安装数据并返回
  doctor 对应非零退出码。
- 启动失败必须清理本次创建的 backend/owned Ollama，不影响任何用户 Ollama。

### 4.4 `stop.command`

- 先验证 PID ownership，再向项目 backend 发 TERM。
- 等待 health 关闭和进程退出；超时后只允许对已验证属于本项目的 PID/进程组 KILL。
- 只停止 11435 上 ownership 完整匹配的项目-owned Ollama，保留 11434 和其他用户
  Ollama。
- 重复执行成功且无副作用；清理 stale PID 文件。
- 最终验证无本项目 backend、worker、yt-dlp、FFmpeg 子进程残留。

### 4.5 yt-dlp/FFmpeg 子进程 ownership 与崩溃清理

- 禁止 `pkill`、`killall`、按名称扫描后发送信号或对未验证 PGID 调用 `killpg`。
- backend 为每个外部工具运行写入
  `runtime/processes/<run_id>/<kind>.json`，使用 temp + fsync + rename + directory
  fsync；记录至少：
  - schema/version、Job UUID、run_id、kind；
  - supervisor PID、PGID、process start time；
  - tool child PID、已验证 executable realpath、device/inode、SHA-256；
  - ownership nonce、创建时间和 lifecycle state。
- `SubprocessExecutor` 只增加可选 ownership observer/supervisor 接口，不改变 Phase 2
  的参数数组、进程组、TERM/KILL/wait、取消或超时语义。
- installed mode 通过 app-owned supervisor 启动每个 yt-dlp/FFmpeg。supervisor 是
  新 session/PGID leader，持有 backend 控制 pipe；backend 崩溃导致 pipe EOF 时，
  supervisor 在自身已验证 PGID 内执行既有 TERM→deadline→KILL→wait 流程并清理记录。
- 正常退出时先 wait/reap 整个进程树，再将 record 标记 completed 并原子删除。
- 应用启动对账只处理未完成 record：
  - supervisor PID、PGID、start time、executable inode/hash 和 nonce 全部匹配，
    才允许向该 PGID 发信号；
  - 任一字段不匹配、PID 已复用或 leader 不存在时绝不发送信号，隔离 record 并由
    doctor 报告 `PROCESS_OWNERSHIP_UNVERIFIED`；
  - 不使用旧 record 推断同名进程属于本项目。
- install/start/stop/upgrade/uninstall 均持有 lifecycle lock，并在切换 service 前等待
  ownership records 收敛；未收敛则失败，不带病切换 release。

### 4.6 `doctor.command`

支持默认人类输出和 `--json`。默认命令只执行已安装且已启动系统的 `runtime-full`；
安装和 start 前检查通过
`packaging/tools/verify_install.py --phase staging-core|dependencies|installed-prerequisites`
调用，不复用默认 doctor 并偷偷放宽必需项。

- schema version、app version、OS、arch、Rosetta、内存、磁盘。
- release/current symlink 和 manifest 完整性。
- Python/venv、包版本、app-owned FFmpeg/ffprobe digest、Ollama 版本与 11435
  ownership。
- ASR package/model、diarization 两模型、主/备翻译模型。
- backend PID ownership、`/health` 和只监听 loopback。
- Token 文件存在、长度有效、owner/mode 正确，但绝不输出内容。
- DB 可读写和 `PRAGMA quick_check`；不得修改业务状态。
- exports/logs 可写，stable extension manifest 完整。

退出码固定：

- `0`：必需项健康。
- `1`：可修复缺失，例如软件、模型、未启动服务。
- `2`：不支持平台、manifest/checksum 错误、权限或路径安全错误。

JSON 只包含状态、版本、布尔值、错误码和去敏建议；不包含 Token、环境变量值、
用户名绝对路径、原始异常或 traceback。

### 4.7 `package.command`

- 仅在干净 Git worktree 和 arm64 发布机运行。
- 从 `VERSION` 派生名称，检查版本一致性。
- 委托 `make package` 的无递归 DAG：先完成 `verify-source`，再重新
  `npm ci && npm run build`、构造 archive 并执行 archive smoke；不得反向调用
  包含 package gate 的 `make verify`。
- 使用明确 allowlist 构造 staging，不从工作树根递归排除。
- 规范化文件顺序、mtime 和模式，移除 xattr/resource fork。
- 生成内部 `MANIFEST.sha256`、ZIP 和外部 `.sha256`。
- 解压后核对文件模式、manifest、禁用项和绝对路径/秘密扫描。
- 同 commit、同工具版本重复构建的 ZIP SHA-256 必须一致。

### 4.8 `uninstall.command`

- 先调用安全 stop。
- 默认只删除 `app/`、稳定 extension 和 runtime PID/state；保留 config、Token、db、
  models、exports、logs 和 backups。
- 明确提供“删除所有数据”选项，必须输入固定中文确认短语。
- purge 前显示将删除的 canonical root，不接受任意参数路径。
- 用户 11434 Ollama 及其模型永不删除；项目 11435 Ollama 模型只有 purge 才删除。
- purge 全程持有 parent-scoped lifecycle flock，并先 durable 写
  `.LocalVideoTranscriber.lifecycle/uninstall-journal/{slot-a.json,slot-b.json}`；
  使用第 4.9 节相同的 generation/checksum/temp-write/file-fsync/rename/dir-fsync
  双槽协议。
- uninstall journal substate 固定为
  `INTENT_WRITTEN → ROOT_TO_TOMBSTONE_RENAMED → PARENT_SYNCED_AFTER_RENAME →
  DELETE_STARTED → TOMBSTONE_REMOVED → PARENT_SYNCED_AFTER_DELETE → COMPLETE`；
  每次 rename/delete 和 parent fsync 前后均有 SIGKILL failpoint。
- 确认服务和 ownership records 收敛后，将
  `LocalVideoTranscriber` 原子 rename 为同 parent 下的
  `.LocalVideoTranscriber.tombstone.<nonce>`，fsync parent，再递归删除 tombstone，
  最后再次 fsync parent；整个删除完成前不得释放 flock。
- purge 不删除 `.LocalVideoTranscriber.lifecycle/lock`。即使最终只剩这个 0600
  lock inode，也必须保留，确保等待中的 install/start 不可能创建不同 inode 并越过
  当前 purge。
- purge 崩溃后，下一命令先取得同一 parent lock，再根据 uninstall journal 和
  root/tombstone 状态幂等完成删除或恢复；禁止在未对账时创建新安装根。
- purge 恢复矩阵：
  - root 存在、tombstone 不存在且已确认 purge：继续 root → tombstone；
  - root 不存在、tombstone 存在：继续删除 tombstone；
  - root/tombstone 均不存在：写 COMPLETE；
  - root/tombstone 均存在：只有 tombstone identity 与 journal 匹配且 root 被证明是
    purge 后外部篡改时才 fail closed；正常并发 install/start 因 parent lock 和启动
    对账不可能创建该组合。
- 所有等待中的 install/start 在取得相同 lock 后必须先完成 uninstall 对账，不能在
  purge 释放后直接创建 root。

### 4.9 通用发布/升级 transaction journal 与启动对账

Checkpoint 7 先引入与数据库无关的通用
`packaging/tools/transaction_journal.py` 和
`runtime/transaction-journal/{slot-a.json,slot-b.json}`，负责首次发布的
current、stable extension、
precommit service、commit 和 activation。Checkpoint 8 只能复用该 API 并向 schema
增加 services/DB/migration 状态；不得让 Checkpoint 7 import 或调用 Checkpoint 8 的
upgrade helper。

journal 权限为 `0600`，只保存 schema、单调 generation、operation
(`first_install|upgrade`)、事务 ID、版本、相对路径、预期 identity/checksum、
decision 和 substate，不保存 Token、环境变量或原始异常。使用双槽而不是原地覆盖：

1. 读取两个 slot，验证 schema、事务 ID、generation 和内容 checksum；选择最高有效
   generation，单个损坏/截断 slot 不影响恢复。
2. 把下一 generation 写入非活动 slot 的唯一临时文件。
3. flush 并 `fsync` 临时文件。
4. rename 临时文件为非活动 slot。
5. 打开 journal directory 并 `fsync` directory FD；至此新 generation 才生效。
6. 不删除仍是上一有效 generation 的另一 slot；下次更新才轮换。

任何会改变 filesystem identity 的操作均使用相同协议：

1. 先 durable 写 intent，包含 source、destination、old/new identity 和预期 checksum。
2. 执行一次 rename 或 file write。
3. durable 写 `effect_observed`。
4. fsync 被写文件（若有）。
5. durable 写 `file_synced`。
6. fsync 每个受影响 parent directory。
7. durable 写 `parent_synced`。

测试必须在上述每个 intent write、journal file fsync、journal rename、目标 rename、
目标 file fsync、parent directory fsync 的前后注入 SIGKILL；不能只在粗粒度 state
结束后注入。

通用发布状态：

```text
PREPARED
CURRENT_SWITCHING
CURRENT_SWITCHED
EXTENSION_SWITCHING
EXTENSION_SWITCHED
SERVICE_PRECOMMIT_READY
COMMITTED
ACTIVATED
```

每个 current/extension switch 在 journal 中分别维护：

```text
intent_written
next_prepared
next_parent_synced
old_to_previous_renamed
parent_synced_after_old
next_to_live_renamed
parent_synced_after_live
identity_verified
```

current 使用 `current.next/current.previous/current`，extension 使用
`extension.next/extension.previous/extension`；首次安装的 `previous` identity 明确为
absent。禁止覆盖式逐文件复制。

通用 current/extension 子步骤恢复矩阵：

| live | next | previous | 未 COMMITTED 自动动作 | 已 COMMITTED 自动动作 |
| --- | --- | --- | --- | --- |
| old | new | absent | 删除 next，保持 old | 发布 new 后验证 |
| absent | new | old | previous → live，删除 next | next → live，保留 previous 到验证完成 |
| new | absent | old | live → next，previous → live | 保持 new，验证后删除 previous |
| new | absent | absent（首次安装） | 删除 live，恢复未安装状态 | 保持 new |
| old | absent | absent | 保持 old | 仅当 old identity 等于 committed new 才完成，否则按 intent 重建 new |
| absent | absent | old | previous → live | 从 journal 指定 candidate 恢复 new，否则停止并报告缺失 artifact |

每行操作都再次走 intent/substate/fsync 协议，因此恢复过程自身可重复崩溃并保持幂等。
live/next/previous 同时存在等额外组合先按预期 identity 分类为 old/new，再归一化到上表；
只有 checksum/identity 与 journal 均冲突、且无法归入任一合法部分完成组合时才
`unsafe_or_corrupt` fail closed。不得把普通“rename 已发生但 journal 完成位未写”
当作不可恢复不一致。

首次发布顺序（Checkpoint 7）：

1. 持 lifecycle lock，对账旧 transaction journal。
2. staging/core 和 dependencies 通过后写 `PREPARED`。
3. 按 substate 协议切换 current，再切换 stable extension。
4. 启动 candidate precommit service；worker 在 claim 前 barrier 阻塞。
5. runtime-full 通过后 durable 写 `COMMITTED`。
6. 发送 ACTIVATE，确认 normal health，写 `ACTIVATED`。
7. 清理 previous/candidate；清理动作本身也有 intent/substate。

升级扩展状态（Checkpoint 8）：

```text
SERVICES_STOPPED
DB_BACKUP_WRITING
DB_BACKED_UP
MIGRATING
MIGRATED
DB_RESTORE_QUARANTINING
DB_RESTORE_WRITING
DB_RESTORED
```

升级在 `PREPARED` 后、current switch 前执行：

1. 停止 backend 和项目-owned Ollama；等待 ownership records 收敛，确认
   ProcessInstanceLock 可取得且没有 backend SQLite connection，写
   `SERVICES_STOPPED`。
2. 数据库备份严格按以下顺序：
   - 由旧 release app-owned Python 打开唯一维护连接；
   - 执行 `PRAGMA wal_checkpoint(TRUNCATE)`，检查返回值，不允许 busy；
   - 执行 `PRAGMA quick_check`；
   - durable 写 backup intent；
   - 使用 SQLite `Connection.backup()` 写同一文件系统 staging backup；
   - commit/close source 和 backup connections；
   - 确认没有进程持有 DB/WAL/SHM，且所有 Python connections 已关闭；
   - fsync backup file 和 backup directory；
   - 独立打开 backup，`quick_check=ok` 后关闭，写 `DB_BACKED_UP`；
   - 不把 live DB/WAL/SHM 当作三个普通文件复制。
3. 使用 candidate 的 migration-only/maintenance entrypoint：
   - 获取同一个 DB instance lock；
   - 只执行 schema initialize/migration 和兼容性检查；
   - 不创建 Pipeline、不启动 JobWorkerPool、不 claim、不恢复 queued Job；
   - 所有 SQLite connections 关闭后写 `MIGRATED`。
4. 复用通用 current/extension/precommit/commit/activation 流程。

DB restore 为每个 DB/WAL/SHM 文件维护独立
`quarantine_intent → renamed → parent_synced` substate，并为 restore temp 维护
`write_intent → written → file_synced → live_renamed → parent_synced → quick_checked`。

DB 部分完成恢复矩阵：

| live DB | quarantine DB | restore temp | WAL/SHM 状态 | 自动动作 |
| --- | --- | --- | --- | --- |
| old/migrated | absent | absent | live 或 absent | 依据 global decision 保持或开始 quarantine |
| absent | present | absent | 任意部分 quarantine | 完成 WAL/SHM quarantine，再写 restore temp |
| absent | present | valid temp | 已 quarantine | fsync temp 后 temp → live |
| restored | present | absent | quarantine 存在 | 验证 restored digest/quick_check，再清理 quarantine |
| restored | absent | absent | live WAL/SHM absent | 标记 DB_RESTORED |
| live 与 quarantine 同时存在 | present | 任意 | 任意 | 按 journal identity 判断 live 是 old/migrated/restored，归一化到前述行 |

WAL 和 SHM 各自按相同 substate 独立恢复；文件不存在是 journal 记录的合法 identity，
不能与“尚未执行”混淆。恢复固定顺序仍是：

1. 持锁并确认无 service、worker、tool supervisor 或 SQLite connection。
2. 依次 quarantine live DB、WAL、SHM，每次 rename 前后更新 journal 并 fsync parent。
3. 从已验证 backup 写 DB 同目录 temp，fsync file。
4. temp → `lvt.sqlite3`，fsync DB directory。
5. 确认 live `-wal/-shm` 不存在；打开唯一连接 quick_check 后关闭。
6. 按通用矩阵恢复 extension/current，启动旧 service 并通过 runtime-full。

全局恢复决策矩阵：

| operation/decision | DB | current/extension | service |
| --- | --- | --- | --- |
| first_install，未 COMMITTED | 无 DB rollback | 自动回到未安装/旧 identity | 停 candidate |
| upgrade，未 COMMITTED 且未 MIGRATED | 保持旧 DB | 自动回 old | 启动旧 service |
| upgrade，未 COMMITTED 且已 MIGRATED | 自动完成 DB restore | DB 完成后自动回 old | runtime-full 后启动旧 service |
| 任一 operation，已 COMMITTED | 不恢复旧 schema | 自动收敛 committed new | 启动/激活新 service |

`COMMITTED` 后不得自动回滚旧 DB/schema，因为 ACTIVATE 后可能已经 claim。如果在
`COMMITTED` 与 `ACTIVATED` 间崩溃，启动对账必须继续新 release 并完成 ACTIVATE。

`start.command`、`install.command` 和 `uninstall.command` 取得 parent lifecycle lock
后的第一动作都是读取 journal 并执行上述矩阵。所有合法部分完成组合必须自动 rollback
或 converge；只有 artifact identity/checksum 真正损坏、无法映射到矩阵时才
fail closed。failpoint 测试覆盖每个子步骤前后，且对账过程自身重复 SIGKILL 后仍幂等。

## 5. Checkpoint 计划

### Checkpoint 1：release 契约、版本、锁文件和许可证门禁

范围：

- 建立 `VERSION`、`backend/uv.lock`、`packaging/release-manifest.json`、
  `packaging/dependencies.json`。
- 建立根 `Makefile` 的 setup/lint/typecheck/test/integration/build-extension/smoke 和
  `verify-source`；Checkpoint 1 严禁提前创建 `package`、`verify-archive`、
  `extracted-smoke` 或最终 `verify` target，也不能用空 target、固定成功命令或 TODO
  冒充验证。
- 建立 project license 决策和第三方 inventory 生成规则。
- 固定所有下载依赖的版本、arm64 URL、SHA-256 和许可证。

计划文件：

- `VERSION`
- `Makefile`
- `backend/uv.lock`
- `packaging/release-manifest.json`
- `packaging/dependencies.json`
- `packaging/tools/check_versions.py`
- `packaging/tools/license_inventory.py`
- `packaging/tests/test_release_contract.py`
- `LICENSE`
- `THIRD_PARTY_NOTICES.md`
- `docs/LICENSES/*`

测试：

- frozen lock 可在空 cache 同步。
- 四处版本一致。
- dependency manifest 拒绝 HTTP、缺 SHA、错误 arch、未知许可证和浮动 tag。
- Python/npm 许可证 inventory 与 notices 对齐。
- `make verify-source` 严格串行执行已存在的 Phase 1–3 源码门，任一子命令失败时立即
  非零退出。
- `make package`、`make verify-archive`、`make extracted-smoke` 和 `make verify`
  在本 checkpoint 必须返回“target 不存在”，证明没有假验证。

完成标准：

- 所有 runtime Python 依赖精确锁定。
- 每个可下载 artifact 有来源、版本、arm64、SHA 和许可证。
- 项目许可证经所有者确认；未确认不得进入后续发布 checkpoint。
- `verify-source` 真实执行全部已接线 gate；package/final verify 尚未声明。

建议 commit：

```text
Phase 4: define arm64 release contract
```

### Checkpoint 2：可安装运行路径

范围：

- 消除 backend 对源码树 `vendor/diarization-models` 的运行时依赖。
- 将 model root 明确为配置，默认使用 data root 下的 `models/`。
- launcher 固定 `HF_HOME/HF_HUB_CACHE` 到 app-owned model root。
- 增加 installed mode、`LVT_FFMPEG_DIR` 和本地 `LVT_OLLAMA_URL` 配置；默认发布配置
  指向 app-owned FFmpeg 和项目专用 `127.0.0.1:11435`。
- installed mode 的 FFmpeg resolver 只接受已验证 app-owned binary，禁止运行时
  `static_ffmpeg.add_paths()` 或 PATH fallback。
- 保持 API、任务状态、artifact 和 Phase 3 capabilities DTO 不变。

计划文件：

- `backend/src/lvt/core/config.py`
- `backend/src/lvt/main.py`
- `backend/src/lvt/pipeline/factory.py`
- `backend/src/lvt/core/capabilities.py`（仅路径配置接线）
- `backend/src/lvt/engines/media.py`（仅 strict installed resolver）
- `backend/tests/unit/test_config.py`
- `backend/tests/unit/test_media.py`
- `backend/tests/integration/test_real_pipeline_factory.py`
- `packaging/tests/test_relocatable_runtime.py`

测试：

- release 位于含空格/中文路径时能启动。
- 临时 HOME/data root 下所有 model probe 只访问 app-owned model root。
- 缺模型显示 missing，不触发下载。
- installed mode 在 app-owned FFmpeg 缺失、digest 错、symlink、PATH 中存在其他
  FFmpeg 时均 fail closed；网络 sentinel 证明任务运行零下载。
- backend 只连接配置的 11435，测试中 11434 即使健康也不被访问。
- Phase 1–3 全量测试和冻结行为回归通过。

完成标准：

- 删除源码树 `vendor/` 后，安装目录配齐模型即可启动。
- runtime 不读取原仓库、开发 venv 或开发者绝对路径。
- 生产任务只执行 install state 中 digest 匹配的 app-owned FFmpeg/ffprobe。

建议 commit：

```text
Phase 4: make runtime model paths installable
```

### Checkpoint 3：命令框架、preflight 与 doctor

范围：

- 建立共享 shell library、路径安全、日志去敏、download/checksum helper、两级
  lifecycle lock。
- 实现 staging-core/dependencies/installed-prerequisites/runtime-full 四个显式
  validator，以及默认只读 `doctor.command` 和 `--json` schema。
- 实现 macOS/arch/Rosetta/磁盘/内存/依赖/模型/权限/health 检查。

计划文件：

- `scripts/lib/common.zsh`
- `scripts/lib/process.zsh`
- `scripts/lib/download.zsh`
- `scripts/doctor.command`
- `packaging/tools/doctor.py`
- `packaging/tools/verify_install.py`
- `packaging/tools/lifecycle_lock.py`
- `packaging/schemas/doctor-v1.schema.json`
- `packaging/tests/test_doctor.py`
- `packaging/tests/test_command_paths.py`

测试：

- fake PATH 覆盖缺 Python、FFmpeg、Ollama 和端口占用。
- temp HOME 覆盖空格、中文、symlink 和不可写目录。
- arm64/macOS/Rosetta、磁盘边界值和三类 exit code。
- JSON schema 验证及递归秘密/绝对路径/traceback 扫描。
- staging-core 明确不读取 current/backend/models；dependencies 明确不读取
  current/backend；runtime-full 缺 current 或 backend 必须失败。
- 两个并发首次 install 只允许一个 bootstrap lease；切换到 flock 无无锁窗口；
  start/stop/install/upgrade/uninstall 相互排斥。
- 持锁进程 SIGKILL、stale PID 和 PID reuse 后按 ownership 规则恢复，不能误删活锁。

完成标准：

- doctor 对未安装、部分安装、健康安装给出稳定状态和中文建议。
- 不加载模型、不联网、不修改数据库或 Token。
- 四阶段 validator 的依赖边界由失败测试锁定，不存在 current/backend 循环依赖。

建议 commit：

```text
Phase 4: add preflight and doctor commands
```

### Checkpoint 4：核心幂等安装

范围：

- 只实现 staging/core：固定 app-owned uv/Python 路径、release venv、frozen sync、
  数据树、Token 和 extension candidate。
- 本 checkpoint 不安装模型、不启动 backend、不写 stable extension、不切换 current。
- 支持同版本 staging 重跑、任意解压路径和失败清理。

计划文件：

- `scripts/install.command`
- `packaging/tools/install.py`
- `packaging/tests/test_install.py`
- `packaging/tests/fixtures/fake-release/*`

测试：

- 空 HOME 首装。
- 路径含空格、中文和非 ASCII。
- 同版本重复安装两次，Token/db inode 和内容不变。
- 在 uv、Python、venv sync、Token、extension candidate 前后注入失败。
- existing current 存在时失败保持旧 release。
- Token mode/owner、日志和进程参数秘密扫描。
- 精确断言本 checkpoint 完成后没有新 current 或 stable extension。

完成标准：

- `--phase staging-core` 可建立完整 candidate/config 结构并通过 staging-core validator。
- 不需要 sudo，不修改系统 Python，不依赖 Node。
- 失败时没有 current/stable extension 发布或损坏数据。

建议 commit：

```text
Phase 4: add idempotent local installer
```

### Checkpoint 5：引擎和模型供应

范围：

- 将 FFmpeg 预热、ASR、diarization、Hy-MT2 和 qwen 安装接入默认 install。
- 下载使用固定 manifest、SHA、`.partial` 和原子 rename。
- 实现 Hy-MT2 fixed Modelfile；qwen 在下载前已固定 manifest 和全部 blob digest，
  下载后逐项验证，禁止记录实际收到内容作为信任来源。
- 提供 `--skip-models` 仅用于测试/受限网络，不能宣称安装完成。
- 本 checkpoint 只完成 dependencies candidate，仍不切换 current/stable extension。

计划文件：

- `packaging/tools/provision.py`
- `packaging/ollama/Modelfile.hy-mt2-1.8b-q4km`
- `packaging/tests/test_provision.py`
- `packaging/tests/fixtures/download-server/*`
- `scripts/install.command`
- `scripts/doctor.command`

测试：

- 本地 HTTP fixture 覆盖正常、断点、截断、checksum 错、重定向到非 HTTPS。
- ASR package/model、diarization 两文件、Ollama down/model missing 状态矩阵。
- 模型已存在时零下载；损坏 cache 只替换损坏文件。
- qwen mutable tag 指向新 manifest、manifest digest 错、任一 blob 错/缺失/size 错均
  失败并隔离；manifest 更新必须修改受审查的 dependency manifest。
- 11435 项目-owned Ollama 正常和端口被占用；11434 用户 Ollama 完全不受影响。
- app-owned FFmpeg 两个文件均验证预置 digest、arm64 和 executable mode；生产
  network sentinel 证明运行时不会再次触发 static-ffmpeg 下载。
- 主模型创建失败时不伪装成功。
- 安装日志、命令行、env dump 无 Token 或受保护 URL query。

人工烟雾：

- 真实 Apple Silicon 下载全部默认模型。
- 运行 FFmpeg、mlx-whisper、sherpa-onnx、Hy-MT2 和 qwen 最小 smoke。

完成标准：

- dependencies candidate 通过 dependencies validator，七项依赖均可供后续发布。
- ZIP 仍不含任何模型或外部二进制。
- current 和 stable extension 仍保持旧版本或不存在。

建议 commit：

```text
Phase 4: provision local engines and models
```

### Checkpoint 6：外部工具 ownership 与崩溃清理

范围：

- 为 yt-dlp/FFmpeg 接入 app-owned supervisor 和持久化 ownership records。
- 保持 Phase 2 SubprocessExecutor 的参数数组、PGID、timeout、cancel 和 wait 语义。
- 实现 backend crash pipe EOF 清理和下次启动的 fail-closed 对账。

计划文件：

- `backend/src/lvt/core/processes.py`（只增加 observer/supervisor 接口）
- `backend/src/lvt/engines/media.py`（只接入 run ownership metadata）
- `packaging/tools/tool_supervisor.py`
- `packaging/tools/reconcile_processes.py`
- `backend/tests/unit/test_process_control.py`
- `backend/tests/integration/test_process_ownership.py`
- `packaging/tests/test_process_reconciliation.py`

测试：

- record 写入在进程暴露给 worker 前完成，并经过 file/directory fsync。
- backend 正常退出、SIGTERM、SIGKILL、supervisor pipe EOF、tool leader 先退出。
- PID/PGID reuse、start time 错、executable inode/hash 错、nonce 错时零 signal。
- backend 崩溃后真实 parent/child/grandchild 全部 TERM→KILL→wait，无 zombie。
- monkeypatch/进程哨兵使 `pkill`、`killall` 和未验证 `killpg` 一旦调用即失败。
- 原 Phase 2 进程控制、取消、shutdown 和全量回归保持通过。

完成标准：

- 可验证 ownership 的崩溃残留被清理；不可验证对象只报告错误，绝不误杀。
- 每个 Job run 的外部工具可按 run_id 审计，record 不含 URL、Token 或媒体参数。

建议 commit：

```text
Phase 4: supervise owned media processes
```

### Checkpoint 7：start/stop、首次发布与 Chrome 连接

范围：

- 实现 backend/项目-owned Ollama 生命周期、健康收敛和日志。
- 将已通过 staging/core 和 dependencies 的首次安装 candidate 发布为 current 和
  stable extension，并执行切换后的 runtime/full。
- 本 checkpoint 自己引入通用首次发布 transaction journal、双槽 durability、
  current/extension substate 和恢复矩阵；不得依赖尚未存在的 upgrade/DB helper。
- 引入 precommit service/worker activation barrier，保证 durable commit 前零 claim。
- 安装完成时安全复制 Token，输出稳定 extension 路径和 Chrome 加载步骤。

计划文件：

- `scripts/start.command`
- `scripts/stop.command`
- `scripts/install.command`
- `scripts/lib/process.zsh`
- `packaging/tools/process_state.py`
- `packaging/tools/transaction_journal.py`
- `packaging/schemas/transaction-journal-v1.schema.json`
- `packaging/tools/publish_install.py`
- `backend/src/lvt/api/app.py`（仅 precommit 启动模式）
- `backend/src/lvt/workers/runner.py`（仅 claim 前 activation barrier）
- `backend/tests/integration/test_precommit_startup.py`
- `packaging/tests/test_service_lifecycle.py`
- `packaging/tests/test_transaction_journal.py`
- `packaging/tests/test_first_install_publish.py`
- `packaging/tests/test_chrome_connection.py`

测试：

- staging/core、dependencies、current switch 后 runtime/full 的严格顺序；前两阶段均不
  读取 current/backend。
- start prestart 四类 exit/status 组合；只有
  `0/ready_to_start` 创建进程，`10/already_running` 只做 runtime-full，其余零启动。
- backend 启动前 runtime-full sentinel 必须未调用；health 成功后 runtime-full 必须
  调用且失败会清理本次服务。
- current 或 stable extension 任一切换后 fail，恢复为旧值或首次安装的“不存在”。
- current 和 extension 的每个 intent、journal slot write/file fsync/rename、
  target rename、parent fsync 前后 SIGKILL；重启按通用矩阵自动恢复。
- 双槽 journal 单槽截断、checksum 错、generation 落后均选择另一有效 generation；
  两槽均坏才 fail closed。
- 首次安装所有 live/next/previous 合法部分组合都自动 rollback/converge，不能仅报告
  filesystem inconsistent。
- precommit service 完成 repository/pipeline/thread 初始化但 activation 前
  `execution_count_total=0`、无 claimed event。
- commit journal fsync 完成后才释放 barrier；pipe EOF/SIGKILL 在 commit 前零 claim。
- 并发执行两个 start，只有一个 backend 实例。
- stale PID、PID reuse、错误 executable、实例锁占用和 11435 端口冲突。
- TERM 正常退出、受控超时 KILL、worker/yt-dlp/FFmpeg 子进程和 ownership record
  归零。
- 11434 用户 Ollama 永不访问或停止；11435 owned Ollama 仅完整匹配时停止。
- Token 不进入 ps、env、URL、日志、DOM 或错误。
- 真实 unpacked Chrome 从 stable extension 路径连接并显示七项 capabilities。

完成标准：

- 首次安装形成完整 current/stable extension/service 事务，无循环 doctor 依赖。
- 通用 journal 和首次发布恢复在本 checkpoint 可独立运行，文件中没有 upgrade 模块
  import，Checkpoint 8 未实现时仍能通过全部首次安装 failpoint。
- 双击 start 后 `/health` healthy；双击 stop 后项目进程归零。
- durable commit 前没有任务处理；用户可完成 Chrome 加载和 Token 粘贴。

建议 commit：

```text
Phase 4: publish and manage local services
```

### Checkpoint 8：事务升级、启动对账和卸载

范围：

- 复用 Checkpoint 7 的 `transaction_journal.py`、current/extension 恢复矩阵和
  precommit service；只向 schema/API 添加 upgrade operation、services、DB backup、
  migration 和 restore substates。
- 实现 migration-only、DB backup/restore、current/extension/service 整体切换和
  rollback；不得复制或替换 Checkpoint 7 的通用 journal。
- `start.command` 在启动任何服务前执行 journal 对账。
- 实现默认保留数据卸载和显式 purge。

计划文件：

- `packaging/tools/upgrade.py`
- `packaging/tools/transaction_journal.py`（仅向后兼容地扩展 upgrade substates）
- `packaging/schemas/transaction-journal-v1.schema.json`（additive schema）
- `packaging/tools/sqlite_backup.py`
- `backend/src/lvt/maintenance.py`
- `backend/tests/integration/test_maintenance_mode.py`
- `scripts/install.command`
- `scripts/start.command`
- `scripts/uninstall.command`
- `packaging/tests/test_upgrade.py`
- `packaging/tests/test_upgrade_recovery.py`
- `packaging/tests/test_uninstall.py`
- `packaging/tests/test_uninstall_concurrency.py`

测试：

- 0.1.0 → 测试版 0.1.1；默认拒绝降级。
- migration-only 模式不构造 Pipeline/JobWorkerPool，不写 claimed event。
- DB/WAL/SHM 每个 quarantine intent、rename、file fsync、directory fsync，以及
  restore temp write/fsync/rename/directory fsync 前后 SIGKILL；重启按 DB 矩阵自动
  恢复且幂等。
- 参数化所有可达的 DB/WAL/SHM `live/quarantine/restore-temp` 子状态笛卡尔组合；
  每个组合必须自动归一化为旧版本 rollback 或 committed 新版本 converge。只有
  identity/checksum 与 journal 矛盾的不可达组合才允许 fail closed。
- current/extension 子步骤沿用 Checkpoint 7 测试向量，upgrade 不允许改变其恢复结果。
- commit 前所有 failpoint 均 `execution_count_total=0`；COMMITTED 后只向新版本收敛。
- WAL active、checkpoint busy、open connection、backup quick_check 失败均阻止切换。
- DB restore 顺序验证：无连接 → quarantine DB/WAL/SHM → restore temp/fsync/rename →
  无旧 WAL/SHM → quick_check/close。
- current 已切/extension 未切、extension 已切/service 未 ready、precommit process
  crash 的逆序 rollback。
- rollback 后 DB、current、stable extension 和旧 service 均为旧版本且 health 正常。
- Token、Job、artifact、models 和 exports 在成功升级/rollback 中保持。
- uninstall 默认保留数据；purge 需要固定确认短语；恶意路径/symlink 全部拒绝。
- Barrier 将 purge 阻塞在 root→tombstone rename 前后及 tombstone 删除中，同时启动
  install/start：后两者只能等待同一 parent flock，释放后先完成 uninstall 对账。
- purge 前后 `stat` 验证 parent lock device/inode 恒定；任何测试出现第二 lock inode
  或等待方越过 purge 即失败。
- purge 在每个 uninstall journal substate 后 SIGKILL；root/tombstone 恢复矩阵最终
  自动完成删除，不能留下允许新 install 混入的窗口。

完成标准：

- 每个 commit 前 failpoint 后完整回旧版本；COMMITTED 后完整收敛到新版本。
- DB、current、stable extension 和 service 不出现可观察混合状态。
- migration-only 和 precommit barrier 证明 commit 前零任务 claim。
- Checkpoint 7 的首次发布 transaction tests 无修改继续通过，证明不存在反向依赖。
- purge 删除完数据后 parent-scoped lock inode 仍存在且后续 install/start 使用同一
  inode。

建议 commit：

```text
Phase 4: add transactional upgrade and uninstall
```

### Checkpoint 9：可复现 arm64 ZIP 与最终 Make gates

范围：

- 实现 allowlist staging、内部 manifest、可执行位、确定性 ZIP 和外部 SHA。
- 本 checkpoint 才新增 `package`、`verify-archive`、`extracted-smoke` 和最终
  `verify` Make targets；此前它们必须不存在。
- 加入 ZIP 安全扫描、架构检查、版本检查和解压 smoke。

计划文件：

- `scripts/package.command`
- `packaging/tools/package_release.py`
- `packaging/tools/verify_archive.py`
- `packaging/tests/test_package.py`
- `packaging/tests/test_extracted_install.py`
- `Makefile`
- `.gitignore`

精确 archive 规则：

- `SOURCE_DATE_EPOCH` 固定为 release Git commit 的 UTC commit timestamp；早于 ZIP
  下限时才 clamp 到 `1980-01-01T00:00:00Z`。
- allowlist 路径转换为 `/` 分隔的 POSIX relative path，按 UTF-8 bytes 升序；禁止
  absolute、`..`、重复归一化路径、symlink、hardlink、device 和 socket。
- Python `zipfile.ZipFile` 固定 `ZIP_DEFLATED`、`compresslevel=9`、
  `strict_timestamps=True`；每个 `ZipInfo` 固定 `create_system=3`、UTF-8 flag、
  空 comment/extra。`external_attr` 精确写为：目录
  `(stat.S_IFDIR | 0o755) << 16 | 0x10`、`.command`
  `(stat.S_IFREG | 0o755) << 16`、其他 regular
  `(stat.S_IFREG | 0o644) << 16`。使用 `writestr` 已知 bytes，general-purpose
  bit 3 必须为 0，禁止 data descriptor 和宿主 xattr。
- `MANIFEST.sha256` 只哈希顶层目录内除自身外的所有 regular files；格式固定为按
  path 排序的 `<lowercase sha256><two spaces><relative POSIX path>\\n`。
  manifest 不哈希自身，外部 `.zip.sha256` 只哈希最终 ZIP，二者不存在循环。
- `package.command` 只执行 `/usr/bin/make package`；Makefile 标记相关 release
  targets `.NOTPARALLEL`。`package` 依赖真实 `verify-source` 后直接调用 packaging
  tool，不能再次调用 `package.command`；`verify` 严格串行执行
  `verify-source → package → verify-archive → extracted-smoke`。

测试：

- Checkpoint 8 commit 与 Checkpoint 9 修改前，四个新 target 均不存在；本 checkpoint
  加入后每个 target 都有可观察产物和失败反例，禁止空 recipe。
- 同 commit、相同 Python/zlib/tool versions 连续构建两次 SHA-256 相同；机器报告
  记录这些工具版本。
- ZIP 顶层单目录、无 traversal、absolute path、symlink、xattr/resource fork 或
  data descriptor。
- `.command` 解压后为 executable；其他文件 mode 精确匹配。
- allowlist 与 `MANIFEST.sha256` 的“除自身外”集合完全一致，增删任一文件都会失败。
- 扫描模型、Token、数据库、日志、临时媒体、用户路径、source map 和测试产物。
- 所有 Mach-O/native Python 扩展为 arm64 或 universal2 且包含 arm64，不含
  x86_64-only。
- 在新的含空格/中文目录解压，使用临时 HOME 完成完整
  install/start/health/doctor/stop，不只执行 core staging。

完成标准：

- 生成 `dist/LocalVideoTranscriber-mac-arm64-v0.1.0.zip` 和同名 `.sha256`。
- `make package`、`make verify-archive`、`make extracted-smoke`、`make verify`
  均执行真实门并 exit 0。
- ZIP 解压完整 smoke 通过；同环境双构建 SHA-256 相同。

建议 commit：

```text
Phase 4: build reproducible arm64 release zip
```

### Checkpoint 10：中文文档与最终交付验收

范围：

- 编写安装、使用、Chrome 连接、排错、升级、卸载、已知限制和许可证文档。
- 在近似干净 Apple Silicon 用户环境执行真实首次安装。
- 更新 PROGRESS、TEST_REPORT 和机器可读 Phase 4 报告。

计划文件：

- `README.md`
- `docs/INSTALL.zh-CN.md`
- `docs/USER_GUIDE.zh-CN.md`
- `docs/TROUBLESHOOTING.zh-CN.md`
- `docs/KNOWN_LIMITATIONS.md`
- `PROGRESS.md`
- `TEST_REPORT.md`
- `docs/PHASE-4-ACCEPTANCE.json`

文档必须包含：

- 最短流程：解压 → 核对 SHA → 右键打开 install → start → Chrome 加载 → 粘贴 Token。
- 首次安装联网范围、模型大小、预计耗时、磁盘和 8GB/16GB 内存建议。
- FFmpeg、Ollama、Python、ASR、diarization、主/备翻译模型逐项缺失处理。
- Chrome reload、Token 重复制、端口冲突、doctor 错误码和日志位置。
- 升级、rollback、数据保留卸载、彻底删除和备份恢复。
- 本地处理边界：媒体下载需要网络；已下载模型的推理可离线。
- unsigned ZIP 的 Gatekeeper 限制；不得建议关闭系统安全机制。
- 不支持登录/DRM/验证码、Intel Mac、自动更新、签名、公证和 Web Store。

最终测试：

- 新用户或隔离 HOME、无开发 venv、无 Node 依赖。
- 真实安装全部模型、start/doctor、Chrome unpacked 加载和 Token 连接。
- 至少一个短真实/本地媒体任务生成 8 artifact。
- stop、start、upgrade fixture、默认 uninstall 和数据保留。
- ZIP SHA、manifest、敏感文件扫描、残留进程和工作树检查。

完成标准：

- 原始规格第 14 节 A–F 全部有命令、退出码和产物证据。
- `make verify` exit 0。
- `docs/PHASE-4-ACCEPTANCE.json` 可独立复核。
- 提交后工作树干净；Phase 4 才可宣布完成。

建议 commit：

```text
Phase 4: document and verify clean installation
```

## 6. 测试分层

### 6.1 确定性测试

- Python pytest 驱动 shell scripts，使用临时 HOME 和 fake PATH。
- 本地 HTTP fixture 提供固定下载字节、redirect、截断和 checksum 错误。
- Event/Barrier 同步并发 start、stop 和升级 failpoint，不用随机 sleep。
- 进程测试记录 PID、启动时间和 executable，专门覆盖 PID reuse。
- 文件系统测试覆盖空格、中文、symlink、权限、跨卷 rename 失败和磁盘不足。

### 6.2 真实 Apple Silicon smoke

- 真实 uv/Python 3.11、static-ffmpeg、Ollama 和默认模型。
- static-ffmpeg 只用于 provisioning；任务 smoke 必须执行已校验的 app-owned
  FFmpeg/ffprobe，并用网络 sentinel 证明运行时没有隐式下载。
- 真实 unpacked Chrome extension 从 stable path 加载。
- 真实 backend 只监听 127.0.0.1。
- 最小本地媒体任务验证 ASR、diarization、翻译和 8 artifact。
- 真实 ZIP 解压到新目录和近似干净 HOME。

### 6.3 最终统一命令

计划中的最终 gate：

```bash
make setup
make lint
make typecheck
make test
make test-integration
make build-extension
make smoke
make verify-source
make package
make verify-archive
make extracted-smoke
make verify

shasum -a 256 -c dist/LocalVideoTranscriber-mac-arm64-v0.1.0.zip.sha256
```

Checkpoint 1 结束时上面只有 `make setup` 至 `make verify-source` 存在；其余四个
release target 必须到 Checkpoint 9 才加入。Checkpoint 9 后 `make verify` 必须通过
`.NOTPARALLEL` 或单 recipe 严格串行执行：

1. 版本、lock、license 和 dependency manifest 检查。
2. backend Ruff、mypy、unit/integration。
3. extension lint、typecheck、unit、build、真实 Chrome E2E。
4. packaging tests、doctor schema、script syntax。
5. 安全的临时 HOME install/start/health/stop smoke。
6. ZIP allowlist、SHA、arm64、权限、秘密和解压检查。

## 7. 风险与人工前置条件

| 风险/前置条件 | 处理 |
| --- | --- |
| 项目自身 LICENSE 尚未确定 | 所有者必须在 Checkpoint 1 选择许可证；未确认不得发布 |
| Python/npm/模型传递许可证 | 自动 inventory + THIRD_PARTY_NOTICES；最终需人工法律复核 |
| uv/Python/模型下载 URL 或 SHA 变化 | 固定 manifest；变更必须单独审查和重跑真实安装 |
| Ollama 未安装 | 给官方链接和中文步骤；不静默 sudo、不自动安装未知 `.app` |
| Ollama HF GGUF 自动模板问题 | 始终使用已验证项目 Modelfile；Ollama 升级后重新 smoke |
| 首次下载体积大 | 安装前至少要求 8 GB 可用，建议 12 GB；显示分项大小和进度 |
| 8 GB 内存性能不足 | 文档建议 16 GB；允许使用更小 ASR，但不改变默认验收模型 |
| Gatekeeper 阻止 unsigned `.command` | 文档说明核对 SHA 后右键打开；不关闭 Gatekeeper |
| Chrome unpacked extension ID/path变化 | 使用稳定 extension 路径，并在升级 smoke 验证 storage 保留 |
| 用户 Ollama 与项目 daemon 冲突 | 项目固定 11435/app-owned store；11434 永不访问或停止 |
| 数据库 migration 不可逆 | maintenance 模式、SQLite backup、durable journal；COMMITTED 前回滚，之后只向新版本收敛 |
| 安装中断或磁盘满 | staging + `.partial` + checksum + 原子切换；旧版本继续可用 |
| 真实下载服务不可用 | 固定本地 fixture 保证测试；最终报告如实记录真实网络状态 |
| 干净 Apple Silicon 机器 | 最终验收需要至少一个新用户/近似干净环境人工提供 |

人工确认项：

1. 项目 LICENSE 选择。
2. 第三方 notice 和模型许可证最终审核。
3. 发布用 uv、Python、模型和工具 URL/SHA 固定。
4. 一台 macOS 13+ Apple Silicon 的近似干净用户环境。
5. 安装测试期间可访问 PyPI、Astral/GitHub、Hugging Face 和 Ollama 模型源。
6. Chrome 稳定版中手工确认开发者模式加载和 Gatekeeper 首次打开体验。

## 8. 最终验收矩阵

| 验收项 | 必须证据 |
| --- | --- |
| Apple Silicon/macOS | arm64、macOS 13+ preflight 正反例 |
| 首装 | 新 HOME 的真实 install exit 0 |
| 启停 | 重复 start/stop、health、零残留进程 |
| doctor | human/JSON、七项 capability、秘密递归扫描 |
| Chrome | stable unpacked path、Token 连接、升级后 storage 保留 |
| 模型 | FFmpeg、ASR、diarization、Hy-MT2、qwen 真实 smoke |
| Token | 0600、剪贴板流程、URL/log/ps/env/ZIP 无秘密 |
| 升级 | maintenance 零 claim、DB backup、journal 对账、current/extension/service 整体切换 |
| 卸载 | 默认保留数据、显式 purge、用户 11434 Ollama 保留 |
| ZIP | allowlist、可执行位、内部 manifest、外部 SHA、可复现 |
| 安全 | 无模型/Token/db/log/media/cache/source map |
| 解压 smoke | 新目录安装、start、health、doctor、stop |
| 文档 | README、安装、用户、排错、限制、license |
| 回归 | Phase 1–3 全量质量门保持通过 |

## 9. 建议实施顺序和总体退出条件

必须按 Checkpoint 1 → 10 顺序推进。每个 checkpoint：

- 先写失败测试。
- 只修改列明文件范围。
- 运行受影响测试和 Phase 1–3 回归。
- 更新 PROGRESS/TEST_REPORT。
- 创建单一、可独立回滚的 commit。
- 独立审查通过后冻结，再进入下一个 checkpoint。

Phase 4 只有在以下全部成立时完成：

- `make verify` exit 0。
- arm64 ZIP 和 `.sha256` 存在且可复核。
- 新目录、近似干净 HOME 的真实解压安装 smoke 通过。
- Chrome unpacked extension 可连接实际 backend。
- ZIP 不含模型、秘密或用户数据。
- 中文文档和机器报告完整。
- 工作树干净。
- 签名、公证、DMG/PKG、Web Store 和自动更新明确仍是后续项。
