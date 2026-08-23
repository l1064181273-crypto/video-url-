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

第 5.1 节记录的 capabilities 契约阻塞已经批准采用动态、只读、有限超时的
`CapabilitiesProvider` 解决。该后端改动必须先形成独立前置 commit 并单独审查，
不得与扩展代码或其他后端重构混合。除此之外，Phase 1/2 后端保持冻结。

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

前端不得假设未出现在该表中的字段或路由。错误响应由适配层按响应形状归一化：

```text
HTTPException: detail.error_code + detail.message
FastAPI validation: detail 为数组；只读取安全的 type/loc，不回显 input/ctx
批量 rejected: error_code + message
网络失败/非 JSON: 前端生成本地错误分类，不伪造后端 error_code
```

FastAPI 422 的 `detail` 是 Pydantic validation error 数组，不是
`{error_code, message}`。适配层必须：

- 将其归一化为本地 `validation` 错误。
- 只接受 `detail` 为数组且元素是对象；其他形状归入 `invalidResponse`。
- 仅从 `loc` 提取 allowlist 字段名，不显示后端英文 `msg`、原始 `input`、`ctx`
  或请求中的 URL/Token。
- jobs 422 显示“提交内容格式不正确，请检查 URL 数量和任务选项”。
- settings 422 显示“并发数只能为 1 或 2”。
- events 422 显示“事件分页参数无效，请重新加载时间线”。
- 对 jobs、settings、events 分别使用真实 422 fixture 做确定性测试。

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
- 所有 `chrome.runtime`、`chrome.action`、`chrome.storage` listener 必须在模块顶层
  同步注册，不能等待异步初始化后再注册。
- service worker 不保存 Token、端口、连接状态、listener 注册标志或业务状态到全局
  易失变量；每次被唤醒后从 `chrome.storage.local` 和消息 payload 重建所需上下文。
- 初始化操作必须幂等；service worker 多次启动不能重复注册语义动作、重复打开 Side Panel
  或产生多个消息响应。
- Chrome E2E 使用 DevTools Protocol `ServiceWorker.stopWorker` 按 scope 确定性终止
  extension service worker，再通过 action 或 runtime message 触发复活，并等待
  `serviceworker` target/握手事件；不得使用固定 sleep 猜测生命周期。

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
- 在附加 Token 前验证最终 URL origin 精确等于当前
  `http://127.0.0.1:<validated-port>`，并统一使用 `redirect: "error"`。
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

已确认并批准处理以下真实 API 阻塞：

```text
backend/src/lvt/main.py 调用 create_app() 时未传 capabilities；
backend/src/lvt/api/app.py 的 GET /api/v1/capabilities 返回 capabilities or {}；
因此生产响应恒为 {}，前端无法区分依赖“可用”“缺失”或“未检查”。
```

解决方案固定为动态、只读、有限超时的 `CapabilitiesProvider`。不得继续使用
`create_app(capabilities=<启动时静态字典>)` 作为生产状态来源。Provider 在每次缓存
失效后的请求中重新探测本地状态，并返回：

```text
available   已确认本地组件或模型当前可用
missing     已确认本地二进制、Python 包、配置模型或模型文件不存在
unavailable 已安装或已配置，但有限超时探测失败或服务不可达
unchecked   因前置依赖不可用、当前平台不支持安全探测或尚无有效探测结果而未检查
```

响应必须分别报告 FFmpeg、Ollama、ASR Python 包、ASR 模型、diarization、
主翻译模型和 fallback 模型，不得用一个聚合绿色状态掩盖部分缺失：

```json
{
  "checked_at": "2026-08-23T10:00:00+00:00",
  "ttl_seconds": 5,
  "ffmpeg": {
    "status": "available",
    "checked_at": "2026-08-23T10:00:00+00:00",
    "version": "7.0"
  },
  "ollama": {
    "status": "available",
    "checked_at": "2026-08-23T10:00:00+00:00",
    "version": "0.32.15"
  },
  "asr_package": {
    "status": "available",
    "checked_at": "2026-08-23T10:00:00+00:00",
    "version": "0.4.3"
  },
  "asr_model": {
    "status": "available",
    "checked_at": "2026-08-23T10:00:00+00:00",
    "model": "mlx-community/whisper-small-mlx"
  },
  "diarization": {
    "status": "available",
    "checked_at": "2026-08-23T10:00:00+00:00",
    "engine": "sherpa-onnx"
  },
  "translation_primary": {
    "status": "available",
    "checked_at": "2026-08-23T10:00:00+00:00",
    "model": "hy-mt2:1.8b-q4km-fixed"
  },
  "translation_fallback": {
    "status": "available",
    "checked_at": "2026-08-23T10:00:00+00:00",
    "model": "qwen2.5:1.5b"
  }
}
```

Provider 约束：

- 使用 monotonic 计时的 5 秒短 TTL；TTL 内返回同一 snapshot 和 `checked_at`，
  TTL 后首个请求触发刷新，并使用 single-flight 防止并发重复探测。
- 每项探测最多 1 秒，整体最多 2 秒；单项超时只把该项标记 unavailable，不阻塞其他项。
- FFmpeg 探测只检查已配置/现有 executable 并运行有界 `-version`；不得调用可能下载
  二进制的 `static_ffmpeg.add_paths()`。
- Ollama 使用有界本地 `/api/version` 和 `/api/tags`；Ollama down 时 Ollama 为
  unavailable，主模型和 fallback 为 unchecked，而不是猜测 missing。
- ASR Python 包通过本地 package metadata 检查；ASR 模型通过本地缓存索引检查。
  两者分别报告，使“包缺失”和“模型缺失”可区分。
- diarization 同时检查本地 Python 包与所需模型文件；任一模型文件缺失时状态为 missing。
- 主翻译模型和 fallback 必须分别根据 Ollama tags 报告 available/missing。
- 探测不得实例化推理引擎、加载模型、拉取网络资源、创建模型或触发任何软件/模型下载。
- 响应不得包含绝对路径、Token、模型缓存位置、命令行、环境变量、原始异常或 traceback。
- 异常只映射为固定状态和可公开的简短中文摘要。

必须新增确定性后端测试：

- Ollama 受控 server down → up，TTL 到期后由 unavailable 变 available。
- Ollama available 时主模型 missing/available、fallback missing/available 独立组合。
- ASR Python 包 missing 且模型 unchecked；包 available 但模型 missing；两者 available。
- diarization 任一必需模型文件 missing，以及全部文件 available。
- TTL 内不重复调用 probe；TTL 后并发请求只执行一次刷新。
- 超时不超过预算；响应不含绝对路径、Token、缓存位置或原始异常。
- 每个下载/安装入口均替换为失败哨兵，证明 capability probe 不会触发下载。

该 Provider 和现有 `/api/v1/capabilities` 接线必须形成独立后端前置 commit，在任何
extension commit 之前完成并单独审查。不得同时修改 worker、Repository、checkpoint、
进程控制、控制 API 的其他路由或 Phase 1 strict-token 文件。

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
- 每个 JSON 预览设置 5 MiB 字节硬上限，并在 `JSON.parse` 之前执行：
  - `Content-Length` 存在且为可信非负整数时，超过上限立即中止。
  - `Content-Length` 缺失、非法或小于实际响应时，使用
    `response.body.getReader()` 有限流式读取，累计解压后的实际字节。
  - 一旦累计超过上限，立即 `reader.cancel()`、释放 buffer，不再解码或解析 JSON。
  - 即使 Content-Length 未超限，也必须按实际流式字节再次执行硬上限。
- 字节检查通过后才使用严格 UTF-8 解码和 `JSON.parse`，随后再执行例如
  2,000 Segment 的渲染上限。
- 超限只禁用当前预览并显示“文件过大，请下载后查看”，不得禁用或截断正常文件下载。

### 5.6 Artifact 下载

不能把 `download_url` 直接放入普通 `<a href>`，因为接口要求 Header Token。
也不能信任 API 返回的任意 `download_url` 并直接 fetch。

流程：

1. 将 artifact ID 验证为规范 UUID；拒绝 `/`、`\`、`..`、query、fragment 和控制字符。
2. 忽略服务端 `download_url` 的 origin；使用验证后的 ID 构造固定相对路由
   `/api/v1/artifacts/${encodeURIComponent(id)}/download`。
3. 若保留 `download_url` 做契约校验，它必须与上述相对路由逐字符一致，否则将响应视为
   `invalidResponse`，不得跟随该值。
4. API client 在附加 Token 前再次断言目标 URL 的 scheme 为 `http:`、hostname 精确为
   `127.0.0.1`、显式端口等于当前 validated port，origin 与当前连接完全一致。
5. 所有鉴权 fetch 使用 `redirect: "error"`，禁止重定向把 Token 或响应带到其他 origin。
6. 检查 HTTP 状态、Content-Type 和非空 body。
7. 在 Side Panel 创建 Blob URL。
8. 调用 `chrome.downloads.download`，文件名只取 API artifact kind，并加安全任务标题目录。
9. download API 返回后及时 revoke Blob URL。
10. 401、404、断线分别显示中文错误；404 不猜测内部路径。

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

### 前置 commit：动态 capabilities provider

该提交在任何扩展代码前完成，不属于扩展 Checkpoint 1，不得混入前端文件。

范围：

- 定义 `CapabilitiesProvider`、公开 DTO、四态枚举、checked_at 和 5 秒 TTL。
- 对 FFmpeg、Ollama、ASR package、ASR model、diarization、主翻译模型和 fallback
  分别执行只读、有限超时探测。
- 将现有鉴权 `GET /api/v1/capabilities` 接到动态 Provider。
- 保持其他 API、worker、Repository、checkpoint、进程控制和 strict-token 不变。

确定性测试：

- Ollama down/up、主/备模型 missing/available 的完整状态矩阵。
- ASR package missing、package available/model missing、两者 available。
- diarization 任一模型文件 missing 和全部 available。
- fake monotonic clock 验证 TTL；Barrier 验证并发刷新 single-flight。
- 受控超时和所有下载入口失败哨兵。
- 响应递归扫描，禁止绝对路径、Token、缓存位置和原始异常。

验收：

- 每个组件只返回 available/missing/unavailable/unchecked。
- TTL 内 checked_at 不变，过期后刷新；探测不触发下载。
- 独立后端专项、全量 pytest、Ruff、format、mypy 和冻结检查通过。
- commit 只包含 capabilities provider、最小 endpoint 接线及对应测试。

建议 commit：

```text
Phase 3: expose dynamic local capabilities
```

### Checkpoint 1：MV3 骨架与契约锁定

范围：

- 固化 TypeScript DTO、状态和错误码。
- 建立 Vite/Vitest/ESLint/Prettier。
- 生成最小 MV3 manifest、service worker 和 Side Panel 空壳。
- 用真实 Uvicorn 验证 host permission、Token header、health/settings/jobs。
- 锁定已批准 capabilities DTO，不在扩展中复制探测逻辑。

确定性测试：

- manifest JSON schema/权限白名单测试。
- build 不包含远程 URL、inline script 或 eval。
- unpacked extension 从 `chrome-extension://` 成功请求 127.0.0.1。
- 错误 Token 得到 401；Token 不出现在 request URL。
- capabilities 七个组件、四态、checked_at 和 TTL 字段的运行时校验。

验收：

- Chrome action 可打开 Side Panel。
- 只有 storage/downloads/sidePanel 和 127.0.0.1 host permission。
- 前端只展示 Provider 返回状态，不使用静态绿色状态或任务历史推断。

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
- jobs、settings、events 的真实 FastAPI 422 detail 数组均归一化为 validation；
  不回显 input、ctx、Token 或完整 URL。
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
- jobs 422 使用固定中文提示并保留待修改输入，不把 Pydantic detail 直接渲染。

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
- events offset/limit 422 安全归一化，不渲染 Pydantic input/ctx。

验收：

- queued/active cancel、failed/cancelled retry、terminal delete 与后端契约一致。
- 动作失败不进行虚假本地状态转换。

建议 commit：

```text
Phase 3: add job controls and event timeline
```

### Checkpoint 5A：预览与 artifact 下载

范围：

- source/zh-CN JSON 预览。
- 8 artifact 分组和鉴权下载。
- 预览字节上限、流式读取和超限降级。
- artifact ID 与固定 127.0.0.1 下载路由。

确定性测试：

- artifact 列表必须恰好按 kind 呈现，不信任服务器文件路径。
- 恶意 absolute、scheme-relative、其他端口、localhost、traversal `download_url`
  全部被拒绝；请求只由验证后的 artifact ID 构造。
- 鉴权 fetch 使用当前 127.0.0.1 validated-port、Header Token 和 `redirect: "error"`。
- Blob URL 在成功、失败和取消后均 revoke。
- source/zh-CN tab 保留 Segment 顺序、Speaker 和时间。
- Content-Length 超限、缺失、非法、低报四种情况均在 `JSON.parse` 前停止预览。
- 流式累计达到 5 MiB 上限时 cancel reader 并释放 buffer。
- 预览超限后正常 artifact 下载仍可成功。

验收：

- completed Job 可预览原文/中文，并下载全部 8 个文件。
- Token 从不发送到当前 validated 127.0.0.1 origin 之外。
- 超限只影响预览，不影响下载。

建议 commit：

```text
Phase 3: add bounded previews and artifact downloads
```

### Checkpoint 5B：设置、capabilities 与中文错误

范围：

- ASR/diarization 新任务选项及 worker concurrency 设置。
- 动态 capabilities 七组件状态和 checked_at 展示。
- Token 保存、替换、清除以及完整中文错误建议。
- FastAPI 422 detail 数组的 route-specific 安全提示。

确定性测试：

- concurrency 只允许 1/2；settings 422 显示固定中文提示，后端失败时回滚控件。
- capabilities 四态逐组件渲染；TTL 内响应不造成 UI 闪烁，checked_at 更新可见。
- Ollama unavailable 时主/fallback unchecked；模型 missing 与服务 down 文案不同。
- ASR package missing 与 ASR model missing 文案和建议不同。
- diarization 模型文件 missing 显示安装建议，不展示文件绝对路径。
- Token 输入从 DOM 清空，storage trusted access，日志和错误对象无秘密。
- jobs/settings/events 422 fixture 递归扫描，确保 input/ctx 未进入 UI/store/log。

验收：

- settings 刷新后与后端持久化 concurrency 一致。
- 所有组件状态来自动态 Provider，且显示最近 checked_at。
- 所有错误均有中文下一步，不展示内部路径、原始异常或敏感输入。

建议 commit：

```text
Phase 3: add settings diagnostics and safe errors
```

Checkpoint 5A 和 5B 分别形成独立 commit、独立测试和独立审查。由于原 Checkpoint 5
拆为两个交付单元，原最终验收 Checkpoint 6 顺延为 Checkpoint 7。

### Checkpoint 7：Chrome 集成与 Phase 3 验收

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
- 使用 DevTools Protocol 按 scope 终止 service worker，等待 target 消失；随后通过
  action 和 runtime message 分别触发复活，并等待新 worker 握手。
- service worker 复活后验证 action 仍只打开一次 Side Panel、listener 只响应一次、
  storage trusted access 保持、Token/端口从 storage 重建。
- 生命周期测试不得依赖固定 sleep，也不得依赖终止前的 service worker 全局变量。
- 320、400、600 px 宽度及常用高度截图；无文字溢出、控件重叠和布局跳动。
- Tab/Shift+Tab、Enter/Space、Escape、modal focus trap 和返回焦点。

验收：

- `npm run lint`、`npm run typecheck`、`npm test`、`npm run build`、
  `npm run test:e2e` 全部通过。
- 后端冻结测试全量通过，Phase 1/2 冻结文件无差异。
- `extension/dist` 可由 Chrome “加载已解压的扩展程序”加载。
- 不包含 source map 中的 Token、测试凭证、远程代码或宽泛 host permission。
- service worker 被 Chrome 回收后，action、Side Panel、storage 和消息 listener
  行为与首次启动一致。
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
5. Chrome E2E：真实 MV3 permission、storage、Side Panel、downloads 和 service
   worker 终止/复活。
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
CAPABILITIES_COMMIT=<独立前置 capabilities commit hash>
git diff --name-only \
  07bedfc5ddecfa68b3f053c7d80cf3fb40889ea6..$CAPABILITIES_COMMIT
git diff $CAPABILITIES_COMMIT..HEAD -- \
  backend/src/lvt/workers \
  backend/src/lvt/db \
  backend/src/lvt/pipeline \
  backend/src/lvt/core/processes.py \
  backend/src/lvt/api/app.py \
  backend/src/lvt/api/control.py
```

第一条 capabilities diff 必须只包含动态 Provider、最小 endpoint 接线和对应测试；
从该前置 commit 到 Phase 3 HEAD 的后端 diff 必须无输出。其他冻结后端文件始终无变化。

## 10. Phase 3 退出标准

- Manifest V3 权限最小化，Side Panel 可加载。
- Token 安全保存在 `chrome.storage.local` trusted contexts，不回显、不进 URL/日志。
- 只连接 `http://127.0.0.1:<validated-port>`。
- 批量输入、accepted/rejected、任务列表和四类筛选可用。
- 轮询真实后端进度，无重叠、无旧响应覆盖、无模拟进度。
- retry/cancel/delete 与后端状态矩阵一致。
- 事件时间线分页稳定。
- source/zh-CN 预览在 5 MiB 字节上限内工作；超限在 JSON.parse 前停止且不影响下载。
- 8 artifact 下载仅由验证后的 ID 构造当前 127.0.0.1 origin 路由。
- 扩展设置与后端 concurrency 一致。
- 动态 capabilities 分别报告七个组件、四态和 checked_at，TTL 与超时契约通过。
- jobs、settings、events 的 422 detail 数组均安全归一化，不泄漏 input/ctx。
- 中文错误包含原因和可执行下一步。
- Side Panel 重开不影响后端任务，断线恢复后状态收敛。
- service worker 被确定性终止并复活后，action、Side Panel、trusted storage 和
  listener 行为保持正确，不依赖全局易失状态。
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
