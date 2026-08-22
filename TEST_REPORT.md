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
# 167 项通过，另有 1 条第三方 StarletteDeprecationWarning

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
- 每次受保护 token 出现均替换为全批唯一占位符。
- 每个 batch 使用随机、无碰撞 nonce，实际格式为 `LVT_<nonce>_TOKEN_XXXX`；
  生成后扫描全部 source_text，发生命名空间碰撞则重新生成。
- 数字边界只考虑 ASCII 标识符字符，因此中文、日文、韩文、西里尔字母相邻数字
  仍被保护，而 abc2026、GPT5、version2 不会被错误拆分。
- URL 使用扫描器提取，保留平衡圆括号、query、fragment、百分号编码和 Unicode，
  同时剥离句末句号、逗号等标点。
- 模型结果必须按 Segment 返回完全相同的占位符 ID、数量和顺序，且占位符不能与
  ASCII 字母、数字或下划线粘连；通过后才逐次恢复原 token。
- 恢复使用一次正则替换回调，不会再次扫描刚恢复的 URL 或原文字面量。
- Ollama 输出限制为 `num_predict=1024`，避免异常生成无限延长单次尝试。
- 主模型和 fallback 都失败时继续返回明确错误，绝不使用原文伪装翻译成功。
- `source_text` 永久不变，只有 `translated_text` 接收 passthrough 或译文。

新增测试覆盖普通 Title Case/全大写反例、白名单规则、Unicode 相邻数字、
ASCII 标识符边界、平衡括号 URL、URL 尾随标点、nonce 碰撞、跨 Segment 唯一性、
单次恢复、重复 NASA/URL、token 删除/增加/修改/重复/错序、混合 batch、完整 ID
合并、Segment 字段不变量、模型输出上限以及主备模型双失败。

### Fake/Recording Engine 确定性测试

上述分类、只发送部分 ID、占位符增删改错序、边界、合并和双失败反例由 pytest
中的 Fake/Recording Engine 确定性测试覆盖，不作为真实模型成功证据。真实模型与
真实媒体结果分别记录在下面两个独立章节。

### Phase 1 最终边界 follow-up

- 若整段恰好包含一个 passthrough token，且其余字符只有空白、中英文句末标点、
  成对圆/方括号或成对引号，则整段原样 passthrough，不调用模型。
- `https://example.com.`、`(https://example.com)`、`NASA.`、`OpenAI!`、
  `GPT-5,`、`2026.`、`Speaker 1:` 均保留每个原始字符。
- Visit URL、NASA launched、Speaker 1 said hello 等包含正文的句子仍进入模型。
- `2026-2027`、`10-20`、`123-456` 及 en dash/em dash 范围作为单一 token；
  与中文、韩文和西里尔文字相邻时仍受保护。
- `GPT-5` 仍按产品 token 处理；abc-2026、version-2、GPT5 不被错误拆分。
- 模型输出阶段统一扫描所有 `LVT_` 保留前缀；只有 manifest 中精确列出的占位符
  才允许出现。其他 nonce、位数错误、额外、重复、修改或错序均失败。
- 原文中的 `LVT_TOKEN_0001` 等字面量先作为普通受保护 token 替换，恢复后允许存在；
  普通单词 `LVT` 和 `TOKEN` 不受该规则影响。

### Phase 1 URL、多段数字和包装顺序 follow-up

- URL 扫描遇到 ASCII 逗号或中文 `，。！？；：` 时立即结束，因此无空格正文
  `,then continue`、`。Continue`、`。继续` 不会被吞入 URL。
- `?`、`#`、`%`、端口冒号、Unicode 路径和平衡圆括号继续视为 URL 内容；
  末尾 ASCII `.,;:!?` 仍按既有规则剥离。
- 歧义策略：裸 ASCII 逗号和中文句读优先解释为正文分隔符。合法 URL 若确实需要这些
  字符，应使用百分号编码；ASCII 句号、分号、感叹号在非末尾位置仍可能是合法 URL
  字符，无空格正文使用这些字符分隔时仍存在歧义。
- hyphen、en dash、em dash 或 `/` 连接的两段及以上纯数字链作为一个 token，例如
  `2026-08-22`、`010-1234-5678`、`2026/08/22`。裸点分数字链沿用数字规则，
  因此 IPv4 和裸数字版本整体保护但不做语义合法性校验。
- 原子包装改为顺序栈校验：前缀只允许压入开放括号/引号，后缀必须按逆序闭合。
  `([“NASA”])` 合法；`([NASA)]`、`”NASA“`、缺失或反向包装进入模型。

### Phase 1 右括号 URL 与前缀版本 follow-up

- URL 扫描器分别维护半角 `()` 与全角 `（）` 的预期闭合栈。URL 内左括号压栈，
  匹配右括号计入 URL 并出栈；无对应左括号或类型不匹配的右括号在其前终止 URL。
- Wikipedia `Foo_(bar)`、带 query/fragment 的 `a_(b)?x=2026#part` 以及全角
  URL 内括号保持完整；外层 `(...)`、`（...）` 继续由原子包装规则处理。
- `Visit (https://example.com)Continue`、`Visit https://example.com)Continue`、
  `访问（https://example.com）继续`、`https://example.com）继续` 均进入模型；
  prompt 中 URL 本体被占位符替换，`)Continue` 或 `）继续` 保留为可翻译正文。
- 版本号策略改为完整保护：`v1.2.3`、`version1.2.3`、
  `release-v1.2.3` 均为单一 token；纯版本整段 passthrough，混合正文中的版本由
  placeholder 保护。普通数字模式禁止从 `.` 或 `,` 后开始，不能再只保护尾部 `2.3`。
- `prefix1.2.3` 不匹配受支持版本前缀，且不会生成部分 token；这是有意的保守策略。

### 真实 Ollama passthrough/token smoke

```bash
.venv-smoke/bin/python scripts/run-translation-passthrough-smoke.py \
  --output "$PWD/.tmp-strict-token-smoke/report.json"
# exit 0
```

- 本次 nonce：`AAD66A1ECF24DF6D`。
- 未发送模型的 ID：`[1, 2, 3, 4, 5, 13]`，包括 URL、数字、NASA、GPT-5、
  OpenAI 和 Wikipedia 括号 URL。
- 实际发送模型的 ID：`[6, 7, 8, 9, 10, 11, 12, 14, 15]`。
- 实际模型批次包含 Good Morning、STOP、重复 NASA、重复 URL、时间码、
  `发布于2026年`、`В2026году`、原文字面量 `LVT_TOKEN_0001`，以及包含
  `LVT_TOKEN_0002` 的 URL。
- 重复 NASA、重复 URL、Unicode 相邻数字、时间码和旧占位符字面量均恢复一致。
- 本次使用 `hy-mt2:1.8b-q4km-fixed`，未触发 fallback。
- 最新机器报告：`docs/PHASE-1-UNICODE-NONCE-OLLAMA-SMOKE.json`。

最终边界 smoke：

- nonce：`2B15629AF58F1A95`。
- 带标点原子 passthrough IDs：`[16, 17, 18, 19, 20]`。
- 纯数字范围 passthrough ID：`[21]`。
- 新增混合正文模型 IDs：`[22, 23, 24]`。
- 总 passthrough IDs：`[1,2,3,4,5,13,16,17,18,19,20,21]`。
- 总模型 IDs：`[6,7,8,9,10,11,12,14,15,22,23,24]`。
- Hy-MT2 成功，未触发 fallback。
- 报告：`docs/PHASE-1-FINAL-BOUNDARY-OLLAMA-SMOKE.json`。

URL、多段数字和包装顺序 smoke：

- 首次把全部 31 段放入单个模型批次时安全失败：Hy-MT2 重排占位符，qwen 返回
  截断 JSON，最终抛出 `TRANSLATION_ALL_MODELS_FAILED`，没有放宽校验或返回原文。
- 最终脚本按真实字幕规模拆成两个模型批次，nonce 均为
  `FBD1EC4DBE32FF87`；两个批次都由 Hy-MT2 成功完成，未 fallback。
- URL 无空格正文 IDs `[25,26,27,28]` 实际发送模型；报告中的模型输入只包含 URL
  占位符，同时保留 `,then continue`、`。Continue`、`。继续` 正文。
- 多段数字 IDs `[29,30,31]` 实际发送模型；`2026-08-22` 和
  `010-1234-5678` 均作为单一占位符并完整恢复。
- 报告：`docs/PHASE-1-URL-MULTIPART-WRAPPING-OLLAMA-SMOKE.json`。

右括号 URL 与前缀版本 smoke：

- 独立审查提供的历史记录：2 次尝试，第一次
  `TRANSLATION_ALL_MODELS_FAILED`，第二次成功，成功率 `1/2`；审查方未提供两次
  nonce 和双模型内部错误，因此报告明确标记为未知，未补造细节。
- 本轮扩展后连续运行 2 次，均退出 0，成功率 `2/2`：
  - Attempt 1 nonce：`A8CCB0A5658DB1AF`；
  - Attempt 2 nonce：`904921B8DE99F31E`。
- 每次均分为 3 个真实模型批次；依据是把回归集合控制在接近真实字幕任务的批量规模。
  任一批次主备模型双失败仍直接非零退出，不跨批吞错或返回原文。
- IDs `[32,33,34,35]` 的 prompt 仅含 URL 占位符，并保留
  `)Continue`/`）继续`；IDs `[36,37,38]` 完整保护三种前缀版本；
  IDs `[39,40,41]` 为原子版本 passthrough。
- 两次成功均由 Hy-MT2 完成 3 个批次，未触发 fallback。
- 机器报告：`docs/PHASE-1-PAREN-VERSION-OLLAMA-SMOKE.json`。

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

Unicode 数字、随机 nonce 与完整 URL 修复后又执行相同五样本回归，结果仍为
5 个任务、40 个文件全部通过。最新报告：
`docs/PHASE-1-UNICODE-NONCE-E2E.json`。

最终边界修复后再次执行相同五样本回归，5 个任务、40 个文件全部通过。
最新报告：`docs/PHASE-1-FINAL-BOUNDARY-E2E.json`。

URL、多段数字和包装顺序修复后再次执行相同五样本回归，5 个任务、40 个文件
全部通过。俄语样本显式使用 qwen fallback，其余样本使用 Hy-MT2。最新报告：
`docs/PHASE-1-URL-MULTIPART-WRAPPING-E2E.json`。

右括号 URL 与前缀版本修复后再次执行相同五样本回归，5 个任务、40 个文件
全部通过。俄语样本显式使用 qwen fallback，其余样本使用 Hy-MT2。最新报告：
`docs/PHASE-1-PAREN-VERSION-E2E.json`。

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
