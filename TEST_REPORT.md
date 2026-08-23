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
# 193 项通过，另有 1 条第三方 StarletteDeprecationWarning

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
- URL 内缺失右括号时，扫描器会保留未闭合左括号并继续到下一个 URL 边界；
  这是无法仅凭局部文本消除的歧义边界。

### Phase 1 数字候选链与句末版本 follow-up

- 数字保护改为最大候选扫描：先从首个数字读取完整的点、逗号、斜杠、
  hyphen、en dash、em dash 数字链，再根据整个候选前后的 ASCII 标识符上下文决定
  整体接收或整体拒绝；拒绝后游标跳过完整候选，不从 `08/22` 等中段重新匹配。
- `Year,2026`、`Date,2026-08-22`、`Date,2026/08/22`、
  `Call,010-1234-5678`、`计划,2026-08-22`、`Date.2026-08-22`
  均保护完整数字或数字链。
- `prefix2026/08/22`、`prefix2026–08–22`、
  `prefix2026—08—22`、`prefix1.2.3` 完全不生成 token。
- 版本内部点与句末点分离；`v1.2.3.` 仅保护 `v1.2.3`，混合句 prompt 为
  `Use <placeholder>. now`。更长 `v1.2.3.4` 不截断。
- 支持完整 prerelease/build token：`v1.2.3-beta`、`v1.2.3+build`、
  `v1.2.3-beta+build`；`foo-v1.2.3` 不属于支持前缀并完全不匹配。
- 恢复前会移除 expected placeholder 后再次扫描模型输出；新增 URL、数字、版本或
  其他受保护 token 均失败。因此 `2027/<expected placeholder>` 不能通过校验。

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

数字候选链与句末版本 smoke：

- 历史独立审查 `1/2` 记录继续保留；上一轮右括号/版本复测为 `2/2`。
- 本轮扩展 smoke 连续两次退出 0，成功率 `2/2`：
  - Attempt 1 nonce：`798AD684EDDC8608`；
  - Attempt 2 nonce：`82B9D463AE4AAD6D`。
- 每次分为 5 个真实模型批次；任一批次双失败仍导致脚本非零退出。
- 标点后数字 IDs `[42,43,44,45,46,47]` 的 prompt 均为完整单占位符；
  句末版本 IDs `[48,49,50,51,52]` 保留 token 外标点；
  扩展版本 IDs `[53,54,55,56]` 完整保护 `.4`、prerelease 和 build；
  IDs `[57,58,59]` 为带句末点号的原子版本 passthrough。
- 两次运行的 5 个批次均由 Hy-MT2 完成，未触发 fallback。
- 机器报告：`docs/PHASE-1-NUMERIC-CHAIN-VERSION-OLLAMA-SMOKE.json`。

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

数字候选链与句末版本修复后再次执行相同五样本回归，5 个任务、40 个文件
全部通过。俄语样本显式使用 qwen fallback，其余样本使用 Hy-MT2。最新报告：
`docs/PHASE-1-NUMERIC-CHAIN-VERSION-E2E.json`。

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

## Phase 2 Checkpoint 1：生命周期契约与 schema v3

本轮遵循测试先行。生产代码修改前执行：

```bash
cd backend
../.venv-smoke/bin/python -m pytest \
  tests/unit/test_job_contracts.py \
  tests/unit/test_repository.py \
  tests/unit/test_config.py
# exit 2：测试收集按预期失败
# ModuleNotFoundError: lvt.core.jobs
# ImportError: UnsupportedSchemaVersionError
```

实现后的目标测试：

```bash
cd backend
../.venv-smoke/bin/python -m pytest \
  tests/unit/test_job_contracts.py \
  tests/unit/test_repository.py \
  tests/unit/test_config.py
# 178 passed
```

全量质量门：

```bash
cd backend
../.venv-smoke/bin/python -m pytest
# 363 passed，1 条第三方 StarletteDeprecationWarning

../.venv-smoke/bin/python -m ruff check src tests ../scripts
# All checks passed!

../.venv-smoke/bin/python -m ruff format --check src tests ../scripts
# 45 files already formatted

../.venv-smoke/bin/python -m mypy src/lvt
# Success: no issues found in 28 source files
```

已验证：

- Job 的 12 个持久化状态、全部合法转换和非法转换；`interrupted` 仅是事件类型。
- 所有公开 Job 错误码均有自动重试、手工 retry、缓存恢复点和非空中文建议。
- 结构化错误 adapter 只读取 `error.code`，不根据异常消息字符串推断策略。
- 真实 schema v2 fixture 原位升级到 v3，并保留 Job、JobOptions、events 和 artifacts。
- v2 `attempts` 回填为 `execution_count_total`；新增 retry cycle、run 和 checkpoint 字段。
- migration 中途因重复 artifact 无法创建唯一索引时，DDL、settings 和版本更新全部回滚。
- 重复 initialize 幂等；高于 v3 的未来 schema 拒绝启动且不修改数据库。
- WAL、`busy_timeout=5000`、foreign keys、claim 索引和 artifact 唯一约束生效。
- Settings 和 Repository 均只接受 worker concurrency 1 或 2。

范围限制：

- 本轮只完成 Checkpoint 1，没有实现 claim、worker、Pipeline checkpoint、取消、恢复
  或控制 API。
- 本轮没有改动媒体处理或翻译路径，因此未重新运行真实 Ollama 和五样本媒体 E2E；
  既有 Phase 1 真实报告不冒充本轮验证。
- `attempts` 作为 v2 兼容列暂时保留，只用于 migration 回填；后续逻辑使用
  `execution_count_total`、`retry_cycle` 和 `automatic_requeue_count_in_cycle`。

## Phase 2 Checkpoint 2：Repository CAS

### Checkpoint 1 前置审查修正

- queued 合法目标在测试中逐项独立枚举，不再使用生产 `ACTIVE_JOB_STATUSES` 构造期望值。
- `ClassifiedError` 同时返回规范化 `ErrorCode` 和 `ErrorPolicy`；未知结构化 code
  返回 `INTERNAL_ERROR`，异常消息中的 `DOWNLOAD_FAILED` 等文本不影响分类。
- `initialize()` 先取得 `BEGIN IMMEDIATE` 写事务，再读取 schema version。
- 受控双连接测试在修复前稳定复现 `database is locked`；修复后两个 initialize
  均成功，最终只有一个 v3 schema version 和一条 concurrency setting。
- WAL 首次启用对 `SQLITE_BUSY/SQLITE_LOCKED` 使用 5 秒内有限等待，不解析错误消息。

### 测试先行

Repository CAS 实现前执行：

```bash
cd backend
../.venv-smoke/bin/python -m pytest tests/unit/test_repository_cas.py
# exit 2：ImportError: ArtifactRegistrationResult
```

最终专项与全量质量门：

```bash
cd backend
../.venv-smoke/bin/python -m pytest tests/unit/test_repository_cas.py
# 14 passed

../.venv-smoke/bin/python -m pytest
# 380 passed，1 条第三方 StarletteDeprecationWarning

../.venv-smoke/bin/python -m ruff check src tests ../scripts
# All checks passed!

../.venv-smoke/bin/python -m ruff format --check src tests ../scripts
# 46 files already formatted

../.venv-smoke/bin/python -m mypy src/lvt
# Success: no issues found in 28 source files
```

### 已验证的 Repository 契约

- 两个连接同时竞争同一 queued Job 时只有一个 claim 成功。
- 多 Job 严格按 `next_attempt_at`、`created_at`、`uuid` 排序。
- claim 验证 active `first_required_stage`，生成唯一 `run_id`，增加
  `execution_count_total`，只设置一次 `started_at` 并写 claimed event。
- reclaimed Job 使用新 `run_id`；旧 run 对状态、进度、metadata、错误、artifact
  和 completed 更新全部返回零写入。
- 当前 run 可通过一次 CAS 写入 title、duration、detected_language、work directory
  和 checkpoint pointer。
- `stage_progress` 和 `overall_progress` 单调；旧 stage 或较小值被拒绝。
- 状态和 event 在同一事务；触发器注入 event 失败时状态更新回滚。
- 自动 requeue 恰好允许每周期 2 次；手工 retry 增加 cycle、重置周期自动次数，
  不重置总执行数。
- queued cancel 直接 cancelled；running cancel 保留 run，worker cancelled 后清空。
- artifact 注册明确返回 created、idempotent、conflict 或 stale。
- Repository 的公开写方法拒绝字符串 status、`interrupted` 和字符串 error code。
- 持有 SQLite 写锁时，另一连接在 `busy_timeout` 内等待，释放锁后 CAS 成功。

范围限制：

- 未实现 lifespan worker、Pipeline/checkpoint manifest、yt-dlp/FFmpeg 进程控制、
  启动恢复或 FastAPI 控制路由。
- artifact 本轮只有数据库登记和冲突语义，不发布、移动、删除或下载文件。
- 本轮没有修改媒体或翻译执行路径，因此未运行真实 Ollama 或五样本媒体 E2E。

## Phase 2 Checkpoint 3：Pipeline checkpoint

### Checkpoint 2 前置修正

- 所有 Repository datetime 写入先执行 `astimezone(UTC).isoformat()`；跨 offset 的
  created/next-at、到期边界和三字段排序均有测试。
- automatic requeue 使用三态结果；第三次合法失败在同一事务写最终错误、
  `finished_at`、failed 状态和 event，stale run 仍零写入。
- 普通 `complete_job` 已移除；只允许 `complete_job_with_artifacts` 完成任务。
- 完成事务要求精确八种 source/zh-CN artifact；0/1/7/9 个、重复 kind、跨 Job
  artifact、stale run 或 completed event 失败时均不产生部分完成。
- retry/cancel 和 complete/cancel 使用真实多连接竞争，确认只有一个事务获胜。

前置修正 commit：

```text
93da0fa Fix Phase 2 retry timing and atomic artifact completion
```

### Checkpoint 3 测试先行

```bash
cd backend
../.venv-smoke/bin/python -m pytest tests/integration/test_pipeline_checkpoints.py
# exit 2：ImportError: DownloadedMedia
```

最终专项和质量门：

```bash
cd backend
../.venv-smoke/bin/python -m pytest tests/integration/test_pipeline_checkpoints.py
# 16 passed

../.venv-smoke/bin/python -m pytest
# 410 passed，1 条第三方 StarletteDeprecationWarning

../.venv-smoke/bin/python -m ruff check src tests ../scripts
# All checks passed!

../.venv-smoke/bin/python -m ruff format --check src tests ../scripts
# 49 files already formatted

../.venv-smoke/bin/python -m mypy src/lvt
# Success: no issues found in 29 source files
```

### Manifest schema

每个 manifest 包含：

```text
schema_version
job_id
stage
run_id
created_at
source_url_sha256
job_options
options_fingerprint
engine_names
engine_versions
engine_fingerprint
input_checkpoint_fingerprints
previous_manifest
outputs[].relative_path
outputs[].kind
outputs[].byte_size
outputs[].sha256
outputs[].record_count
manifest_fingerprint
```

### 缓存复用矩阵

| 首个无效阶段 | 复用 | 重跑 |
| --- | --- | --- |
| downloaded_media | 无 | 全部七阶段 |
| normalized_audio | downloaded_media | normalized_audio 及下游 |
| asr_result | 前两阶段 | asr_result 及下游 |
| diarization_result | 前三阶段 | diarization_result 及下游 |
| source_transcript | 前四阶段 | source_transcript 及下游 |
| translated_transcript | 前五阶段 | translated_transcript、export |
| export_manifest | 前六阶段 | 仅 export |
| 全部合法 | 全部七阶段 | 不调用引擎或 exporter，只原子完成 DB |

已验证：

- manifest 截断、hash、size、record_count、路径穿越和符号链接均被拒绝。
- JobOptions、ASR model、diarization、目标语言及 engine version 按受影响阶段失效。
- 每个 run 只写 `work/<job_id>/runs/<run_id>/`；重跑输出与旧 run 路径不同。
- checkpoint pointer 只通过当前 run/status CAS 发布；stale run 只能清理自己的未发布目录。
- `diarization=false` 不调用 diarization engine，并保留单说话人分段语义。
- MLX checkpoint 路径使用持久化 `asr_model`，不读取当前默认值覆盖。
- 八个导出文件完成 TXT/SRT/VTT/JSON 回读；source/zh-CN 的所有 Segment 不变量一致。
- 最终任务完成调用 `complete_job_with_artifacts`，artifact、completed 和 event 原子提交。
- Phase 1 既有同步 Pipeline 接口和测试保持通过。

范围限制：

- 本轮只拆分已有 download/normalize 调用，没有改变 yt-dlp/FFmpeg 子进程启动、
  TERM/KILL/wait 或 timeout；这些属于 Checkpoint 4。
- 未实现 lifespan worker、启动恢复或控制 API。
- 本轮使用确定性 Fake Engine 验证 checkpoint，没有运行真实 Ollama 或五样本媒体 E2E。

## Phase 2 Checkpoint 3 独立审查修复

根因与修复：

- Pipeline 在 stage 目录 rename 后继续使用临时目录中的 `DownloadedMedia.media_path`
  和 `MediaInfo.audio_path`。现改为每阶段 publish 后从 manifest 重载最终路径。
- FFmpeg normalizer 错误要求输入位于当前输出临时目录。现保留输出 containment，
  输入则必须是 Pipeline 已验证、非 symlink、存在的上游 checkpoint 普通文件。
- API 默认 `"default"` 不是 mlx-whisper 模型名。JobOptions 现持久化 canonical
  `mlx-community/whisper-small-mlx`，同时在输入边界解析旧 `default` alias。
- checkpoint 路径此前先 resolve 再检查 symlink，可能丢失链接身份。现对工作根以下
  每个现有组件执行 `lstat`，读取、marker、manifest 和删除均 no-follow。
- 导出此前只验证文件存在和数量。现于 export manifest 发布前及 DB complete 前，
  回读 JSON/SRT/VTT/TXT 并验证全部 Segment 语义。
- manifest 缺少媒体和 transcript schema 信息。现增加 `media_duration_ms` 和
  `transcript_schema_version`，恢复 normalized audio 时真实读取 WAV 时长。
- downloader/normalizer 曾共享组合版本。现分离 yt-dlp 与 FFmpeg fingerprint。

专项证据：

```bash
cd backend
../.venv-smoke/bin/python -m pytest tests/integration/test_pipeline_checkpoints.py
# 33 passed

../.venv-smoke/bin/python -m pytest \
  tests/integration/test_pipeline_checkpoints.py \
  tests/integration/test_api.py \
  tests/unit/test_job_options.py \
  tests/unit/test_pipeline_factory.py \
  tests/unit/test_real_engines.py
# 44 passed，1 条第三方 StarletteDeprecationWarning

../.venv-smoke/bin/python -m pytest
# 433 passed，1 条第三方 StarletteDeprecationWarning

../.venv-smoke/bin/python -m ruff check src tests ../scripts
# All checks passed!

../.venv-smoke/bin/python -m ruff format --check src tests ../scripts
# 52 files already formatted

../.venv-smoke/bin/python -m mypy src/lvt
# Success: no issues found in 30 source files
```

反例结果：

- Strict downloader/ASR 会真实打开上游文件，并确认路径位于 published
  `downloaded_media` / `normalized_audio` 目录。
- API 默认模型和自定义模型均传到 configurable MLX adapter；Transcript 和 manifest
  记录对应实际模型版本。
- 工作根内部 output、manifest、published marker symlink 均使缓存失效。
- stale run 目录指向 current run 的 symlink 时 cleanup 抛错，current 文件不变。
- normalized audio 即使伪造匹配 hash/size，WAV probe 失败仍从 extracting 重跑。
- downloader、normalizer、ASR、diarization、segmenter、translation、exporter
  各自版本变化分别从 downloading 至 exporting 的对应阶段失效。
- 故意修改 Speaker、时间戳、ID、段落顺序、source_text、SRT 时间和 VTT 时间的
  exporter 全部被拒绝；Job 不进入 completed，artifact 表和 completed event 均为空。

范围限制：

- 没有实现进程组、TERM/KILL、lifespan worker、启动恢复或控制 API。
- 未修改 Phase 1 strict-token 文件，也未扩展翻译 token 边界。
- 本轮未运行真实 Ollama 或五样本媒体 E2E；验证聚焦确定性 checkpoint 和 Repository。

## Phase 2 Checkpoint 4：外部进程控制

实现：

- `SubprocessExecutor` 只接受参数序列，使用 `Popen(..., start_new_session=True)` 创建
  独立进程组并立即保存 PGID，不使用 shell。
- 正常和非零 leader 均先通过 `communicate()` 完成 wait/reap，再独立使用
  `killpg(pgid, 0)` 判断进程组是否仍有成员。
- timeout 或 cancellation 先向整个进程组发送 SIGTERM；宽限期后仍未退出则发送
  SIGKILL；最后再次 `communicate()` 回收父进程并排空管道。
- 正常、非零、timeout、cancellation 和 Popen 后内部异常共用同一 group cleanup；
  TERM 和 KILL 后均按 monotonic deadline 轮询 PGID 消失。
- stdout/stderr 同时由 `communicate(timeout=...)` 排空，避免任一管道填满造成死锁。
- yt-dlp 改为 `python -m yt_dlp` 外部命令；yt-dlp、FFmpeg、ffprobe 共用同一执行器。
- Pipeline 在每个 stage 开始、外部调用期间以及 MLX/sherpa/Ollama 等进程内调用返回后
  检查 CancellationToken。
- 进程内模型不能安全强杀；最坏取消延迟明确为当前模型调用的剩余时间。
- 异常或取消调用只清理当前 `job_id/run_id` 的未发布 stage；已发布 checkpoint 保留。
- 可信 work root 启动时 canonicalize，覆盖 macOS `/var` 与 `/private/var` 别名；
  root 以下路径继续逐组件 no-follow。

测试：

```bash
cd backend
../.venv-smoke/bin/python -m pytest tests/unit/test_process_control.py
# 11 passed

../.venv-smoke/bin/python -m pytest tests/integration/test_pipeline_checkpoints.py
# 35 passed

../.venv-smoke/bin/python -m pytest
# 448 passed，1 条第三方 StarletteDeprecationWarning

../.venv-smoke/bin/python -m ruff check src tests ../scripts
# All checks passed!

../.venv-smoke/bin/python -m ruff format --check src tests ../scripts
# 55 files already formatted

../.venv-smoke/bin/python -m mypy src/lvt
# Success: no issues found in 31 source files
```

已验证：

- 正常退出、非零退出、timeout 和显式 cancellation。
- 正常响应 TERM；忽略 TERM 后由 KILL 终止。
- 2 MiB stdout 与 2 MiB stderr 同时输出无死锁。
- 创建子进程的父进程取消后，父子 PID 均不再存活；进程组 leader 先退出而子进程
  仍持有管道时，timeout 仍会清理整个组。
- leader 正常 0 或非零 7 退出、child 三条 stdio 全部 DEVNULL 时，返回或抛错前
  child 均已停止，原始 leader returncode 保持。
- closed-pipe child 忽略 TERM 时升级 SIGKILL；child 再创建 grandchild 时两个 PID
  均在 executor 返回前消失。
- yt-dlp、FFmpeg 和 ffprobe 均通过同一 executor 及参数数组执行。
- download 取消不产生 checkpoint pointer 或 published manifest。
- normalize 取消保留已发布 downloaded checkpoint；同一 run 重试不重新下载。
- stale run 清理不会删除 current run，内部 symlink 清理攻击继续被拒绝。
- macOS `/var` alias 规范化到 `/private/var` 可信根。

范围限制：

- 未实现 lifespan worker、自动重试调度、启动恢复或控制 API。
- 没有更改 yt-dlp/FFmpeg 的业务参数、模型算法或 Phase 1 strict-token。
- 未运行真实网络下载和模型 E2E；外部进程生命周期使用真实本地子进程组验证。

## Phase 2 Checkpoint 5：worker / retry / progress

实现：

- FastAPI lifespan 启动和停止 `JobWorkerPool`；POST 创建任务只写 queued 并通知
  worker，不在请求线程执行 Pipeline。
- worker concurrency 仅允许 1 或 2，并同步持久化到 settings。
- worker 先解析连续 checkpoint，再使用 Repository 原子 claim 获得唯一 run_id。
- Pipeline 从 Repository 读取持久化 JobOptions，并接受阶段 progress callback。
- 固定权重：
  - downloading 15
  - extracting 5
  - transcribing 35
  - diarizing 15
  - segmenting 5
  - translating 20
  - exporting 5
- overall 使用 `floor(base + weight * stage_progress / 100)`，并与当前高水位取最大值。
- Pipeline 对每个实际阶段报告 0 和 100；完整缓存直接恢复到 exporting 时报告 100。
- 进度仍通过 `job_id + run_id + expected_status` CAS；stale、旧 stage 和倒退全部拒绝。
- 自动重试只接受结构化 ErrorCode 且 policy 标记 `auto_requeue` 的错误。
- Job 级 backoff 固定为第一次 2 秒、第二次 10 秒，写入 `next_attempt_at`。
- 第三次执行失败由 Repository 原子进入 failed；工具内部 retry 不增加 Job execution。
- shutdown 先设置 stop，禁止新 claim；等待 graceful deadline 后取消活动 token，再有限
  等待 worker 退出。测试中的协作式 Pipeline 全部退出且无后台线程。

测试：

```bash
cd backend
../.venv-smoke/bin/python -m pytest tests/integration/test_worker.py
# 13 passed，1 条第三方 StarletteDeprecationWarning

../.venv-smoke/bin/python -m pytest
# 461 passed，1 条第三方 StarletteDeprecationWarning

../.venv-smoke/bin/python -m ruff check src tests ../scripts
# All checks passed!

../.venv-smoke/bin/python -m ruff format --check src tests ../scripts
# 59 files already formatted

../.venv-smoke/bin/python -m mypy src/lvt
# Success: no issues found in 34 source files
```

已验证：

- HTTP 响应在线程阻塞的 Pipeline 完成前返回，执行线程名称为 `lvt-worker-*`。
- concurrency=1/2 的活动 run 上限分别为 1/2。
- 双 worker 同时到达 claim barrier 时同一 Job 只执行一次。
- 固定阶段权重和公式逐项验证；真实 checkpoint Pipeline 按阶段报告 0/100。
- 同 stage 进度倒退、旧 stage、stale run 和迟到回调均零写入。
- 从 transcribing 高水位 55 恢复 downloading 后，overall 仍保持 55。
- DOWNLOAD_FAILED 恰好执行三次；2 秒和 10 秒到期前不可 claim，第三次进入 failed。
- MEDIA_INVALID 直接 failed，不自动重试。
- 异常消息仅含 `DOWNLOAD_FAILED` 但无结构化 code 时按 INTERNAL_ERROR 直接失败。
- 模拟工具内部三次尝试时 `execution_count_total` 只增加一次。
- 手工 retry 仍只增加 retry_cycle、重置周期自动次数并保留总执行数。
- shutdown 期间第二个 queued Job 不被 claim；协作取消后 live worker thread 为 0。

范围限制：

- 未实现 Checkpoint 6 的启动恢复、`cancelling → cancelled` 完整编排。
- 未实现 Checkpoint 7 retry/cancel/delete/settings 等控制 API。
- 对不支持取消的 MLX/sherpa/Ollama 原生调用仍无法强杀；若超过 shutdown deadline，
  `WorkerShutdownError` 会明确报告未停止线程，不会谎报干净退出。
- 本轮使用确定性 Fake Pipeline 验证 worker 编排，没有执行真实五样本 API+worker E2E。

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
