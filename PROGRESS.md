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

## 进度日志

- **2026-08-22**
  - ✅ 读取 brief，确认需求（含用户两处修正：翻译写入 `translated_text` 不覆盖 `source_text`；候选引擎需实测后锁定）。
  - ✅ Phase 0：环境检查（芯片/内存/磁盘/网络/依赖）完成，见上表。
  - ✅ 创建项目骨架 `local-video-transcriber/`、`.gitignore`、本文件。
  - 🔄 正在做：创建 smoke venv，准备 ASR / diarization / Ollama 三类最小烟雾测试。
  - ⏭️ 下一步：安装 ffmpeg 静态构建 + 生成测试音频 + 逐一烟雾测试候选引擎，写 ADR-001。

## 已知阻塞

- 暂无硬阻塞。潜在阻塞：pyannote diarization 需要 Hugging Face 免费账号 + 只读 Token 接受模型条款；若缺 Token，先完成不依赖 Token 的架构、接口、Fake Engine 测试与安装流程，仅在真实 diarization 无法继续且替代方案不可行时上报。
