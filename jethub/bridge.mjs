/**
 * OCV 侧的 OpenAI 兼容桥：把 OCV 的**非流式** `/chat/completions` 请求
 * 转成 dsh-codearts-auth 适配器的**流式**调用，聚合成一个普通 JSON 响应。
 *
 * ## 为什么需要这层
 *
 * 两端契约不一致，且**都不该改**：
 *
 * | 维度 | OCV（`gemini_client.py`） | vendor 适配器 |
 * |---|---|---|
 * | 传输 | `requests.post`，**非流式**（全仓 `stream` 出现 0 次） | body 里写死 `stream:true`（`buddy-adapter.ts:989`） |
 * | 响应 | 读 `choices[0].message.content` | 吐 `StreamChunk` 异步迭代器 |
 * | 请求体 | 发 `response_format`（JSON 模式时） | **不转发** `response_format`（已核实：grep 零命中） |
 * | 超时 | 读超时默认 **120 s**（`GEMINI_READ_TIMEOUT_SECONDS`） | 内部排队重试最长 **180×10 s = 30 min** |
 *
 * 所以桥的职责是四条：**收 JSON → 组装 GenerateOptions → 消费流并聚合 → 回 JSON**，
 * 外加两条契约补丁（见下）。
 *
 * ## 契约补丁 1：`response_format`
 *
 * 适配器不转发它 ⇒ 请求不会 400（好事），但模型不会再被强制输出 JSON。
 * OCV 的 Agent 0/1/2 对 JSON 是**硬依赖**（`story_plan.json` 的
 * `generation_source` 必须为 `"gemini"` 才允许继续，`pipeline.py:2345-2349`）。
 *
 * 桥的对策**不是**自己去改 vendor，而是：把 JSON 模式**降级成提示词约束**
 * （在 system 后追加一句明确的「只输出 JSON」），并在响应里回填
 * `finish_reason`，让 OCV 自己的解析/重试逻辑照常工作。
 * 这与 `cloud_free_stack/json_mode_guard.py` 的既有思路一致。
 *
 * ## 契约补丁 2：超时错配
 *
 * vendor 的排队重试可能长达 30 分钟，而 OCV 120 s 就断连。桥**不缩短** vendor
 * 的重试（那是它应对限流的核心机制），而是给每个请求挂一个**软上限**：
 * 超过 `timeoutMs` 就返回 503（带 `retryable` 语义），让 OCV 走它自己的
 * `RETRYABLE_STATUS_CODES` 重试，而不是让它在 120 s 处拿到一个断掉的连接。
 */

import { createRuntime, startProviderLogin, listBackups, readBackup, writeBackup } from './runtime.mjs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const PLUGIN_ROOT = dirname(HERE)

/** 默认软超时：略低于 OCV 的 120 s 读超时，留出返回错误体的时间。 */
const DEFAULT_TIMEOUT_MS = Number(process.env.JETHUB_BRIDGE_TIMEOUT_MS || 110_000)

let _runtime = null
let _runtimePromise = null

/**
 * 懒加载单例：桥与面板共用同一个 runtime（账号池必须单实例）。
 *
 * ## ⚠ 失败必须重置 promise（F-012）
 *
 * 首版一旦 `createRuntime` reject，`_runtimePromise` 就**永久留着那个已
 * reject 的 promise** —— 之后每次 `getRuntime()` 都 await 同一个失败结果，
 * **本次进程生命周期内桥彻底不可用**，而且用户只能靠重启 OCV 恢复。
 *
 * 现在失败时把 `_runtimePromise` 清空，下一次调用会**重新尝试装配**
 * （例如 vendor 目录刚被补全、端口刚被释放）。
 */
export async function getRuntime() {
  if (_runtime !== null) return _runtime
  if (_runtimePromise === null) {
    _runtimePromise = createRuntime({
      pluginRoot: PLUGIN_ROOT,
      logger: (msg) => console.log(`[jethub] ${msg}`),
    }).catch((error) => {
      // 清空以便下次重试；错误本身继续抛给调用方
      _runtimePromise = null
      throw error
    })
  }
  _runtime = await _runtimePromise
  return _runtime
}

// ---------------------------------------------------------------------------
// 消息与请求体转换
// ---------------------------------------------------------------------------

/**
 * 把 OpenAI 的 `messages` 转成适配器要的形状。
 *
 * 适配器的 `serializeMessages()` 只要求 `{role, content}`，且对
 * 0.1.5 简单形状**原样透传**（`message-shape.ts:119-122`）。所以这里保持
 * 最小转换：**system 单独摘出来**（`GenerateOptions.system` 是独立槽位，
 * 适配器会把它 unshift 到 messages 前），其余原样带过。
 *
 * 多模态 parts 会**降级成纯文本**：本插件只做文本 LLM，不带附件服务；
 * 图片块如果透传下去，适配器会因拿不到 `readImage` 而报 UNSUPPORTED_CONTENT。
 * 显式降级比让它抛错更好排查。
 */
export function toAdapterMessages(openaiMessages) {
  const systemParts = []
  const messages = []

  for (const raw of Array.isArray(openaiMessages) ? openaiMessages : []) {
    if (raw === null || typeof raw !== 'object') continue
    const role = String(raw.role ?? 'user')
    const text = flattenContent(raw.content)

    if (role === 'system' || role === 'developer') {
      if (text.length > 0) systemParts.push(text)
      continue
    }
    if (role !== 'user' && role !== 'assistant') continue
    messages.push({ role, content: text })
  }

  return { system: systemParts.join('\n\n'), messages }
}

/**
 * 把 OpenAI 的 content 展平成字符串。
 *
 * 支持三种形态：字符串、parts 数组、`null`（带 tool_calls 的 assistant 消息）。
 */
export function flattenContent(content) {
  if (typeof content === 'string') return content
  if (content === null || content === undefined) return ''
  if (!Array.isArray(content)) return String(content)
  const chunks = []
  for (const part of content) {
    if (part === null || typeof part !== 'object') {
      if (typeof part === 'string') chunks.push(part)
      continue
    }
    const type = String(part.type ?? '')
    if (type === 'text' && typeof part.text === 'string') {
      chunks.push(part.text)
    } else if (type === 'input_text' && typeof part.text === 'string') {
      chunks.push(part.text)
    } else if (type === 'image_url' || type === 'input_image') {
      // 显式降级，见函数上方说明
      chunks.push('[图片已忽略：本插件仅支持文本模型]')
    }
  }
  return chunks.join('')
}

/**
 * 把请求的 `{messages:[{role:'system'},...]}` 里 **messages 内**的 system 也摘出来。
 * OCV 已经把 system 单独放 `payload.messages[0]`，这里统一处理两种情况。
 */
function extractSystemFromMessages(system, messages) {
  const kept = []
  const extra = []
  for (const message of messages) {
    if (message.role === 'system') extra.push(message.content)
    else kept.push(message)
  }
  const combined = [system, ...extra].map((s) => String(s ?? '').trim()).filter(Boolean).join('\n\n')
  return { system: combined, messages: kept }
}

/** JSON 模式的提示词兜底（补 vendor 不转发 `response_format` 的缺口）。 */
const JSON_ONLY_HINT =
  '\n\n【输出格式要求】只输出一个合法的 JSON，不要任何解释、前言、Markdown 代码块或 ```json 包裹。'

function wantsJson(payload) {
  const rf = payload?.response_format
  if (rf === null || typeof rf !== 'object') return false
  const type = String(rf.type ?? '')
  return type === 'json_object' || type === 'json_schema'
}

/**
 * 构造适配器的 `GenerateOptions`。
 *
 * 注意 `provider` 必须是**已注册的路由名**（我们注册成 `buddy`），而
 * `model` 是具体模型 id。
 */
export function buildGenerateOptions(payload, providerId) {
  const initial = toAdapterMessages(payload?.messages)
  const { system, messages } = extractSystemFromMessages(initial.system, initial.messages)

  let finalSystem = system
  if (wantsJson(payload) && !/只输出.*JSON|JSON.*只输出/i.test(finalSystem)) {
    finalSystem += JSON_ONLY_HINT
  }

  const options = {
    provider: providerId,
    model: String(payload?.model ?? ''),
    messages,
  }
  if (finalSystem.length > 0) options.system = finalSystem
  if (Number.isFinite(Number(payload?.temperature))) options.temperature = Number(payload.temperature)
  if (Number.isFinite(Number(payload?.max_tokens))) options.maxTokens = Math.trunc(Number(payload.max_tokens))
  if (Array.isArray(payload?.stop) && payload.stop.length > 0) options.stop = payload.stop.map(String)
  return options
}

// ---------------------------------------------------------------------------
// 流聚合
// ---------------------------------------------------------------------------

/**
 * 消费适配器的 `StreamChunk` 流并聚合成 OpenAI 形状。
 *
 * `StreamChunk` 的联合类型（`dsh-llm/lib/types/types.d.ts:359-389`）：
 * `block-start` / `text-delta` / `reasoning-delta` / `tool-call-delta` /
 * `block-end` / `usage` / `finish`。我们只取 `text-delta` 拼正文，
 * `reasoning-delta` 拼思考（可选回吐），`usage` 与 `finish` 记元信息。
 */
export async function collectStream(stream, { timeoutMs = DEFAULT_TIMEOUT_MS, signal } = {}) {
  let text = ''
  let reasoning = ''
  let usage = null
  let finishReason = 'stop'

  const controller = new AbortController()
  const onAbort = () => controller.abort()
  if (signal !== undefined) {
    if (signal.aborted) controller.abort()
    else signal.addEventListener('abort', onAbort, { once: true })
  }
  // 软超时：到点 abort，让 vendor 的重试循环停下，桥返回 503 而不是被 OCV 断连。
  const timer = setTimeout(() => controller.abort(), Math.max(1000, timeoutMs))

  try {
    for await (const chunk of stream) {
      if (chunk === null || typeof chunk !== 'object') continue
      switch (chunk.type) {
        case 'text-delta':
          text += String(chunk.text ?? '')
          break
        case 'reasoning-delta':
          reasoning += String(chunk.text ?? '')
          break
        case 'usage':
          usage = chunk.usage ?? null
          break
        case 'finish':
          // **保留原始对象**，不要在这里 `String()` —— `FinishReason` 是
          // 形如 `{kind:'stop'}` 的对象，提前字符串化会得到 "[object Object]"
          // 并让 OCV 永远检测不到 `length` 截断（见 mapFinishReason 的说明）。
          finishReason = chunk.reason ?? 'stop'
          break
        default:
          break
      }
    }
  } finally {
    clearTimeout(timer)
    if (signal !== undefined) signal.removeEventListener?.('abort', onAbort)
  }

  return { text, reasoning, usage, finishReason }
}

/** 把聚合结果包成 OpenAI 的 chat.completion 响应体。 */
export function toOpenAiResponse({ text, reasoning, usage, finishReason }, model) {
  const message = { role: 'assistant', content: text }
  if (reasoning.length > 0) message.reasoning_content = reasoning
  const body = {
    id: `chatcmpl-jethub-${Date.now().toString(36)}`,
    object: 'chat.completion',
    created: Math.floor(Date.now() / 1000),
    model: String(model ?? ''),
    choices: [
      {
        index: 0,
        message,
        // OCV 读 finish_reason 判定是否被长度截断（`gemini_client.py:613`）
        finish_reason: mapFinishReason(finishReason),
      },
    ],
  }
  if (usage !== null && typeof usage === 'object') {
    const prompt = Number(usage.inputTokens ?? usage.promptTokens ?? 0) || 0
    const completion = Number(usage.outputTokens ?? usage.completionTokens ?? 0) || 0
    // ⚠ `totalTokens` 在 vendor 那边是**可选**字段（`TokenUsage.totalTokens?`，
    // provider 不给就 omit）。首版直接 `?? 0` 会回一个 total=0 的 usage ——
    // 虽然 OCV 当前不读 total_tokens，但「有 prompt/completion 却 total=0」
    // 是明显不合理的账，会误导任何看 usage 的人（含未来的自己）。
    // 所以缺的时候就**自己加出来**。
    const declaredTotal = Number(usage.totalTokens)
    const total = Number.isFinite(declaredTotal) && declaredTotal > 0
      ? declaredTotal
      : prompt + completion
    body.usage = {
      prompt_tokens: prompt,
      completion_tokens: completion,
      total_tokens: total,
    }
    // 思考 token 单列（vendor 有就给）—— 排查「预算被思考吃光」时是唯一线索
    const reasoningTokens = Number(usage.reasoningTokens)
    if (Number.isFinite(reasoningTokens) && reasoningTokens > 0) {
      body.usage.completion_tokens_details = { reasoning_tokens: reasoningTokens }
    }
  }
  return body
}

/**
 * 适配器的 finish reason → OpenAI 措辞。
 *
 * ## ⚠ `FinishReason` 是**对象**，不是字符串
 *
 * `dsh-llm` 的 `FinishReason` 形如 `{ kind: 'stop' }` / `{ kind: 'length' }`
 * （vendor 的权威范本写的是 `reason.kind === 'stop'`，见
 * `sse.js` 的 `resolveEmptyResponseReason` 注释）。
 *
 * **首版用 `String(reason)` 直接得到字面量 `"[object Object]"`** ——
 * 那是个真实缺陷：OCV 靠 `finish_reason === 'length'` 判定截断并抛
 * `GeminiOutputTruncated`（`gemini_client.py:613`），
 * 拿不到 `length` 就**永远不会发现输出被截断**，只会拿到半截 JSON 然后
 * 在更下游报一个莫名其妙的解析错误。
 *
 * 所以这里先把各种可能形态**归一化成字符串**再判断。
 */
export function finishReasonText(reason) {
  if (reason === null || reason === undefined) return ''
  if (typeof reason === 'string') return reason.trim().toLowerCase()
  if (typeof reason === 'object') {
    // `{ kind: 'stop' }` 是权威形态；再兜几个可能的字段名
    for (const key of ['kind', 'type', 'reason', 'value', 'name']) {
      const value = reason[key]
      if (typeof value === 'string' && value.trim() !== '') return value.trim().toLowerCase()
    }
    return ''
  }
  return String(reason).trim().toLowerCase()
}

export function mapFinishReason(reason) {
  const text = finishReasonText(reason)
  if (text === 'length' || text === 'max-tokens' || text === 'max_tokens'
      || text === 'maxtokens' || text === 'token-limit') {
    return 'length'
  }
  if (text === 'tool-calls' || text === 'tool_calls' || text === 'toolcall') return 'tool_calls'
  if (text === 'content-filter' || text === 'content_filter') return 'content_filter'
  // 空、stop、end-turn、stop-sequence 一律当正常结束
  if (text === '' || text === 'stop' || text === 'end-turn' || text === 'end_turn'
      || text === 'stop-sequence' || text === 'stop_sequence' || text === 'completed') {
    return 'stop'
  }
  return text
}

// ---------------------------------------------------------------------------
// 对外 API（给 Python 侧的 HTTP 层调用）
// ---------------------------------------------------------------------------

/**
 * 把 `model` 字段拆成 **(providerId, modelId)**。
 *
 * ## 为什么需要它
 *
 * 用户在**语言模型分页**选择云端 OAuth 的某个模型，而 OCV 只把
 * `model` 这一个字符串发过来。不同提供商的模型 id 会重名
 * （`deepseek-v4-flash` 在 CodeBuddy / LobsterAI / Loomy 都存在），
 * 光看裸 id 无法路由。
 *
 * 所以模型清单里的 `value` 是 **`<provider>::<model>`**（见
 * `models_catalog._fetch_jethub`），这里按 `::` 拆开还原。
 *
 * 兼容两种形态：
 *   * `buddy::deepseek-v4-flash` → 路由到 buddy；
 *   * `deepseek-v4-flash`（裸 id，手工填的/历史值）→ 用 `fallbackProvider`。
 */
export function parseModelRef(rawModel, fallbackProvider = 'buddy') {
  const text = String(rawModel ?? '').trim()
  const index = text.indexOf('::')
  if (index > 0) {
    const providerId = text.slice(0, index).trim()
    const modelId = text.slice(index + 2).trim()
    if (providerId.length > 0 && modelId.length > 0) {
      return { providerId, modelId }
    }
  }
  return { providerId: String(fallbackProvider || 'buddy').trim(), modelId: text }
}

/**
 * 执行一次对话补全。返回 `{ok:true, body}` 或 `{ok:false, status, error}`。
 *
 * 这个函数**不碰 HTTP**，由 Python 侧的 `jethub_bridge.py` 决定怎么暴露
 * —— 这样桥的核心逻辑可以被 self_test 直接调用，不用起服务器。
 *
 * `payload.model` 既可以是 `provider::model`（面板下拉给的值），
 * 也可以是裸 model id（那时用 `providerId` 兜底）。
 */
export async function complete(payload, { providerId = 'buddy', timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
  const runtime = await getRuntime()

  const { providerId: target, modelId: model } = parseModelRef(payload?.model, providerId)
  if (model.length === 0) {
    return { ok: false, status: 400, error: '缺少 model 字段' }
  }

  const adapter = runtime.adapterFor(target)
  if (adapter === undefined) {
    return { ok: false, status: 400, error: `未接线的提供商：${target}` }
  }

  const accounts = await runtime.listAccounts(target).catch(() => [])
  if (accounts.length === 0) {
    return {
      ok: false,
      status: 503,
      error: `「${target}」还没有任何账号，请先在「云端 OAuth」分页登录`,
    }
  }

  let options
  try {
    // 注意：buildGenerateOptions 里 provider 用**真实提供商**，
    // model 用**去掉前缀的裸 id** —— 上游只认裸 id。
    options = buildGenerateOptions({ ...payload, model }, target)
  } catch (error) {
    return { ok: false, status: 400, error: `请求体转换失败：${String(error?.message ?? error)}` }
  }

  try {
    const collected = await collectStream(adapter.stream(options), { timeoutMs })
    if (collected.text.length === 0 && collected.reasoning.length === 0) {
      // 空响应：vendor 侧常见于排队/限流未抛错但也没内容。返回可重试语义，
      // 让 OCV 的 RETRYABLE_STATUS_CODES 接手，而不是把空串当成功。
      return { ok: false, status: 503, error: '上游返回空响应（可能被限流或排队，可重试）' }
    }
    return { ok: true, body: toOpenAiResponse(collected, model) }
  } catch (error) {
    const name = String(error?.name ?? '')
    if (name === 'AbortError') {
      return { ok: false, status: 503, error: `请求超过 ${Math.round(timeoutMs / 1000)} 秒未完成（可重试）` }
    }
    return { ok: false, status: 502, error: `${name || 'Error'}: ${String(error?.message ?? error)}` }
  }
}

/** 列出可选模型（OpenAI `/v1/models` 形状）。 */
export async function listModelsOpenAi(providerId = 'buddy') {
  const runtime = await getRuntime()
  let models = []
  try {
    models = await runtime.listModels(providerId)
  } catch (error) {
    console.log(`[jethub] listModels 失败：${String(error?.message ?? error)}`)
  }
  const visible = models.filter((model) => model.disabled !== true)
  return {
    object: 'list',
    data: visible.map((model) => ({
      id: model.id,
      object: 'model',
      owned_by: providerId,
      // 非标准字段：面板拿它显示中文名，OCV 会忽略
      display_name: model.name,
    })),
  }
}

export { startProviderLogin, listBackups, readBackup, writeBackup, PLUGIN_ROOT }
