# PROGRESS — Local Video Transcriber v0.1

> 本文件持续更新。记录：已完成、正在做、失败原因、下一步。

## 环境快照（Phase 0）

| 项目 | 值 |
|---|---|
| 芯片 | Apple M5 (arm64, Apple Silicon) |
| macOS | 26.5.2 (build 25F84) |
| 内存 | 16 GB |
| 磁盘可用 | ~327 GB (卷 /System/Volumes/Data) |
| Python | 3.11.15 (`~/.local/bin/python3.11`，uv 管理的 CPython) + 系统 3.9.6 |
| Node | v24.10.0 |
| npm | 10.9.8 |
| git | 2.50.1 |
| Chrome | 151.0.7922.172 |
| `say` | 可用（用于生成测试语音） |

### 依赖存在性

| 工具 | 状态 | 备注 |
|---|---|---|
| git | ✅ | |
| python3.11 | ✅ | venv/pip 正常，pip 26.1.2 |
| node/npm | ✅ | |
| Chrome | ✅ | |
| say | ✅ | |
| uv | ❌ 未安装 | 只有 uv 管理的 Python 缓存；Phase 0 采用标准 venv 降级路径 |
| ffmpeg / ffprobe | ❌ 未安装 | 无 Homebrew；将从静态构建（evermeet.cx，200 可达）或引导安装 |
| ollama | ❌ 未安装 | ollama.com 可达（200）；本地 11434 未监听 |
| yt-dlp | ❌ 未安装 | 将通过 pip 安装到 venv |
| Homebrew | ❌ 未安装 | 安装脚本不得静默 sudo；采用免 sudo 方案 |

### 网络可达性（首次安装允许联网）

- PyPI: 200 ✅
- ollama.com: 200 ✅
- huggingface.co: 200 ✅
- evermeet.cx (ffmpeg static): 200 ✅

## 决策

- **Python 环境管理**：brief 首选 uv；本机无 uv 二进制，按 brief 第 4.2 条"环境不允许时提供标准 venv 降级路径"，Phase 0 使用 `python3.11 -m venv`。是否补装 uv 于 Phase 4 打包时评估。
- **候选引擎**（待 Phase 0 烟雾测试后在 ADR-001 锁定）：
  - ASR：mlx-whisper（优先，Apple Silicon）→ faster-whisper（降级）
  - Diarization：pyannote.audio（需 HF 只读 Token）→ 等价 CPU/MPS 方案
  - 翻译：Ollama + Hy-MT2 1.8B GGUF（优先）→ 许可证允许的等价开源模型
  - 上述均为**候选**，非固定依赖。

## Phase 0 烟雾测试结果

| 引擎 | 候选 | 结果 | 证据 |
|---|---|---|---|
| ASR | mlx-whisper 0.4.3 (mlx 0.32.1) | ✅ 通过 | en 样本：detected=en，5 段，时间戳单调 `[0.00→2.88]…[11.68→14.12]`，14s 音频 6.4s；ru 样本：detected=ru，4 段，1.8s（tiny 模型文字粗糙属正常，生产用更大模型） |
| ffmpeg/ffprobe | static-ffmpeg (ffmpeg 7.0/8.0 darwin_arm64) | ✅ 通过 | pip 安装，二进制在 `.venv-smoke/.../static_ffmpeg/bin/darwin_arm64/`；成功抽音频、生成测试资产、probe 时长 |
| 翻译 | Ollama 0.32.15 + Hy-MT2-1.8B Q4_K_M（主）/ qwen2.5:1.5b（fallback） | ✅ Phase 0.1 通过 | 腾讯官方 GGUF 直拉成功；修正 Ollama 自动模板后，EN/RU/探索性超范围 Swahili 各 3 轮共 9/9 次通过结构不变量。Phase 1 已按官方表映射全部 38 个支持代码；完整结果见 `docs/PHASE-0.1-TRANSLATION-AB.json` |
| Diarization | sherpa-onnx 1.13.6（**token-free**） | ✅ 通过 | 双人 16s 样本，`num_clusters=2`：4 段，spk0/spk1 交替与脚本吻合，distinct={0,1}，0.8s。模型：pyannote-seg-3.0 onnx(MIT,7MB)+titanet-small(40MB)，**无需 HF token** |

**关键发现（影响选型）**：
- `registry.ollama.ai/v2/library/hy-mt2` 返回 404 仅表示官方 library 没有短名称，**不能推导为模型不可用**。腾讯官方 Hugging Face 仓库提供 `ollama run hf.co/tencent/Hy-MT2-1.8B-GGUF:Q4_K_M`，已真实下载（1.1 GB，Apache-2.0）并运行。
- Ollama 0.32.15 对该 HF GGUF 自动生成的模板损坏（包含 `{{ end }}onse }}`），导致 `format=json` 返回错误结构。使用腾讯官方 `chat_template.jinja` 创建本地 Modelfile 后恢复正常；模板固化在 `packaging/ollama/Modelfile.hy-mt2-1.8b-q4km`。
- Phase 0.1 A/B：Hy-MT2 热运行中位数 0.350s / 99.27 tok/s / 1379 MiB VRAM；qwen 0.307s / 112.62 tok/s / 1109 MiB VRAM。两者结构测试均 9/9 通过；Swahili 不在官方支持表，仅作为探索性超范围压力样本，不能据此宣称官方支持。结合模型定位与实测，**Hy-MT2 锁定为主引擎，qwen2.5:1.5b 为 fallback**。
- ollama 默认写 `~/.ollama`，沙箱/受限环境需用 `OLLAMA_MODELS`/`HOME` 指向应用工作目录；已在 smoke 环境用项目内 `vendor/ollama-home` 验证可行，生产将指向 Application Support。
- mlx_whisper 通过 subprocess 调 `ffmpeg`，必须保证 ffmpeg 在 PATH（或注入路径）。

## 进度日志

- **2026-08-22**
  - ✅ 读取 brief，确认需求（含用户两处修正：翻译写入 `translated_text` 不覆盖 `source_text`；候选引擎需实测后锁定）。
  - ✅ Phase 0：环境检查（芯片/内存/磁盘/网络/依赖）完成，见上表。
  - ✅ 创建项目骨架 `local-video-transcriber/`、`.gitignore`、本文件、`scripts/make-test-assets.sh`。
  - ✅ 生成可重复测试资产：en_single(14s)、ru_single、two_speakers(16s)、silence、tone、含中文空格文件名。
  - ✅ ASR 烟雾测试（mlx-whisper，en+ru）通过。
  - ✅ ffmpeg 通过 static-ffmpeg 安装并验证。
  - ✅ Ollama 安装 + 启动 + qwen2.5:1.5b 翻译烟雾测试（JSON id-mapping 不变量）通过。
  - ✅ Phase 0.1：通过官方 HF 引用真实下载 Hy-MT2-1.8B Q4_K_M；确认 1.79B / Q4_K_M / 1.1 GB / Apache-2.0。
  - ✅ 定位并修复 Ollama 0.32.15 自动生成 Hy-MT2 chat template 损坏问题；使用本地 GGUF + Modelfile + `ollama create` 验证成功。
  - ✅ Phase 0.1：完成 Hy-MT2 与 qwen2.5:1.5b 的 EN/RU/斯瓦希里语 A/B（每语言 3 轮），验证 JSON id mapping、`source_text` 永久不变、只写 `translated_text`、速度与内存。
  - ✅ Phase 0.1 结论：Hy-MT2 为 v0.1 默认翻译引擎；qwen2.5:1.5b 为已验证 fallback。
  - ✅ sherpa-onnx 免 token diarization 烟雾测试（双人）通过 —— **消除 HF token 硬阻塞**。
  - ✅ 定稿 `docs/ADR-001-engine-selection.md`，锁定四引擎与版本。
  - **Phase 0 退出条件已满足**：ASR / 音频 / diarization / 翻译 四类引擎均有真实可运行结果。
  - ✅ Phase 1 基础：建立 Python 包、Pydantic Segment/Transcript、不变量、路径/URL 安全、SQLite jobs/job_events/artifacts schema。
  - ✅ Phase 1 审查修复：Host 仅允许 `127.0.0.1`/`::1`；导出目录使用安全标题 + job_id 后缀；schema v2 持久化 JobOptions；完整映射 Hy-MT2 官方语言；实现有界重试与显式 qwen fallback。
  - ✅ Phase 1 API：实现 `/health`、Token 鉴权、capabilities、批量 jobs 创建/列表/详情；单条无效 URL 不阻塞其他任务；重启后 Options 可恢复。
  - ✅ Phase 1 纵切：通过可注入 Fake Engines 串通下载结果 → ASR → 最大重叠 Speaker 映射 → 翻译 → 规范化 JSON → 8 文件导出，中文路径正常。
  - ✅ Phase 1 真实引擎：接入 yt-dlp + FFmpeg 规范化、mlx-whisper 自动语言转写、sherpa-onnx threshold diarization、Hy-MT2/qwen fallback。
  - ✅ Phase 1 本地 HTTP 真实纵切：英语单人 14.016s、俄语单人 11.328s、双人 16.256s、中文空格文件名、两个同标题任务均生成 8 文件；共 5 样本/40 文件独立回读验证通过。
  - ✅ Phase 1 公开 URL：Wikimedia Commons 英语演讲 47.488s，6 Segment、1 Speaker、8 文件真实纵切与回读通过。
  - ✅ Phase 1 fallback 实测：俄语任务的 Hy-MT2 连续 3 次返回包装文本，被语义校验拒绝；qwen2.5:1.5b 成功接管，warnings 与 engine_versions 明确记录；其余本地任务和公开样本未降级。
  - ✅ Phase 1 follow-up：增加非翻译文本分类与过滤层；纯 URL、时间码、数字、Speaker、NASA/GPT-5 直接 passthrough，混合批次只发送需翻译 ID，合并后保持完整 ID/顺序/Segment 字段。
  - ✅ Phase 1 follow-up：普通正文仍强制中文；混合句强制保留 URL、数字和专有 token；主备模型双失败仍明确报错，不用原文伪装成功。
  - ✅ Phase 1 follow-up 真实回归：本地 HTTP 5 样本/40 文件再次生成并回读通过，机器报告为 `docs/PHASE-1-PASSTHROUGH-FOLLOWUP.json`。
  - ✅ Phase 1 strict-token follow-up：移除宽泛 Title Case/全大写 passthrough；仅保留 NASA/OpenAI 显式白名单及 GPT-5 类连字符+数字强特征，Good Morning/STOP/Elon Musk 等均进入模型。
  - ✅ Phase 1 strict-token follow-up：每次 token 出现使用唯一 `LVT_TOKEN_XXXX`；严格验证 Segment 内占位符 ID、数量、顺序和 ASCII 边界后逐次恢复；Ollama 增加 `num_predict=1024` 有界输出。
  - ✅ Phase 1 真实 Ollama smoke：passthrough IDs `[1,2,3,4,5]` 未发送模型，translate IDs `[6,7,8,9,10]` 实际发送；重复 NASA/URL、数字、时间码恢复一致，报告为 `docs/PHASE-1-STRICT-TOKEN-OLLAMA-SMOKE.json`。
  - ✅ Phase 1 strict-token 媒体回归：本地 HTTP 5 样本/40 文件再次生成并回读通过，报告为 `docs/PHASE-1-STRICT-TOKEN-E2E.json`。
  - ✅ Phase 1 Unicode/nonce follow-up：数字边界改为 ASCII 标识符边界；中文、日韩文、西里尔字母相邻数字可保护，abc2026/GPT5/version2 不拆分。
  - ✅ Phase 1 Unicode/nonce follow-up：每个 batch 生成无碰撞随机 nonce；使用单次正则回调恢复，支持原文 `LVT_TOKEN_0001`、跨 Segment 碰撞规避和 URL 内旧占位符文本。
  - ✅ Phase 1 URL follow-up：扫描器支持平衡圆括号、query、fragment、百分号编码和 Unicode URL，并剥离句末标点。
  - ✅ Phase 1 扩展真实 Ollama smoke：nonce `AAD66A1ECF24DF6D`；passthrough IDs `[1,2,3,4,5,13]`，模型 IDs `[6,7,8,9,10,11,12,14,15]`；报告为 `docs/PHASE-1-UNICODE-NONCE-OLLAMA-SMOKE.json`。
  - ✅ Phase 1 最新媒体回归：本地 HTTP 5 样本/40 文件再次通过，报告为 `docs/PHASE-1-UNICODE-NONCE-E2E.json`。
  - ✅ Phase 1 最终边界 follow-up：恰好一个 passthrough token 且外围仅有原始空白、中英文标点、成对圆/方括号或成对引号时整段逐字符 passthrough；含真实正文的混合句仍进入模型。
  - ✅ Phase 1 数字范围 follow-up：`2026-2027`、`10-20`、`123-456` 及 en dash/em dash 形式作为单一 token；中文、韩文和西里尔文字相邻时仍保护，GPT-5/abc-2026/version-2/GPT5 不被错误拆分。
  - ✅ Phase 1 内部前缀 follow-up：模型输出统一扫描 `LVT_` 保留前缀，只允许 manifest 精确列出的占位符；其他 nonce、位数错误、增加、重复、修改或错序均失败，原文字面量仅在单次恢复后允许重新出现。
  - ✅ Phase 1 最终边界真实 Ollama smoke：nonce `2B15629AF58F1A95`；带标点原子 passthrough IDs `[16,17,18,19,20]`、纯范围 ID `[21]` 未发送模型，新增混合正文 IDs `[22,23,24]` 实际发送；Hy-MT2 成功且未 fallback，报告为 `docs/PHASE-1-FINAL-BOUNDARY-OLLAMA-SMOKE.json`。
  - ✅ Phase 1 最终边界媒体回归：本地 HTTP 5 样本/40 文件再次生成并回读通过；俄语样本显式使用 qwen fallback，其余样本使用 Hy-MT2，报告为 `docs/PHASE-1-FINAL-BOUNDARY-E2E.json`。
  - ✅ Phase 1 URL 边界 follow-up：ASCII 逗号和中文句读终止 URL 扫描，无空格英文/中文正文继续进入模型；query、fragment、百分号编码、Unicode 路径和平衡括号能力保持不变。
  - ✅ Phase 1 多段数字 follow-up：hyphen、en dash、em dash 和 `/` 数字链整体保护；日期、电话号码及中日韩/西里尔文字相邻形式均覆盖，ASCII 标识符不拆分。
  - ✅ Phase 1 包装顺序 follow-up：使用栈验证括号和方向引号的嵌套与方向；合法 `([“NASA”])` passthrough，交叉、缺失和反向包装进入模型。
  - ✅ Phase 1 扩展真实 Ollama smoke：首次 31 段单批压力测试因占位符重排与 fallback JSON 截断而安全双失败；最终按真实字幕规模分为两个批次，nonce `FBD1EC4DBE32FF87`，均由 Hy-MT2 成功，报告为 `docs/PHASE-1-URL-MULTIPART-WRAPPING-OLLAMA-SMOKE.json`。
  - ✅ Phase 1 最新媒体回归：本地 HTTP 5 样本/40 文件再次生成并回读通过；俄语样本显式使用 qwen fallback，其余样本使用 Hy-MT2，报告为 `docs/PHASE-1-URL-MULTIPART-WRAPPING-E2E.json`。
  - ✅ Phase 1 质量门：`pytest` 147 passed；Ruff lint/format（含 scripts）通过；mypy 27 个源码文件通过；真实产物验证通过。

- **2026-08-23**
  - ✅ Phase 1 右括号 URL follow-up：URL 扫描维护半角/全角圆括号闭合栈；匹配的 URL 内右括号保留，无对应左括号的右括号在其前终止，后续无空格正文继续进入模型。
  - ✅ Phase 1 前缀版本 follow-up：`v1.2.3`、`version1.2.3`、`release-v1.2.3` 整体保护；普通数字匹配禁止从点分链中间开始，`prefix1.2.3` 不再错误生成尾部 `2.3` token。
  - ✅ Phase 1 生产 prompt 回归：确定性测试直接捕获 `OllamaTranslationEngine` prompt，确认 URL/版本本体完全替换，`)Continue`、`）继续` 和正文仍对模型可见；模型返回 `v9.2.3` 会被严格拒绝。
  - ✅ 真实 smoke 稳定性如实记录：独立审查历史为 1/2；本轮扩展后连续两次为 2/2，nonce 分别为 `A8CCB0A5658DB1AF`、`904921B8DE99F31E`。每次 3 个真实批次均由 Hy-MT2 完成；双失败仍保持非零退出。报告为 `docs/PHASE-1-PAREN-VERSION-OLLAMA-SMOKE.json`。
  - ✅ Phase 1 最新媒体回归：本地 HTTP 5 样本/40 文件再次生成并回读通过；俄语样本显式使用 qwen fallback，其余样本使用 Hy-MT2，报告为 `docs/PHASE-1-PAREN-VERSION-E2E.json`。
  - ✅ Phase 1 数字候选链 follow-up：先扫描完整点/逗号/斜杠/三类横线数字链，再按候选两端 ASCII 标识符上下文整体接收或拒绝；拒绝后跳过整个候选，禁止从中段重新匹配。
  - ✅ Phase 1 句末版本 follow-up：`v`、`version`、`release-v` 点分版本支持句末标点、更长数字段、prerelease 与 build 后缀；`foo-v1.2.3` 等非支持前缀完全不匹配。
  - ✅ Phase 1 输出增量校验：移除 expected placeholder 后重新扫描模型输出，模型额外生成的 URL、数字、版本或其他 protected token 会触发有限重试/fallback；主备模型同错仍抛出 `TRANSLATION_ALL_MODELS_FAILED`。
  - ✅ Phase 1 最新真实 Ollama smoke：历史独立审查 1/2 和上一轮 2/2 记录均保留；本轮连续两次为 2/2，nonce `798AD684EDDC8608`、`82B9D463AE4AAD6D`，每次 5 个批次均由 Hy-MT2 完成，报告为 `docs/PHASE-1-NUMERIC-CHAIN-VERSION-OLLAMA-SMOKE.json`。
  - ✅ Phase 1 最新媒体回归：本地 HTTP 5 样本/40 文件再次生成并回读通过；俄语样本显式使用 qwen fallback，其余样本使用 Hy-MT2，报告为 `docs/PHASE-1-NUMERIC-CHAIN-VERSION-E2E.json`。
  - ✅ Phase 1 质量门：`pytest` 193 passed；Ruff lint/format（含 scripts）通过；mypy 27 个源码文件通过；真实产物验证通过。
  - **Phase 1 退出条件已满足**：三个真实媒体引擎已接入统一 Pipeline，并通过真实 HTTP fixture 生成与验证合规 8 文件。
  - ✅ Phase 1 已由独立审查在 commit `872c42d614a77056c1e9510425955a53ffe40361`
    验收，strict-token 实现冻结。

- **2026-08-23**
  - ✅ Phase 2 实施计划已完成并在 commit `4576edb279875c752921058a1992430ed591c33c`
    补充实施前澄清：run-scoped 所有权、worker 元数据 CAS、取消 run 生命周期、
    重试周期、生产错误码、resume stage、orphan reconciliation 和时间戳语义。
  - ✅ Phase 2 Checkpoint 1 完成：新增 Job 状态、合法转换、事件类型、完整错误策略和
    结构化错误 adapter 契约。
  - ✅ SQLite schema v2→v3 事务 migration：保留 Job、JobOptions、events 和
    artifacts；增加 run/retry/checkpoint 字段、settings 表、claim 索引和 artifact
    唯一约束。
  - ✅ SQLite 连接启用 WAL、`busy_timeout=5000` 和 foreign keys；未来 schema
    版本拒绝启动；migration 失败完整回滚；重复 initialize 幂等。
  - ✅ worker concurrency 在领域配置和 Repository 中只允许 1 或 2。
  - ✅ TDD 红灯已记录：实现前测试因缺少 `lvt.core.jobs` 和
    `UnsupportedSchemaVersionError` 在收集阶段失败；实现后目标测试 178 passed。
  - ✅ Checkpoint 1 全量质量门：`pytest` 363 passed（1 条第三方弃用警告）；
    Ruff lint/format 通过；mypy 28 个源码文件通过。
  - ✅ Checkpoint 1 审查缺口修复：queued 期望集合完全独立枚举；新增
    `ClassifiedError` 并将未知结构化 code 规范化为 `INTERNAL_ERROR`；initialize
    在 `BEGIN IMMEDIATE` 后读取 schema version，并通过双连接并发 migration 测试。
  - ✅ Phase 2 Checkpoint 2 完成：实现最早到期 queued Job 的原子 claim、
    `first_required_stage` 校验、每次唯一 `run_id`、总执行计数和 claimed event。
  - ✅ 所有 worker 状态、进度、错误、metadata、checkpoint pointer、artifact 和完成
    更新均要求 `job_id + run_id + expected_status`；stale run、旧 stage 和较小进度
    均为零写入。
  - ✅ 状态变化与 event 同事务；注入 event 写失败时状态完整回滚。
  - ✅ 自动 requeue 每周期最多 2 次；手工 retry 增加 cycle、重置周期计数并保留总执行数。
  - ✅ queued cancel 直接 cancelled；running cancel 保留 `active_run_id` 进入
    cancelling，worker 使用原 run 收敛到 cancelled 后清空。
  - ✅ artifact Repository 提供 created/idempotent/conflict/stale 数据库语义，未实现
    文件发布或下载。
  - ✅ Checkpoint 2 专项测试 14 passed；全量 `pytest` 380 passed（1 条第三方警告）；
    Ruff lint/format 通过；mypy 28 个源码文件通过。
  - ✅ Checkpoint 2 审查修正：所有新 datetime 写入统一规范化为 UTC；自动 requeue
    返回 REQUEUED/BUDGET_EXHAUSTED_AND_FAILED/STALE，第三次合法失败原子进入 failed。
  - ✅ 普通 complete 已移除；`complete_job_with_artifacts` 在同一事务核对并登记
    source/zh-CN 八文件、更新 completed 和写 event。缺失、重复、跨 Job 冲突、
    stale run 和 event 失败均不会留下部分完成。
  - ✅ retry/cancel、complete/cancel 多连接竞争测试确认每次只有一方成功。
  - ✅ Phase 2 Checkpoint 3 完成：新增七阶段 checkpoint manifest、原子输出发布、
    SHA-256/大小/记录数验证、连续前缀 resolver 和 run-scoped 路径隔离。
  - ✅ checkpoint Pipeline 从 Repository 读取持久化 JobOptions；真实 MLX 调用显式使用
    持久化 `asr_model`；`diarization=false` 生成 skipped checkpoint 且不调用引擎。
  - ✅ 缓存损坏、截断、路径穿越、符号链接、options/engine 版本变化均从正确阶段及
    下游重跑；stale run 不能发布 pointer 或删除当前 run 数据。
  - ✅ checkpoint Pipeline 最终生成、解析并原子登记 8 个 artifact；全部 Segment
    ID、时间戳、Speaker、语言、source_text、metadata、顺序和数量保持一致。
  - ✅ Checkpoint 3 专项测试 16 passed；全量 `pytest` 410 passed（1 条第三方警告）；
    Ruff lint/format 通过；mypy 29 个源码文件通过。
  - ✅ Checkpoint 3 独立审查阻塞已修复：阶段目录 rename 后立即从 manifest 重载
    published path；Strict Fake 会真实打开 downloaded media 和 normalized WAV。
  - ✅ 默认 `asr_model` 持久化为 canonical
    `mlx-community/whisper-small-mlx`；`default` alias 在 JobOptions 边界解析；
    manifest、fingerprint 和 Transcript 记录本次实际模型版本。
  - ✅ `create_real_pipeline` 支持注入 Repository；API 默认和自定义模型均验证传入
    configurable MLX adapter。
  - ✅ checkpoint 路径改为逐组件 `lstat` no-follow；内部 output/manifest/marker
    symlink 被拒绝，stale-run 指向 current-run 的清理尝试失败且 current 数据保持。
  - ✅ 最终 artifact validator 在 export manifest 发布前及 DB 完成前回读
    JSON/SRT/VTT/TXT；Speaker、时间、ID、顺序、source_text 或 cue 时间被修改时，
    Job 保持 exporting，且无 artifact/completed event。
  - ✅ manifest 增加 `media_duration_ms` 和 `transcript_schema_version`；恢复时真实探测
    WAV；downloader 与 normalizer 版本指纹分离，七类 engine 变更均从正确阶段失效。
  - ✅ Checkpoint 3 专项测试 33 passed；全量 `pytest` 433 passed（1 条第三方警告）；
    Ruff lint/format 通过；mypy 30 个源码文件通过。
  - ✅ Phase 2 Checkpoint 4 完成：新增统一 `SubprocessExecutor`，yt-dlp、FFmpeg 和
    ffprobe 全部通过参数数组和独立进程组执行。
  - ✅ 正常、非零、timeout 和 cancellation 路径均完成 wait/reap；超时/取消先 TERM
    整个进程组，宽限期后仍存活则 KILL，再最终 communicate/wait。
  - ✅ stdout/stderr 通过 `communicate(timeout=...)` 持续并行排空；2 MiB 双管道压力
    测试无死锁，父子进程取消后均不再存活。
  - ✅ Pipeline 在每个 stage 和进程内模型调用前后检查取消；MLX/sherpa/Ollama
    无法强杀时，最坏取消延迟为当前调用剩余执行时间。
  - ✅ download/normalize 取消只清理当前 run 的未发布 stage；此前合法 downloaded
    checkpoint 保留并可在同一 run 重试复用。
  - ✅ macOS `/var` 与 `/private/var` 可信 work root 规范化回归通过；no-follow
    symlink 和已发布 checkpoint 保护保持不变。
  - ✅ Checkpoint 4 最终阻塞修复：Popen 后保存 PGID；leader 正常/非零退出并
    communicate/wait 后仍独立探测进程组，closed-pipe child/grandchild 统一执行
    TERM、deadline 轮询、必要时 KILL 和消失确认。
  - ✅ Checkpoint 4 后全量 `pytest` 448 passed（1 条第三方警告）；process-control
    11 passed；checkpoint 集成
    35 passed；Ruff lint/format 通过；mypy 31 个源码文件通过。
  - ✅ Phase 2 Checkpoint 5 完成：FastAPI lifespan 启停本地 worker pool，HTTP
    创建 Job 只持久化 queued 后立即返回，媒体处理只在 `lvt-worker-*` 线程执行。
  - ✅ worker concurrency 仅允许 1/2；真实线程与 Barrier/Event 测试确认活动 Job
    不超过配置，双 worker 竞争同一 Job 仅一个 claim/run 获得执行权。
  - ✅ 固定权重进度、stage 0/100 回调及 overall high-water 已持久化；stale run、
    旧 stage、较小进度和恢复到较早 checkpoint 均不能使进度倒退。
  - ✅ Job 自动重试严格为初次加 2 次；持久化 backoff 为 2 秒、10 秒；第三次失败
    原子进入 failed。仅结构化 auto-requeue 错误重试，消息文本不能开启重试。
  - ✅ shutdown 先停止 claim，有限等待后取消协作式执行并再次有限等待；测试结束
    worker thread 数为 0，第二个 queued Job 未被 claim。
  - ✅ Checkpoint 5 生命周期审查修复：stop 标志、claim 和 token 注册通过统一
    admission barrier 同步；stop 前未进入 admission 的 worker 零 claim，已进入的
    claim 必须先注册 token，shutdown 才能继续。
  - ✅ factory 在 start/lifespan 返回前同步构造全部 Pipeline；首次或第二次构造失败
    均阻止启动且无线程残留。
  - ✅ resolver、Repository peek/claim 等未捕获异常统一记录 fatal 并退出当前线程；
    任一 fatal 或 live worker 少于配置数均使 `/health` 返回 503 unhealthy。
  - ✅ admission 两类竞态各重复 20 次，无漏取消、WorkerShutdownError 或额外 claim；
    fatal 后 stop 可重复调用且线程数归零。
  - ✅ Checkpoint 5 后全量 `pytest` 471 passed（1 条第三方警告）；worker 专项
    23 passed；Ruff lint/format 通过；mypy 34 个源码文件通过。
  - ⏭️ 下一步：等待 Checkpoint 5 独立审查；未实现 Checkpoint 6 启动恢复和完整取消
    编排，也未实现 Checkpoint 7 控制 API。

## 已知阻塞

- **无硬阻塞**。原潜在阻塞（pyannote 需 HF token）已通过 sherpa-onnx 免 token 方案消除；pyannote 降为可选后端。
