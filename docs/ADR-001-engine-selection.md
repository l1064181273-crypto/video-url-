# ADR-001：引擎选型（Phase 0 实测锁定）

- 状态：已接受（v0.1）
- 日期：2026-08-22
- 决策者：执行 Agent（依据 brief 第 4.3 / 15 节要求，实测后锁定）

## 背景

brief 给出的 mlx-whisper / faster-whisper / WhisperX / pyannote / Hy-MT2 均为**优先验证候选**，
不是未经测试就强制使用的固定依赖。本 ADR 记录 Phase 0 在真实环境的最小烟雾测试结果，
并锁定 v0.1 实际采用的引擎与版本。

## 实测环境

| 项 | 值 |
|---|---|
| 机器 | Apple M5（arm64, Apple Silicon） |
| macOS | 26.5.2 (25F84) |
| 内存 | 16 GB |
| 磁盘可用 | ~327 GB |
| Python | 3.11.15（uv 管理的 CPython；本机无 uv 二进制，用标准 venv） |
| Node / npm | v24.10.0 / 10.9.8 |
| Chrome | 151.0.7922.172 |
| FFmpeg | 7.0 / 8.0（static-ffmpeg 提供的 darwin_arm64 二进制） |
| Ollama | 0.32.15（Metal，检测到 M5 iGPU 11.8 GiB） |

## 决策

### 1. ASR：**mlx-whisper**（主）+ faster-whisper（降级预留）

- 版本：`mlx-whisper 0.4.3`，`mlx 0.32.1`。
- 烟雾测试（`mlx-community/whisper-tiny`，仅为最小验证）：
  - 英语 14s：`detected=en`，5 段，时间戳单调 `[0.00→2.88]…[11.68→14.12]`，耗时 6.4s（含首次模型下载）。
  - 俄语：`detected=ru`，4 段，1.8s。
- 结论：mlx-whisper 在 Apple Silicon 上能返回可靠段级时间戳且能自动识别语言，满足 brief 4.3。
- 生产默认模型：`small` / `medium` / `large-v3-turbo`（16GB 上可跑 medium；测试模式用 tiny/small）。
- 降级：若某环境 mlx 不稳定，切 faster-whisper CPU（接口层已抽象，切换不影响上层）。
- 放弃 WhisperX 作为 v0.1 强制项：它主要解决对齐/整合，且在纯 Apple Silicon 上依赖链更重；
  v0.1 用 mlx-whisper 段级时间戳 + 独立 diarization 已足够，避免过度耦合。

### 2. 音频规范化：**static-ffmpeg** 提供的 ffmpeg/ffprobe

- 本机无 Homebrew，evermeet.cx 静态包下载过慢（26MB 118s 超时）。
- 选用 PyPI 的 `static-ffmpeg`（首次调用 `add_paths()` 下载 `ffmpeg 8.0 darwin_arm64`）。
- 规范化目标：单声道 16kHz PCM WAV（mlx-whisper / sherpa-onnx 均要求 16k）。
- 注意：mlx-whisper 通过 subprocess 调用 `ffmpeg`，运行时必须把 ffmpeg 注入 PATH。
- 许可：ffmpeg 采用 LGPL/GPL 构建；打包再分发需在 THIRD_PARTY_NOTICES 声明，或安装时下载（首选后者，避免再分发争议）。

### 3. 说话人区分（Diarization）：**sherpa-onnx（token-free）**，取代 pyannote 作为 v0.1 默认

- 版本：`sherpa-onnx 1.13.6`（`OfflineSpeakerDiarization`）。
- 模型（**均无需 Hugging Face token**）：
  - 分段模型：`sherpa-onnx-pyannote-segmentation-3-0/model.onnx`（MIT，来源 pyannote/segmentation-3.0 的 ONNX 转换）。
  - 说话人嵌入：`nemo_en_titanet_small.onnx`（40MB，来自 sherpa-onnx `speaker-recongition-models` release）。
- 烟雾测试（双人 16s，`num_clusters=2`）：
  - 4 段，`[0.03→4.30] spk0 / [4.30→8.42] spk1 / [8.42→11.84] spk0 / [11.74→16.23] spk1`，
    与测试脚本交替发言（Daniel/Samantha 交替）完全吻合；distinct speakers = {0,1}；0.8s。
- **关键决策**：v0.1 默认 diarization 走 sherpa-onnx，彻底避免 pyannote.audio 的 HF gated token 依赖。
  - 好处：安装向导无需强制用户注册 HF、接受条款、贴 token；模型下载后可离线；无 token 泄漏风险。
  - pyannote.audio 保留为**可选后端**（接口层 `DiarizationEngine` 已抽象），供愿意用 HF token 的用户启用；
    但不是 v0.1 验收依赖。这直接消除 brief 已知阻塞（DIARIZATION_TOKEN_REQUIRED）成为默认路径的风险。
  - 集群数：已知说话人数时用固定 `num_clusters`；未知时用阈值聚类（`cluster.threshold`）。

### 4. 翻译：**Ollama + Hy-MT2-1.8B Q4_K_M**（主）+ qwen2.5:1.5b（fallback）

- Ollama `0.32.15`；主模型为腾讯官方
  `hf.co/tencent/Hy-MT2-1.8B-GGUF:Q4_K_M`（1.79B、Q4_K_M、1.1 GB、**Apache-2.0**）。
- Phase 0.1 纠正：`registry.ollama.ai/v2/library/hy-mt2` 的 404 只表示 Ollama 官方 library
  没有短名称，不表示 Hugging Face GGUF 不可运行。以下命令已真实下载成功：
  `ollama pull hf.co/tencent/Hy-MT2-1.8B-GGUF:Q4_K_M`。
- 直接导入缺陷与修复：
  - Ollama 0.32.15 为该模型自动生成了损坏的 TEMPLATE（出现 `{{ end }}onse }}`），
    导致 JSON 测试稳定返回错误键 `{"name":"Nyonse"}`。
  - 按腾讯官方 `chat_template.jinja` 转写为
    `packaging/ollama/Modelfile.hy-mt2-1.8b-q4km`，复用已下载 GGUF 执行
    `ollama create hy-mt2:1.8b-q4km-fixed -f <Modelfile>` 后恢复正常。
  - 安装流程必须使用该 Modelfile 创建本地模型，不能直接信任当前 Ollama 自动模板。
- Phase 0.1 A/B 方法：
  - EN、RU、斯瓦希里语各组成独立的双 Segment JSON 批次，符合单个视频通常只有一种源语言的实际场景；提示使用完整语言名。
  - **范围说明**：Swahili 不在 Hy-MT2 官方支持表中，本项仅为探索性超范围压力测试，不能用于宣称官方支持。生产代码只映射官方列出的 38 个代码，未知代码明确报错。
  - 每模型每语言运行 3 次，共 9 次；`format=json`、`temperature=0`、固定 seed。
  - 强制验证 id 集合、值类型/非空/含中文、原文未原样返回，以及翻译前后
    `id/start_ms/end_ms/speaker/source_language/source_text` 完全不变，只写 `translated_text`。
  - 完整机器可读结果：`docs/PHASE-0.1-TRANSLATION-AB.json`；可复现脚本：
    `scripts/phase01_translation_ab.py`。
- A/B 实测（Apple M5 / 16 GB）：

| 指标 | Hy-MT2-1.8B Q4_K_M | qwen2.5:1.5b |
|---|---:|---:|
| JSON/结构不变量 | 9/9 通过 | 9/9 通过 |
| 冷首请求 | 1.008s | 1.264s |
| 热请求中位数 | 0.350s | 0.307s |
| 热生成速度 | 99.27 tok/s | 112.62 tok/s |
| Ollama `size_vram`（统一内存） | 1,446,772,735 B（约 1379 MiB） | 1,163,080,498 B（约 1109 MiB） |
| EN/RU 人工抽查 | 准确、自然 | 基本准确 |
| 探索性 Swahili（官方范围外） | 核心语义基本保留，第二段把“故障后”弱化成“完成后” | 两段均出现明显语义幻觉 |

- **决策**：Hy-MT2 在目标场景的小语种质量优势超过约 14% 的速度差和约 270 MiB
  的内存差，锁定为 v0.1 默认翻译引擎。`qwen2.5:1.5b` 保留为已验证 fallback，
  用于 Hy-MT2 安装或运行失败时；不能因官方 library 短名称 404 而取代 Hy-MT2。
- 禁止：OpenAI / DeepL / Google Translate 等在线翻译接口（brief 4.3 / 6）。

## 模型磁盘占用（实测/预估）

| 组件 | 占用 |
|---|---|
| whisper-tiny（测试） | ~75 MB |
| whisper-small（生产建议） | ~500 MB |
| whisper-medium（16GB 可选） | ~1.5 GB |
| diar segmentation (pyannote-3.0 onnx) | ~7 MB |
| diar embedding (titanet-small) | ~40 MB |
| Hy-MT2-1.8B Q4_K_M（默认） | 1.1 GB |
| qwen2.5:1.5b（fallback） | 986 MB |
| ffmpeg 静态二进制 | ~80 MB |

## 引擎接口抽象（避免耦合，brief 第 9 条）

- `ASREngine.transcribe(audio_path) -> {language, segments:[{start_ms,end_ms,text}]}`
- `DiarizationEngine.diarize(audio_path) -> [{start_ms,end_ms,raw_speaker}]`
- `TranslationEngine.translate(batch:{id:source_text}) -> {id:translated_text}`
- 测试注入 `FakeASR/FakeDiarizer/FakeTranslator` 保证确定性；真实 smoke test 单独保留。

## 被放弃/降级方案与原因

| 方案 | 处置 | 原因 |
|---|---|---|
| WhisperX | v0.1 不强制 | 对齐/整合价值在本架构可由独立 diarization 替代，依赖更重 |
| pyannote.audio | 降为可选后端 | 需 HF gated token，安装摩擦大；sherpa-onnx 免 token 已满足双人验收 |
| qwen2.5:1.5b | 已验证 fallback | 更快、更省内存；探索性范围外 Swahili 测试出现明显语义幻觉 |
| evermeet.cx ffmpeg | 放弃 | 下载过慢导致超时；改用 PyPI static-ffmpeg |
| Homebrew 安装依赖 | 不依赖 | 本机无 brew；且 brief 要求不静默 sudo |

## 后续复核触发条件

- 若目标机 mlx 不稳定 → 启用 faster-whisper 分支并在此 ADR 追加。
- 若 Hy-MT2 安装或运行失败 → 自动降级 qwen2.5:1.5b，并明确提示小语种质量风险。
- 升级 Ollama 后重新验证 HF GGUF 自动模板；确认修复前继续使用项目 Modelfile。
- 若用户提供 HF token 且需要 pyannote → 作为可选 diarization 后端启用。
