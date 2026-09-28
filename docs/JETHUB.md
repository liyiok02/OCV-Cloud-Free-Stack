# 云端 OAuth 接入开发文档（DEVLOG）

> **本文件是本功能唯一的开发台账。任何改动 —— 代码、配置、面板、
> 脚本、文档、开关默认值 —— 都必须先在这里登记，再落到代码。**
> 未登记的改动视为未完成。
>
> 本功能**寄居在 `cloud_free_stack` 插件内**（不新建插件目录）：它复用该插件
> 已有的 `.pth` 注入锚点、面板服务（:8799）、配置层与自愈机制。因此本文件是
> `docs/DEVLOG.md` 的**姊妹文档**，铁律以那份为准。
>
> 📌 **命名**：功能原名「Jet Hub」（沿用上游项目名），1.12.0 起在**界面上**
> 一律称「**云端 OAuth**」；代码里的 `jethub` 前缀与 `JETHUB_*` 配置键
> **保持不变**（改名会牵动 `.env` 受管块、`.pth`、自愈逻辑与既有用户配置，
> 收益不抵风险）。

- 适用目录：`E:/1B1BLaoYang/plugins/cloud_free_stack/`
- 宿主项目：OCV（One-Click VidGen）`E:/1B1BLaoYang`
- 姊妹文档：`docs/DEVLOG.md`（cloud_free_stack 主台账）、`README.md`
- 上游参考：<https://gitee.com/iJetLi/deepseek-harness-codearts>（MIT）
- 立项日期：**2026-09-28**
- 当前版本：**1.12.0**
- 当前状态：**可用（9 家提供商全部接线；后端 + 面板 + 模型选择全通；尚未真实登录验收）**

---

## 0. 目标与边界

### 0.1 用户需求（原话要点）

1. 参考 `deepseek-harness-codearts`（Jet Hub），把它的 LLM 能力适配进 OCV；
2. **本次只适配 LLM**（不碰图片/视频/TTS）；
3. 在 `cloud_free_stack` 基础上做，把 **Jet Hub 前端面板并入 cloud_free_stack 面板**；
4. 在 **LLM 面板中可以选择其中的模型**；
5. **必须可独立运行 —— 不要在目标机上依赖 DSH**；
6. **所有文件都放在 OCV 插件目录内**；
7. **凭据仅储存在 OCV，不桥接 DSH**；但要**支持 Provider 凭据管理与多账号 + 备份/恢复**；
8. 先适配 **CodeBuddy**（buddy），但**先要可行**，不要求立刻完工。

### 0.2 明确不做（本次范围外）

- 不接线除 CodeBuddy 外的 7 个 provider（codearts / workbuddy / lobsterai /
  qoder / trae / cline / loomy / atomcode）—— vendor 里带着实现，未接线；
- 不做短信验证码登录（那是 loomy 的备用路径，且 msgid 存进程内存）；
- 不做积分/签到/onboarding（`credits.*` / `onboarding.*`）；
- 不改 OCV 源码一行。

### 0.3 铁律（继承主台账，此处重申与本功能相关的）

| # | 铁律 |
|---|---|
| A | **禁止直接改动 OCV 源码。** 只能改 `plugins/cloud_free_stack/` 下的文件。 |
| B | **所有对 OCV 的改动都经注入 / 卸载脚本。** 本功能对 OCV 的写入仅 `.env` 的插件块。 |
| C | **所有文件都在插件目录内。** 凭据/账号池/备份一律落 `<插件>/state/`，**绝不写 `~/.dsh`**。 |
| D | **每个插件/功能有独立开发文档。** 本文件即 Jet Hub 接入的台账。 |

---

## 1. 架构

```
OCV 后端 (:8010)  ──┬─ 面板 shim (:8799) ──┐
                    │                       │  /api/panel/jethub/*
                    │  patches._patch_main  │  （Python，经 urllib 转发）
                    │   └─ 守护线程拉起 ────┐│
                    │                       ▼▼
                    │            Jet Hub 桥 (:8801, Node)
                    │              ├─ OpenAI 兼容面  /v1/chat/completions
                    │              │                 /v1/models
                    │              ├─ 面板 RPC 面     /rpc/*
                    │              └─ /health
                    │                       │
                    └─ LLM 调用 ────────────┘
                       （OCV 把桥当成一个
                         OpenAI 兼容服务商）

状态（全部在 <插件>/state/）：
  credentials.json      凭据（自建，非 DSH 格式）
  jet-hub/state.json    账号池（多账号 + 限流记录 + 模型黑名单）
  backups/*.json        备份（含凭据明文）
```

### 1.1 为什么是「Python 托管 + Node 子进程」

* 桥必须用 Node：`dsh-codearts-auth` 的适配器是 ESM JS，封装了 CodeBuddy 的
  认证协议、多账号限流轮换、SSE 容错 —— **重写成 Python 等于重做最值钱的部分**；
* 托管必须在 OCV 后端进程内：与界面同生命周期，且**账号池只能有一个实例**
  （`account-pool.ts:48-56` 记录了多进程互相覆盖 `state.json` 的真实缺陷）；
* 不用分离进程：沿用 `shim_launcher.py:12-17` 的结论（受限环境里
  `CREATE_BREAKAWAY_FROM_JOB` 被拒，产物是随父进程消失的僵尸）。

### 1.2 与 DSH 的耦合面（实测结论）

| 它需要 | 谁提供 | 证据 |
|---|---|---|
| `ctx`（cordis 容器） | `new Context()`，cordis 在 vendor 里 | 实测可构造 |
| `ctx.credentials` | **自建** `jethub/credentials.mjs` | `buddy-auth.ts` 只调 resolve/set/unset/describe |
| `ctx.llm` | 桩：只记录注册 | 我们直接调 `adapter.stream()` |
| `ctx.logger` | cordis 自带 | — |
| `attachments` | **不提供** —— 只做纯文本 LLM | 图片会得到 UNSUPPORTED_CONTENT |

**实测（2026-09-28）**：`AccountPool` / `BuddyAuth` / `BuddyAdapter` 全部能在裸
cordis 容器里构造并注册成功，且 `storageReport()` 确认两个落点都在插件目录内。

---

## 2. 文件清单

| 文件 | 职责 |
|---|---|
| `vendor_tool.py` | **开发机侧**打包器：解析 pnpm 符号链接、求依赖闭包、复制到 `vendor/`。`--scan` / `--verify` |
| `vendor/` | 打包进来的第三方（MIT）：`dsh-codearts-auth/lib` + 12 个依赖包，**4.4 MB** |
| `jethub/credentials.mjs` | **自建凭据存储**：单文件 JSON，只存插件目录；含导入导出（备份用） |
| `jethub/runtime.mjs` | 装配层：cordis 容器 + 凭据服务 + 账号池 + provider 注册；账号 CRUD、模型清单、备份/恢复 |
| `jethub/bridge.mjs` | OpenAI 兼容桥核心：消息转换、流聚合、两条契约补丁；**23 项纯函数单测** |
| `jethub/server.mjs` | HTTP 服务（`node:http`）：`/v1/*`、`/rpc/*`、`/health`；本机请求校验 |
| `ocv_cloud_stack/jethub_bridge.py` | Python 侧托管：找 Node、拉起、探活、`call()` 转发、`status_snapshot()` |
| `ocv_cloud_stack/panel.py` | 新增 `jethub` 分组字段 + 9 个 `jethub_*` handler |
| `ocv_cloud_stack/image_shim.py` | 新增 9 条 `/api/panel/jethub/*` 路由 |
| `ocv_cloud_stack/models_catalog.py` | 新增 `llm_jethub` 用途（provider = `jethub`，走本机桥） |
| `ocv_cloud_stack/config.py` | 新增 8 个 `JETHUB_*` 访问器 |
| `ocv_cloud_stack/patches.py` | `_patch_main` 里加 `_host_jethub_bridge()` 守护线程 |
| `panel/index.html` | 新增 `tab-jethub` 分页 + Jet Hub JS |
| `verify_jethub.py` | 专项自检（46 项） |
| `dev_patch_panel_html.py` | **从 zip 还原并重新给 panel HTML 打补丁**（见 §5 事故 F-001） |
| `dev_serve_panel.py` | 开发期起面板 shim（指定端口） |
| `dev_render_check.mjs` | 用 headless Chrome 真实渲染面板并断言（15 项） |

---

## 3. 关键设计决策

### 3.1 凭据只存 OCV，自建存储

**决策**：不用 `dsh-credentials-local`，自己写 120 行的 `credentials.mjs`。

**原因**：DSH 那个实现把数据放 `$DSH_HOME/.credentials.yaml`，且其 Config 里的
`dshHome` **默认回退 `~/.dsh`**。在没装 DSH 的目标机上这要么失败，要么更糟：
**静默写到用户主目录**，直接破坏「所有文件都在插件目录内」这条硬约束。

自建换来三件确定性：路径可控、格式可控（单文件 JSON ⇒ 备份=复制）、接口最小。

> `@deepseek-ai/dsh-credentials` **仍留在 vendor 里** —— `buddy-auth.js` 需要它的
> `credentialRef()`（把字符串打 brand 的薄函数）。我们只是**不启用**它的
> `LocalCredentialProvider`，改用自己提供的服务。

### 3.2 状态目录两级钉死

`jet-hub-store.js` 的回退链是
`DSH_JET_HUB_STATE_DIR` → `profileContext.home` → `DSH_HOME` → `~/.dsh`。
最后一级是危险默认值，所以 `runtime.mjs` **两级钉死**：

```js
process.env.DSH_JET_HUB_STATE_DIR = stateDir   // 优先级最高
ctx.provide('profileContext', { home: stateDir })  // 防绕过容器直读环境
```

### 3.3 两条契约补丁（桥存在的理由）

| 维度 | OCV | vendor 适配器 | 桥的对策 |
|---|---|---|---|
| 传输 | 非流式（全仓 `stream` 出现 0 次） | body 写死 `stream:true` | 消费 SSE → 聚合 → 回非流式 |
| `response_format` | JSON 模式会发 | **不转发**（grep 零命中） | 降级成**提示词约束**（追加「只输出 JSON」） |
| 超时 | 读超时 120 s | 排队重试最长 180×10s=30min | 软超时 **110 s** 返回结构化 503 |

第三条的关键：**不缩短** vendor 的重试（那是它应对限流的核心机制），
而是让桥在 OCV 断连**之前**返回 503 —— OCV 会按
`RETRYABLE_STATUS_CODES` 处理，而不是拿到一个断掉的连接。

### 3.4 为什么把 Jet Hub 做成 models_catalog 的一个「用途」

`llm_jethub` 作为 `KINDS` 的新条目挂进既有抽象，于是**免费获得**：
面板右上角的「↻ 模型」、内存+磁盘两级缓存、回落逻辑、`verify_note` 说明文案、
以及「用户可手填模型」的能力。

### 3.5 安全：桥只绑回环 + 三道本机校验

`server.mjs` 的 `isLocalRequest()`：

1. `Host` 必须是回环 —— 挡 **DNS rebinding**（攻击者域名解析到 127.0.0.1 时
   Host 头仍是攻击者域名）；
2. 有 `Origin` 时必须是回环；
3. `Sec-Fetch-Site` 不允许 `cross-site`。

**实测**：伪造 `Host: evil.example.com` 被 **403 拦截**；
`Origin: https://evil.example.com` 与 `sec-fetch-site: cross-site` 同样 403。

> 注意：用 Node 的 `fetch` 测 Host 头**测不出来** —— `Host` 是 forbidden header，
> fetch 会忽略你设的值。必须用 `node:http` 的 `request()` 才能真实验证。

---

## 4. 改动登记表

| 日期 | 版本 | 改动摘要 | 触发原因 | 验证方式 | 状态 |
|---|---|---|---|---|---|
| 2026-09-28 | — | vendor 打包 dsh-codearts-auth + 12 个依赖闭包（8.8→4.4 MB 精简） | 目标机不装 DSH | 中立 cwd 装配成功 | 已完成 |
| 2026-09-28 | — | `credentials.mjs` 自建凭据存储 | 不桥接 DSH + 要求备份 | 与真实 vendor 联调 | 已完成 |
| 2026-09-28 | — | `runtime.mjs` 装配层 | — | 裸 cordis 容器装配成功 | 已完成 |
| 2026-09-28 | — | `bridge.mjs` OpenAI 兼容桥 | OCV 非流式 vs 适配器流式 | **23/23 单测** | 已完成 |
| 2026-09-28 | — | `server.mjs` HTTP 服务 + 本机校验 | — | 端点实测 + 安全矩阵 6 项 | 已完成 |
| 2026-09-28 | — | `jethub_bridge.py` Python 托管 | 桥要随 OCV 起 | `ensure_running()` 实测 | 已完成 |
| 2026-09-28 | — | 修 `resolveCredential` 走账号池（**非** `auth.refresh()`） | 假凭据报「未配置凭据」 | 假 token 打到腾讯 401 | 已修复 |
| 2026-09-28 | — | `config.py` 8 个 `JETHUB_*` | — | verify_jethub 第 2 节 | 已完成 |
| 2026-09-28 | — | `patches.py` `_host_jethub_bridge()` | 随后端自动拉起 | — | 已完成 |
| 2026-09-28 | — | `panel.py` jethub 分组 + 9 handler | 需求 3 | verify_jethub 第 5/6 节 | 已完成 |
| 2026-09-28 | — | `image_shim.py` 9 条路由 | 需求 3 | 路由存在性断言 | 已完成 |
| 2026-09-28 | — | `models_catalog.py` `llm_jethub` 用途 | **需求 4（核心）** | verify_jethub 第 4 节 | 已完成 |
| 2026-09-28 | — | `panel/index.html` tab-jethub + JS | 需求 3 | **headless 渲染 15/15** | 已完成 |
| 2026-09-28 | — | 修前端 `payload.data` 解包 bug | 分页整页空白且无报错 | 渲染验证抓出 | 已修复 |
| 2026-09-28 | — | **F-001**：PowerShell 写坏 HTML → 从 zip 还原 + 补丁脚本 | 乱码事故 | 0 个 U+FFFD + 渲染通过 | 已修复 |

---

## 5. 故障档案

### F-001 PowerShell 读写 UTF-8 中文文件导致全文件乱码

**事故**：用 `Get-Content -Raw` + `Set-Content` 给 `panel/index.html` 做字符串替换，
结果**整个文件的中文变成乱码**（`云端免费栈` → `浜戠鍏嶈垂鏍?`）。

**根因**：Windows PowerShell 5 的 `Get-Content` **默认按 ANSI(cp936) 解码**，
而该文件是 **UTF-8 无 BOM**。于是：按 GBK 误解码 → 字符串里全是错的中文 →
`Set-Content -Encoding UTF8` 再把这份错内容按 UTF-8 写回。
**每一次读-改-写都会叠加一层损坏。**

**为什么危险**：文件**仍是合法 UTF-8**，`read`/`grep` 都能正常打开，
只有人眼看得见乱码；而且它照样能被浏览器解析（只是显示成乱码）。

**修复**：
1. 找到无损源 —— `plugins/cloud_free_stack.zip` 里有原版
   （38,681 字节，2026-09-24 的干净副本）；
2. 写 `dev_patch_panel_html.py`：**从 zip 还原 + 用 Python 重新打三处补丁**，
   幂等且兼容 LF/CRLF。

**教训（已升格为铁律）**：

> **改含中文的 UTF-8 文件，一律用 Python（显式 `encoding='utf-8'`），
> 不要用 PowerShell 的 `Get-Content`/`Set-Content`。**
> 只读检查可以用 `read`/`grep` 工具（它们按 UTF-8）。

**副作用清理**：事故文件已备份为 `panel/index.html.before-repatch` 后删除；
最终文件 0 个 U+FFFD 替换字符，渲染验证通过。

### F-002 适配器 resolveCredential 走了错路径（已修）

**症状**：注入一个格式正确的假凭据后，`complete()` 返回
`502 buddy: 未配置凭据，请先登录` —— 而不是走到网络层。

**根因**：我最初把 `refresh` 接成 `auth.refresh()`。但
`BuddyAuth.refresh()` 内部**写死用产品默认 ref**（`BUDDY_ACCESS_TOKEN`），
而本插件的凭据存在**账号 ref**（`BUDDY_ACCOUNT_XXXXXXXX`）下
（`buddy-auth.ts:222`）。

**修复**：`resolveUsableCredential()` 自己走账号池 ——
`pool.getAvailableAccount()` → 过期则 `auth.refreshAccountCredential(ref)`
（那个方法**接收 ref 参数**，是对的路）→ 读回新值。

**验证**：修完后同一个假 token 能真正打到腾讯服务器并返回
`401 Authorization Required (openresty)` —— 证明整条链路
（凭据解析 → 适配器 → 真实 HTTPS 请求）已打通。

### F-003 面板响应解包约定不一致（已修）

**症状**：Jet Hub 分页在浏览器里**整页空白**，而**页面没有任何 JS 报错**。

**根因**：`image_shim._panel_call` 的约定是
「handler 返回含 `ok` 的 dict → **原样下发**；否则包成 `{ok:true,data:...}`」。
我的 Jet Hub handler 一律返回含 `ok` 的扁平 dict，**所以响应没有 `data` 层**；
而前端写的是 `payload.data.accounts` → 恒为 `undefined` → 静默渲染空白。

**为什么静态检查没发现**：HTML 结构、路由、字段全都在，只是运行时取不到值。
**只有真浏览器渲染才验得出来** —— 这正是 `dev_render_check.mjs` 存在的理由。

**修复**：前端加 `jhUnwrap(payload)`，用 `payload.data || payload` 兼容两种形态。

### F-004 面板出现一大段「乱码」——CSS 里的标签文本提前关闭了样式块（已修）

**症状**：用户报告面板上出现**一大段 CSS 源码文本**（`.jh-layout { display: flex; …`
一直铺到 `--accent`），看起来像编码乱码，但**内容其实完全正确**。

**根因**：`cloud-oauth.css` 的**注释里出现了 style 元素的标签文本**
（原句是「注入到 index.html 的 `</style>` 之前」）。

`<style>` 是 HTML 的 **raw text 元素**：解析器在其内部
**不做标签解析、也不认 CSS 注释**，一遇到结束标签就**立刻关闭样式块**。
后面的 CSS 于是从「样式」变成「正文」，整段显示在页面上。
（`<script>` 同理。）

**为什么容易被误判为编码问题**：文本内容本身是正常的 CSS 源码，
只是位置错了。真正的编码损坏会出现 `U+FFFD` 替换字符 —— 这次**一个都没有**。
判断方法：看有没有替换字符，没有就不是编码问题。

**修复过程（连续踩了两次，值得记）**：

1. 第一次：把注释改成「不要写出字面量的 style 结束标签」，
   同时加了护栏 `guard_raw_text()`；
2. **护栏当场把第二次也抓住了** —— 因为我在说明里写了
   **开标签文本**，而开标签同样会被解析器识别。首版护栏只查了结束标签，
   所以第一轮修复**自己又制造了同一个 bug**。

最终护栏**开闭两种标签文本都查**（`<style` / `</style` / `<script` / `</script`），
并额外断言拼装结果里 `<style>` 恰好只有 1 处。

**验证（三件证据，全部来自真浏览器）**：

| 指标 | 修复前 | 修复后 |
|---|---|---|
| `<style>` 元素数 | 2（被提前关闭后又开了一个） | **1** |
| 最后样式表的规则数 | 0（CSS 没被当样式解析） | **122** |
| body 可见文本里的 CSS 残留 | `.jh-layout`、`display: flex`… | **（无）** |

`dev_render_check.mjs` 新增 3 条断言长期守这条：
「样式块只有一个」「CSS 真的被解析（规则数 > 0）」「可见文本无 CSS 残留」。

**教训**：

> 往 HTML 的 raw text 元素（style / script）里注入内容时，
> **注入物本身绝不能含有该元素的标签文本** —— 包括写在注释里的说明文字。
> 这类问题**静态看 HTML 结构完全正常**，只有真渲染才暴露。

> 📌 **这个坑连踩三次**：第一次是 CSS 注释里的结束标签；第二次是「修第一次时
> 在说明里写了开标签」；第三次是「修备份弹窗时在 JS 注释里写了 script 结束标签」。
> 三次都是护栏当场抓住的 —— 说明**护栏比记性可靠**，这类规则必须落到代码里。

### F-005 点「恢复」不弹出文件选择框——注入的 DOM 排在脚本之后（已修）

**症状**：用户报「点恢复不跳出让我上传本地备份文件」，按钮点了没有任何反应。

**根因**：`dev_patch_panel_html.py` 把备份弹窗插到了 `</body>` **之前**，
而面板的 `</script>` 也在 `</body>` 之前 → **弹窗 DOM 落到了脚本之后**。

而面板的绑定函数 `jhBind(id, handler)` 是**立即执行**的：

```js
jhBind("btn-jh-backup-pick", …)   // 脚本执行到这里时，元素还不存在于 DOM
```

`el(id)` 返回 `null` → `if (node)` 判定失败 → **静默跳过绑定**。
于是按钮永远没有 click 处理器，点了当然没反应。
**整个过程没有任何报错**，`console.error` 也是干净的。

**为什么静态检查没发现**：HTML 里有这个元素、JS 里有这个绑定，
两边单独看都「存在」—— 缺的是**它们的先后顺序**。

**修复（两件事一起做）**：

1. **按用户要求把弹窗彻底去掉** —— 备份与恢复改成两张**常驻卡片**，
   直接展开在「云端 OAuth」分页里，不再需要开关按钮；
2. **绑定改成 `jhBindWhenReady()`** —— 元素就绪即绑，否则每 200ms 重试
   （最多 ~6 秒）。这样「绑定时机」不再依赖「元素恰好写在脚本前面」
   这种脆弱假设。

**验证**（真浏览器，两条互补的断言）：

| 断言 | 说明 |
|---|---|
| `切到本页后「选择文件」按钮可见可点` | `offsetParent !== null` 且未 disabled |
| `点「选择文件」确实唤起文件选择框（原故障回归）` | 给 `<input type=file>` 挂一个 click 监听，**真的点按钮**，断言监听被触发 |

第二条是关键：它**模拟了用户的真实操作路径**，而不只是检查「元素存在」。

**教训**：

> 注入 DOM 时，**被绑定的元素必须排在绑定代码之前**；或者绑定本身要写成
> 「等元素就绪」。这类顺序问题**没有任何运行时报错**，静态看也完全正常 ——
> 只有「真的点一下」才验得出来。

### F-006 一键签到的函数签名与 vendor 不符（已修，5 个 provider 全中）

**症状**：一键签到「什么都不发生」—— 计数为 0、也没有明显报错。
（真机上更容易表现为「签到失败但不知道为什么」。）

**根因**：`credits.mjs` 里调用 vendor 函数时**凭记忆写参数**，而 vendor 的
签名各不相同。逐个核对（`src/*-credits.ts`）后发现的错配：

| provider | vendor 真实签名 | 首版调用 | 问题 |
|---|---|---|---|
| buddy | `(credential, product)` | `(credential, product)` | ✅ |
| **qoder** | `(credential, product, fetcher?)` | `(credential)` | ❌ 少 `product` |
| **trae** | `(credential, product, fetcher?, …)` | `(credential)` | ❌ 少 `product` |
| **loomy** | `(credential, product, fetcher?)` | `(credential)` | ❌ 少 `product` |
| **lobsterai** | `(credential, product, **clientVersion**, fetcher?)` | `(credential, product)` | ❌ 少 `clientVersion` |
| codearts | `(credential, fetcher?)` | `(credential)` | ✅ |

**为什么危险**：这类错误**不在加载时报错** —— 函数拿到了 `undefined` 的
`product`，通常在里面某处才抛 `Cannot read properties of undefined`，
被 `catch` 吞掉后变成一条不起眼的失败消息。**5 个 provider 全中**。

**修复**：逐个按真实签名传参；lobsterai 的 `clientVersion` 通过
`auth.resolveClientVersion()` 取真值（带缓存），取不到才回退产品声明的
兜底版本。并把这些签名写进 `credits.mjs` 的注释表格 + `verify_jethub.py`
的断言，防止再凭记忆写。

### F-007 `finish_reason` 是对象，`String()` 后变成 `"[object Object]"`（已修）

**症状**：真实调用返回 `finish_reason: "[object Object]"`。

**根因**：`dsh-llm` 的 `FinishReason` 是**对象**，形如 `{ kind: 'stop' }`
（vendor 的权威范本写的是 `reason.kind === 'stop'`）。
首版 `collectStream` 里写了 `String(chunk.reason ?? 'stop')`，
把对象字符串化成 `"[object Object]"`。

**为什么这是真缺陷**：OCV 靠 `finish_reason === 'length'` 判定输出被截断
（`gemini_client.py:613` 抛 `GeminiOutputTruncated`）。拿不到 `length`
就**永远不会发现截断**，用户只在更下游看到「JSON 解析失败」这类误导性报错。

**修复**：
1. `collectStream` **保留原始对象**，不在那里 `String()`；
2. 新增 `finishReasonText()` 做对象→字符串归一化（认 `kind`/`type`/…）；
3. `mapFinishReason()` 扩充同义写法（`max-tokens`/`end_turn`/`completed`…）。

**顺带修的问题**：`usage.total_tokens` 恒为 0 —— 因为
`TokenUsage.totalTokens` 在 vendor 那边是**可选**字段，首版直接 `?? 0`。
现在缺了就自己用 `prompt + completion` 加出来，并额外回吐
`completion_tokens_details.reasoning_tokens`（排查「预算被思考吃光」的
唯一线索 —— 实测一个短提示就花掉 48 个 reasoning token）。

### F-008 无法导入 DSH 原生导出的备份（已修，用户报告）

**症状**：用户从 DSH 导出账号凭证后导入，报
「importBackup: 不是本插件导出的备份（schema 不匹配）」。

**根因**：我们只认自己的格式。两种格式的差异：

| | DSH 原生 | 本插件 |
|---|---|---|
| 标识字段 | `format: 'dsh-codearts-auth/backup'` | `schema: 'ocv-cloud-free-stack.jethub-backup'` |
| `credentials` | **平铺** `{ref: "JSON 字符串"}` | 嵌套 `{refs: {ref: {value, createdAt, updatedAt}}}` |
| 账号/黑名单 | **顶层** `accounts` / `disabledModels` | 包在 `accounts: {accounts, disabledModels}` |

**修复**：新增 `detectBackupKind()` + `normalizeBackup()`，**按标识字段**
（而不是「有哪些键」）判断来源，再归一化成内部形态。两种都收。

**刻意保留的严格性**：
* 只认这两个标识，**其它一律明确拒绝**并报出实际拿到的键 —— 导入的是凭据，
  写坏等于把用户账号搞丢，**宁可拒绝也不要猜**；
* 账号行做结构过滤（缺 `id`/`provider`/`credentialRef` 的行会让账号池写坏）；
* 归一化时统计 `missingCredentials` 与 `expiredAccounts`（对齐 DSH
  `backup.ts:159-170` 的语义），让面板能提示「这份备份里有几个账号不可用」。

**真实验收**：用用户提供的 18554 字节真实 DSH 备份实测 ——
**7 个账号 + 7 份凭据全部导入**，`source=dsh` 正确识别，
导入后 buddy 能列出模型、真实补全返回「可用」。

### F-019 模型下拉混入 6 家「没有账号」的模型（已修）

**症状**：语言模型页的「云端 OAuth 模型」下拉里有 **45 个**模型，
但实际只有 3 家登录过 —— 选中其余 30 多个只会得到
「还没有任何账号，请先登录」。

**根因**：`models.list` 对**没登录**的 provider 也会返回 vendor 的
**静态默认模型表**（这是 vendor 的正常行为，用于「登录前预览」）。
我原以为「`usable.length > 0` 就说明有账号」，于是 6 家零账号的
provider 全部通过了过滤。

实测数据：

| provider | 账号 | 模型 |
|---|---|---|
| buddy | 5 | 16 |
| **workbuddy** | **0** | 23 |
| **lobsterai** | **0** | 19 |
| **qoder** | **0** | 17 |
| **trae** | **0** | 28 |
| **cline** | **0** | 24 |
| loomy | 1 | 8 |
| **codearts** | **0** | 9 |
| atomcode | 1 | 7 |

**修复**：光看模型列表分不出有没有账号，所以让 `/rpc/status` 给每家
附上 `accountCount`，聚合层据此过滤。

**结果**：45 → **22** 个模型，只剩 buddy / loomy / atomcode 三家。

**教训**：

> 「接口返回了数据」不等于「这份数据可用」。
> 过滤条件要盯**业务可用性**（有没有账号），而不是**接口有没有报错**。

### F-020 `render()` 钩子插到了函数体外，成了死代码（已修）

**症状**：云端 OAuth 分页**首屏空白**，要手动切一次左栏才出内容。

**根因**：补丁脚本的锚点是

```js
    renderMisc();
    renderVideoStatus();
  }        ← 这个 `}` 是 **render() 自己的结束括号**
```

`_insert_after` 于是把 `renderJetHub()` 插到了**函数体外** ——
永远不会执行。而 `render()` 是首屏唯一会调它的地方。

**为什么没被发现**：`dev_render_check.mjs` 里「切到该分页」的断言
**自己会点一次分页按钮**，正好绕过了「首屏是否渲染」这条路径。
另外 JS 语法完全合法（函数体外的语句是合法的），无任何报错。

**修复过程（两次才对）**：
1. 第一次改成「锚定下一个函数声明、插在它前面」—— **仍不对**，
   因为那个锚点字符串带两空格缩进，而 `render()` 的结束括号就在两段之间；
2. 最终做法：仍用原锚点插入，再用 `_move_brace_after_hook()` 把那对
   「`}` + 钩子」的顺序对调，让结束括号回到函数末尾。

已加断言：解析 `panel/index.html`，逐字符平衡大括号，确认钩子落在
`render()` 的**函数体内**。

### F-021 提示条被 `renderGroup()` 擦除（已修）

**症状**：语言模型页的「当前来源」提示条**首次加载后就消失**
（探针实测 `hintExists: false`）。

**根因**：面板的 `renderGroup()` 会执行 `card.innerHTML = ""` 把卡片
清空重画。提示条被插进 `card-llm`，于是**每次渲染都被擦掉**。

**修复**：`jhEnsureSourceHint()` 用 `MutationObserver` 盯着卡片，
被清空就立刻补回来（同步补，避免闪烁）。

**教训**：

> 往 OCV 面板卡片里插**自定义 DOM** 时，必须假设它随时会被
> `renderGroup()` 清掉 —— 要么固定重建，要么挂观察器。

### F-022 提示文案设计：要显示「当前选用哪个模型」（用户要求）

用户要求：「当我语言模型中选择云端 OAuth 时，下面的自行填 api 的选项
直接就不在面板上显示就行了，然后另外**要显示当前选用哪个云端 OAuth 的模型**」。

实现：
* 切到云端 OAuth 时，`source: "custom"` 的字段**只隐藏、不卸载** ——
  DOM 与草稿值都留着，切回来时填过的 Key / 地址 / 模型都还在
  （卸载会让用户以为配置丢了，那是更糟的体验）；
* 提示条显示 **`提供商友好名 · 模型 id`**（如
  「CodeBuddy（腾讯） · hy4-preview」），并**校验可用性**：
  未选 → 警告；模型不在清单里 → 警告「可能已下线」；
  被「显示列表」关掉 → 警告「OCV 里选不到它」。

### F-023 去掉「刷新凭据」按钮（用户要求）

用户要求：「账号凭据刷新后台按照原来的参考项目后台自动刷新就可以了，
前端不需要自己刷新，去掉该按钮」。

**核实**：后台自动刷新**本来就正确**
（`resolveUsableCredential()` 在每次调用前检查过期，留 60 秒余量，
过期就按账号自己的 credentialRef 刷新再发请求）。
所以手动按钮确实多余 —— 它只会让人误以为「不点就不会刷新」。

**处理**：
* 账号卡上移除该按钮；
* 面板 handler 的 `refresh` 分支**保留**（桥的 RPC 还在），
  这样排障时仍能手动触发一次；只是不再暴露按钮；
* 账号卡的 `refreshable` 信息行保留（只读展示，让人知道可自动续期）。

### F-024 连通性测试不跟随「模型来源」，选了云端 OAuth 还是打商汤（已修，用户报告）

**症状**：用户在语言模型页选了「云端 OAuth」、保存、点「测试 JSON 输出」，
结果显示的仍是**商汤**的模型名。

**根因**：`test_llm()` 把商汤那一套**写死**在函数体里
（`config.sensenova_base_url()` / `sensenova_model()` / `sensenova_api_key()`），
**完全没读 `LLM_SOURCE`**。

**为什么比单纯报错更糟**：这个 bug **只影响测试按钮**，不影响真实 pipeline
（真实链路走 `LANGUAGE_PROVIDER`，由 `_sync_language_provider()` 同步）。
所以「测试通过」与「实际能用」会给出**互相矛盾**的结论 ——
用户会以为「云端 OAuth 没生效」，其实只是这个按钮从来没切过路。

**修复**：`test_llm()` 按 `LLM_SOURCE` 分成两个实现，且**明确标出在测哪一路**：

| `LLM_SOURCE` | 打哪里 | 凭据 | 模型 |
|---|---|---|---|
| `custom` | 面板填的服务地址 | `SENSENOVA_API_KEY` | `SENSENOVA_MODEL` |
| `jethub` | 本机桥 `127.0.0.1:<port>/v1` | 桥自管账号池 | `JETHUB_MODEL` |

云端那一路**刻意走 HTTP**（而不是直接调 `jethub_bridge.call` 内部函数）：
这样测的是 **OCV 将来真正会走的那条网络路径**，能覆盖
「桥没起来」「端口不对」这类真实故障 —— 只测内部函数会漏掉它们。

测试结果的 `detail` 里**始终标明来源与模型**，并在消息前缀带 `[云端 OAuth]`
—— 用户点「测试」就是为了确认「到底在用哪一路」。

**真机验收**（在浏览器里真的点了那个按钮）：

```
[云端 OAuth] JSON 约束生效，用时 3.8 秒
来源：云端 OAuth  地址：http://127.0.0.1:8801/v1/chat/completions
模型：loomy::GLM-5.3-Flash  输出：{"ok": true, "note": "pong"}
```

### F-025 body 注入器只认商汤 base，选云端 OAuth 时整段静默失效（已修）

**发现过程**：修完 F-024 顺手跑 `self_test`，发现 `1t-2`/`1t-5` 两项
**早已失败** —— 这一查牵出一个更严重的问题。

**根因**：`requests.post` 注入器判断「这是不是语言模型请求」时，
只比对 `config.sensenova_base_url()`：

```python
base = config.sensenova_base_url().rstrip("/")
if base and str(url).startswith(base) and ...:
```

选云端 OAuth 后请求打向**本机桥**（`127.0.0.1:<port>/v1`），
url 不以商汤地址开头 → 整个注入器**静默跳过**，于是这些全部失效：

* `max_tokens` 的「留 0 不指定」（退回 OCV 的 4096）；
* `reasoning_effort`（**深度思考档位**）；
* `CLOUD_STACK_LLM_EXTRA_BODY` 自定义字段。

**没有任何报错** —— 只表现为「设置看起来不起作用」。

**修复**：候选 base **同时包含两路**（按当前来源优先，另一路也留着 ——
面板改配置后环境变量立刻变，但**正在飞行中**的请求可能还持着旧 base）。

### F-026 `max_tokens` 的「下限」语义只写了文档没写代码（已修）

**症状**：面板帮助文字承诺「填正整数则作为**下限**生效（低于它会被抬到该值）」，
但实际填 32768 时请求体里仍是 OCV 传下来的 4096。

**根因**：注入器里只有「0 → 删掉 `max_tokens` 键」这一个分支，
**非零时什么都不做**。

**为什么长期没被发现**：`self_test` 的 `1t-5` 断言一直在守着这条，
但它跑在「来源 = custom」的默认路径上；而 F-025 让注入器在
**非默认路径**下也开始真正工作，才把这条暴露出来。

**修复**：补上 `if limit > current_num: merged["max_tokens"] = limit`。

**教训**：

> 修好一个「让代码开始生效」的 bug 之后，**必须重跑全套断言** ——
> 原先被掩盖的缺陷会一起浮现。这次 `1t-2`/`1t-5` 早就在失败，
> 只是没人看见。

### F-027 保存后互斥可见性丢失，界面退回「API Key」形态（已修，用户报告）

**症状**：用户在语言模型页选「云端 OAuth」、选好模型、点保存 ——
**界面立刻变回「自行填写 API Key」的形态**，必须关掉面板再打开才正常。

**根因**：`data-source` 是**运行时打在 DOM 上**的：

```js
wrap.setAttribute("data-source", field.source)
```

而保存流程是：

```
save() → state = payload.data → render() → renderGroup("llm")
       → card.innerHTML = ""      ← DOM 被整个重建
```

重建后的新 DOM **没有 `data-source` 属性** → 可见性逻辑无从判断 →
所有字段按默认显示（即 API Key 那一套）。

**为什么一直没被发现**：首版用**有上限的轮询**来打标：

```js
if (ready && tries > 30) clearInterval(timer);   // 30 × 400ms ≈ 12 秒后停
```

首次加载时轮询还在跑，所以「打开面板」总是正常的；
但**保存发生在轮询结束之后**，于是必然复现。
当时的渲染检查恰好没覆盖「保存之后」这个时序。

**修复**：轮询只负责**首次绑定**；打标交给 **`MutationObserver`** 长期守着
`card-llm` 的 `childList` —— 卡片被清空重画就立刻重打标。
（与 F-021 的提示条同一手法：**假设自定义 DOM 随时会被 `renderGroup` 清掉**。）

顺带修一处我这次改动引入的回归：清理条件被我写成
`if (ready && tries > 30)` —— `ready` 恒 false 时 interval 永不清理，
正是 F-014 描述过的形态。已改回无条件 `if (tries > 30)`。

**验证**（真机点「保存并立即生效」后比对）：

| 指标 | 保存前 | 保存后 |
|---|---|---|
| 来源 | `jethub` | `jethub` |
| API Key 字段 | 隐藏 | **隐藏** ✅ |
| 云端 OAuth 模型 | 显示 | **显示** ✅ |
| `data-source` 标记数 | 6 | **6** ✅ |

（修复前保存后 API Key 会变回可见、标记数归零。）

**教训**：

> OCV 面板的 `renderGroup()` 会 `innerHTML = ""` 重建卡片。
> 任何**运行时写在 DOM 上**的状态（属性、class、内联样式）
> 都必须假定**随时会被清掉** —— 要么写进 `state` 由渲染函数带上，
> 要么用 `MutationObserver` 盯着补回来。
> **有上限的轮询**只能保证「首屏正确」，覆盖不了「之后的每次重渲染」。

---

## 6. 验证记录

### 6.1 桥的纯函数单测（23/23）

`bridge.mjs`：`flattenContent` 四种输入、`toAdapterMessages` 的 system 摘出、
`buildGenerateOptions` 的 JSON 提示幂等、`mapFinishReason` 归一化、
`toOpenAiResponse` 的 usage/reasoning 映射。

### 6.2 专项自检 `verify_jethub.py`（46/46）

六节：模块导入与语法 / 配置访问器 / Node 运行时与桥进程 /
模型清单接入 / 面板字段与路由 / 面板 handler 端到端。

### 6.3 真实浏览器渲染 `dev_render_check.mjs`（15/15）

用 headless Chrome + CDP 直连（不依赖 puppeteer）：
分页按钮渲染、Jet Hub 分页出现且可点击、桥状态/账号/模型/备份四个卡片都有内容、
**`JETHUB_MODEL` 确实在 `tab-llm` 分页里且是 select 且有「自定义」兜底**、
无 pageerror、无 console.error。

### 6.4 端到端链路（假凭据）

| 场景 | 期望 | 实测 |
|---|---|---|
| 无账号 | 503 + 提示登录 | ✅ `provider buddy 还没有任何账号，请先在插件面板里登录` |
| 有效假凭据 | 走到网络层 | ✅ 打到 `copilot.tencent.com` 返回 401 openresty |
| 过期假凭据 | 尝试按账号 ref 刷新 | ✅ 走 `refreshAccountCredential` 路径 |
| 账号列表 | 不含凭据明文 | ✅ 只回 nickname/enabled/expiresAt/refreshable |
| 备份往返 | 导出→恢复 | ✅ schema 校验 + 凭据/账号计数正确 |
| 清理 | 删除后归零 | ✅ |

### 6.5 安全矩阵

| 请求 | 结果 |
|---|---|
| `Host: evil.example.com`（DNS rebinding） | **403 拦截** |
| `Origin: https://evil.example.com` | **403 拦截** |
| `sec-fetch-site: cross-site` | **403 拦截** |
| `Host: 127.0.0.1:8801` | 放行 |
| `Host: localhost:8801` | 放行 |
| 面板跨端口调用（Origin `:8799`） | 放行 |

### 6.6 存储落点（硬约束）

```
stateDir           = <插件>/state
poolPath           = <插件>/state/jet-hub/state.json        poolInPlugin = true
credentialsPath    = <插件>/state/credentials.json          credentialsInPlugin = true
```

---

## 7. 配置项登记

| 键 | 默认 | 作用 |
|---|---|---|
| `JETHUB_ENABLED` | 1 | 总开关；关掉后桥不拉起，OCV 看不到它 |
| `JETHUB_PROVIDER` | buddy | 使用哪个 provider（目前只接线 CodeBuddy） |
| `JETHUB_BRIDGE_PORT` | 8801 | 桥端口（避开 8010/5173/8799）；改动需重启后端 |
| `JETHUB_AUTOSTART` | 1 | 随 OCV 后端自动拉起桥 |
| `JETHUB_BRIDGE_TIMEOUT_MS` | 110000 | 单次补全软超时，**刻意 < OCV 的 120 s 读超时** |
| `JETHUB_MODEL` | （空） | 面板选中的 Jet Hub 模型 id |
| `JETHUB_LLM_FALLBACK` | （空） | 离线静态回退模型表（JSON），空则用内置三项 |

---

## 8. 已知限制与遗留

1. **尚未用真实账号登录验收。** 全链路已用假凭据验证到「真实打到上游并拿到
   401」，但**成功路径（真账号拿到模型清单、真跑一次补全）未验证** —— 需要
   一个真实 CodeBuddy 账号。这是当前**最大的未验证项**。
2. **只接线 CodeBuddy。** 其余 7 个 provider 的 lib 在 vendor 里但未接线；
   接线时在 `runtime.mjs` 的 `PROVIDER_SPECS` 加一行，并照着
   `index.ts` 的同名 provider 段落抄三个函数。
3. **桥的 `/v1/chat/completions` 不支持流式**（明确返回 400）。OCV 用不到，
   但若将来要接支持流式的消费方需另做。
4. **图片输入不支持**：不提供 `attachments`，适配器会报 UNSUPPORTED_CONTENT；
   桥已把它降级成文本占位（`[图片已忽略：本插件仅支持文本模型]`）。
5. **账号池单进程假设**：不可多 worker / 多实例共用一个 `state/`，
   否则 `state.json` 会互相覆盖（这是 vendor 的设计前提，非本插件引入）。
6. **备份含凭据明文**。仅存插件目录，文件名经白名单过滤（防路径穿越），
   但**不做加密**。若要跨机器传输，用户需自行加密。
7. **`vendor/` 是开发机产物**：目标机不需要 npm/网络，但升级
   `dsh-codearts-auth` 需在开发机重跑 `vendor_tool.py`。
8. **`dev_*.py` / `dev_*.mjs` 是开发期工具**，不属于运行时；可安全保留。

---

## 9. 1.12.0：多提供商 + 来源互斥 + 面板重做（本次迭代）

### 9.1 用户四项要求与实现

| # | 要求 | 实现 |
|---|---|---|
| 1 | 适配其他 LLM 提供商 | **9 家全部接线**（原只 buddy）：`PROVIDER_SPECS` 通用化 + `CODEARTS_SPEC`/`ATOMCODE_SPEC`（两家无 product 对象）；模型目录合计 **132 个** |
| 2 | 面板改名 | 「Jet Hub 账号」→「**云端 OAuth**」；「Provider」→「**提供商**」 |
| 3 | 恢复凭据改为本地上传 | `backup.export` 支持 `download` 模式（回文件本体 → 浏览器 `Blob`+`<a download>`）；`backup.import` 接受**上传的 `document`**，不再要求文件放进固定目录 |
| 4 | 面板参考原 Jet Hub + 保留一键签到 | 提供商左栏、账号卡（键值网格）、模型开关（`appearance:none` 自绘）、三态积分、**一键签到**、通知条；样式移植自 `jet-hub-styles.js` |

### 9.2 追加的两项设计决策（用户后续澄清）

**A. 语言模型与云端 OAuth 互斥，用下拉选来源**

> 用户原话：「语言模型和云端OAuth也就是原Jet Hub两项，两者都是LLM不能同时使用，
> 在语言模型中给用户下拉选择，是自行用api还是云端OAuth」

实现：新增 `LLM_SOURCE` 字段（`custom` / `jethub`），由
`config.resolved_language_provider()` 推导出 OCV 的 `LANGUAGE_PROVIDER`。
字段级打 `source` 标记（`both`/`custom`/`jethub`），前端按当前来源**只显示相关字段**。

> ⚠ `LANGUAGE_PROVIDER` **刻意从 `_CONFIG_VIEW_MAP` 移除** —— 留着它会让
> 该键成为第三个真相源，出现「界面选了云端 OAuth、这个键还停在 sensenova」的错配。

**B. 模型选择留在语言模型页，云端 OAuth 页只管账号**

> 用户原话：「语言模型应可选择使用云端OAuth中的具体哪个模型。云端OAuth用于
> 配置账号等功能，并不用于选择具体使用哪个模型」

实现：`JETHUB_MODEL` 字段**放在 llm 分组**（紧随商汤模型之后），
清单由 `models_catalog` 的 `llm_jethub` 用途**聚合全部提供商的可用模型**。

**关键编码：`<provider>::<model>`**

不同提供商的模型 id 会**重名**（`deepseek-v4-flash` 在 CodeBuddy、LobsterAI、
Loomy 都存在）。若只列裸 id，桥无从知道该把请求发给哪一家。所以：

```
value = "buddy::deepseek-v4-flash"
label = "CodeBuddy（腾讯） · DeepSeek V4 Flash"
```

OCV 把它当 `model` 发过来 → 桥的 `parseModelRef()` 按 `::` 拆开 →
路由到对应 provider，并把**裸 id** 传给上游（上游只认裸 id）。

### 9.3 修复的三个真实缺陷（侦察发现）

| # | 缺陷 | 后果 | 修法 |
|---|---|---|---|
| 1 | `listModels` 调 `auth.fetchModels(pool)`，但 **qoder/cline/loomy 没有这个方法** | 静默返回空清单 → 用户看到「没有模型」 | 改用 `adapter.listAllModels()`（**8 个适配器全都有**），`auth.fetchModels` 仅作回退 |
| 2 | `startProviderLogin` 把 loomy 归到「有 `login()`」类，但 **loomy 两个都没有** | 抛「没有可用的登录入口」→ loomy 完全不可用 | 加「类别 2.5」专分支：`startWechatLogin()` + **必须再调** `persistWechatLogin()` |
| 3 | 有效期只认毫秒数字串，而 **CodeArts 用 ISO 字符串** | 账号卡永远显示不出过期时间 | 两种编码都试（`Number()` → 失败再 `Date.parse()`） |

### 9.4 一键签到：两条铁律

1. **跨渠道必须串行** —— 原版注释（`jet-hub.js:1659-1661`）记录：单渠道内部已逐账号
   顺序执行以免风控，**跨渠道并发会同时发出多路真实领积分写请求**。所以
   `credits.mjs` 的 `claimAll` 用 `for...await`，**绝不 `Promise.all`**。
   `verify_jethub.py` 有一条断言专门守它。
2. **能力门控是「不请求」，不是「请求后吞错」** —— 不支持就不发请求
   （原版 `credits-capabilities.js:9-14` 记录：无门控地调 `credits.balances`
   让 CodeArts 每次开面板都报错）。能力表在 `PROVIDER_SPECS` 里声明：
   `workbuddy` 与 `cline` **不支持签到**，`loomy` **独有新手任务**。

### 9.5 面板视觉：移植策略

从 `jet-hub-styles.js` 移植，但做三处**必要适配**：

1. **预计算颜色，不用 `color-mix()`** —— 原版 15 处 color-mix 需 Chrome 111+，
   而原 bundle 的 target 是 chrome100。这里把混合结果算好写死。
2. **走本面板自己的 CSS 变量**（`--bg/--panel/--text/--muted/--border`）→
   自动跟随本面板深浅色；原 Jet Hub **不支持深色模式**。
3. 类名前缀 `jh-`，与既有 `.card/.mini/.kv` 并存不冲突。

**核心洞见**（来自侦察）：观感 99% 在 CSS、交互状态 100% 通过 `data-*` 暴露 ——
所以 JS 只负责把状态属性写对（`data-enabled / data-tone / data-disabled /
aria-selected / checked`），样式表原样工作。

### 9.6 本次验证

| 项目 | 结果 |
|---|---|
| `verify_jethub.py` | **80/80**（原 46 项，新增 34 项覆盖多提供商/互斥/签到/上传备份） |
| `dev_render_check.mjs`（真浏览器） | **27/27**（含「切换来源时两路字段互斥显示」「JETHUB_MODEL 位于 tab-llm」） |
| `verify_panel_render.py`（既有回归） | **22/22** |
| `self_test.py`（主自检） | **EXIT 0** |
| 9 家提供商模型目录 | **132 个**（修好 listModels 前 qoder/cline/loomy 均为 0） |

### 9.7 文件变更

| 文件 | 变更 |
|---|---|
| `jethub/runtime.mjs` | `PROVIDER_SPECS` 扩到 9 家；`listModels` 改用 `adapter.listAllModels()`；通用 `startProviderLogin`（含 loomy 专分支）；有效期双编码 |
| `jethub/credits.mjs` | **新增**：积分/签到控制器（串行 + 能力门控 + 三态语义） |
| `jethub/bridge.mjs` | 新增 `parseModelRef()`；`complete()` 按 `provider::model` 路由 |
| `jethub/server.mjs` | 新增 `models.setAllDisabled`、`credits.*`；备份改 download/upload 双模式 |
| `ocv_cloud_stack/panel.py` | 新增 `LLM_SOURCE` 与字段 `source` 标记；`JETHUB_MODEL` 移到 llm 分组；新增 `jethub_credits` |
| `ocv_cloud_stack/config.py` | 新增 `llm_source()` / `resolved_language_provider()` / `jethub_bridge_base_url()` |
| `ocv_cloud_stack/patches.py` | 注册 `jethub` provider；新增 `_sync_language_provider()`；`LANGUAGE_PROVIDER` 移出 `_CONFIG_VIEW_MAP` |
| `ocv_cloud_stack/models_catalog.py` | `_fetch_jethub` 改为**聚合全部提供商** + `provider::model` 编码 |
| `ocv_cloud_stack/image_shim.py` | 新增 `/api/panel/jethub/credits` 路由 |
| `panel/cloud-oauth.{css,html,js}` | **新增**：面板资源（独立文件，便于编辑器高亮与断言） |
| `dev_patch_panel_html.py` | 重写：从 zip 还原 + 读三个资源文件 + 打五处补丁 + 15 项校验 |

---

## 10. 下一步（按优先级）

1. **真实账号验收**（阻塞项）：登录任一提供商 →
   `verify_jethub.py` 第 4 节应看到非空模型清单 →
   面板「对话测试」应成功 →
   在 OCV 界面把「模型来源」切到云端 OAuth、选一个 `provider::model` 跑通一个 Agent 阶段。
2. 把 `JETHUB_*` 加进 `cloud_stack_ctl.py` 的 `_BLOCK_KEYS`（可选；不加则
   卸载时无需还原，更干净）。
3. 账号体检（`account-probe`）：原版的分派表**只覆盖 4 条线**
   （buddy/workbuddy/lobsterai/trae + else=codearts），
   对 qoder/cline/loomy/atomcode 会**当成 CodeArts 发华为云签名请求 → 必然失败**。
   若要用「重测账号」，必须在桥侧自建分派。
4. **备份加密**：原版 `backup-crypto.js`（PBKDF2+AES-GCM，108 行，纯 Web Crypto、
   零依赖）可原样搬，实现「导出带口令」。

---

*最后更新：2026-09-28（1.12.0：9 家提供商 + 来源互斥 + 面板重做 + 本地上传备份）*
