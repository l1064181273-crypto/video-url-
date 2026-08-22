# Local Video Transcriber v0.1 Phase 2 实施计划

## 1. 文档状态

- 项目：`/Users/bytedance/Desktop/Batydance/Public/YYY/local-video-transcriber`
- 原始规格：`/Users/bytedance/Desktop/Batydance/Public/YYY/AGENT_PROJECT_BRIEF_LOCAL_VIDEO_TRANSCRIBER.md`
- Phase 1 冻结基线：`872c42d614a77056c1e9510425955a53ffe40361`
- 本文档性质：Phase 2 实施前的约束性计划，不代表 Phase 2 已实现。
- Phase 2 目标：实现单机持久化任务队列、状态机、进度、有限重试、取消、重启恢复、控制 API 和可独立复现的验收测试。

以下 Phase 1 strict-token 文件相对冻结基线必须保持不变：

```text
backend/src/lvt/engines/translation.py
backend/src/lvt/engines/ollama.py
```

## 2. 范围

### 2.1 Phase 2 内

- FastAPI 生命周期管理的后台 worker。
- SQLite 持久化队列，默认并发 1，可配置为 1 或 2。
- Job 状态机、每次执行唯一 `run_id`、事务 CAS。
- 固定阶段权重、阶段进度、总进度和事件审计。
- 1 次初始执行加最多 2 次 Job 级自动重新入队。
- 手工 retry、取消、外部子进程清理和启动恢复。
- Pipeline 阶段 checkpoint、缓存验证、复用和下游失效。
- retry、cancel、delete、events、artifacts、download、settings API。
- 稳定错误码、中文用户建议、确定性集成测试和真实媒体回归。

### 2.2 明确不做

- Chrome UI、Side Panel、`chrome.storage.local` 或浏览器轮询界面。
- WebSocket、SSE、分布式队列、多用户权限或云同步。
- 安装、LaunchAgent、start/stop/doctor、ZIP、签名、公证或商店发布。
- ASR、diarization、翻译或下载引擎重新选型。
- 新的 strict-token 边界、lexer 或 fuzz 重构。

## 3. 实施前澄清：核心领域契约

### 3.1 Job 状态

持久化状态只有：

```text
queued
downloading
extracting
transcribing
diarizing
segmenting
translating
exporting
completed
failed
cancelling
cancelled
```

`interrupted` 仅是 `job_events.status` 中的审计事件，不是长期 Job 状态。

### 3.2 状态转换矩阵

| 当前状态 | 允许目标状态 | 触发者和条件 |
| --- | --- | --- |
| `queued` | checkpoint resolver 返回的 `first_required_stage` | worker 原子 claim，创建新 `run_id`；无缓存时为 `downloading` |
| `queued` | `cancelled` | cancel API，尚未被 claim |
| `downloading` | `extracting` / `queued` / `failed` / `cancelling` | 当前 run 下载成功、可自动重试错误、终态错误或取消请求 |
| `extracting` | `transcribing` / `queued` / `failed` / `cancelling` | 当前 run 提取成功、可自动重试错误、终态错误或取消请求 |
| `transcribing` | `diarizing` / `segmenting` / `queued` / `failed` / `cancelling` | 当前 run 转写成功；`diarization=false` 时跳过 diarizing；或发生重试、失败、取消 |
| `diarizing` | `segmenting` / `queued` / `failed` / `cancelling` | 当前 run 分离成功、可自动重试错误、终态错误或取消请求 |
| `segmenting` | `translating` / `queued` / `failed` / `cancelling` | 当前 run 分段成功、可自动重试错误、终态错误或取消请求 |
| `translating` | `exporting` / `queued` / `failed` / `cancelling` | 当前 run 翻译成功、可自动重试错误、终态错误或取消请求 |
| `exporting` | `completed` / `queued` / `failed` / `cancelling` | 当前 run 导出成功、可自动重试错误、终态错误或取消请求 |
| `cancelling` | `cancelled` | 当前 run 已停止并完成资源清理 |
| `failed` | `queued` | 合法手工 retry |
| `cancelled` | `queued` | 合法手工 retry |
| `completed` | 无 | 只能读取、下载或确认删除 |

未列出的转换全部拒绝。启动恢复是受控例外：活动阶段记录 `interrupted` 事件后原子转为 `queued`；`cancelling` 原子转为 `cancelled`。

### 3.3 `run_id` 和 CAS

- 每次 worker 成功 claim 时生成全局唯一 UUID `run_id`，写入 Job 的 `active_run_id`。
- 初始创建和重新入队时 `active_run_id = NULL`；新 claim 不复用旧值。
- 每个 run 只能写入自己的 `work/<job_id>/runs/<run_id>/` 临时目录。不同 run
  不共享可写文件，也不能覆盖其他 run 的 checkpoint 或 artifact。
- 所有 worker 发起的状态、进度、错误、artifact 和完成更新必须使用：

```text
job_id + run_id + expected_status
```

- SQL 更新影响行数不是 1 时视为 stale callback，不重试写入，不覆盖新 run 数据，并记录结构化诊断日志。
- `title`、`duration_ms`、`detected_language`、`work_dir`、checkpoint pointer
  及其他 worker 派生元数据同样必须使用上述三元 CAS，不能使用普通 UPDATE。
- checkpoint 先在 run-scoped 目录完整落盘，再由数据库 CAS 发布其 pointer。未发布目录
  不得作为恢复依据；stale run 只能清理自己的目录，不能删除当前 run 或已发布 checkpoint。
- artifact 文件先写入 run-scoped 临时目录；artifact 行、最终 artifact pointer 和
  `completed` 状态在同一数据库事务中以当前 run CAS 提交。
- 自动重新入队和启动恢复会使旧 `run_id` 失效。运行中取消仅在当前 worker
  完成清理并提交 `cancelled` 时清空旧 run。旧 worker 的迟到回调必须因 CAS 不匹配被拒绝。
- API 控制操作不冒充 worker；其转换至少使用 `job_id + expected_status` 的事务 CAS，并在需要时使 `active_run_id` 失效。
- 运行中 cancel 转为 `cancelling` 时必须保留 `active_run_id`。当前 worker 完成进程和
  run-scoped 临时文件清理后，使用原 `run_id` CAS 转为 `cancelled`，再原子清空
  `active_run_id`。只有启动恢复遇到遗留 `cancelling` 时才强制清空旧 run。

## 4. 重试模型

### 4.1 精确定义

- 一个 Job 最多有 3 次 Job 级执行：`1 次初始执行 + 最多 2 次自动重新入队`。
- `execution_count_total` 在成功 claim 时加 1，跨手工重试周期永久累加。
- `retry_cycle` 初始为 0；每次合法手工 retry 加 1。
- `automatic_requeue_count_in_cycle` 在当前周期发生 Job 级自动重新入队时加 1；
  手工 retry 时重置为 0。历史执行和重试通过 `execution_count_total` 与
  `job_events` 永久保留。
- 第三次执行失败后不得再次自动入队，必须进入 `failed`。
- 手工 retry 不重置历史计数；它创建新的手工重试周期，并为该周期重新提供最多 2 次自动重新入队。事件必须记录操作者、原错误和新周期编号。
- Job 级重试与工具内部重试严格分离：
  - yt-dlp 内部网络/fragment 重试固定最多 3 次并在一次 `downloading` stage 内完成，
    不增加 `execution_count_total`。
  - Ollama 引擎内部请求重试及 Hy-MT2 到 qwen fallback 属于一次 `translating`
    stage，不增加 `execution_count_total`，也不修改 Phase 1 校验。
  - 只有整个 stage 最终返回分类后的 `LVTError`，worker 才决定是否 Job 级重新入队。
- 测试注入时钟和 backoff，不真实等待。生产 Job 级 backoff 固定为第 1 次 2 秒、第 2 次 10 秒。

### 4.2 错误策略矩阵

“缓存恢复点”表示 retry 时复用的最近合法上游 checkpoint；失败 stage 及其下游全部失效。

| 错误码 | 自动重新入队 | 手工 retry | 缓存恢复点 | 中文建议 |
| --- | --- | --- | --- | --- |
| `INVALID_URL` | 否 | 否 | 无 | 请检查并重新提交有效的 HTTP/HTTPS 视频地址 |
| `DOWNLOAD_UNSUPPORTED` | 否 | 是 | 无 | 当前地址或站点不受支持，更新下载组件或更换地址后重试 |
| `DOWNLOAD_FAILED` | 是 | 是 | 无；不复用不完整下载 | 检查网络、登录限制或源站状态后重试 |
| `FFMPEG_NOT_FOUND` | 否 | 是 | 已完成的原始下载 | 安装或修复 FFmpeg 路径后重试 |
| `MEDIA_INVALID` | 否 | 是 | 无；下载缓存及下游失效 | 媒体文件无效，请更换来源后重新提交或重试 |
| `ASR_MODEL_MISSING` | 否 | 是 | 规范化音频 | 安装配置的转写模型后重试 |
| `TRANSCRIPTION_FAILED` | 否 | 是 | 规范化音频 | 检查模型、内存和媒体音轨后重试 |
| `DIARIZATION_TOKEN_REQUIRED` | 否 | 是 | ASR 结果和规范化音频 | 配置所需凭证后重试 |
| `DIARIZATION_MODEL_MISSING` | 否 | 是 | ASR 结果和规范化音频 | 安装或修复说话人分离模型后重试 |
| `DIARIZATION_FAILED` | 否 | 是 | ASR 结果和规范化音频 | 检查说话人模型、内存和音频质量后重试 |
| `UNSUPPORTED_SOURCE_LANGUAGE` | 否 | 是 | 已分段 source transcript | 当前源语言不受翻译模型支持，请更换模型或输入 |
| `OLLAMA_UNAVAILABLE` | 是 | 是 | 已分段 source transcript | 启动 Ollama 并确认本地服务可访问 |
| `TRANSLATION_MODEL_MISSING` | 否 | 是 | 已分段 source transcript | 安装主模型或 fallback 模型后重试 |
| `TRANSLATION_INVALID_RESPONSE` | 否 | 是 | 已分段 source transcript | 模型响应未通过严格校验，请检查模型状态后重试 |
| `TRANSLATION_FAILED` | 否 | 是 | 已分段 source transcript | 翻译执行失败，请检查本地模型资源后重试 |
| `TRANSLATION_ALL_MODELS_FAILED` | 否 | 是 | 已分段 source transcript | 主模型和备用模型均失败，请检查两个模型后重试 |
| `EXPORT_FAILED` | 否 | 是 | 已验证 translated transcript | 检查输出目录权限和磁盘空间后重试 |
| `DISK_SPACE_LOW` | 否 | 是 | 最近合法 checkpoint | 清理磁盘空间后重试 |
| `CANCELLED_BY_USER` | 否 | 是 | 最近合法 checkpoint | 任务已取消，可手工重新加入队列 |
| `INTERNAL_ERROR` | 否 | 是 | 最近通过完整性验证的 checkpoint | 查看本地日志，确认环境后手工重试 |

错误分类依据异常类型和明确错误码，不得根据错误消息字符串模糊猜测。由于
`ollama.py` 属于 Phase 1 冻结文件，新增生产错误映射由非冻结 orchestration/error
adapter 完成，并读取 `LVTError.code` 等结构化字段。未识别异常统一为不可自动重试的
`INTERNAL_ERROR`。

## 5. Checkpoint 与缓存

### 5.1 安全恢复点

每个阶段成功后才产生 checkpoint：

```text
downloaded_media
normalized_audio
asr_result
diarization_result
source_transcript
translated_transcript
export_manifest
```

下载和 FFmpeg 提取必须是两个独立阶段，不得继续隐藏在一个 downloader 完成回调中。
queued claim 必须先运行只读 checkpoint resolver，得到连续合法缓存之后的
`first_required_stage`，再在一个事务中直接从 `queued` 转入该阶段并写入包含
`resume_stage` 的 claim 事件。不得先进入 `downloading` 再伪造跳阶段事件。

### 5.2 Manifest 字段

每个 checkpoint manifest 至少包含：

```text
schema_version
job_id
stage
created_at
created_by_run_id
source_url_sha256
job_options
options_fingerprint
engine_names
engine_versions
engine_fingerprint
input_checkpoint_fingerprints
outputs[]:
  relative_path
  kind
  byte_size
  sha256
  media_duration_ms (适用时)
  record_count (适用时)
transcript_schema_version (适用时)
```

- Pipeline 只能读取 Job 创建时持久化的 `JobOptions`，禁止使用请求临时值或当前默认值替代。
- `options_fingerprint` 使用规范化、稳定排序 JSON，包含所有影响结果的选项，包括 ASR model、目标语言、diarization 开关/参数和导出选项。
- `engine_fingerprint` 包含对应阶段引擎名、模型标识、版本和影响结果的实现/schema 版本。

### 5.3 原子写入和验证

1. 输出先写同目录临时文件。
2. flush 并 `fsync` 文件。
3. 计算大小和 SHA-256，写临时 manifest 并 `fsync`。
4. 使用原子 rename 发布输出和 manifest，最后 `fsync` 父目录。
5. 只有完整 manifest 发布成功，stage 才能 CAS 到下一状态。

恢复时验证 schema、Job、URL、options、engine、上游指纹、相对路径、根目录约束、存在性、大小、SHA-256、结构化记录数和媒体可探测性。任何验证失败都把该 checkpoint 视为损坏，不解析部分结果，也不报成功。

### 5.4 失效规则

- 某 stage checkpoint 损坏或指纹不匹配：删除或隔离该 stage manifest，并忽略该 stage 及全部下游缓存。
- 上游输出变化：所有依赖其 fingerprint 的下游缓存失效。
- 只允许复用连续、完整、指纹一致的 checkpoint 前缀，禁止跨过损坏阶段复用更下游结果。
- 引擎版本变化只使使用该引擎的 stage 及下游失效；不影响无关上游下载。
- 删除和隔离路径必须经过 data root containment 检查，不能跟随越界符号链接。
- 如果文件已落盘但 checkpoint pointer 或 artifact DB 事务因 CAS 失败、锁错误或进程
  崩溃而未发布，该目录属于 orphan run output。启动和任务结束 reconciliation 只能在
  确认其 `run_id` 不是当前 active run、也未被任何已发布 pointer 引用后清理。
- reconciliation 必须限制在 `work/<job_id>/runs/<run_id>/` 或对应 artifact 临时根内；
  不能根据数据库外路径递归删除，也不能让 stale run 清理其他 run 的文件。

## 6. 进程控制与取消边界

### 6.1 yt-dlp 和 FFmpeg

- 使用参数数组启动，不使用 shell。
- 使用独立进程组/session；记录 PID 和所属 `run_id`。
- cancel 或 stage timeout 时：
  1. 向进程组发送 TERM。
  2. 最多等待 5 秒。
  3. 未退出则向进程组发送 KILL。
  4. 再等待最多 5 秒并执行 `wait`/reap。
  5. 清理不完整临时文件，最终以当前 run CAS 进入 `cancelled` 或失败。
- 下载默认总超时 30 分钟；FFmpeg 默认超时为 `max(10 分钟, 媒体时长的 2 倍 + 2 分钟)`，均可由内部配置覆盖但不开放任意 API 值。
- stdout/stderr 必须限量采集并去敏，不能因管道无人读取造成死锁。

### 6.2 进程内 ASR、diarization 和翻译调用

- 在调用前、调用返回后、分段循环边界和阶段提交前检查取消令牌。
- 不在线程中异步注入异常，也不声称能安全中断不提供取消 API 的原生调用。
- ASR/diarization 调用开始后，Job 可立即显示 `cancelling`，但只能在当前原生调用返回后进入 `cancelled`。
- 最坏取消延迟等于当前进程内调用的剩余耗时；若底层 native 调用永久挂起，则 Phase 2 无硬上界。这是明确限制，不得在验收中描述为即时取消。
- Phase 2 不为此引入新的模型 worker 进程架构。若真实用户出现永久挂起，再单独评估进程隔离。

## 7. 进度契约

固定阶段权重：

| 阶段 | 权重 |
| --- | ---: |
| `downloading` | 15 |
| `extracting` | 5 |
| `transcribing` | 35 |
| `diarizing` | 15 |
| `segmenting` | 5 |
| `translating` | 20 |
| `exporting` | 5 |

`stage_progress` 为整数 `0..100`。设当前阶段前所有权重之和为 `base`，当前阶段权重为 `weight`：

```text
candidate_overall = floor(base + weight * stage_progress / 100)
persisted_overall = max(previous_overall, candidate_overall)
```

- `queued` 初始为 0；进入新 stage 时 `stage_progress = 0`；阶段完成为 100；`completed` 原子写为 100。
- 同一 `run_id`、同一 stage 的 `stage_progress` 只能单调增加。
- 自动重试、手工 retry 和恢复到更早阶段时，`stage_progress` 可重置为 0，但 `overall_progress` 保留历史高水位，因此 UI 可能暂时平台化但不能倒退。
- failed、cancelling、cancelled 保留最后进度。
- 所有进度回调使用 `job_id + run_id + expected_status` CAS；旧 `run_id`、旧 stage 或较小进度全部拒绝。

## 8. 启动恢复

恢复必须在任何 worker 开始 claim 前，在单个 `BEGIN IMMEDIATE` 事务中完成：

| 启动时状态 | 恢复动作 |
| --- | --- |
| `queued` | 保持不变 |
| `downloading` 至 `exporting` | 写 `interrupted` 事件，清除旧 `active_run_id`，依据合法 checkpoint 原子转为 `queued` |
| `cancelling` | 写恢复事件，清除旧 `active_run_id`，原子转为 `cancelled`，错误码为 `CANCELLED_BY_USER` |
| `completed` / `failed` / `cancelled` | 保持不变 |

- 恢复操作使用 expected status 条件更新；重复启动不会重复产生 `interrupted` 事件。
- 恢复事务提交后才能启动 worker。
- 新 worker claim 会生成新 `run_id`；旧进程或迟到回调因此无法更新恢复后的 Job。
- completed artifact 必须重新通过路径 containment 和存在性检查后提供下载；缺失文件不篡改 completed 历史，而由 artifact API 返回稳定错误并记录事件。

### 8.1 时间戳和错误字段

| 场景 | 字段语义 |
| --- | --- |
| 创建 queued | 设置 `created_at`、`updated_at`；其余时间和错误字段为空 |
| 首次 claim | 设置 `started_at`；后续 retry claim 不覆盖首次开始时间；更新 `updated_at` |
| 自动重新入队 | 保留 `started_at`，清空 `finished_at`；保留本次错误到 event，但 Job 的 `error_code/error_message` 清空；更新 `updated_at` |
| 手工 retry | 保留 `created_at` 和首次 `started_at`，清空 `finished_at` 及 Job 当前错误；事件保留原错误；更新 `updated_at` |
| completed | 设置 `finished_at` 和 `updated_at`，清空当前错误 |
| failed | 设置 `finished_at`、`updated_at`、最终 `error_code/error_message` |
| cancelled | 设置 `finished_at`、`updated_at` 和 `CANCELLED_BY_USER` |

`error_message` 只保存去敏、可安全展示的摘要；内部异常和完整 stderr 只能进入受控日志。

## 9. SQLite 设计

- 从 schema v2 事务迁移到 schema v3；迁移使用 `BEGIN IMMEDIATE`，所有 DDL、数据回填和 schema version 更新一起提交。
- 应用只接受当前版本和可迁移旧版本；检测到高于应用支持的未来版本时拒绝启动并给出明确错误，不尝试降级。
- 新字段至少包括 `active_run_id`、`execution_count_total`、`retry_cycle`、
  `automatic_requeue_count_in_cycle`、`next_attempt_at`、取消请求时间和已发布
  checkpoint pointer。
- 新增持久化 settings 表，至少保存 worker concurrency。
- 启用 `PRAGMA foreign_keys=ON`、WAL 和 `busy_timeout=5000`；每个连接一致配置。
- claim 在一个事务中选择并更新到期的最早 queued Job，排序为 `next_attempt_at, created_at, uuid`。
- 建立 claim 索引：`(status, next_attempt_at, created_at)`。
- artifacts 增加唯一约束 `(job_id, kind)`；重复提交必须冲突或幂等读取，不能生成重复逻辑 artifact。
- migration 测试必须从真实 v2 fixture 升级，并验证失败时完整回滚。

## 10. API 契约

所有 `/api/v1` API 使用现有本地 token 鉴权。未知资源返回 404；合法资源但状态冲突返回 409；输入错误返回 422。响应不得泄漏绝对文件路径或内部异常。

| API | 合法状态和行为 | 重复调用与安全语义 |
| --- | --- | --- |
| `POST /jobs/{id}/retry` | `failed`、`cancelled` 转 `queued`；`queued` 返回当前状态；活动、cancelling、completed 为 409 | 对 queued 重复调用为 200 no-op，不重复创建执行；复用最近合法 checkpoint |
| `POST /jobs/{id}/cancel` | queued 直接 cancelled；活动阶段转 cancelling | cancelling/cancelled 重复调用为 200 no-op；completed/failed 为 409 |
| `DELETE /jobs/{id}?confirm=true` | 仅 completed、failed、cancelled；先校验路径，再删文件和 DB 记录 | 缺 confirm 为 409；首次 204，重复删除 404；不能删除活动 Job 或其他 Job 文件 |
| `GET /jobs/{id}/events` | 任意已存在 Job | 按数据库 ID 稳定排序，分页且只读 |
| `GET /jobs/{id}/artifacts` | completed | 非 completed 为 409；不返回内部 checkpoint |
| `GET /artifacts/{artifact_id}/download` | artifact 所属 Job completed 且文件验证通过 | 404 不泄漏归属；路径必须在 export root 内，拒绝符号链接越界 |
| `GET /settings` | 返回允许公开的本地设置 | 不返回 token、绝对敏感路径或模型凭证 |
| `PATCH /settings` | 只允许 concurrency 为整数 1 或 2 | 相同值为 200 no-op；其他值 422；降为 1 不杀死已运行任务，只限制新 claim |

控制 API 的状态变化和 `job_events` 写入必须同事务完成。Job detail 继续返回持久化 `JobOptions`、当前进度、错误码和可安全展示的中文建议。

## 11. 八个实施 Checkpoint

### Checkpoint 1：contracts/migration

- 范围：Job/状态/事件/错误/重试契约，schema v2 migration，WAL、busy timeout、索引和约束。
- 完成标准：状态矩阵可执行；v2 原位升级和失败回滚通过；未来版本拒绝。
- 测试：状态参数化、migration fixture、schema introspection、配置边界。
- 预计：1 天。
- Commit：`Phase 2: define lifecycle contracts and migrate queue schema`

### Checkpoint 2：repository CAS

- 范围：原子 claim、`run_id`、worker CAS、事件、进度、retry 周期和 artifact repository。
- 完成标准：并发 claim 只有一个胜者；旧 run/stage 回调零写入；事务事件一致。
- 测试：多连接竞争、CAS rowcount、旧 `run_id`、重复 artifact、锁等待。
- 预计：1.5 天。
- Commit：`Phase 2: add run-scoped transactional queue repository`

### Checkpoint 3：pipeline checkpoint

- 范围：持久化 JobOptions 驱动 Pipeline；阶段回调；manifest、指纹、原子发布、验证和失效。
- 完成标准：仅复用连续合法缓存；损坏/选项/引擎变化从正确阶段重跑。
- 测试：Fake Engine 调用次数、每类损坏、options/engine fingerprint、下游失效、8 文件不变量。
- 预计：2 天。
- Commit：`Phase 2: add validated pipeline checkpoints`

### Checkpoint 4：process control

- 范围：拆分 download/extract；yt-dlp/FFmpeg 进程组、timeout、TERM/KILL/wait；取消令牌边界。
- 完成标准：外部命令取消后均被 reap，无不完整 checkpoint；进程内取消限制写入报告。
- 测试：正常退出、TERM、忽略 TERM 后 KILL、timeout、stderr 饱和、临时文件清理。
- 预计：1.5 天。
- Commit：`Phase 2: control downloader and ffmpeg subprocesses`

### Checkpoint 5：worker/retry/progress

- 范围：lifespan worker、并发 1/2、固定权重、自动重试预算和 backoff。
- 完成标准：HTTP 与 worker 解耦；初始加 2 次自动执行边界准确；进度不倒退。
- 测试：成功/失败、错误矩阵、3 次执行上限、内部重试不计数、并发上限、旧回调。
- 预计：1.5 天。
- Commit：`Phase 2: run queued jobs with bounded retries`

### Checkpoint 6：cancel/recovery

- 范围：queued/running cancel、cancelling 收敛、启动恢复和 checkpoint 续跑。
- 完成标准：恢复先于 claim；重复启动幂等；旧 run 失效；terminal 保持。
- 测试：queued cancel、每个活动阶段 cancel、取消/完成竞争、每阶段崩溃恢复、无孤儿进程。
- 预计：2 天。
- Commit：`Phase 2: add cancellation and restart recovery`

### Checkpoint 7：control API

- 范围：retry/cancel/delete/events/artifacts/download/settings API。
- 完成标准：鉴权、状态、404/409/422、幂等、确认删除和路径安全符合第 10 节。
- 测试：API 状态矩阵、重复请求、跨 Job artifact、路径穿越、符号链接、concurrency 1/2。
- 预计：1.5 天。
- Commit：`Phase 2: expose durable job control APIs`

### Checkpoint 8：final acceptance

- 范围：全量集成、真实 API+worker 媒体回归、报告和文档。
- 完成标准：第 12 节全部通过；Phase 1 冻结文件与基线无差异；工作区干净。
- 测试：完整质量门、机器可读报告、敏感文件检查。
- 预计：1.5 天，不含模型下载和独立审查。
- Commit：`Phase 2: verify queue recovery and control workflows`

总预计：12.5 个工程日，不含独立审查、模型下载和外部网络故障等待。

## 12. 最终验收矩阵

| 类别 | 必须证明 |
| --- | --- |
| Claim/CAS | 多 worker 并发 claim 同一 Job 只有一个成功；每次 claim 的 `run_id` 唯一 |
| Stale update | 旧 `run_id` 的状态、进度、错误、artifact 和 completed 回调全部被拒绝 |
| Progress | 每阶段公式正确；同阶段及 overall 单调；retry/recovery 不倒退；最终为 100 |
| Retry | 初始 1 次加最多 2 次自动重新入队；内部 yt-dlp/Ollama retry 不计 Job 次数 |
| Errors | 全部错误码按矩阵验证自动/手工策略、恢复点和中文建议 |
| Cache | Fake Engine 精确调用次数证明合法缓存复用；损坏、option/engine 变化和下游失效正确 |
| Cancel | queued 立即 cancelled；每个 running stage 收敛到 cancelled；重复调用幂等 |
| Recovery | 在 downloading 到 exporting 每个阶段模拟崩溃；interrupted 事件、重新 queued 和安全续跑正确 |
| Process | TERM/KILL/wait 和 timeout 分支均测试；无 yt-dlp/FFmpeg 孤儿进程 |
| Concurrency | 设置 1 时最多 1 个运行 Job，设置 2 时最多 2 个；非法值拒绝 |
| API | retry/cancel/delete/events/artifacts/download/settings 的鉴权、404/409/422、路径和重复调用 |
| Real E2E | 通过真实 API 提交五个样本，由真实 worker 执行并生成 40 个 artifact |
| Artifact readback | 五个任务各 8 文件均可通过 API 下载并回读 |
| Segment invariants | source/zh-CN 的 ID、时间戳、Speaker、语言、顺序、metadata 和 Segment 数量完全一致；`source_text` 永久保留，仅 `translated_text` 写入译文 |
| Regression | Phase 1 全量 pytest、真实引擎和 strict-token 既有报告要求无退化，冻结文件无修改 |
| Batch API | 同一批提交两条 URL，一条成功、一条失败；失败任务手工 retry 后成功 |
| Options | `diarization=false` 从持久化 JobOptions 生效，不调用 diarization 引擎 |
| Process model | 验收只启动一个 Uvicorn 服务进程；测试结束必须停止服务并确认无 worker、HTTP server、yt-dlp 或 FFmpeg 遗留进程 |

最终执行并记录：

```text
pytest
ruff check
ruff format --check
mypy
git diff --check
真实 API + worker 五样本 E2E
40 artifact 下载及回读验证
进程和敏感文件检查
git diff 872c42d614a77056c1e9510425955a53ffe40361 -- \
  backend/src/lvt/engines/translation.py \
  backend/src/lvt/engines/ollama.py
```

最后一条命令必须无输出。不得把 Fake Engine、跳过测试或已有 Phase 1 报告描述为本次真实 Phase 2 验收结果。
