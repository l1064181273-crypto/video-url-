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
| 翻译 | Ollama 0.32.15 + qwen2.5:1.5b (986MB, Apache-2.0) | ✅ 通过 | EN+RU→中文，JSON id-mapping 模式，id 集合一致、全部非空字符串、1.5s；`format=json` + `temperature=0` 输出稳定 |
| Diarization | sherpa-onnx 1.13.6（**token-free**） | ✅ 通过 | 双人 16s 样本，`num_clusters=2`：4 段，spk0/spk1 交替与脚本吻合，distinct={0,1}，0.8s。模型：pyannote-seg-3.0 onnx(MIT,7MB)+titanet-small(40MB)，**无需 HF token** |

**关键发现（影响选型）**：
- `Hy-MT2` 不在 Ollama registry（`registry.ollama.ai/v2/library/hy-mt2` 返回 404）。证实"候选需实测"——翻译基线锁定为 **qwen2.5:1.5b**（Apache-2.0，可再分发，EN/RU→中文表现稳定）。Hy-MT2 若后续能取得 GGUF 可作为可选增强，非 v0.1 依赖。
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
  - ✅ sherpa-onnx 免 token diarization 烟雾测试（双人）通过 —— **消除 HF token 硬阻塞**。
  - ✅ 定稿 `docs/ADR-001-engine-selection.md`，锁定四引擎与版本。
  - **Phase 0 退出条件已满足**：ASR / 音频 / diarization / 翻译 四类引擎均有真实可运行结果。
  - ⏭️ 下一步：进入 Phase 1 后端纵切（项目/配置/DB/API + 单 URL 到 8 文件导出，含 Speaker 与翻译结构不变量）。

## 已知阻塞

- **无硬阻塞**。原潜在阻塞（pyannote 需 HF token）已通过 sherpa-onnx 免 token 方案消除；pyannote 降为可选后端。
