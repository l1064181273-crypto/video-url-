# Local Video Transcriber v0.1 Phase 3 实施计划

## 1. 文档状态

- 项目：`/Users/bytedance/Desktop/Batydance/Public/YYY/local-video-transcriber`
- 原始规格：`/Users/bytedance/Desktop/Batydance/Public/YYY/AGENT_PROJECT_BRIEF_LOCAL_VIDEO_TRANSCRIBER.md`
- Phase 1 strict-token 冻结基线：`872c42d614a77056c1e9510425955a53ffe40361`
- Phase 2 冻结基线：`07bedfc5ddecfa68b3f053c7d80cf3fb40889ea6`
- 本文档性质：Phase 3 实施前的约束性计划，不包含生产代码实现。
- Phase 3 目标：交付 Manifest V3 Chrome Side Panel，使用户在已安装并启动后端的前提下，
  无需终端即可完成连接、批量提交、任务跟踪、控制、事件查看、结果预览和 artifact 下载。

以下模块在 Phase 3 默认只读，不允许借 UI 工作顺便重构：

```text
backend/src/lvt/engines/translation.py
backend/src/lvt/engines/ollama.py
backend/src/lvt/workers/
backend/src/lvt/db/
backend/src/lvt/pipeline/
backend/src/lvt/core/processes.py
backend/src/lvt/api/app.py
backend/src/lvt/api/control.py
```

只有第 5.1 节记录的 capabilities 契约阻塞经确定性复现和独立审查确认后，才允许单独提交
最小、向后兼容的只读诊断改动。不得把其他后端重构混入 Phase 3。

## 2. 范围

### 2.1 Phase 3 内

- Manifest V3 扩展及最小构建、类型检查和测试工具链。
- Chrome Side Panel 主界面；浏览器 action 只负责打开 Side Panel。
- 本地后端连接设置和手工配对 Token。
- 一次 1–100 条 URL 的输入、客户端预检查和批量提交结果。
- 任务列表、状态筛选、真实阶段进度和详情视图。
- 固定 HTTP 轮询；可见时 1 秒，隐藏时降低频率。
- retry、cancel、confirmed delete。
- Job 事件时间线与分页加载。
- source/zh-CN JSON 结果预览。
- 8 类 artifact 的鉴权下载。
- worker concurrency 1/2 设置。
- 中文状态、错误和恢复建议。
- Chrome 中的确定性 E2E、可访问性和安全边界验证。

### 2.2 明确不做

- WebSocket、SSE、推送服务或 service worker 常驻保活。
- Chrome popup 承担主界面。
- React 或其他大型 UI 框架；当前范围使用原生 TypeScript、HTML 和 CSS。
- 自动配对或新增 `/pair`；v0.1 使用规格允许的手工粘贴长期 Token。
- Chrome Web Store、安装器、签名、公证、ZIP 或 Phase 4 脚本。
- Native Messaging、远程 JavaScript、CDN、遥测或云同步。
- 后端状态机、Repository、checkpoint、worker、进程控制和 artifact 语义调整。
- ASR、diarization、翻译模型重新选型。

## 3. 已确认的后端 HTTP 契约

扩展只消费 Phase 2 已冻结接口：

| 接口 | Phase 3 用途 | 关键行为 |
|---|---|---|
| `GET /health` | 后端和 worker 健康 | 无 Token；200 healthy，503 unhealthy |
| `GET /api/v1/capabilities` | 依赖诊断 | 要求 Token；当前生产返回空对象，见 5.1 |
| `POST /api/v1/jobs` | 批量提交 | 每条 URL 独立进入 accepted/rejected |
| `GET /api/v1/jobs` | 列表轮询 | 返回全部 Job，按创建时间倒序 |
| `GET /api/v1/jobs/{id}` | 动作后确认、详情刷新 | 404 表示 Job 已不存在 |
| `POST /api/v1/jobs/{id}/retry` | failed/cancelled 重试 | queued 幂等；冲突 409 |
| `POST /api/v1/jobs/{id}/cancel` | queued/active 取消 | cancelling/cancelled 幂等；冲突 409 |
| `DELETE /api/v1/jobs/{id}?confirm=true` | 终态删除 | 成功 204；未确认或状态冲突 409 |
| `GET /api/v1/jobs/{id}/events` | 时间线 | `offset >= 0`，`1 <= limit <= 100` |
| `GET /api/v1/jobs/{id}/artifacts` | 完成产物列表 | 非 completed 为 409 |
| `GET /api/v1/artifacts/{id}/download` | 鉴权下载 | 必须发送 `X-LVT-Token`，失败统一 404 |
| `GET /api/v1/settings` | 当前并发 | 仅公开 worker concurrency |
| `PATCH /api/v1/settings` | 修改并发 | 只接受严格整数 1/2 |

前端不得假设未出现在该表中的字段或路由。错误响应统一经适配层读取：

```text
HTTPException: detail.error_code + detail.message
批量 rejected: error_code + message
网络失败/非 JSON: 前端生成本地错误分类，不伪造后端 error_code
```

Job DTO 只消费公开字段，包括 `uuid`、`sanitized_display_url`、`title`、`status`、
`stage_progress`、`overall_progress`、`detected_language`、`duration_ms`、
`error_code`、`error_message`、时间字段、重试计数和 `options`。禁止寻找或展示
`work_dir`、`checkpoint_pointer`、artifact path 或完整带 query 的原始 URL。

## 4. 技术方案

### 4.1 工具链

- TypeScript strict mode。
- Vite 多入口构建：Side Panel 页面和 MV3 service worker。
- Vitest + jsdom：纯函数、store、API client、轮询和 DOM 行为。
- Playwright Chromium persistent context：加载 unpacked extension，验证真实
  `chrome-extension://` 页面、host permission、storage 和下载。
- ESLint + Prettier：扩展源码和测试；不接管 Python 文件。
- 不使用 React。当前 UI 状态规模有限，原生模块可减少依赖、bundle 和供应链面积。

建议目录：

```text
extension/
  package.json
  package-lock.json
  tsconfig.json
  vite.config.ts
  eslint.config.js
  public/
    manifest.json
  src/
    background.ts
    sidepanel.html
    sidepanel.ts
    styles.css
    api/client.ts
    api/contracts.ts
    api/errors.ts
    state/store.ts
    state/poller.ts
    ui/job-list.ts
    ui/job-detail.ts
    ui/timeline.ts
    ui/artifacts.ts
    ui/settings.ts
    ui/status.ts
    storage/settings.ts
  tests/
    unit/
    integration/
    e2e/
```

### 4.2 Manifest V3

最低 manifest 设计：

```json
{
  "manifest_version": 3,
  "name": "Local Video Transcriber",
  "version": "0.1.0",
  "minimum_chrome_version": "114",
  "permissions": ["storage", "downloads", "sidePanel"],
  "host_permissions": ["http://127.0.0.1/*"],
  "background": {"service_worker": "background.js", "type": "module"},
  "side_panel": {"default_path": "sidepanel.html"},
  "action": {"default_title": "打开 Local Video Transcriber"},
  "content_security_policy": {
    "extension_pages": "script-src 'self'; object-src 'self'"
  }
}
```

- 不申请 `<all_urls>`、tabs、scripting、nativeMessaging 或 clipboard 权限。
- 不声明 content script。
- service worker 在安装/启动时调用
  `chrome.storage.local.setAccessLevel({accessLevel: "TRUSTED_CONTEXTS"})`。
- action 点击通过 `chrome.sidePanel.setPanelBehavior` 打开 Side Panel。
- 所有脚本随扩展打包；CSP 不允许远程脚本或 `unsafe-eval`。

### 4.3 本地连接和 Token

扩展不保存任意 base URL，只保存：

```ts
type ConnectionSettings = {
  port: number; // 1..65535
  tokenConfigured: boolean;
};
```

运行时固定构造 `http://127.0.0.1:${port}`：

- 禁止 `localhost`、`0.0.0.0`、LAN IP、域名、IPv6、HTTPS 降级和用户自定义 path。
- Token 只存 `chrome.storage.local`，禁止 `storage.sync`。
- Token 只进入 `X-LVT-Token` 请求头，不进入 URL、query、下载 URL、DOM 文本、
  console、错误对象持久化或分析日志。
- 设置页不回填 Token 明文；只显示“已保存”，替换输入默认空，提供显式清除操作。
- Token 输入使用 password 控件和短时显示按钮；离开设置页立即恢复遮罩。
- 清除 Token 后中止请求和轮询，连接状态回到“需要配对”。
- 不实现自动 `/pair`；用户从本地安装流程获得 Token 后手工粘贴。

### 4.4 API client

单一 typed client 负责：

- 固定 host 构造和端口校验。
- 每次请求从受信 storage 读取 Token，并设置 `X-LVT-Token`。
- JSON content type、204、二进制 artifact 和非 JSON 错误分别处理。
- 使用 `AbortController` 支持视图销毁、设置切换和新轮询覆盖旧轮询。
- 统一错误类型：
  `notConfigured`、`unreachable`、`unauthorized`、`backendUnhealthy`、
  `validation`、`conflict`、`notFound`、`server`、`invalidResponse`。
- 不自动重放 POST/PATCH/DELETE。网络结果未知时先重新 GET Job，再由用户决定。
- 列表和详情响应做运行时最小结构校验；畸形响应显示“后端响应格式异常”。

### 4.5 状态与轮询

Side Panel 内维护一个单向 store：

```text
connection → settings → jobsById/order/filter → selectedJob
           → eventsByJob → artifactsByJob → preview → pendingActions
```

轮询规则：

1. Token 和端口有效后立即请求 `/health`，再请求 settings、capabilities 和 jobs。
2. 页面可见时 jobs 每 1 秒轮询；隐藏时每 5 秒。
3. 同一资源最多一个 in-flight 请求；下一 tick 不与前一请求重叠。
4. 每轮带 generation ID；旧连接配置或旧选择返回的数据直接丢弃。
5. 失败退避为 1、2、5、10 秒，上限 10 秒；用户“重新连接”立即触发。
6. action 成功后立即刷新对应 Job 和列表，不等待下一轮。
7. selected Job 为 active 时轮询详情；事件只在状态/updated_at 变化或用户刷新时获取。
8. Side Panel `visibilitychange` 只调整频率，不创建多个 timer。
9. 关闭 Side Panel 停止前端 timer，不影响后端任务；重新打开从 API 重建状态。
10. progress 完全使用后端数值，不插值、不模拟、不在 retry/recovery 时自行倒退。

测试使用 fake timers、deferred Promise 和 AbortSignal，不以随机 sleep 证明无重叠、
旧响应丢弃或可见性切换。

## 5. UI 与交互

### 5.1 连接与依赖诊断

顶部固定显示：

- 后端：未配置、连接中、正常、未授权、不可达、服务异常。
- Worker：configured/live/fatal 的健康摘要。
- FFmpeg、Ollama、ASR、diarization、主翻译模型和 fallback 模型。

当前存在一个真实 API 阻塞：

```text
backend/src/lvt/main.py 调用 create_app() 时未传 capabilities；
backend/src/lvt/api/app.py 的 GET /api/v1/capabilities 返回 capabilities or {}；
因此生产响应恒为 {}，前端无法区分依赖“可用”“缺失”或“未检查”。
```

Phase 3 不得用静态“正常”、本地文件猜测或任务历史冒充实时依赖状态。Checkpoint 1
首先用真实 Uvicorn 固化失败契约。若独立审查确认原始规格仍要求逐项状态，则允许另开
一个仅后端的前置 commit，对现有只读 `/api/v1/capabilities` 增加有界、去敏响应：

```json
{
  "ffmpeg": {"status": "available", "version": "7.0"},
  "ollama": {"status": "available", "version": "0.32.15"},
  "asr": {"status": "available", "model": "mlx-community/whisper-small-mlx"},
  "diarization": {"status": "available", "engine": "sherpa-onnx"},
  "translation": {
    "status": "available",
    "primary_model": "hy-mt2:1.8b-q4km-fixed",
    "fallback_model": "qwen2.5:1.5b"
  }
}
```

约束：不得返回绝对路径、Token、模型缓存位置或完整异常；不得改变其他路由；检查必须
有超时且不能触发模型下载。若不批准该最小契约，UI 必须诚实显示“后端未提供诊断信息”，
Phase 3 的“逐依赖连接状态”验收项保持未通过。

Chrome extension 对 127.0.0.1 的跨域访问优先依赖 MV3 `host_permissions`。只有真实
unpacked extension 测试证明仍被浏览器 CORS 拒绝，才提交后端 CORS 证据；不得预先扩大
allow-origin，更不得使用 `*`。

### 5.2 批量输入

- 多行 textarea，一行一条，支持粘贴 1–100 条。
- trim 空白并保留原顺序；空行忽略。
- 客户端仅做语法预检：HTTP/HTTPS、非空、总数不超过 100；后端仍是权威验证者。
- 实时显示“有效 N / 无效 M”，无效行保留行号和原因。
- 提交时发送全部客户端合法 URL；展示后端 accepted/rejected，不因单条失败丢弃成功项。
- 提交中的按钮有 busy 状态并防重复；成功后只清除 accepted 行，保留 rejected 行供修改。
- JobOptions：ASR 模型、目标语言固定 zh-CN、diarization toggle。默认值与后端一致。

### 5.3 任务列表

- 顶部 tabs：全部、处理中、已完成、失败。
- “处理中”包含 queued、七个 active stage 和 cancelling；“失败”包含 failed/cancelled。
- 每行显示标题；标题为空时显示 sanitized URL。
- 显示中文状态、overall progress、stage progress、语言、媒体时长、开始/完成时间。
- queued 显示等待中而非 0% 假进度；terminal 状态保持最终持久化进度。
- 长标题省略并提供原文 tooltip；URL 使用可断行文本，不允许撑破 Side Panel。
- 列表 keyed by UUID；轮询更新不重排同一响应之外的数据，不因进度变化闪烁。
- 空状态区分：尚未提交、筛选无结果、后端不可达。

状态文案固定：

| 状态 | 中文 |
|---|---|
| queued | 等待中 |
| downloading | 正在下载 |
| extracting | 正在提取音频 |
| transcribing | 正在转写 |
| diarizing | 正在识别说话人 |
| segmenting | 正在整理句段 |
| translating | 正在翻译 |
| exporting | 正在导出 |
| completed | 已完成 |
| failed | 失败 |
| cancelling | 正在取消 |
| cancelled | 已取消 |

### 5.4 控制动作

- cancel：仅 queued/active 可用；点击后立即进入按钮 busy，响应后以后端状态为准。
- retry：仅 failed/cancelled 可用；queued 的重复响应按幂等成功处理。
- delete：仅 terminal 可用；必须打开 modal，显示任务标题并要求再次确认。
- 所有动作按 Job ID 独立串行；一个 Job busy 不阻塞其他 Job。
- 409 后立即刷新 Job，并显示“任务状态已变化”；不得乐观覆盖后端状态。
- 请求中断或网络结果未知时不自动重发写请求。

### 5.5 详情、事件和预览

详情使用任务列表旁的可返回视图，不嵌套装饰性卡片：

- 摘要：状态、两个进度、语言、时长、JobOptions、执行次数和 retry cycle。
- 时间线：调用 events API，按 ID 顺序显示；首批 50 条，“加载更早/更多”分页。
- message 若为 JSON，只提取允许展示的 `from_status`、`resume_stage`、error code、
  retry reason；不直接渲染未知原始对象或 HTML。
- completed 后请求 artifact 列表，严格按 source/zh-CN 与 TXT/SRT/VTT/JSON 分组。
- 预览只下载 `source.json` 和 `zh-CN.json`，解析后以原文/中文 tabs 展示 Segment。
- 预览转义所有文本；不使用 `innerHTML` 渲染后端内容。
- 大结果预览设上限，例如 2,000 Segment；超限仍允许文件下载并显示说明。

### 5.6 Artifact 下载

不能把 `download_url` 直接放入普通 `<a href>`，因为接口要求 Header Token。

流程：

1. typed client 使用 `X-LVT-Token` fetch artifact。
2. 检查 HTTP 状态、Content-Type 和非空 body。
3. 在 Side Panel 创建 Blob URL。
4. 调用 `chrome.downloads.download`，文件名只取 API artifact kind，并加安全任务标题目录。
5. download API 返回后及时 revoke Blob URL。
6. 401、404、断线分别显示中文错误；404 不猜测内部路径。

下载 URL、日志和错误中不得出现 Token。文件名再次执行客户端安全字符过滤，但不修改
后端 artifact kind。

### 5.7 设置

- 后端端口：整数 1–65535；host 固定且不可编辑为 `127.0.0.1`。
- 配对 Token：保存、替换、清除；不回显。
- ASR 模型：用于新提交 JobOptions，不修改历史 Job。
- Diarization：用于新提交 JobOptions。
- Worker concurrency：读取/写入后端 settings，只允许 segmented control 1/2。
- 输出目录：仅说明由本地服务管理；API 未公开路径，UI 不显示或编辑绝对目录。
- 设置分为“扩展本地设置”和“后端运行设置”，避免误导用户认为全部由一个请求保存。

## 6. 中文错误策略

优先显示后端安全 message，并用 error code 补充稳定操作建议。至少覆盖：

| 类别 | error code | UI 建议 |
|---|---|---|
| 输入 | INVALID_URL | 检查 HTTP/HTTPS 地址后重新提交 |
| 下载 | DOWNLOAD_UNSUPPORTED / DOWNLOAD_FAILED | 更换公开地址，或检查网络后重试 |
| 媒体 | FFMPEG_NOT_FOUND / MEDIA_INVALID | 检查本地服务依赖或更换媒体 |
| ASR | ASR_MODEL_MISSING / TRANSCRIPTION_FAILED | 检查模型安装、磁盘和内存 |
| 说话人 | DIARIZATION_MODEL_MISSING / DIARIZATION_FAILED | 检查 diarization 模型 |
| 翻译 | OLLAMA_UNAVAILABLE / TRANSLATION_MODEL_MISSING | 启动 Ollama 或安装模型 |
| 翻译结构 | TRANSLATION_INVALID_RESPONSE / TRANSLATION_ALL_MODELS_FAILED | 检查模型后手工重试 |
| 导出 | EXPORT_FAILED / DISK_SPACE_LOW | 检查磁盘空间和本地目录权限 |
| 控制 | CANCELLED_BY_USER | 可手工重新加入队列 |
| 未知 | INTERNAL_ERROR | 查看本地日志后重试 |

网络层中文文案必须说明“发生了什么”和“下一步”：

- 未配置：请先设置本地端口和配对 Token。
- 连接拒绝：本地服务未启动，请先启动 Local Video Transcriber。
- 401：配对 Token 无效，请重新输入。
- 503 health：服务已连接，但 worker 当前异常；不要继续提交新任务。
- 409：任务状态已变化，列表已刷新。
- invalidResponse：后端响应格式异常，请确认前后端版本一致。

## 7. Checkpoint 拆分

### Checkpoint 1：contract gate 与 MV3 骨架

范围：

- 固化 TypeScript DTO、状态和错误码。
- 建立 Vite/Vitest/ESLint/Prettier。
- 生成最小 MV3 manifest、service worker 和 Side Panel 空壳。
- 用真实 Uvicorn 验证 host permission、Token header、health/settings/jobs。
- 固化 capabilities `{}` 阻塞证据并完成第 5.1 节决策。

确定性测试：

- manifest JSON schema/权限白名单测试。
- build 不包含远程 URL、inline script 或 eval。
- unpacked extension 从 `chrome-extension://` 成功请求 127.0.0.1。
- 错误 Token 得到 401；Token 不出现在 request URL。
- capabilities 真实响应契约测试。

验收：

- Chrome action 可打开 Side Panel。
- 只有 storage/downloads/sidePanel 和 127.0.0.1 host permission。
- capabilities 阻塞已解决或明确保持未通过，不允许伪造绿色状态。

建议 commit：

```text
Phase 3: establish extension shell and API contracts
```

### Checkpoint 2：安全连接与轮询基础

范围：

- storage adapter、端口和 Token 设置。
- typed API client、错误归一化和连接状态。
- visibility-aware poller、AbortController、退避和 generation 隔离。

确定性测试：

- fake timers 验证 visible 1 秒、hidden 5 秒、失败退避上限 10 秒。
- deferred Promise 证明无重叠请求和旧 generation 响应不覆盖新连接。
- 401、503、断线、非 JSON、204 和二进制响应分类。
- storage access level、Token 清除和不使用 sync。

验收：

- 重新打开 Side Panel 后恢复端口与“Token 已保存”状态，但不回显 Token。
- 后端状态变化能自动收敛；断线不清空最后一次任务快照。

建议 commit：

```text
Phase 3: add secure local connection and polling
```

### Checkpoint 3：批量提交与任务列表

范围：

- 多行 URL 输入、计数和后端 accepted/rejected。
- JobOptions。
- 列表、筛选、中文状态、真实双进度和耗时。

确定性测试：

- 1、100、101 条边界；空行、空白、混合合法/非法 URL。
- accepted 行清除、rejected 行保留。
- 全部状态的筛选矩阵和中文文案。
- progress 只使用后端值，轮询重排不破坏 UUID keyed row。
- 长 URL、长标题、中文和俄文布局快照。

验收：

- 一次提交 1–100 条；单条失败不影响其他任务。
- 关闭再打开 Side Panel 后从后端恢复任务。

建议 commit：

```text
Phase 3: add batch submission and job monitoring
```

### Checkpoint 4：任务控制与事件时间线

范围：

- retry、cancel、confirmed delete。
- 详情摘要、事件分页和状态变化后的即时刷新。

确定性测试：

- 每种 Job 状态的动作可用性矩阵。
- 双击只发送一次写请求。
- 409/404/503 和网络结果未知处理。
- delete modal 的取消、确认和键盘焦点约束。
- 事件稳定排序、分页去重和结构化 message 白名单。

验收：

- queued/active cancel、failed/cancelled retry、terminal delete 与后端契约一致。
- 动作失败不进行虚假本地状态转换。

建议 commit：

```text
Phase 3: add job controls and event timeline
```

### Checkpoint 5：预览、artifact 下载与设置

范围：

- source/zh-CN JSON 预览。
- 8 artifact 分组和鉴权下载。
- ASR/diarization 新任务选项及 worker concurrency 设置。
- 依赖诊断展示和完整中文错误建议。

确定性测试：

- artifact 列表必须恰好按 kind 呈现，不信任服务器文件路径。
- fetch 使用 Header Token，普通 href 和 URL 不包含 Token。
- Blob URL 在成功、失败和取消后均 revoke。
- source/zh-CN tab 保留 Segment 顺序、Speaker 和时间。
- concurrency 只允许 1/2；后端失败时回滚控件显示。
- Token 输入从 DOM 清空且日志无秘密。

验收：

- completed Job 可预览原文/中文，并下载全部 8 个文件。
- settings 刷新后与后端持久化 concurrency 一致。
- 所有错误均有中文下一步，不展示内部路径或异常。

建议 commit：

```text
Phase 3: add artifact workflows and settings
```

### Checkpoint 6：Chrome 集成与 Phase 3 验收

范围：

- 真实 unpacked extension + 真实 Phase 2 API 的浏览器 E2E。
- 键盘、焦点、对比度、响应式宽度和错误恢复。
- 构建产物审计、报告和文档。

确定性测试：

- Playwright 启动 persistent Chromium profile 并加载 extension。
- 受控 API server 使用 Barrier/deferred response 驱动轮询和动作竞争，不使用随机 sleep。
- 真实后端至少验证：连接、批量提交、任务状态更新、取消、retry、delete、事件、
  artifact 预览/下载和 concurrency 1/2。
- Side Panel 关闭/重开、后端停止/恢复、Token 替换。
- 320、400、600 px 宽度及常用高度截图；无文字溢出、控件重叠和布局跳动。
- Tab/Shift+Tab、Enter/Space、Escape、modal focus trap 和返回焦点。

验收：

- `npm run lint`、`npm run typecheck`、`npm test`、`npm run build`、
  `npm run test:e2e` 全部通过。
- 后端冻结测试全量通过，Phase 1/2 冻结文件无差异。
- `extension/dist` 可由 Chrome “加载已解压的扩展程序”加载。
- 不包含 source map 中的 Token、测试凭证、远程代码或宽泛 host permission。
- 用户在后端已安装并启动的前提下，无需终端完成日常任务操作。

建议 commit：

```text
Phase 3: verify Chrome side panel workflows
```

## 8. 跨 Checkpoint 测试策略

测试层次：

1. 纯函数：URL 行解析、状态映射、错误建议、时长格式、文件名。
2. 状态层：store reducer、poller、generation、动作去重。
3. DOM：表单、筛选、详情、modal、timeline、preview。
4. HTTP integration：受控本地 server 记录 method/path/header/body。
5. Chrome E2E：真实 MV3 permission、storage、Side Panel 和 downloads。
6. 后端回归：现有 Python 全量测试，不修改断言适配前端。

每个测试必须控制时间和异步边界：

- fake timers 驱动轮询。
- deferred Promise 控制响应顺序。
- Barrier/Event 或受控 server route 控制竞态。
- Playwright 使用 locator 状态等待，不用固定 sleep 作为主要同步。
- 每个 case 使用独立 Chrome profile 和 storage namespace。

## 9. 最终验证命令

Phase 3 完成时至少执行并记录：

```bash
cd extension
npm ci
npm run lint
npm run typecheck
npm test
npm run build
npm run test:e2e

cd ../backend
../.venv-smoke/bin/python -m pytest
../.venv-smoke/bin/python -m ruff check src tests ../scripts
../.venv-smoke/bin/python -m ruff format --check src tests ../scripts
../.venv-smoke/bin/python -m mypy src/lvt

cd ..
git diff --check
git diff 872c42d614a77056c1e9510425955a53ffe40361 -- \
  backend/src/lvt/engines/translation.py \
  backend/src/lvt/engines/ollama.py
git diff 07bedfc5ddecfa68b3f053c7d80cf3fb40889ea6 -- \
  backend/src/lvt/workers \
  backend/src/lvt/db \
  backend/src/lvt/pipeline \
  backend/src/lvt/core/processes.py \
  backend/src/lvt/api/app.py \
  backend/src/lvt/api/control.py
```

若 capabilities 前置 commit 获得批准，最后一条检查必须只允许该独立 commit 中经审查的
只读契约差异，其他冻结后端文件仍须无变化。

## 10. Phase 3 退出标准

- Manifest V3 权限最小化，Side Panel 可加载。
- Token 安全保存在 `chrome.storage.local` trusted contexts，不回显、不进 URL/日志。
- 只连接 `http://127.0.0.1:<validated-port>`。
- 批量输入、accepted/rejected、任务列表和四类筛选可用。
- 轮询真实后端进度，无重叠、无旧响应覆盖、无模拟进度。
- retry/cancel/delete 与后端状态矩阵一致。
- 事件时间线分页稳定。
- source/zh-CN 预览和 8 artifact 鉴权下载通过。
- 扩展设置与后端 concurrency 一致。
- 依赖状态真实可验证；若 capabilities 仍为空，则 Phase 3 不得宣告完全通过。
- 中文错误包含原因和可执行下一步。
- Side Panel 重开不影响后端任务，断线恢复后状态收敛。
- 键盘操作、焦点、对比度、长文本和窄宽度验收通过。
- Chrome E2E、扩展质量门、后端回归和冻结检查全部通过。
- 不包含 Phase 4 安装、签名、公证、打包或商店工作。

## 11. 回滚与审查规则

- 每个 checkpoint 单独 commit；不得 amend 已通过 checkpoint。
- checkpoint 失败时只回滚该前端 commit，不触碰 Phase 1/2 冻结历史。
- capabilities 若需后端变更，必须独立于所有扩展 commit，附真实空响应证据、
  OpenAPI/鉴权/去敏测试和明确回滚方法。
- 独立审查按 checkpoint 读取源码、重跑测试并使用真实 unpacked Chrome；截图不能替代
  DOM、网络和 storage 断言。
- Phase 3 最终审查通过前，不开始 Phase 4。
