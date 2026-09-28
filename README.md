# OCV 全云端免费栈插件

> 🏠 **宿主项目：[One-Click VidGen（OCV / 一键成片）](https://github.com/IFRIT-Zhou/One-Click-VidGen)**
>
> OCV 是一个开源（AGPL-3.0-only）的 AI 视频生产工作台：FastAPI 后端 + Vue 前端，
> 覆盖「文案 → 配音 → 字幕识别校对 → 语义分镜 → 生图 → 精修 → 时序 → BGM →
> 视频合成」的完整流水线，原生支持本地 GPU（IndexTTS-2.5）与多种云端接口。
>
> 本插件是 OCV 的扩展插件：在**没有 NVIDIA 显卡**的机器上，把配音（MiMo TTS）、
> 语言模型与分镜生图（商汤 SenseNova）三层替换为云端免费接口，让 OCV 全流程
> 照常跑通，且**不修改一行 OCV 源码**。使用前请先获取并安装
> [OCV 本体](https://github.com/IFRIT-Zhou/One-Click-VidGen)，再把本插件装入其
> `plugins/` 目录。
>
> 本插件为独立扩展项目，非 OCV 官方发布；OCV 品牌使用边界见
> [TRADEMARKS.md](./TRADEMARKS.md)。

> **English summary** — OCV Cloud Free Stack is a community plugin for
> [One-Click VidGen (OCV)](https://github.com/IFRIT-Zhou/One-Click-VidGen), an
> open-source (AGPL-3.0-only) AI video production workbench. It swaps the TTS,
> LLM, and storyboard-image layers to free cloud APIs (MiMo TTS, SenseNova), so
> the whole text-to-video pipeline runs on machines **without an NVIDIA GPU**.
> It hooks the OCV runtime at interpreter startup and **modifies zero lines of
> OCV source code**; deleting the plugin directory uninstalls it completely.
> This is an independent extension, not an official OCV release. Licensed
> AGPL-3.0-only. Install OCV first, then drop this plugin into `plugins/`.

> ## ⛔ 改这个插件之前必读（两条最高铁律）
>
> **A. 禁止直接改动 OCV 源码。** 只能修改 `plugins/cloud_free_stack/` 下的文件。
> OCV 源码＝`module*.py`、`story_agents.py`、`backend/`、`frontend/`、`tools/`、
> `dev/`、`tests/` 等一切非插件目录。需要改行为时，用**包装 / monkeypatch / 注入**，
> 不要编辑源码 —— 源码改动会被下次软件更新覆盖，也无法通过卸载还原。
>
> **B. 所有对 OCV 的改动操作都必须通过注入 / 卸载脚本完成。**
> 唯一入口是 `cloud_stack_ctl.py` 的 `install` / `uninstall` / `inject` / `reinject`
> （或 `重新注入云端免费栈.bat`）。**不要手工编辑** `.env`、`frontend/index.html`、
> `.pth` 锚点 —— 它们是脚本的受管产物，手工改会破坏"卸载即可完全还原"。
>
> 完整铁律、受管文件白名单、越界自查命令见内部台账 **`docs/DEVLOG.md` §0**
> （维护者本地文档，不随本仓库分发）。

在一台**没有 NVIDIA 显卡**的机器上，用**云端免费接口**跑通 One-Click VidGen 的完整流水线：

```
文案 → 配音 → 字幕识别校对 → AI 分镜 → 生图 → 时序 → 视频合成
```

| 环节 | 原生方案 | 本插件换成 | 改动方式 |
|---|---|---|---|
| 配音 TTS | IndexTTS-2.5（本地 GPU） | **MiMo `mimo-v2.5-tts`**（限时免费） | 新增一个并列的配音引擎选项（不覆盖原生 Qwen-TTS） |
| 语言模型 | Gemini / 三方中转 | **OpenAI 兼容接口**（默认商汤 `deepseek-v4-pro`，可指向任意 OpenAI 兼容服务商） | 新增一个 LLM provider |
| 分镜生图 | RunningHub | **商汤 `sensenova-u1.5-lite`** | 本地协议 shim 转译 |
| 语音识别 ASR | faster-whisper | 不变（本机 CPU） | — |
| 视频渲染 | FFmpeg | 不变（本机 CPU x264） | — |

> **OCV 源码一行都不用改。** 插件通过解释器启动阶段的注入锚点挂载，
> 删掉插件目录即完全卸载。
>
> 唯一的例外是**前端入口页加了一行带标记的 `<script>`**（右下角那个配置按钮），
> 由 `cloud_stack_ctl.py` 可逆管理，`uninstall` / `inject --remove` 会原样移除。
>
> ⚠ **OCV 每次软件更新都会把这一行抹掉** —— `frontend/` 是被整体替换的源码目录，
> 不在启动器 `update.json` 的 `protected_data` 保护范围内。插件会**自动重建**它
> （见下文「软件更新后按钮消失」），也可以手工跑 `reinject` 或双击
> `重新注入云端免费栈.bat`。

> 📒 **开发台账：`docs/DEVLOG.md`（内部文档，不随本仓库与发行包分发）。**
> 本插件的一切改动（代码 / 配置 / 面板 / 脚本 / 开关默认值）都必须**先在台账登记**
> 再落到代码。台账由维护者在本地保留，内含铁律、模块职责与不变量、改动登记表、
> 故障档案、版本记录与配置项登记。

---

## 配置面板（改配置不用碰 .env）

OCV 前端没有配置 TTS 的地方，所以插件自带一个配置面板：

**打开方式**（任选其一）

* 启动 OCV 后点界面**右下角「云端免费栈」按钮** —— 按钮上的圆点就是配置状态
  （红 = 还缺 Key，绿 = 已就绪，黄 = 面板服务没响应）
* 命令行：`cloud_stack_ctl.py panel`

**面板能改什么**

| 分页 | 内容 |
|---|---|
| MiMo 配音 | API Key、服务地址、模型、默认音色、重试次数、**克隆音色**（上传参考音频），以及**试听测试** |
| 音色映射 | OCV 的 48 个音色名 → MiMo 的 8 个预置音色或克隆音色，逐项下拉（含"按性别自动映射"） |
| 语言模型（OpenAI 兼容） | **「模型来源」下拉（二选一互斥）**、API Key、服务地址、模型、**云端 OAuth 模型**、最大输出 token、**深度思考档位**、**30 档阶梯重试开关与次数**、**Agent 1B 容错 / 单镜跳过**，以及 **JSON 输出测试** |
| 图片模型（商汤） | 图片 API Key（**独立配置**，留空与语言模型共用）、图片服务地址、图片模型、水印开关、提示词扩写开关、参考图上限、并发数，以及**出图测试** |
| **云端 OAuth** | 本机桥的启动/停止、**9 家提供商的账号管理**（登录/启停/删除，**凭据由后台自动刷新**）、**一键签到**与积分、**模型显示列表**（黑名单制）、**对话测试**，以及**备份 / 恢复**（直接展开在本页，导出可下载到本地，恢复从本地上传） |
| 运行状态 | shim 地址、OCV 侧被改写的原生键、配置文件路径 |

### 云端 OAuth（9 家免费额度 LLM）

**语言模型只有一路在用。** 在「语言模型」分页顶部的 **「模型来源」** 下拉里
二选一：

| 选项 | 用什么 | 模型从哪来 |
|---|---|---|
| **自行填写 API** | 你填的服务地址 + Key | 该服务商的 `/models` 在线清单 |
| **云端 OAuth** | 本机桥（下方账号里登录的免费额度） | 你已登录账号的可用模型，**9 家聚合在一个下拉里** |

选哪个，界面就只显示哪一路的字段 —— 不会出现「两套都填了、走哪个看运气」。

**云端 OAuth 支持 9 家**：CodeBuddy、WorkBuddy（腾讯）、LobsterAI（有道）、
Qoder（阿里）、TRAE（字节）、Cline、Loomy（讯飞）、CodeArts（华为云）、
AtomCode（AtomGit）。模型清单**只列你已登录的那些家**的可用模型
（没账号的不会出现，免得选中后才发现用不了）。

> **「云端 OAuth」分页只管账号**（登录、启停、签到、积分、模型开关、备份）；
> **选具体用哪个模型在「语言模型」分页** —— 那里的提示条会一直显示
> **当前选用的是哪个模型**（如「CodeBuddy（腾讯） · deepseek-v4-flash」），
> 并在模型已下线或被关掉时给出提醒。模型项形如
> 「CodeBuddy（腾讯） · DeepSeek V4 Flash」—— 前缀标明它属于哪家，
> 因为不同提供商的模型 id 会重名。

* **完全本机、不依赖 DSH**：vendor 里打包了 `dsh-codearts-auth`（MIT）及其
  依赖闭包，桥用 OCV 自带的 `runtime/node/node.exe` 跑；
* **凭据只存插件目录**：`<插件>/state/credentials.json` 与
  `<插件>/state/jet-hub/state.json`，**不读也不写 `~/.dsh`**；
* **凭据自动刷新，无需手动操作**：每次调用前检查过期（留 60 秒余量），
  过期就自动按该账号续期再发请求 —— 所以面板上**没有**「刷新凭据」按钮；
* **多账号 + 自动轮换**：额度耗尽时自动切下一个账号；
* **一键签到**：跨提供商**串行**执行（并发会触发风控）；
  签到能力按各家实际支持情况显隐 —— WorkBuddy 与 Cline 没有每日签到；
  Loomy 另有「新手任务」可单独领；
* **可从 DSH 导出的备份直接恢复**：同时认本插件与
  `dsh-codearts-auth`（DSH）两种备份格式；
* **备份 / 恢复**：**导出**可「下载到本地」（浏览器直接存文件）或留档到插件目录；
  **恢复**从**本地上传**备份文件即可，不需要把文件放进任何固定目录。
  备份**含凭据明文**，请自行保管。

**模型字段是在线清单，不是写死的。** 打开面板会自动调服务商的
`/models` 接口拉取**当前账号实际可用的全部模型**（带上下文长度、
输出模态等标注），点开下拉直接选即可；清单带磁盘缓存（1 小时），
拉取失败时回落到缓存或静态预置表，并可手动输入自定义值。
撞到 429 / 想换模型时（比如 `deepseek-v4-pro` 限流换
`deepseek-v4-flash`），改下拉 → 保存就生效，不用重启 OCV。
**三个关键设计**

1. **面板写的是 `var/config.json`，不是 `.env`。** 读取优先级是
   `var/config.json` > `os.environ` > `.env` > 内置默认值。
   面板层故意放在最上面：`os.environ` 里的值是**进程启动时的快照**，
   改 `.env` 对已经跑起来的 OCV 不生效，而面板层按 mtime 失效，
   **改完立刻生效，不用重启 OCV**（连正在合成的配音都会用上新音色）。
   `.env` 保持为 install 时的基线，卸载时干净还原，两件事互不干扰。
   **保存时会实时把面板值同步进宿主进程的 `os.environ`**：Agent 1 的
   语言模型调用在后端进程内执行（`pipeline` 直接 `import story_agents`），
   而 `gemini_client.language_model()` 调用时读的是环境变量 —— 不同步
   的话「面板换了模型，正在跑的后端还在用旧模型」（实测踩过：
   13:02 保存 `glm-5.2`，13:06 Agent 1 仍在打 `deepseek-v4-pro`）。
   删除面板键时会把此前由面板提供的环境键一并摘除，回落到内置默认。
2. **面板层会同步给 OCV 自己的凭据校验。** OCV 的提交门禁、preflight、
   前端"已就绪"状态灯都读 `main.py` 的 `_project_config_values()`，
   而它只认 `.env` + `os.environ`。插件接管了这个函数，把面板层叠加上去
   （含 `DASHSCOPE_API_KEY ← MIMO_API_KEY` 这条别名 —— qwen 槽位既然已被
   MiMo 接管，这个键的凭据语义也跟着换了）。不叠这一层的话，你在面板里
   填完 Key，前端仍然会判定"未配置"而拒绝提交。
3. **面板层还会被物化进 `os.environ` —— 这样子进程才看得见。** 光有第 2 条
   不够：OCV 里有一大类原生校验是**直接读 `os.environ`** 的，最典型的就是
   `gemini_client.language_provider_configured()`（Agent 0 开头那道
   "语言模型未配置"的门禁正是它）；而阶段子进程（module1 / 2 / 4 / 5）只
   继承环境变量。面板层原先只对插件自己的 `config.get()` 可见，于是会出现
   **「面板里明明填了商汤 Key，Agent 0 却报 *语言模型未配置*」**。
   所以 `bootstrap` 会在每个进程启动时把面板层的**非空**值写进 `os.environ`：
   时机早于 `load_project_env()`，而后者是「key not in os.environ 才写入」，
   因此面板值天然压过 `.env`，与第 1 条声明的优先级完全一致。
   空值刻意不写 —— `os.getenv(key, default)` 只在键**不存在**时才用默认值，
   键存在而值为空会让下游拿到 `""`（例如 module2 的
   `os.getenv("ASR_DEVICE", "auto")` 会直接抛 ValueError）。

**清空覆盖层**：面板底部的「清空覆盖层」按钮，或直接删 `var/config.json`，
所有配置立即回退到 `.env` 与默认值。

---

## 为什么这不是一个"正规"的 OCV 插件

OCV 的 `plugins/` 目录目前是**纯清单占位框架**：它只读取 `plugin.json` 并展示，
**不会导入或执行任何插件代码** —— 后端接口明确返回 `execution_enabled: False`，
仓库里也没有 `importlib` / hook / 事件注册。把代码放进 `plugins/` 是永远不会被执行的。

所以本插件换了一个仍然"自包含、可整体删除"的挂载点：

```
runtime/python/Lib/site-packages/ocv_cloud_free_stack.pth   ← 注入锚点
        └─ 把 plugins/cloud_free_stack 加进 sys.path
                └─ site.py 紧接着自动导入 sitecustomize.py
                        └─ 装上导入钩子，等目标模块出现就地打补丁
```

关键原理：CPython 处理 `.pth` 的时机（`addsitepackages`）**早于**
`execsitecustomize()`，所以这两步能可靠串联。而 OCV 的每个重活阶段
（配音 / 识别 / 生图 / 合成）都是用 `sys.executable` 拉起独立子进程，
它们全都跑同一个解释器 —— 也就全都会走到注入点。

**验证过的注入效果**：主进程、`module1_agent_director.py`、`module4_video_render.py`
等每一层都能拿到补丁后的实现，不需要改任何启动脚本。

---

## 安装

### 0. 先装 OCV 本体

本插件是 OCV 的扩展，**不能独立运行**。请先获取并安装宿主项目
[One-Click VidGen（OCV / 一键成片）](https://github.com/IFRIT-Zhou/One-Click-VidGen)：

```bat
git clone https://github.com/IFRIT-Zhou/One-Click-VidGen.git
```

按该项目 README 完成源码部署或安装 Windows 整合包，然后进入下一步。

### 1. 放入插件目录

把本仓库（或 Release 里的 `OCV-Cloud-Free-Stack-v*.zip`，解压后是
`cloud_free_stack/`）放进 OCV 根目录的 `plugins/` 下，最终路径为：

```text
<OCV 根目录>\plugins\cloud_free_stack\
```

### 2. 写入配置

```bat
cd /d E:\1B1BLaoYang
runtime\python\python.exe plugins\cloud_free_stack\cloud_stack_ctl.py install
```

这一步会：

* 把 `.env` 备份成 `.env.cloud_stack_backup`
* 写入插件配置块（`CLOUD_STACK_*` / `MIMO_*` / `SENSENOVA_*`）
* 把 `LANGUAGE_PROVIDER` 改成 `sensenova`
* 把 `IMAGE_API_BASE_URL` 指向本机 shim（**必须写进 .env** —— OCV 的
  `_load_runninghub_env_from_file()` 会把不在 `.env` 里的 `IMAGE_*` 变量从
  环境里 pop 掉，靠临时环境变量是不生效的）
* 写入 `runtime/python/Lib/site-packages/ocv_cloud_free_stack.pth`
* 往 `frontend/index.html` 注入一行 `<script src="…/panel.js" defer>`
  （前后带 `<!-- cloud_free_stack BEGIN/END -->` 标记，可逆）

### 3. 填入 API Key

**推荐**：启动 OCV，点界面右下角「云端免费栈」按钮，在面板里填
MiMo Key 与商汤 Key，点「保存并立即生效」。

也可以继续手工编辑 `.env`（作为基线）：

```dotenv
MIMO_API_KEY=<小米 MiMo 的 Key>
SENSENOVA_API_KEY=<商汤的 Key>
```

> 一个坑：`MIMO_API_KEY` 为空时模块 1 会直接报
> `未配置 MIMO API Key`。这句话是插件抛的，来源就是面板/`.env` 里这两个键。

### 4. 自检

```bat
runtime\python\python.exe plugins\cloud_free_stack\cloud_stack_ctl.py check
```

会依次验证：配置完整性 → 商汤 LLM（带 `response_format: json_object`）→
MiMo TTS → 商汤图片（经本地 shim 真实出一张图）。
如果只想跳过耗额度的出图测试，加 `--skip-image`。

### 5. 启动

**用你平时的入口启动就行**（`双击启动.bat` / `OCV_Launcher.exe`），插件会自动挂载。

也可以走专用入口 `启动_云端免费栈.bat`，它会先打印配置状态、预先把 shim 拉起来，
再调用原来的 `start_windows.bat`。

启动后在 OCV 界面里：

* **配音引擎**在「执行方式」下拉里选 **`MiMo TTS（云端免费栈）`**。
  1.10.0 起它是**独立的第 4 个选项**，和原生 `Qwen-TTS` **同时可见、互不覆盖**：
  选 MiMo 就走 MiMo，选 Qwen-TTS 就走 OCV 原生的 DashScope。
  （"系统音色"随便选一个，插件按音色映射表转成 MiMo 音色；"配音描述"会作为
  风格指令传给 MiMo）
* **语言模型**下拉里会出现「OpenAI 兼容接口（云免费栈）」—— 默认打商汤，
  把面板里的服务地址/Key/模型换成其它 OpenAI 兼容服务商即可整体切换
* 生图不用管，已经通过 `IMAGE_API_BASE_URL` 转到本地 shim
* 右下角有「云端免费栈」按钮，随时改配置

> **插件代码改动需要重启 OCV**（Python 模块按进程加载）；但**配置改动不需要** ——
> 面板写的是被 mtime 感知的 JSON 层，下一个进程/下一句配音就读到新值。

---

## 三个适配器的实现细节

### 1. TTS：MiMo 作为**独立引擎**（1.10.0 起）

> **历史**：1.9.x 及以前，MiMo 是**顶替** `Qwen-TTS` 槽位的（选 Qwen 实际调 MiMo）。
> 因为用户要求「两个选项同时可见」，1.10.0 改成了**并列**：
> `MiMo TTS（云端免费栈）` 与 `Qwen-TTS` 是两个独立选项，选哪个用哪个。
> 想回退旧行为：面板/环境变量 `CLOUD_STACK_MIMO_SEPARATE_ENGINE=0`。

并列怎么实现的（三段）：

1. **前端**（`panel/panel.js`）：在每个「执行方式」`<select>` 里**插入**一个
   `option[value="mimo"]`，并**还原**被旧版改写成「MiMo TTS」的 qwen 文案
   —— 只加不还原的话，两个选项都会叫 MiMo。
2. **后端校验**：`tts_engine` 是 Pydantic `Literal`，而 FastAPI 在**路由注册时**
   就固化了校验器，所以光 `model_rebuild` **无效**，必须连
   `route.dependant.body_params[]._type_adapter` 一起重建（见 DEVLOG 铁律 58）。
3. **运行时分流**：`module1_agent_director.py` 的 `--tts-engine` 是 argparse
   `choices`（OCV 源码，不可改），所以 `mimo` 任务在 `run_pipeline` 入口被
   **归一化成 `qwen`** 并留痕 `_cloud_stack_tts_engine`；子进程按环境变量
   `CLOUD_STACK_MIMO_ACTIVE=1` 把 `synthesize_to_file` 换成 MiMo 实现。

这样 MiMo 复用了整条已验收的云端配音链路（断句、`[QWEN_TTS_PROGRESS]` 进度、
逐句下载合并、结构化留白），只换合成实现；而选 `qwen` 时插件**一行都不碰**，
回到 OCV 原生 DashScope。

`module1_agent_director.py` 里是 `from backend.app.qwen_tts import ... synthesize_to_file`，
**绑定的是函数名**，所以替换必须打在 `backend.app.qwen_tts` 的模块属性上，
**并且连 `module1_agent_director` 自己的命名空间一起换**（铁律 29），
而不是去 patch `module1_agent_director`（它是 `__main__`，走不到导入钩子）。

签名保持完全一致：

```python
synthesize_to_file(*, text, destination, instructions="", voice=..., language_type=None, ...)
```

接口映射（**正文必须放 `assistant` 角色**，风格指令放 `user`）：

| OCV 入参 | MiMo 位置 |
|---|---|
| `text` | `messages[role="assistant"].content` |
| `instructions`（配音描述） | `messages[role="user"].content` |
| `voice` | `audio.voice` |
| 返回 | `choices[0].message.audio.data`（base64） |

两个注意点：

* **鉴权头同时发两个**。官方 cURL 示例用 `api-key:`，官方 Python SDK 用
  `Authorization: Bearer`。两个都是官方文档原文，所以这里两个都发，避免 401 反复排查。
* **不要用 voiceclone 做长视频**。`mimo-v2.5-tts-voiceclone` 要把参考音频
  base64 内联进**每一次**分片请求，几十上百个分片会把带宽打爆。
  主路径用 `mimo-v2.5-tts` + 自然语言风格指令。

`_normalize_qwen_audio` / `_normalize_qwen_loudness` 由 module1 自己完成
（统一转 24 kHz 单声道 pcm_s16le + loudnorm），插件不重复做。

### 2. LLM：新增 `sensenova` provider

插进 `backend/app/gemini_client.py` 的 `LANGUAGE_PROVIDER_OPTIONS`：

```python
"sensenova": {
    "source": "official",          # ← 关键，见下
    "protocol": "openai",
    "key_env": "SENSENOVA_API_KEY",
    "default_base": "https://token.sensenova.cn/v1",
    "default_model": "deepseek-v4-pro",
    ...
}
```

**为什么必须是 `official` 而不是 `custom`**：`_generate_openai_compatible_text`
里有这么一段分支：

```python
if config.get("source") == "official" or provider in {"deepseek","openai","qwen","kimi","glm"}:
    payload.update(extra_body)          # 平铺到请求根部
elif extra_body:
    payload["extra_body"] = extra_body  # 整体包成子对象
```

走 `custom` 会被包成 `extra_body`，**商汤在请求根部收不到
`response_format: {"type":"json_object"}`，JSON 约束静默失效** ——
而 Agent 0/1/2 全部依赖强制 JSON 输出。

另外两点：

* `language_base_url()` 对 `official` 源强制使用 `default_base` 并忽略 `base_env`
  （防止官方凭证串到别的厂商）。所以插件把用户配置的 `SENSENOVA_API_BASE`
  **固化进 `default_base`**，既保留防串厂商的设计，又支持切到商汤大装置
  `https://api.sensenova.cn/compatible-mode/v2`。
* **输出长度上限默认"不指定"，交给模型自身默认值。** 服务商的 `/models`
  声明**不可信**：实测 `deepseek-flash` 声明 65536、**真实 393216（少报 6 倍）**，
  且每个模型都不同（`glm-5.2` 131072、`kimi-k3` 更大），端点没有真值可读。
  所以"按模型自动"的唯一可靠做法就是**不指定** `max_tokens`
  （实测不指定时上游给 65536，是原先硬定 16384 的 4 倍）。
  面板「最大输出 token」填 0 即此语义；填正整数则作为**下限**生效。
  长文案（海报 50+ 张）走 `finalize_prompts` 时会一次性输出全部提示词，
  旧版定死 16384 **必然**被截断（`GeminiOutputTruncated`，见 F-017）。
* **思考 token 占满预算**（报错里 `reasoning_tokens ≈ completion_tokens`、
  正文为零）：思考型模型（`deepseek-flash`、glm 系等）可能把整个输出预算
  全部耗在思考上。对策是把面板「语言模型」分页的**扩展请求参数**填为
  `{"reasoning_effort": "none"}` 关闭思考（实测有效；`minimal`/`low` 只能
  压缩量、关不掉），或直接换非思考型模型。截断报错里出现这种情况时
  插件会自动附上这条诊断提示。

  > **区分两种截断**：`reasoning_tokens ≈ completion_tokens` ⇒ 思考吃满预算
  > （用上面的办法）；`reasoning_tokens = 0` 且 `completion_tokens == max_tokens`
  > ⇒ **纯输出量不够**，抬预算或分批 —— 别去调思考参数。

### 2b. 语言模型 30 档阶梯重试（限流专项）

给语言模型请求加了**阶梯退避重试**，档位与**云端视频完全一致**
（用户要求"和视频机制类似"，两边共用同一张表）：

| 第几次重试 | 等待 |
|---|---|
| 1-3 | 1 秒 |
| 4-8 | 5 秒 |
| 9-12 | 10 秒 |
| 13-15 | 15 秒 |
| 16-20 | 30 秒 |
| 21-25 | 45 秒 |
| 26-30 | 1 分钟（封顶） |

合计约 **13 分钟**。

* 每次等待再叠加 **±30% 随机抖动** —— 多个阶段进程同时撞限流时，不抖动的
  相同退避会让它们在同一毫秒一起重发，把限流变成持续拥塞。
* 默认**最多重试 30 次**（合计约 13 分钟），可在面板「语言模型」分页调整
  （0–60）或整个关掉。
* **只有上游明确可重试的错误才重试**：HTTP 408 / 409 / 429 / 5xx。
  鉴权（401/403）与参数错误（400/422）**立刻失败**并报出真实原因 ——
  所以开着它不会把"Key 填错了"这类问题掩盖成"一直在重试"。
* 关掉它即完全回到 OCV 原生行为（`GEMINI_RETRY_COUNT` 默认 3 次）。

> **为什么从"15 次、30s 封顶"改成这个**：日志实测（2026-09-25）显示旧的
> 指数退避**第 6 次起就顶死在 30s**，后 10 次只是原地重复、合计仅 5.8 分钟。
> 而 429 的 `tpm/rpm` 窗口通常是**分钟级**，30s 封顶等于把大部分重试预算
> 浪费掉。新档位在 16 次之后才进入 30s 档、尾档抬到 1 分钟，
> 总窗口拉到 13 分钟 —— 给上游留出真正恢复的时间。

### 2c. 报 `401 Authentication Fails ... is invalid` 时先看这里

这条报错**未必是 Key 失效**。实测确认：它也可能意味着**请求打到了别的厂商**——
因为不同服务商的 401 文案是一个可靠指纹：

| 服务商 | 401 报文形态 |
|---|---|
| DeepSeek | `{"error":{"message":"Authentication Fails, Your api key: ****XXXX is invalid"}}` |
| 商汤 SenseNova | `{"error": {"code": 16, "message": "Forbidden"}}` |

所以看到 `Authentication Fails, Your api key: ... is invalid`，**先怀疑地址串了**。
1.9.1 起插件已修复"面板改地址后请求仍打旧地址"的缺陷（见下），
若你用的是更早版本，请升级——旧版会**新模型名 + 旧地址**，产生假 401。

**两条命令快速分辨**（把 `$BASE`/`$KEY` 换成你的）：

```bash
# ① 探 Key 是否有效（200 = Key 没问题）
curl -s -o /dev/null -w "%{http_code}\n" "$BASE/models" -H "Authorization: Bearer $KEY"
# ② 看报错文案属于哪家（对照上表）
```

**另一个容易踩的坑**：面板下拉里的型号来自服务商 `/models`，但**清单里有 ≠ 你的套餐能用**。
实测商汤会列出 `deepseek-v4.1-flash`，而它返回
`403 model is not available in the current token plan`。
1.9.1 起该型号已从下拉剔除；若你手填了它，报错会附带一句可操作的提示
（不是 Key/余额问题、该换成哪个）。

**`kimi-k3` 的采样参数**：该模型只接受 `temperature=1` 且 `top_p=0.95`，
而 OCV 默认传 0.3 / 1 ⇒ 直接 400。1.9.1 起插件会在发请求前自动纠正这两个值。

**实现要点（为什么它不会"重试放大"）**：OCV 自己的 `gemini_client` 里**已经**
有一层重试循环（原生 3 次）。插件**没有再套一层**，而是只替换了它的两个策略
函数 `_gemini_retry_count()` / `_gemini_retry_delay()`，让 OCV 原有的循环按
新节奏跑。若在外面再包一层，实际请求数会变成 3 × 16 = 48 次。

> 这两个函数在全仓库**没有任何 `from ... import` 绑定**（只被
> `gemini_client` 自己以模块级名字调用），所以替换模块属性即可全量生效，
> 不必逐个模块追打 —— 这是选择它们做挂点的关键原因。

**副作用**：重试在当前阶段进程内**同步**进行，最坏情况下该次模型调用会多等
约 13 分钟才开始报错。这对"熬过短暂限流"是必要的代价；若你希望失败暴露得
更快，把次数调小即可。

### 3. 图片：本地 shim 讲 RunningHub 协议

OCV 的生图层把整个并发体系（账号池、`clientJobId` 幂等、421 排队、
审核错误码映射、重试代际）都建立在**提交拿 taskId → 轮询**之上，
而商汤是**同步返回**的。与其重构 OCV 的协议层，不如在本地做一个讲对协议的 shim：

```
POST /openapi/v2/<model>/text-to-image   →  POST /v1/images/generations
POST /openapi/v2/<model>/image-to-image  →  POST /v1/images/edits
POST /openapi/v2/query                   →  返回本地任务状态
POST /uc/openapi/accountStatus           →  {"code":0,"data":{"currentTaskCounts":0}}
```

shim 内部用后台线程池执行商汤调用，对外立即返回 `taskId`，完全贴合 OCV 的轮询模型。
任务状态、`clientJobId` 幂等映射、结果图片都在 shim 进程内，因此可以跨多个
module4 子进程复用。

三个必须显式下发的商汤参数（默认值会破坏 OCV 的约束）：

| 参数 | 商汤默认 | 插件设置 | 不设的后果 |
|---|---|---|---|
| `watermark` | `true` | **`false`** | 每张图带日日新水印 |
| `prompt_extend` | `true` | **`false`** | 商汤自动扩写提示词，破坏 OCV 锁死的单镜头/画风/角色一致性 |
| `response_format` | `url` | **`b64_json`** | 拿到的是 24 小时失效的临时链接 |

尺寸换算（商汤要求宽高均为 32 的倍数、512–4096、最大比例 3:1）：

| OCV 预设 | 商汤 size |
|---|---|
| 16:9 / 2k | `2720x1536` |
| 9:16 / 2k | `1536x2720` |
| 1:1 / 2k | `2720x2720` |
| 2:1 / 2k | `2720x1376` |

参考图上限从 4 收到 **3**（商汤官方调优提示：多参考图合成 2–3 张为宜，过多会稀释主体）。
OCV 传的 `imageUrls` 是 Data-URL 字符串数组，shim 转成商汤要求的
`images: [{"image_url": "..."}]` 对象数组；遇到指向 127.0.0.1 的本地 URL 会自动
读回并转成 Data-URL（商汤无法访问本机回环地址）。

**故意不实现** `/openapi/v2/media/upload/binary`：OCV 有内置回退，上传失败时会把参考图
转成 Data-URL 直传，而商汤正好接受 Data-URL。少一个端点就少一次无意义的本地往返。

#### shim 的生命周期

shim 需要覆盖整个图片阶段（任务状态、`clientJobId` 幂等映射、结果图片都在
它进程内，跨多个 module4 子进程共享），所以必须有个**长驻宿主**。

**托管方式是唯一的：由长驻进程在自己进程里以守护线程托管。**

* 正常使用时宿主是 **OCV 后端**（`backend.app.main` 导入时就启动，
  `mode: inproc`）—— 与界面同生命周期；
* 自检 `/` 打开面板时宿主是 `cloud_stack_ctl.py` 自己。

> **这里踩过一个很贵的坑。** 最初的做法是"探活 + 必要时拉起分离进程"，
> 分离进程带 `CREATE_BREAKAWAY_FROM_JOB` 以便父进程退出后存活。
> 在受限环境（沙箱、部分启动器）里这个标志会直接抛 **WinError 5**，
> 于是降级成"不脱离"，子进程仍留在父进程的 job object 里 —— 父进程一退出
> 就被连带杀掉。症状是 shim 日志里十几条「已启动」、而端口上最终什么都没有，
> 同时每个新进程都以为是"shim 没起来"又拉起一个新的。
>
> 现在这条路径被**彻底删掉**了：宁可明确要求"由谁托管"，也不要一个
> 看起来能自动修复、实际在制造僵尸的机制。

配套的几个细节：

* **探活**：每个进程启动时做一次本地 TCP connect，失败即刻返回，开销可忽略。
* **不做自动拉起**：注入发生在每个进程启动时，其中很多是秒级退出的短命进程，
  由它们去拉进程只会产生随父进程消失的僵尸，反而掩盖真实状态。
* **不会误杀后端**：`stop()` 在 taskkill 之前会拿 `/health` 的回报核对 pid
  （pid 会被系统复用，而进程内托管模式下 shim 根本没有自己的 pid 文件）。
  端口上是"被托管的 shim"时，`stop` 只提示"关闭 OCV 即可一并停止"，不动手。
* **排查口径**：`curl http://127.0.0.1:8799/health` 会返回
  `{"pid":…, "mode":"inproc"|"detached"}`，一眼看出是谁在托管。

---

## 自检脚本

四层验证。前三层不需要 API Key、不消耗额度；第四层会真调语言模型。

| 脚本 | 验证内容 |
|---|---|
| `self_test.py` | 第 0 节：环境编码（`.pth` 是否纯 ASCII、控制台降级是否生效）；第 1 节：导入钩子；**第 1b 节：面板覆盖层是否已物化进 `os.environ`**（Agent 0 那道门禁就靠它）；第 1e 节：克隆音色（`mimo-v2.5-tts-voiceclone`）；**第 1f 节：软件更新后自愈**（会真的抹掉前端注入再让重建，并按字节还原）；第 2–4 节：补丁本身（TTS、`sensenova` provider、图片基址）；第 5 节：模块 2 的冷启动超时风险（预热语句是否与 OCV 逐字一致）；**第 5b 节：深度思考开关**（默认 off、档位映射、遗留参数不覆盖档位）；**第 5c 节：Agent 1B 容错**（单镜跳过、失败降级、两开关独立）；**第 5d 节：输出重组切分不变量**（无重复/无缺口、不变量被破坏时如实报错）；**第 5e 节：JSON 模式守卫**（12 条：缺 json 字面量时补上、出厂提示词下零干预、幂等、图片/非 json_object 请求不被碰、挂在 `Session.request` 上）；**第 1r 节：语言模型 15 次阶梯重试**（档位逐项、抖动区间、可重试分类、真实挂载、作用域隔离、序号语义） |
| `shim_selftest.py` | shim 协议：size 换算、参考图归一化、提交/轮询/下载、幂等键 |
| `ocv_integration_test.py` | **与 OCV 真实解析函数对齐**：直接调用 `_submit_poster_request`、`_find_image_url`、`_download_image`、`_account_active_task_count` 去消费 shim 的返回 |
| `verify_agent1b.py` | **Agent 1B 端到端的对照复现**：用真实数据集跑完整链路（Agent 1 → Agent 1B）并做覆盖校验。`--native` 做原生对照组（必定抛 `AgentPlanningFatalError`，退出码 `3`）；`--no-agent1` 跳过 Agent 1 的真调模型步骤 |
| `verify_llm_retry.py` | **语言模型 30 档阶梯重试**（62 项，不发真实请求）：30 档逐项断言、**与视频同表**校验、抖动区间与均值、可重试分类、真实挂载（防"3×31 放大"）、作用域隔离、可还原性、开关关闭后回原生，以及**端到端行为**（伪造 `requests.post` 验证真实调用路径：3 次 429 后成功、持续 429 有界耗尽、400/401 不重试） |
| `verify_llm_base_live.py` | **LLM 基址/模型实时化 + 模型清单可信度**（29 项，不发真实请求）：面板改地址后基址**实时**跟进（F-015 核心，防"新模型名发到旧地址"造成的假 401）、`main` 命名空间同步（铁律 29）、套餐外型号剔除、`kimi-k3` 采样参数纠正（在最终报文层捕获验证） |

```bat
runtime\python\python.exe plugins\cloud_free_stack\self_test.py
runtime\python\python.exe plugins\cloud_free_stack\shim_selftest.py
runtime\python\python.exe plugins\cloud_free_stack\ocv_integration_test.py
runtime\python\python.exe plugins\cloud_free_stack\verify_llm_retry.py
runtime\python\python.exe plugins\cloud_free_stack\verify_llm_base_live.py

:: 会真调语言模型（消耗额度）
runtime\python\python.exe plugins\cloud_free_stack\verify_agent1b.py
runtime\python\python.exe plugins\cloud_free_stack\verify_agent1b.py --native
```

---

## 配置项速查

**读取优先级**：`var/config.json`（面板） > `os.environ` > `.env` > 内置默认值。
面板里留空即删除该项，回退到下层的值。

| 键 | 默认 | 说明 |
|---|---|---|
| `CLOUD_STACK_ENABLED` | `1` | 总开关，设 `0` 后对 OCV 完全透明 |
| `CLOUD_STACK_DEBUG` | `0` | 打印调试日志 |
| `CLOUD_STACK_SHIM_PORT` | `8799` | 本地 shim 端口（改动需重启 OCV） |
| `CLOUD_STACK_SHIM_AUTOSTART` | `1` | 允许自动托管 shim；设 `0` 则完全不托管 |
| `CLOUD_STACK_IMAGE_WORKERS` | `4` | shim 内部并发出图数 |
| `CLOUD_STACK_IMAGE_MAX_REFERENCE` | `3` | 参考图截断上限（商汤建议 2–3 张） |
| `CLOUD_STACK_IMAGE_WATERMARK` | `0` | 保留商汤水印 |
| `CLOUD_STACK_IMAGE_PROMPT_EXTEND` | `0` | 允许商汤扩写提示词（会破坏 OCV 的约束，建议保持关闭） |
| `CLOUD_STACK_VOICE_MAP` | 内置 32 条 | Qwen 音色名 → MiMo 音色名映射（JSON）；面板里的音色映射表 |
| `MIMO_API_KEY` | — | 小米 MiMo 的 Key（**必填**） |
| `MIMO_TTS_BASE_URL` | `https://api.xiaomimimo.com/v1` | 服务地址 |
| `MIMO_TTS_MODEL` | `mimo-v2.5-tts` | 也可用 `mimo-v2.5-tts-voicedesign` |
| `MIMO_TTS_VOICE` | `冰糖` | 默认音色（另有 茉莉 / 苏打 / 白桦） |
| `MIMO_TTS_RETRIES` | `3` | 单句重试次数（只对网络/限流类错误重试） |
| `SENSENOVA_API_KEY` | — | 语言模型 Key（**必填**）；任意 OpenAI 兼容服务商的 Key |
| `SENSENOVA_API_BASE` | `https://token.sensenova.cn/v1` | 默认商汤；可整体替换为任意 OpenAI 兼容地址（如 `https://api.deepseek.com/v1`） |
| `SENSENOVA_MODEL` | `deepseek-v4-pro` | 模型名需与所选服务商匹配 |
| `SENSENOVA_MAX_TOKENS` | `0` | **输出上限**。`0`（默认）= **不指定，交模型自身默认**（推荐：端点声明不可信，实测 deepseek-flash 真实上限 393216 而端点只写 65536）；填正整数 = 作为**下限**抬升（如 `32768`） |
| `CLOUD_STACK_LLM_THINKING` | `off` | **深度思考档位**：`off`（默认）/ `low` / `medium` / `high`。关闭时同时发 `reasoning_effort=none` 与 `thinking={"type":"disabled"}`（覆盖两类服务商实现）；开启时只发 `reasoning_effort` |
| `CLOUD_STACK_LLM_RETRY_ENABLED` | `1` | **语言模型阶梯重试开关**：遇 429 限流 / 5xx / 超时等可重试错误时按阶梯退避自动重发。设 `0` 恢复 OCV 原生（3 次、间隔 3s 起） |
| `CLOUD_STACK_LLM_RETRY_COUNT` | `30` | 语言模型**重试次数上限**（不含首次，0–60）。档位与视频共用（1×3,5×5,10×4,15×3,30×5,45×5,60×5）+ ±30% 抖动；30 次合计约 13 分钟 |
| `CLOUD_STACK_LLM_EXTRA_BODY` | 空 | 以 JSON 合并进语言模型请求根部（服务商特有参数，如 `{"top_k": 20}`）。其中 `reasoning_effort` / `thinking` 两键由「深度思考」统一管，档位非 off 时会被忽略 |
| `CLOUD_STACK_AGENT1B_RESILIENT` | `1` | Agent 1B 细化失败时降级为「保留原单元继续」；设 `0` 恢复 OCV 原生严格语义（失败即终止任务） |
| `CLOUD_STACK_AGENT1B_SKIP_INSEPARABLE` | `1` | 只覆盖单个 slide 的父单元直接跳过细化（结构上不可能有合法切分）；设 `0` 照常送模型 |
| `CLOUD_STACK_JSON_MODE_GUARD` | `1` | **JSON 模式守卫**：给 `response_format=json_object` 的请求兜底补上 JSON 输出约束，修掉自定义 `AGENT1_PROMPT_SYSTEM` 导致的 `HTTP 400 … must contain the word 'json'`。出厂提示词含 json 字面量时**完全不干预**；设 `0` 关闭 |
| `SENSENOVA_IMAGE_API_BASE` | 空 | 图片服务地址；留空与语言模型相同 |
| `SENSENOVA_IMAGE_API_KEY` | 空 | 图片独立 Key；留空与语言模型共用（出图与文字不同账号/额度时用） |
| `SENSENOVA_IMAGE_MODEL` | `sensenova-u1.5-lite` | 支持参考图的生成+编辑一体模型 |
| `SENSENOVA_IMAGE_EDIT_MODEL` | 同上 | 画面重绘（图生图）单独指定模型 |
| `CLOUD_STACK_ASR_PREWARM` | `1` | OCV 后端启动时预热模块 2 的 ASR 运行时（见下节） |
| `CLOUD_STACK_ASR_PREWARM_TIMEOUT` | `900` | 预热的超时上限（秒），比模块 2 本身宽松得多 |
| `CLOUD_STACK_AUTO_REINJECT` | `1` | OCV 后端启动时自动重建被软件更新抹掉的注入（前端按钮 / 锚点）；设 `0` 关闭 |
| `ASR_RUNTIME_CHECK_TIMEOUT_SECONDS` | `600` | **OCV 原生键**，模块 2 依赖自检的超时（原生默认 90 秒，冷启动不够） |
| `ASR_DEVICE` | `cpu` | **OCV 原生键**，识别算力取向。本机无 N 卡已设为 `cpu`（跳过 CUDA 探测）；换到有 N 卡的机器改回 `auto` |

---

## 常用命令

```bat
:: 打开配置面板（MiMo / 商汤）
runtime\python\python.exe plugins\cloud_free_stack\cloud_stack_ctl.py panel

:: 查看配置与运行状态
runtime\python\python.exe plugins\cloud_free_stack\cloud_stack_ctl.py status

:: 逐项连通性自检
runtime\python\python.exe plugins\cloud_free_stack\cloud_stack_ctl.py check

:: 预热 ASR 运行时（模块 2 的依赖自检有 90 秒超时，冷启动常常不够）
runtime\python\python.exe plugins\cloud_free_stack\cloud_stack_ctl.py prewarm

:: 只补 / 只撤前端那个悬浮按钮，不动 .env
runtime\python\python.exe plugins\cloud_free_stack\cloud_stack_ctl.py inject
runtime\python\python.exe plugins\cloud_free_stack\cloud_stack_ctl.py inject --remove

:: 软件更新后重建被抹掉的注入（前端按钮 + .pth 锚点），幂等
runtime\python\python.exe plugins\cloud_free_stack\cloud_stack_ctl.py reinject

:: 前台运行 shim（调试用，能看到每张图的提交/完成日志）
runtime\python\python.exe plugins\cloud_free_stack\cloud_stack_ctl.py shim

:: 停止独立运行的 shim（不会去 kill 托管它的 OCV 进程）
runtime\python\python.exe plugins\cloud_free_stack\cloud_stack_ctl.py stop

:: 完全卸载（还原 .env + 移除注入 + 移除前端按钮 + 停 shim）
runtime\python\python.exe plugins\cloud_free_stack\cloud_stack_ctl.py uninstall
```

运行日志：

* shim：`plugins/cloud_free_stack/var/shim.log`、`var/shim.out.log`
* 出图结果：`plugins/cloud_free_stack/var/images/`
* 面板覆盖层：`plugins/cloud_free_stack/var/config.json`

---

## 软件更新后按钮消失（派生注入状态的自动重建）

**症状**：OCV 更新完之后，界面右下角的「云端免费栈」按钮不见了，但配音、语言模型、
出图全部照常工作，「任务控制台」也没有任何报错。

**原因**：插件的落地状态分两层，性质完全不同。

| 层 | 内容 | 更新会被抹掉吗 |
|---|---|---|
| **基线层** | 插件目录、面板覆盖层 `var/config.json`、`runtime/python/Lib/site-packages/ocv_cloud_free_stack.pth` | **不会**。启动器每次更新的 `update.json` 里 `protected_data` 明确保护 `.env` / `runtime` / `output` / `workspace` / `runtime_logs` / user presets / third-party plugins |
| **派生层** | `frontend/index.html` 里那一行 `<script>`（以及锚点自身） | **会**。`frontend/` 是会被整体替换的源码目录（用户数据在 `workspace/`，不在 `frontend/`） |

启动器的更新归档在 `Archives/launcher_updates/<时间戳>/`，里面的 `update.json`
就是上面那张表的依据，`backup/` 是替换前的原文件 —— 出问题时可以先去看一眼
「这次更新到底动了哪些文件」。

所以故障面**只有派生层**，而派生层是完全可以原地重建的。

**怎么修**（三种，任选）：

1. **什么都不用做。** OCV 后端进程每次启动都会检查一次并自动重建。
   更新完照常双击 `双击启动.bat` 即可，日志里会有一行
   `[cloud_free_stack] 检测到软件更新抹掉了注入，已自动重建：前端面板按钮`。
2. 双击插件目录下的 **`重新注入云端免费栈.bat`**。
3. 命令行：`runtime\python\python.exe plugins\cloud_free_stack\cloud_stack_ctl.py reinject`。

前端由 Vite dev server 直接读取 `frontend/index.html`（`start_windows.bat` 里是
`node vite.js frontend --port 5173`），所以重建后**刷新浏览器页面**就能看到按钮，
不需要重启 OCV。`frontend/dist/` 是过期残留，不参与运行，不用管它。

> 想关掉自动重建：在 `.env`（或面板）里设 `CLOUD_STACK_AUTO_REINJECT=0`。

**为什么自动重建挂在后端进程上**：`runtime/` 受保护 ⇒ 锚点必然存活 ⇒
`sitecustomize.py` 必然在每个子进程启动时执行。而 `backend.app.main` 只被后端
那一个长驻进程导入，它也就是更新之后唯一还能自动跑起来的时机。
调用点被刻意放在 `_patch_main()` 的**最开头**，先于任何提前 `return` ——
否则将来某次更新一旦改掉 `_project_config_values` 的名字，自愈会被一起短路掉，
而那就成了它自己要防的那种脆弱性。

`self_test.py` 的 1f 节会真的把注入抹掉再让自愈重建，断言
「重建结果 == 原始模板 + 恰好一个注入块」，并在 `finally` 里按字节还原，
所以自检可以放心反复跑。

---

## 已知限制与排错

### Agent 1 报 `HTTP 400 Prompt must contain the word 'json'`

```
失败: Agent 1 语言模型规划失败，已在提交图像任务前安全终止；配音与字幕已保留，
      可排除 API Key、余额、限流或上游服务问题后断点续跑。
原始错误：OpenAI 兼容接口（云免费栈） 调用失败: deepseek-flash: HTTP 400
      Prompt must contain the word 'json' in some form to use 'response_format' of type 'json_object'.
```

**这条错误信息具有误导性。** 它说「提示词里必须有 json 这个词」，
但按字面去改提示词是治标 —— 真正的机制是下面这条链。

#### 一阶根因：`json_object` 必然被发出，而提示词未必含 json

`backend/app/gemini_client.py` 里只有一处产生 `response_format`：

```python
if response_mime_type == "application/json" and json_root != "array":
    extra_body["response_format"] = {"type": "json_object"}
```

`story_agents.py` 的**四处** json 调用点（`:997` Agent 1B、`:1449` Agent 1、
`:1624` Agent 1、`:1882` 长文分段）**都没有传 `json_root`**，所以 `json_root is None`，
`json_object` 一律发出。而 OpenAI 与 DeepSeek 的硬契约是：一旦声明 `json_object`，
`messages` 全文（含 `role`）必须出现字面量 "json"（不区分大小写），否则直接 400。
服务端**不会**理解你的提示词写得多么明确。

#### 二阶根因：为什么出厂配置一直没事

出厂提示词里**恰好**写着这四个字母：

| 常量 | 是否含字面量 json |
|---|---|
| `TIMELINE_AGENT_SYSTEM_PROMPT` | ✅ `只输出严格 JSON 对象：{...}` |
| `PURE_SCIENCE_TIMELINE_AGENT_SYSTEM_PROMPT` | ✅（继承上式） |
| `STORY_AGENT_SYSTEM_PROMPT` / `GENERAL_` / `SCIENCE_` | ✅ |
| `AGENT1B_BOUNDARY_REFINER_PROMPT` | ✅ |
| `DEVICE_SHOT_CONTRACT` / `ENHANCED_DIRECTOR_AGENT1_CONTRACT` | ❌ |
| 用户自定义 `AGENT1_PROMPT_SYSTEM` | ❌ **通常不含** |

也就是说，**「能跑」完全依赖出厂提示词里偶然出现的四个字母**。

#### 触发条件（三条同时成立）

`story_agents.py:1556-1635` 的 `create_story_plan` 分支：

```python
custom_agent1_prompt = os.getenv("AGENT1_PROMPT_SYSTEM", "").strip()
system_prompt = custom_agent1_prompt or STORY_AGENT_SYSTEM_PROMPT   # ← 整体替换
```

1. 设置了 `AGENT1_PROMPT_SYSTEM`；
2. 该文本**含** `semantic_units` —— 这会命中 `_create_timeline_story_plan` 里
   `if custom_prompt and "semantic_units" not in custom_prompt` 的**取反**分支，
   于是出厂提示词**不会**被追加回来（`:1436`）；
3. 该文本**不含**字面量 `json`。

三条齐备 ⇒ 系统提示被替换成不含 json 的版本 ⇒ 400。

> 第 2 条正是「照着官方 Agent 1 提示词微调」时的典型结果：官方提示词通篇在讲
> `semantic_units` 字段，用户改写时自然会保留这个词，却顺手把 `只输出严格 JSON 对象`
> 那句换成了「只返回对象」。

#### 为什么是「跨次残留」而不是偶发

`story_fingerprint()` 把 `AGENT1_PROMPT_SYSTEM` 的 sha1 算进缓存指纹，
所以带上自定义提示词后**已缓存的 `story_plan.json` 全部失配**，必然重新规划 ——
一次设置，之后每个任务都带着它。**这是确定性故障，不是随机抖动。**

#### 为什么 `AGENT1_PROMPT_SYSTEM` 不在 `.env` 里也会中招

`.env`、`.env.cloud_stack_backup`、面板 `var/config.json` 三处都查不到这个键，
但它仍然生效 —— 因为 `backend/app/main.py` 的请求字段 `agent1_prompt_system`
会在任务提交时写进 `os.environ`。**提交一次就会在长驻后端进程里留下来**，
之后所有任务都继承。排查时不要只看配置文件。

#### 修复：`ocv_cloud_stack/json_mode_guard.py`

不改 OCV 源码、**也不改任何提示词常量**（改常量会连带改掉
`story_fingerprint` 里的提示词参与项，让所有已缓存任务集体重跑）。
补丁挂在 `requests.sessions.Session.request` 这一层，做**四个条件全中**才动手的兜底：

| 条件 | 说明 |
|---|---|
| URL 以面板配置的语言模型地址开头 | 不碰其它服务的请求 |
| 请求体是带 `messages` 的 dict | 图片请求无 `messages`、TTS 走本地 shim，天然排除 |
| `response_format == {"type":"json_object"}` | 只处理会触雷的那一种 |
| `messages` 全文（含 `role`）**不含** `json` | **出厂提示词天然不满足这条 ⇒ 零干预** |

四条同时成立时，往**最后一条 user 消息**追加一句格式硬约束
（`【输出格式硬约束】你必须只返回一个 JSON 对象…`）。
追加在 user 消息上而不是 system 上，是为了不改变 system 提示词的语义。

**为什么打在 `Session.request` 而不是 `requests.post`：**
`_install_llm_body_injector` 安装注入器时把 `original_post` 捕获进了闭包，
之后再替换 `requests.post` 只影响**新**调用 —— 这是本插件踩过的坑（见
「六点五」节）。`Session.request` 在 `Session.send` 真正发出字节之前，
位置最靠后也最可靠。

**为什么不需要改自定义提示词：** 守卫是请求层兜底，对提示词零侵入。
用户不必为了绕过 400 去改自己的提示词，也不必记住「提示词里要写 json」。

**开关：** `CLOUD_STACK_JSON_MODE_GUARD`（默认 `1`）。关掉只会让上面那种配置
组合继续 400，没有收益。

**验证方式：**

```bat
:: 自检里的 5e 节共 12 条断言，含「出厂提示词下零干预」与「幂等」两条关键不变量
runtime\python\python.exe plugins\cloud_free_stack\self_test.py
```

真机复现（用一个本地假 LLM 服务端逐字节校验 OpenAI 契约）时，
`含 json = False → HTTP 400`、`守卫注入后 → 含 json = True，请求被接受`，
且出厂提示词用例**不出现**注入痕迹。

### Agent 1B 报「边界验收失败」，任务在提交图像前终止

```
失败: Agent 1B 语言模型规划失败，已在提交图像任务前安全终止；配音与字幕已保留，
      可排除 API Key、余额、限流或上游服务问题后断点续跑。原始错误：边界验收失败: empty
```

**这条不是输出截断，也不是思考 token 挤占预算。** 已实测排除：

| 被怀疑的原因 | 实测结果 |
|---|---|
| 思考过程过长导致输出截断 | `reasoning_effort=none` 确实已注入；上游 `reasoning_content` 长度 **0**，`completion_tokens` 个位数 |
| `max_tokens` 不够 | Agent 1B 响应体量 1.6–3.4 KB，预算 6144，用过约 3400 字符；`finish_reason=stop`，从未触发 `GeminiOutputTruncated` |
| 随机抖动 | 同一输入重复 6 次：**5 次失败、1 次成功** —— 结构性必现 |
| API Key / 余额 / 限流 | 同上；失败发生在**语义校验**阶段，请求本身是成功的 |

**真正的根因是校验规则的表达力盲区。** 四个环节串成一条必然失败的链：

1. `semantic_unit_refinement_risks()` 只看「时长 / slide 数」两个维度。
   当某个 slide 因字幕合并而长达数十秒（实测 `scene_044` 单独 **39.1 秒**）时，
   某个父单元虽然不是「长单元」，却仍会因 `duration>24s` 被送去细化。
2. `refine_risky_semantic_units()` 把一个只含 **2 个 slide** 的父单元交给模型，
   要求它「切成 6～14 秒的语义单元」。父单元文本跨越三个时间阶段，
   **切分在结构上无解** —— 模型只能在同一个 `slide_id` 上重复起止
   （实测返回 6 个单元全部是 `scene_044 -> scene_044`）。
3. `_normalize_semantic_units()` 要求子单元**严格首尾相接、每个 slide 恰好消费
   一次**，遇到重复立即 `return []`，于是候选为空。
4. `_candidate_refinement_is_safe()` 对空候选返回 `(False, "empty")`，
   而 `require_ai_success=True` 时直接 `raise _planning_failure("Agent 1B")`
   —— **整条流水线终止**。

关键的不一致在于：**Agent 1 有降级路径，Agent 1B 没有。** Agent 1 归一化失败时
会退回确定性划分并只记一条 `fallback_reason`；Agent 1B 却把同一种失败升级为致命错。

**修复（`ocv_cloud_stack/agent1b_resilience.py`，三层）：**

| 层 | 行为 | 开关 |
|---|---|---|
| 前置跳过 | 只覆盖 **1 个 slide** 的父单元结构上不可能有合法切分，直接跳过细化（不消耗模型调用）。2 个及以上照常送模型 | `CLOUD_STACK_AGENT1B_SKIP_INSEPARABLE`（默认 `1`） |
| 失败降级 | 细化失败时保留原单元继续渲染，诊断写入 `failed_units`、日志给出原因 | `CLOUD_STACK_AGENT1B_RESILIENT`（默认 `1`） |
| 输出重组 | 把原函数的扁平输出按父单元顺序重切，保证「跳过」的单元回到正确位置 | — |

**两个开关是独立的**：关掉「容错」不会连带关掉「跳过」。理由是关掉容错的目的
是排查模型质量，不该把注定失败的请求重新发出去（只会让排查更慢）。

**验证方式**（会真调语言模型）：

```bat
runtime\python\python.exe plugins\cloud_free_stack\verify_agent1b.py
```

该脚本用真实数据集跑完整链路并做覆盖校验，`PASS` 表示 107/107 个 slide
连续铺满、无重复、无缺口。加 `--native` 会重新加载 `story_agents` 拿未包装的
原生函数做对照组 —— 原生必定抛 `AgentPlanningFatalError`，退出码 `3`。

覆盖校验是**必须做**的验收项。第一版补丁在重组环节漏了「未触发风险的父单元也要
推进游标」，结果把 `scene_001->scene_005` 连发两遍、107 个 slide 只覆盖了 5 个
—— 任务不再报错，但产物是错的。`self_test.py` 第 5d 节现在钉住了这条不变量。

**ASR 仍是本机 CPU。** OCV 的识别层硬编码 faster-whisper，不可插拔。
`base` 模型在 CPU 上跑 10 分钟音频约 1–3 分钟，可以接受。
只有确认跑不动时才考虑接云端 ASR（那需要改 ASR 层，成本陡增）。

`.env` 里的 `ASR_DEVICE` 已由 `auto` 改为 `cpu`（本机没有 N 卡）。两者差别：

| 取值 | 行为 |
|---|---|
| `auto`（OCV 原生默认） | 先预加载 CTranslate2 的 CUDA 运行库、以 `cuda/float16` 试一次，失败才回退。本机每次都白试一遍，并往日志扔一行 `CUDA driver version is insufficient for CUDA runtime version` |
| `cpu`（本机现状） | 直接进 `run("cpu", "int8")`，不碰 CUDA |

**换到有 N 卡的机器要把这行改回 `auto`。** 别用 `cuda` —— 那是强制模式，
设备不匹配时直接抛错、不回落 CPU。

这个键在插件的 `.env` 块**之外**，`install` / `uninstall` 都不会动它；
`cloud_stack_ctl.py status` 的「ASR 识别」段会把它连同 `ASR_MODEL`、
`ASR_LANGUAGE` 一起回显出来。

**Agent 0 报「语言模型未配置」，但你明明在面板里填了商汤 Key。**

```
失败: Agent 0 语言模型规划失败，已在提交图像任务前安全终止；配音与字幕已保留，
      可排除 API Key、余额、限流或上游服务问题后断点续跑。原始错误：语言模型未配置
```

这条来自 `story_agents.py::create_story_context()` 开头的
`if not gemini_configured()`，而 `gemini_configured()` 最终读的是
`os.environ` —— **它看不见面板层**。所以最可能的成因就是面板里填了 Key、
却没人把它送进环境变量（本插件早期版本的真实缺陷，现由
`bootstrap._export_store_to_environ()` 修复）。

按可能性排查：

| 成因 | 怎么确认 |
|---|---|
| 面板层没物化进环境 | 面板里填了 Key 就说明面板层有值 → 跑 `self_test.py`，**第 1b 节**会直接列出没进 `os.environ` 的键 |
| 面板与 `.env` 里都没有这个 Key | `cloud_stack_ctl.py status` 的「面板覆盖层」「OCV 原生键」两段 |
| provider 选错了 | `curl 127.0.0.1:8010/api/api-keys` → `language.selected_provider` 应为 `sensenova`、`configured` 应为 `true` |
| Key 本身失效 / 余额用尽 | 面板「语言模型（OpenAI 兼容）」分页的 **JSON 输出测试** |

注意一个会让人绕远的反差：**module1 的 MiMo 配音一直能正常跑**，因为那条链路
是插件直接接管的函数、读 `config.get()`，本来就看得到面板层。于是会出现
"配音成功、Agent 0 却说模型未配置"这种看起来自相矛盾的现象。

**OCV 内出图报「未实现的端点: /openapi/v2/v1/images/generations」，面板出图测试却正常。**

```
失败: ... 网络或接口错误：HTTPError；未实现的端点: /openapi/v2/v1/images/generations。
      结果尚未确认，请核对扣费后再生成。
```

根因：整合包 `.env` 自带 `RUNNINGHUB_ENDPOINT=/v1/images/generations`
（商汤 OpenAI 兼容直连模式的遗留值），而 OCV 的
`_runninghub_generate_url()` 会给非 http 开头的端点强套 `/openapi/v2/`
前缀再打给 shim，shim 没有 OpenAI 风格路由就 404。**面板出图测试不走
RunningHub 协议**（直调 `sense_image`），所以两个入口一好一坏。

修复（2026-09-13 已做）：`.env` 该行改为
`/sensenova-u1.5-lite/text-to-image`；shim 同时加了兼容别名
（`/openapi/v2/*/images/generations` 也接受为提交端点，带 `imageUrls`
按图生图处理），即使该值被还原也能跑。`self_test.py` 第 1d 节锁住路由。

**模块 2 的依赖自检会因「冷启动」超时，这不是插件的锅，但会被它放大成致命错误。**
现象是任务在模块 1 成功后直接失败：

```
失败: ASR_PYTHON 不可用或缺少 faster-whisper/ctranslate2: <解释器路径>:
      ASR 依赖导入自检超过 90 秒
```

链条是这样的：

1. `backend/app/pipeline.py::_asr_runtime_available()` 用
   `python -c "import ctranslate2, faster_whisper"` 做自检，**超时只有 90 秒**；
2. `ctranslate2/__init__.py` 的最后一行是
   `from ctranslate2 import converters, models, specs` —— `converters` 会连带
   拉起 `torch` 与 `transformers`；
3. 本整合包的 torch 是 `2.8.0+cu128`，`torch/lib` 有 **6.9 GB / 37 个 DLL**。

所以**第一次**加载这批 CUDA DLL 时（OS 文件缓存冷 + Defender 实时扫描 7 GB）
超过 90 秒是常态；被扫过之后同一个导入只要 6 秒级。实测同一台机器：

| 条件 | `import ctranslate2, faster_whisper` |
|---|---|
| 缓存已热 | 5.7 s |
| 字节码缓存全冷 | 28.1 s |

**这个坑只在"这台机器从没跑到过模块 2"时出现一次** —— 之前模块 1 一直失败，
模块 2 根本没被执行过，所以第一次成功跑到模块 2 时正好撞上冷启动。

插件用两条措施兜住它，都不改 OCV 源码：

* **预热**：OCV 后端启动时，用一个一次性子进程把同样的导入先跑一遍
  （`ocv_cloud_stack/asr_prewarm.py`）。冷启动成本被挪到后端启动那一刻，
  模块 2 自检命中的是热缓存。刻意用子进程而不是后端进程内导入 ——
  7 GB DLL 不该长驻在负责界面的后端里。想手工提前付掉（例如刚重启完机器）：
  `cloud_stack_ctl.py prewarm`。
* **抬高自检超时**：`.env` 里的 `ASR_RUNTIME_CHECK_TIMEOUT_SECONDS=600`。
  这个键只在"导入很慢"时才会触发 —— **依赖没装是立刻返回非零退出码的**，
  不会白等 10 分钟。

顺带一提：`ctranslate2` → `torch` 这条义务导入对**推理**是完全多余的
（`converters` 只用于把 HF 模型转成 CTranslate2 格式）。真要从根上省掉那
6.9 GB 的加载，得去动 `site-packages/ctranslate2/__init__.py`，插件不做这种
对第三方包动刀的事。

**MiMo TTS 是"限时免费"。** 一旦收费或收紧限流，退回零代码方案：
勾选 OCV 的「已有配音」模式，把外部生成的整条配音 WAV 上传，
OCV 会跳过 TTS 直接走后续流程。

**商汤图片是积分制而非按次限流。** 公测期是滚动积分池（5 小时 / 周两个窗口）。
如果出图开始报额度类错误，调低 `CLOUD_STACK_IMAGE_WORKERS` 或
`RUNNINGHUB_TARGET_RATIO`，先去账户页确认余额。

**音色下拉里没有 MiMo 音色名。** 前端音色列表在 `useWorkspace.js` 里硬编码，
插件不改 Vue 代码，改为在 `resolve_voice()` 里做映射：任意 Qwen 音色名 →
`CLOUD_STACK_VOICE_MAP`（面板「音色映射」页可逐项改，内置 32 条默认按男女声分好）
→ 默认落到 `MIMO_TTS_VOICE`。前端下拉文案会被注入脚本标注成 MiMo，方便辨认。

**声音克隆（mimo-v2.5-tts-voiceclone）。** 面板「MiMo 配音」页可上传
10-30 秒参考音频（mp3/wav/m4a 等，≤8MB）登记为克隆音色，本体存
`var/voices/`，注册表存面板层 `CLOUD_STACK_CLONE_VOICES`。克隆音色会
出现在音色映射的目标下拉里；合成时命中注册表即**自动切换** voiceclone
模型，参考音频按官方格式内联为 `audio.voice` 的 data URI（每次请求都要
带上，所以长文合成明显比预置音色慢，长视频仍建议预置音色）。

**插件代码改动要重启 OCV，配置改动不用。** Python 模块按进程加载，改了
`ocv_cloud_stack/*.py` 需要重启；面板写入的 `var/config.json` 是按 mtime
生效的，运行中改完下一句配音就用新值。

**面板服务依赖 shim，而 shim 住在后端里。** 所以 OCV 没启动时，面板地址
（`http://127.0.0.1:8799/panel`）是打不开的 —— 用
`cloud_stack_ctl.py panel` 可以让当前窗口临时托管（用完 Ctrl+C 结束）。

**`IMAGE_API_BASE_URL` 必须写进 `.env`。** module4 的
`_load_runninghub_env_from_file()` 会主动 pop 掉不在 `.env` 里的 `IMAGE_*` 变量。
用 `cloud_stack_ctl.py install` 就不会踩这个坑。

**中文 Windows 的两个编码陷阱（都踩过，别再加回中文）。** 中文 Windows 的
locale 是 GBK（`cp936`），下面两处会被它咬：

1. **`.pth` 必须是纯 ASCII。** CPython 的 `site.addpackage()` 用 **locale 编码**
   读 `.pth`，不是 UTF-8 —— `Lib/site.py` 自己的注释就写着
   *"locale encoding is not ideal especially on Windows. But we have used it for a long time."*
   只要文件里有一个 GBK 编不出的字节，**整行都读不到**并抛 `UnicodeDecodeError`：

   ```
   UnicodeDecodeError: 'gbk' codec can't decode byte 0x89 in position 143
   ```

   在启动器里表现为 `[失败] 便携路径检查`，而在 OCV 日志里**什么都不报** ——
   注入静默失效、插件根本没加载。那个 `0x89` 就是旧版 `.pth` 注释里全角括号
   `）` 的第三个字节。
   现在锚点由 `_pth_line()` 生成，只输出 ASCII（路径含中文时用 `\uXXXX` 转义），
   写入用 `encoding="ascii"` 兜底；`status` 会显示锚点是否「纯 ASCII，编码安全」。

2. **控制台打印 `✓` / `✗` / `⚠` 会崩进程。** 这三个字符不在 GBK 里，
   而 GBK 下 `print` 它们会抛 `UnicodeEncodeError` **直接终止进程**（不只是丢字符）。
   `ocv_cloud_stack/console.py` 注册了一个 codec error handler，把它们降级成
   GBK 里存在的等价符号（`✓`→`√`、`✗`→`×`、`⚠`→`!`），在 UTF-8 控制台下不受影响。
   由 `bootstrap.install()` 和 `cloud_stack_ctl.main()` 负责启用。

   注意：**用 Git Bash / VSCode 的终端调试会掩盖这一类问题** —— 那些环境的
   `PYTHONUTF8=1`、`LC_ALL=C.UTF-8`，locale 是 UTF-8，怎么跑都不报错。
   要复现真实环境，得把 UTF-8 环境变量剥掉：

   ```bash
   env -u PYTHONUTF8 -u PYTHONIOENCODING -u LC_ALL -u LANG \
     runtime/python/python.exe plugins/cloud_free_stack/self_test.py
   ```

   `self_test.py` 的第 0 节就是专门查这两个陷阱的。

**想临时回退某一层**：

* 换回原生 LLM：`.env` 里把 `LANGUAGE_PROVIDER` 改回去
* 换回原生生图：清空 `IMAGE_API_BASE_URL` 和 `IMAGE_MODEL_ID`
* 一次性全关：`CLOUD_STACK_ENABLED=0`

---

## 目录结构

```
plugins/cloud_free_stack/
├─ plugin.json                  清单（OCV 界面可读，但不执行）
├─ README.md                    本文档
├─ sitecustomize.py             解释器级注入入口
├─ cloud_stack_ctl.py           安装 / 卸载 / 自检 / 面板 / 前端注入 / 更新后重建 控制台
├─ 启动_云端免费栈.bat           便捷启动入口
├─ 重新注入云端免费栈.bat        软件更新后一键重建被抹掉的注入（双击即用）
├─ self_test.py                 注入自检（无需网络）
├─ shim_selftest.py             shim 协议自检（无需网络）
├─ ocv_integration_test.py      与 OCV 真实调用点的契约对齐测试
├─ verify_agent1b.py            Agent 1B 端到端对照复现（会真调语言模型）
├─ panel/                       配置面板前端（零依赖，可独立打开）
│  ├─ index.html                面板本体（5 个分页）
│  └─ panel.js                  注入 OCV 前端的悬浮按钮（可拖动、双击打开）+ iframe 弹窗
├─ ocv_cloud_stack/
│  ├─ bootstrap.py              注入总调度（导入钩子 + shim 地址一致性检查）
│  ├─ patches.py                补丁实现（qwen_tts / gemini_client / main / story_agents / JSON 守卫调度）
│  ├─ config.py                 配置读取（面板层 > 环境变量 > .env > 默认）
│  ├─ console.py                控制台编码加固（GBK 下打印符号不再崩）
│  ├─ panel.py                  面板后端（schema / 读写 / 三项连通性测试）
│  ├─ agent1b_resilience.py     Agent 1B 边界细化的容错补丁（跳过 / 降级 / 输出重组）
│  ├─ json_mode_guard.py        JSON 模式守卫（json_object 契约兜底，修自定义提示词导致的 400）
│  ├─ llm_retry.py              语言模型 30 档阶梯重试（与视频共用档位表；包装 OCV 策略函数，不新增循环）
│  ├─ （patches 内）             LLM 基址/模型实时化 + 采样参数纠正（F-015）
│  ├─ mimo_tts.py               MiMo TTS 客户端
│  ├─ sense_image.py            商汤图片客户端
│  ├─ image_shim.py             本地 shim（RunningHub 协议 + 面板 API 托管）
│  ├─ asr_prewarm.py            模块 2 的 ASR 冷启动预热（一次性子进程）
│  ├─ reapply.py                派生注入状态的重建器（更新抹掉注入后的自愈）
│  └─ shim_launcher.py          shim 探活 / 进程内托管 / 安全停止
└─ var/                         运行期产物（日志、出图缓存、pid、config.json）
```

前端注入的落点在 OCV 侧：`frontend/index.html` 里会被写入一段带
`cloud_free_stack BEGIN/END` 标记的 `<script>`，`inject --remove` 或
`uninstall` 会精确摘除该段，不动其它内容。**软件更新会把这个落点整体抹掉**
（`frontend/` 不在启动器的保护名单里），由 `reapply.py` 负责原地重建 ——
自动（后端启动时）或手工（`reinject` / `重新注入云端免费栈.bat`）。

## 许可证与免责声明

本插件自研代码依据 [GNU Affero General Public License Version 3
only](./LICENSE)（`AGPL-3.0-only`）发布，与宿主项目
[One-Click VidGen（OCV / 一键成片）](https://github.com/IFRIT-Zhou/One-Click-VidGen)
保持一致；版权与署名见 [NOTICE](./NOTICE)，附加条款见
[ADDITIONAL_TERMS.md](./ADDITIONAL_TERMS.md)。本插件只在本地把 OCV 接到
第三方云接口，不修改、不再分发 OCV 源码；OCV 本体版权、官方仓库与品牌
使用边界见 NOTICE 和 [TRADEMARKS.md](./TRADEMARKS.md)。第三方组件与在线
服务说明见 [THIRD_PARTY_NOTICES.md](./THIRD_PARTY_NOTICES.md)。其中
「云端 OAuth」（jethub）改编自 MIT 许可的上游项目
deepseek-harness-codearts（Copyright (c) 2026 Jet）。

**风险提示：**

- 本插件依赖的各家免费额度（MiMo TTS、商汤 SenseNova、Agnes、云端 OAuth
  各提供商）的政策随时可能调整或终止，可用性不作任何保证；
- 「云端 OAuth」功能通过用户自己的账号令牌调用第三方编程助手服务。以
  自动化方式或账号池方式使用这些服务的额度**可能违反相应服务商的服务
  条款**，并可能导致账号被限制或封禁。该功能为可选项，是否启用由用户
  自行判断，风险与后果由用户自行承担；
- 本仓库与发行包不包含任何 API Key、账号令牌或用户数据：运行期凭据只
  存在于每台机器本地、被 Git 忽略的 `var/` 与 `state/` 目录。请只在自己
  的本地配置里填写密钥；一旦疑似泄露，立即到对应服务商后台作废轮换，
  详见 [SECURITY.md](./SECURITY.md)；
- 本软件按「原样」提供，不附带任何明示或默示担保。
