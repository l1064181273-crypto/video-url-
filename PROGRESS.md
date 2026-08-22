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
  - ✅ Phase 1 质量门：`pytest` 95 passed；Ruff lint/format（含 scripts）通过；mypy 27 个源码文件通过；真实产物验证通过。
  - **Phase 1 退出条件已满足**：三个真实媒体引擎已接入统一 Pipeline，并通过真实 HTTP fixture 生成与验证合规 8 文件。
  - ⏭️ 下一步：等待独立审查；本轮不开始 Phase 2/3/4。

## 已知阻塞

- **无硬阻塞**。原潜在阻塞（pyannote 需 HF token）已通过 sherpa-onnx 免 token 方案消除；pyannote 降为可选后端。
