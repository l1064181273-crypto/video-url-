# 测试报告 — Local Video Transcriber v0.1

## 测试环境

- 日期：2026-08-22
- 机器：Apple M5、arm64、16 GB 内存
- macOS: 26.5.2 (25F84)
- Python: 3.11.15
- Ollama: 0.32.15
- mlx-whisper: 0.4.3
- sherpa-onnx: 1.13.6
- yt-dlp: 2026.8.19
- static-ffmpeg：3.0（FFmpeg darwin-arm64）
- ASR 烟雾测试模型：`mlx-community/whisper-tiny`
- 默认翻译模型：`hy-mt2:1.8b-q4km-fixed`
- 备用翻译模型：`qwen2.5:1.5b`

## 执行命令与结果

### 静态检查与确定性测试

```bash
cd backend
../.venv-smoke/bin/python -m pytest
# 78 项通过，另有 1 条第三方 StarletteDeprecationWarning

../.venv-smoke/bin/python -m ruff check src tests ../scripts
# 所有检查通过

../.venv-smoke/bin/python -m ruff format --check src tests ../scripts
# 43 个文件格式正确

../.venv-smoke/bin/python -m mypy src/lvt
# 通过：27 个源码文件未发现类型问题
```

### Phase 1 非翻译文本 follow-up

修复前，`OllamaTranslationEngine` 对每条模型输出都强制要求至少一个 CJK 字符，
导致纯 URL、时间码、数字、Speaker 标签、NASA 和 GPT-5 等合法原样文本被错误拒绝。

最终实现：

- 完整 URL、时间码、纯数字和 Speaker 标签直接 passthrough，不调用 Ollama。
- 缩写仅使用显式白名单 `NASA`；产品名使用显式白名单 `OpenAI`；
  `GPT-5` 类文本必须同时具备大写、连字符和数字强特征。
- Good Morning、Thank You、This Is Fine、Hello World、STOP、HELLO 以及无法可靠
  判断的 Elon Musk、New York 均进入模型，不使用宽泛 Title Case/全大写规则。
- 混合批次只把需要翻译的 ID 发送给模型，再按原 ID 顺序合并完整结果。
- 普通英文、俄文等正文仍要求模型输出包含简体中文。
- 每次受保护 token 出现均替换为全局唯一 `LVT_TOKEN_XXXX` 占位符。
- 模型结果必须按 Segment 返回完全相同的占位符 ID、数量和顺序，且占位符不能与
  ASCII 字母、数字或下划线粘连；通过后才逐次恢复原 token。
- Ollama 输出限制为 `num_predict=1024`，避免异常生成无限延长单次尝试。
- 主模型和 fallback 都失败时继续返回明确错误，绝不使用原文伪装翻译成功。
- `source_text` 永久不变，只有 `translated_text` 接收 passthrough 或译文。

新增测试覆盖普通 Title Case/全大写反例、白名单规则、重复 NASA/URL、数字边界、
token 删除/增加/修改/重复/错序、混合 batch、完整 ID 合并、Segment 字段不变量、
模型输出上限以及主备模型双失败。

### Fake/Recording Engine 确定性测试

上述分类、只发送部分 ID、占位符增删改错序、边界、合并和双失败反例由 pytest
中的 Fake/Recording Engine 确定性测试覆盖，不作为真实模型成功证据。真实模型与
真实媒体结果分别记录在下面两个独立章节。

### 真实 Ollama passthrough/token smoke

```bash
.venv-smoke/bin/python scripts/run-translation-passthrough-smoke.py \
  --output "$PWD/.tmp-strict-token-smoke/report.json"
# exit 0
```

- 未发送模型的 ID：`[1, 2, 3, 4, 5]`，分别为 URL、数字、NASA、GPT-5、OpenAI。
- 实际发送模型的 ID：`[6, 7, 8, 9, 10]`，包括 Good Morning、STOP、重复 NASA、
  重复 URL 和时间码混合句。
- 重复 NASA、重复 URL、2026、00:03:21 的数量、边界和顺序恢复一致。
- 本次使用 `hy-mt2:1.8b-q4km-fixed`，未触发 fallback。
- 机器报告：`docs/PHASE-1-STRICT-TOKEN-OLLAMA-SMOKE.json`。

### 可重复生成的媒体测试资产

```bash
FFMPEG="$PWD/.venv-smoke/lib/python3.11/site-packages/static_ffmpeg/bin/darwin_arm64/ffmpeg"
bash scripts/make-test-assets.sh "$FFMPEG"
```

测试媒体生成在已被 Git 忽略的 `test-assets/generated/` 中，包括：英语单人视频、
俄语单人视频、两种声音交替发言的视频、包含中文和空格的文件名、静音、纯音，以及
位于不同目录但标题相同的两个视频。

### 本地 HTTP 真实引擎流水线

```bash
python -m http.server 8891 --bind 127.0.0.1 --directory test-assets/generated

.venv-smoke/bin/python scripts/run-real-e2e.py \
  --base-url http://127.0.0.1:8891 \
  --output-root "$PWD/.tmp-real-e2e-v2"

.venv-smoke/bin/python scripts/verify-real-e2e.py \
  .tmp-real-e2e-v2/real-e2e-report.json
# 已验证 5 个样本和 40 个导出文件
```

完成 passthrough 修复后又执行了一次完整真实回归：

```bash
.venv-smoke/bin/python scripts/run-real-e2e.py \
  --base-url http://127.0.0.1:8891 \
  --output-root "$PWD/.tmp-real-e2e-passthrough"

.venv-smoke/bin/python scripts/verify-real-e2e.py \
  .tmp-real-e2e-passthrough/real-e2e-report.json
# 已验证 5 个样本和 40 个导出文件
```

补充机器可读报告：`docs/PHASE-1-PASSTHROUGH-FOLLOWUP.json`。

严格 token 修复后再次运行：

```bash
.venv-smoke/bin/python scripts/run-real-e2e.py \
  --base-url http://127.0.0.1:8891 \
  --output-root "$PWD/.tmp-real-e2e-strict-token-v2"

.venv-smoke/bin/python scripts/verify-real-e2e.py \
  .tmp-real-e2e-strict-token-v2/real-e2e-report.json
# exit 0：已验证 5 个样本和 40 个导出文件
```

最新机器报告：`docs/PHASE-1-STRICT-TOKEN-E2E.json`。

所有任务均使用真实的 `YtDlpFFmpegDownloader`、`MLXWhisperASREngine`、
`SherpaOnnxDiarizationEngine` 和 Ollama 翻译引擎。以下命令没有使用 Fake Engine。

| 样本 | 时长 | 语言 | 句段数 | 说话人数 | 翻译引擎 | 输出目录 |
|---|---:|---|---:|---:|---|---|
| 英语单人 | 14.016 秒 | en | 5 | 1 | Hy-MT2 | `.tmp-real-e2e-v2/exports/English Single--english-e31b` |
| 俄语单人 | 11.328 秒 | ru | 4 | 1 | qwen 降级 | `.tmp-real-e2e-v2/exports/Русский single--russian-ca95` |
| 中文文件名/双人 | 16.256 秒 | en | 7 | 2 | Hy-MT2 | `.tmp-real-e2e-v2/exports/中文 双人 video--two_speakers` |
| 同标题任务 A | 14.016 秒 | en | 5 | 1 | Hy-MT2 | `.tmp-real-e2e-v2/exports/Same Title--same_title_a` |
| 同标题任务 B | 14.016 秒 | en | 5 | 1 | Hy-MT2 | `.tmp-real-e2e-v2/exports/Same Title--same_title_b` |

两个 `Same Title` 任务使用不同目录，没有相互覆盖。

### 实际触发翻译降级

首次真实俄语导出暴露了语义校验缺口：Hy-MT2 返回的 JSON 字符串中包含额外注释、
换行和花括号。最终运行前已为该问题增加回归测试。最终运行过程如下：

1. Hy-MT2 连续三次返回语义无效的包装文本。
2. 达到配置的重试上限后停止重试。
3. qwen2.5:1.5b 成功生成有效译文。
4. `Transcript.warnings` 记录了降级行为和主模型错误。
5. `engine_versions.translation` 记录为 `ollama:qwen2.5:1.5b`。

其他四个本地任务均未触发降级。

严格 token 最新媒体回归中，俄语任务的 Hy-MT2 三次输出均在
`num_predict=1024` 上限内结束，但 JSON 字符串不完整，因此显式降级到 qwen。
qwen 返回有效结构，`warnings` 与 `engine_versions.translation` 均记录实际模型。

### 公开网络视频烟雾测试

来源：Wikimedia Commons 公开 WebM，无需登录：

```text
https://upload.wikimedia.org/wikipedia/commons/0/0d/%22We_should_do_it_ourselves%22_Francis_K%C3%A9r%C3%A9.webm
```

```bash
.venv-smoke/bin/python scripts/run-public-smoke.py \
  'https://upload.wikimedia.org/wikipedia/commons/0/0d/%22We_should_do_it_ourselves%22_Francis_K%C3%A9r%C3%A9.webm' \
  --output-root "$PWD/.tmp-public-smoke"

jq '{samples:[.]}' .tmp-public-smoke/public-smoke-report.json \
  > /tmp/lvt-public-verify.json
.venv-smoke/bin/python scripts/verify-real-e2e.py /tmp/lvt-public-verify.json
# 已验证 1 个样本和 8 个导出文件
```

结果：时长 47.488 秒，检测语言为 `en`，共 6 个句段、1 名说话人，使用 Hy-MT2
主模型，没有降级告警。输出目录：
`.tmp-public-smoke/exports/_We_should_do_it_ourselves__Francis_Kéré--public-0677a`.

## 已验证的产物不变量

对全部 6 个成功的真实媒体任务（共 48 个导出文件）完成以下验证：

- 每个任务恰好生成 `source` 和 `zh-CN` 两组 TXT/SRT/VTT/JSON，且文件非空。
- 原文与中文版本的 cue 数、ID、时间戳、Speaker 和顺序完全一致。
- 两个 JSON 文件均永久保留 `source_text`。
- `source.json` 的 `translated_text` 为空；`zh-CN.json` 只在 `translated_text`
  中保存中文译文。
- SRT 和 VTT 均可成功解析，并保持 cue 时间一致。
- 所有句段时间戳均位于媒体时长范围内。
- Speaker 编号按照首次出现顺序统一映射。

## 已知限制

- 为保证测试可重复且执行较快，Phase 1 真实测试使用 Whisper tiny；俄语原文准确率
  低于生产环境建议的 small/medium 模型。
- 合成视频时长为 11–16 秒，尚未覆盖 v0.1 后续要求的 30–120 秒测试资产和
  20 分钟以上压力测试。
- 本阶段只测试了一个公开网络 URL，远程资源未来可能失效。
- 专有名词识别故意采用保守策略；NASA/OpenAI 使用显式白名单，其他无法可靠识别
  的人名、地名和产品名进入模型处理。新增名称需显式扩展白名单或使用可靠 NER。
- FastAPI `TestClient` 会输出一条来自上游 `httpx` 的弃用警告，不影响测试结果。
- 队列、取消、重启恢复、Chrome 扩展、安装器和打包明确不属于 Phase 1，本报告
  未将这些能力描述为已实现或已通过。
