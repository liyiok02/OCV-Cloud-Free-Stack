/**
 * Jet Hub 桥的 HTTP 服务端（Node，标准库 `node:http`，无第三方框架）。
 *
 * 两类端点，同一端口（默认 8801）：
 *
 * | 端点 | 谁在用 |
 * |---|---|
 * | `POST /v1/chat/completions`、`GET /v1/models` | **OCV 本体**（当作一个 OpenAI 兼容服务商） |
 * | `GET /health` | Python 侧托管层探活 |
 * | `POST /rpc/{method}` | **插件面板**（账号管理、登录、模型开关、备份恢复） |
 *
 * ## 为什么自己写 HTTP 而不用框架
 *
 * vendor 里已经有 express 之类吗？没有 —— `dsh-codearts-auth` 的运行时依赖
 * 只有 cordis / dsh-llm / jose / zod 等，**不含任何 web 框架**。为了不为了
 * 一个 20 行的路由表再 vendor 一个框架，这里直接用 `node:http`。
 *
 * ## 安全
 *
 * **只绑 127.0.0.1**。DSH 侧的同类端点在插件层是零鉴权的（它的信任栅栏
 * 由宿主提供，且注释自认 "not an auth layer"）。本服务不假设宿主有栅栏，
 * 因此：绑回环 + 校验 `Host` 头必须是回环地址 + 拒绝带 `Origin` 的跨站请求。
 * 这是最小可用的纵深防御，能挡住「浏览器里的恶意页面打本机端口」。
 */

import { createServer } from 'node:http'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { readFileSync, writeFileSync, mkdirSync, existsSync, unlinkSync, statSync } from 'node:fs'

const HERE = dirname(fileURLToPath(import.meta.url))
const PLUGIN_ROOT = dirname(HERE)

const argv = process.argv.slice(2)
function argValue(flag, fallback) {
  const index = argv.indexOf(flag)
  return index >= 0 && index + 1 < argv.length ? argv[index + 1] : fallback
}
const PORT = Number(argValue('--port', process.env.JETHUB_BRIDGE_PORT || '8801'))
const ROOT = argValue('--plugin-root', process.env.JETHUB_PLUGIN_ROOT || PLUGIN_ROOT)
const HOST = '127.0.0.1'

/** 单个请求体的上限：LLM 请求可能带长文案，给 32 MB 足够，也挡住内存炸弹。 */
const MAX_BODY_BYTES = 32 * 1024 * 1024

const {
  getRuntime,
  complete,
  listModelsOpenAi,
  startProviderLogin,
  listBackups,
  readBackup,
  writeBackup,
} = await import(pathToUrl(join(HERE, 'bridge.mjs')))

function pathToUrl(p) {
  const normalized = p.replace(/\\/g, '/')
  return normalized.startsWith('/') ? `file://${normalized}` : `file:///${normalized}`
}

// ---------------------------------------------------------------------------
// HTTP 基础设施
// ---------------------------------------------------------------------------

function sendJson(res, status, payload) {
  const body = Buffer.from(JSON.stringify(payload, null, 2), 'utf-8')
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': String(body.length),
    'Cache-Control': 'no-store',
    // 本服务只给本机回环用；显式声明不共享，避免被代理缓存
    'X-Content-Type-Options': 'nosniff',
  })
  res.end(body)
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = []
    let size = 0
    req.on('data', (chunk) => {
      size += chunk.length
      if (size > MAX_BODY_BYTES) {
        reject(new Error(`请求体超过 ${Math.round(MAX_BODY_BYTES / 1024 / 1024)} MB 上限`))
        req.destroy()
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf-8')))
    req.on('error', reject)
  })
}

/**
 * 本机请求校验。
 *
 * 目的：挡住「用户浏览器里的任意网页偷偷 POST 到 127.0.0.1:8801」。
 * 三道判据都是**宽松但有效**的：
 *  1. `Host` 必须是回环（防 DNS rebinding：攻击者域名解析到 127.0.0.1 时，
 *     Host 头仍是攻击者域名，这条能挡住）；
 *  2. 有 `Origin` 时必须是本插件的面板来源（面板在 :8799，可能跨端口调）；
 *  3. `Sec-Fetch-Site` 不允许 cross-site。
 */
function isLocalRequest(req) {
  const host = String(req.headers.host ?? '')
  const hostname = host.split(':')[0].toLowerCase()
  if (hostname !== '127.0.0.1' && hostname !== 'localhost' && hostname !== '[::1]') return false

  const origin = req.headers.origin
  if (typeof origin === 'string' && origin.length > 0) {
    let ok = false
    try {
      const url = new URL(origin)
      const originHost = url.hostname.toLowerCase()
      ok = originHost === '127.0.0.1' || originHost === 'localhost'
    } catch {
      ok = false
    }
    if (!ok) return false
  }

  const site = String(req.headers['sec-fetch-site'] ?? '').toLowerCase()
  if (site === 'cross-site') return false
  return true
}

// ---------------------------------------------------------------------------
// RPC：面板用的账号/模型/备份操作
// ---------------------------------------------------------------------------

const RPC_METHODS = {
  /** 桥自身的运行信息（不含凭据）。 */
  async 'status'() {
    const runtime = await getRuntime()
    // 给每家附上**账号数**。
    //
    // ## 为什么要带上它（F-019 的真实缺陷）
    //
    // `models.list` 对**没登录**的 provider 也会返回默认模型表（vendor 的
    // 静态兜底），于是「聚合全部提供商的可用模型」会把 6 个没账号的
    // provider 的模型也列进去 —— 用户下拉里 45 个模型只有 15 个真能用。
    // 光看模型列表是分不出「有没有账号」的，必须在 status 里带上账号数，
    // 让聚合层据此过滤（见 `models_catalog._fetch_jethub`）。
    const providers = runtime.providerInfo()
    const withAccounts = []
    for (const item of providers) {
      let accountCount = 0
      try {
        const rows = await runtime.listAccounts(item.id)
        accountCount = rows.length
      } catch {
        /* 取不到就当 0 —— 宁可少列，也不列不可用的 */
      }
      withAccounts.push({ ...item, accountCount })
    }
    return {
      providers: withAccounts,
      storage: runtime.storageReport(),
      pluginRoot: ROOT,
      port: PORT,
    }
  },

  /** 账号列表。 */
  async 'accounts.list'(payload) {
    const runtime = await getRuntime()
    return { accounts: await runtime.listAccounts(payload?.provider ?? 'buddy') }
  },

  /**
   * 开始登录：返回 loginUrl 供前端立刻 window.open。
   *
   * **两步式**：这里立刻返回 URL（保住浏览器手势），后台 Promise 完成授权。
   * 前端随后用 `accounts.poll` 轮询结果。
   *
   * 支持全部 9 个 provider —— 差异由 `startProviderLogin` 收敛
   * （各家的登录形态不同：OAuth 回调 / 设备码 / 服务端 state 轮询 / 微信扫码 / CLI 导入）。
   */
  async 'accounts.login.start'(payload) {
    const runtime = await getRuntime()
    const providerId = payload?.provider ?? 'buddy'
    const started = await startProviderLogin(runtime, {
      providerId,
      nickname: String(payload?.nickname ?? ''),
    })
    // 不 await `done`：它要等用户在浏览器里完成授权
    pruneLoginJobs()
    loginJobs.set(started.accountId, { startedAt: Date.now(), providerId })
    started.done
      .then(() => {
        loginJobs.delete(started.accountId)
        console.log(`[jethub] 登录成功：${started.accountId}`)
      })
      .catch((error) => {
        loginJobs.delete(started.accountId)
        console.log(`[jethub] 登录失败 ${started.accountId}：${String(error?.message ?? error)}`)
      })
    return {
      loginUrl: started.loginUrl,
      accountId: started.accountId,
      provider: providerId,
      note: started.note ?? '',
    }
  },

  /**
   * 轮询登录状态。
   *
   * 判据极简且可靠：**账号是否已出现在池里且凭据已落盘**
   * （与 DSH 侧 `login.poll` 同义）。占位账号在 start 时就已写入，
   * 所以这里额外核对凭据是否真的存在，避免把"占位"误判成"完成"。
   */
  async 'accounts.poll'(payload) {
    const runtime = await getRuntime()
    const providerId = payload?.provider ?? 'buddy'
    const accountId = String(payload?.accountId ?? '')
    if (accountId.length === 0) throw new Error('缺少 accountId')

    const accounts = await runtime.listAccounts(providerId)
    const hit = accounts.find((row) => row.id === accountId)
    if (hit === undefined) {
      return { done: true, success: false, error: '账号不存在（可能已登录失败并被清理）' }
    }
    // 凭据文件里有没有这个账号的 ref
    const all = await runtime.pool.listAccounts(providerId)
    const entry = all.find((row) => String(row.id) === accountId)
    const ref = entry?.credentialRef
    let configured = false
    if (typeof ref === 'string' && ref.length > 0) {
      const resolved = await runtime.credentials.resolve(ref)
      configured = resolved !== undefined
    }
    return {
      done: configured,
      success: configured,
      account: hit,
      pending: !configured,
    }
  },

  /** 启用/停用账号。 */
  async 'accounts.toggle'(payload) {
    const runtime = await getRuntime()
    return {
      accounts: await runtime.setAccountEnabled(
        payload?.provider ?? 'buddy',
        String(payload?.accountId ?? ''),
        Boolean(payload?.enabled),
      ),
    }
  },

  /** 删除账号。 */
  async 'accounts.remove'(payload) {
    const runtime = await getRuntime()
    return {
      accounts: await runtime.removeAccount(
        payload?.provider ?? 'buddy',
        String(payload?.accountId ?? ''),
      ),
    }
  },

  /** 立即刷新某账号凭据。 */
  async 'accounts.refresh'(payload) {
    const runtime = await getRuntime()
    return {
      accounts: await runtime.refreshAccount(
        payload?.provider ?? 'buddy',
        String(payload?.accountId ?? ''),
      ),
    }
  },

  /** 模型清单（含禁用标记）。 */
  async 'models.list'(payload) {
    const runtime = await getRuntime()
    return { models: await runtime.listModels(payload?.provider ?? 'buddy') }
  },

  /** 切换单个模型的显示开关。 */
  async 'models.setDisabled'(payload) {
    const runtime = await getRuntime()
    return {
      models: await runtime.setModelDisabled(
        payload?.provider ?? 'buddy',
        String(payload?.modelId ?? ''),
        Boolean(payload?.disabled),
      ),
    }
  },

  /** 批量开关（「打开全部 / 关闭全部」）。 */
  async 'models.setAllDisabled'(payload) {
    const runtime = await getRuntime()
    const providerId = payload?.provider ?? 'buddy'
    const disabled = Boolean(payload?.disabled)
    // **黑名单制的不对称语义**（对齐原版 setAllDisabled）：
    //   * disabled=true  → 逐项加入黑名单（必须先知道有哪些模型）
    //   * disabled=false → 清空黑名单（**不读目录**，才能清掉历史遗留的幽灵键）
    // 这层不对称 UI 上看不见，但决定了「打开全部」是唯一能清理遗留键的入口。
    if (disabled) {
      const rows = await runtime.listModels(providerId)
      for (const row of rows) {
        await runtime.pool.setModelDisabled(providerId, row.id, true)
      }
    } else {
      await runtime.pool.clearDisabledModels(providerId)
    }
    return { models: await runtime.listModels(providerId) }
  },

  // ---- 积分 / 签到 ----

  /** 能力矩阵：前端据此决定「显示哪些按钮」，不支持的连请求都不发。 */
  async 'credits.capabilities'() {
    const runtime = await getRuntime()
    return { capabilities: runtime.credits.capabilities() }
  },

  /** 查某 provider 的余额（逐账号）。 */
  async 'credits.balances'(payload) {
    const runtime = await getRuntime()
    return await runtime.credits.balances(payload?.provider ?? 'buddy')
  },

  /**
   * **一键签到**：跨 provider 串行（绝不并发 —— 这是真实写请求，
   * 并发会触发风控，见 credits.mjs 文件头铁律 1）。
   */
  async 'credits.claimAll'(payload) {
    const runtime = await getRuntime()
    const providers = Array.isArray(payload?.providers) ? payload.providers : null
    return await runtime.credits.claimAll({ providers })
  },

  /** 领取新手任务（目前只有 Loomy）。 */
  async 'credits.claimOnboarding'(payload) {
    const runtime = await getRuntime()
    return await runtime.credits.claimOnboarding(payload?.provider ?? 'loomy')
  },

  // ---- 备份 / 恢复 ----

  /**
   * 导出备份。
   *
   * **两种出口**，前端按需选：
   *   * `download: true`（默认）→ 把整份备份 JSON **直接回给前端**，
   *     由浏览器触发「下载到本地」——用户自己挑存哪；
   *   * `download: false` → 落到 `<插件>/state/backups/`（内部留档）。
   *
   * 之前只有后者，导致「恢复」被迫要求用户把文件手工塞进某个固定目录 ——
   * 用户要求改成**从本地上传**，所以导出也必须能拿到文件本体。
   */
  async 'backup.export'(payload) {
    const runtime = await getRuntime()
    const doc = await runtime.exportBackup()
    const name = String(payload?.name ?? runtime.backupFileName())
    const summary = {
      name,
      accounts: doc.accounts?.accounts?.length ?? 0,
      credentials: Object.keys(doc.credentials?.refs ?? {}).length,
    }
    if (payload?.download === false) {
      const target = writeBackup(runtime.stateDir, name, doc)
      return { ...summary, path: target, mode: 'server' }
    }
    // 回文件本体：前端用 Blob + <a download> 触发保存
    return { ...summary, mode: 'download', document: doc }
  },

  /** 列出服务端留档的备份（仅供查看/删除，不再是恢复的唯一入口）。 */
  async 'backup.list'() {
    const runtime = await getRuntime()
    return { backups: listBackups(runtime.stateDir) }
  },

  /** 删除服务端留档的备份。 */
  async 'backup.remove'(payload) {
    const runtime = await getRuntime()
    const name = sanitizeBackupName(payload?.name)
    const path = join(runtime.stateDir, 'backups', name)
    if (!existsSync(path)) throw new Error(`备份不存在：${name}`)
    unlinkSync(path)
    return { name, removed: true }
  },

  /**
   * 从备份恢复。**两种来源**：
   *   * `document`（优先）：前端读用户上传的文件后把**内容**发来 —— 这是主路径，
   *     用户可以从任何地方上传，不再要求放进固定目录；
   *   * `name`：服务端留档的文件名（向后兼容）。
   *
   * 无论哪种来源，都先做**schema 与形态校验**再写盘 —— 导入的是凭据，
   * 写坏等于把用户的账号搞丢。
   */
  async 'backup.import'(payload) {
    const runtime = await getRuntime()
    let doc = null
    let source = ''

    if (payload?.document !== undefined && payload.document !== null) {
      if (typeof payload.document === 'string') {
        try {
          doc = JSON.parse(payload.document)
        } catch (error) {
          throw new Error(`上传的文件不是合法 JSON：${String(error?.message ?? error)}`)
        }
      } else if (typeof payload.document === 'object') {
        doc = payload.document
      } else {
        throw new Error('document 必须是对象或 JSON 字符串')
      }
      source = String(payload?.file_name ?? '（上传的文件）')
    } else {
      const name = sanitizeBackupName(payload?.name)
      const path = join(runtime.stateDir, 'backups', name)
      if (!existsSync(path)) throw new Error(`备份不存在：${name}`)
      doc = readBackup(path)
      source = name
    }

    const result = await runtime.importBackup(doc, { mode: String(payload?.mode ?? 'merge') })
    return { ...result, name: source, source: payload?.document !== undefined ? 'upload' : 'server' }
  },
}

/** 备份文件名白名单过滤（防路径穿越）。非法字符一律拒绝而不是静默清洗。 */
function sanitizeBackupName(raw) {
  const name = String(raw ?? '')
  if (name.length === 0) throw new Error('缺少备份文件名')
  const safe = name.replace(/[^A-Za-z0-9._-]/g, '')
  if (safe !== name) throw new Error('备份文件名含非法字符')
  return safe
}

/**
 * 进行中的登录作业（accountId → 元信息）。
 *
 * ## 为什么要 TTL 清理（F-013）
 *
 * 首版只 `set` / `delete`，**从不读取**，也不清理超时项。若某次登录的
 * `done` 永不 settle（用户打开授权页后一直不完成、直接关掉），条目会
 * **永久留在 Map 里**；桥是长驻进程，长时间运行会缓慢累积。
 *
 * 现在：写入时顺手清掉超过 `LOGIN_JOB_TTL_MS` 的陈旧条目。
 * （保留这个 Map 仍有价值 —— 它让 `/health` 能反映「有几个登录在进行中」。）
 */
const loginJobs = new Map()
const LOGIN_JOB_TTL_MS = 30 * 60 * 1000

function pruneLoginJobs() {
  const cutoff = Date.now() - LOGIN_JOB_TTL_MS
  for (const [key, value] of loginJobs) {
    if (!value || typeof value.startedAt !== 'number' || value.startedAt < cutoff) {
      loginJobs.delete(key)
    }
  }
}

// ---------------------------------------------------------------------------
// 路由
// ---------------------------------------------------------------------------

const server = createServer(async (req, res) => {
  const started = Date.now()
  let pathname = '/'
  try {
    const url = new URL(req.url ?? '/', `http://${HOST}:${PORT}`)
    pathname = url.pathname

    if (!isLocalRequest(req)) {
      sendJson(res, 403, { ok: false, error: '仅接受本机回环请求' })
      return
    }

    // ---- 健康检查 ----
    if (pathname === '/health' && req.method === 'GET') {
      sendJson(res, 200, {
        ok: true,
        service: 'ocv-jethub-bridge',
        pid: process.pid,
        root: ROOT,
        port: PORT,
        version: 1,
      })
      return
    }

    // ---- OpenAI 兼容：模型清单 ----
    if ((pathname === '/v1/models' || pathname === '/models') && req.method === 'GET') {
      const provider = new URL(req.url ?? '/', `http://${HOST}:${PORT}`).searchParams.get('provider') || 'buddy'
      sendJson(res, 200, await listModelsOpenAi(provider))
      return
    }

    // ---- OpenAI 兼容：对话补全 ----
    if (pathname === '/v1/chat/completions' || pathname === '/chat/completions') {
      if (req.method !== 'POST') {
        sendJson(res, 405, { ok: false, error: '只支持 POST' })
        return
      }
      const raw = await readBody(req)
      let payload
      try {
        payload = raw.trim().length > 0 ? JSON.parse(raw) : {}
      } catch (error) {
        sendJson(res, 400, { error: { message: `请求体不是合法 JSON：${String(error?.message ?? error)}`, type: 'invalid_request_error' } })
        return
      }
      // OCV 若发 stream:true，明确拒绝而不是静默按非流式处理
      if (payload?.stream === true) {
        sendJson(res, 400, {
          error: {
            message: '本桥仅支持非流式（stream:false）。OCV 使用非流式调用；如需流式请改用上游原生端点。',
            type: 'invalid_request_error',
          },
        })
        return
      }
      const provider = new URL(req.url ?? '/', `http://${HOST}:${PORT}`).searchParams.get('provider') || 'buddy'
      const result = await complete(payload, { providerId: provider })
      if (result.ok) {
        sendJson(res, 200, result.body)
      } else {
        // 用 OpenAI 的错误封套，让 OCV 的 _extract_error_message 能取到 detail
        sendJson(res, result.status, {
          error: { message: result.error, type: result.status === 503 ? 'overloaded_error' : 'api_error' },
        })
      }
      return
    }

    // ---- 面板 RPC ----
    if (pathname.startsWith('/rpc/')) {
      if (req.method !== 'POST') {
        sendJson(res, 405, { ok: false, error: '只支持 POST' })
        return
      }
      const method = pathname.slice('/rpc/'.length)
      const handler = RPC_METHODS[method]
      if (handler === undefined) {
        sendJson(res, 404, { ok: false, error: `未知方法：${method}` })
        return
      }
      const raw = await readBody(req)
      let payload = {}
      try {
        payload = raw.trim().length > 0 ? JSON.parse(raw) : {}
      } catch (error) {
        sendJson(res, 400, { ok: false, error: `参数不是合法 JSON：${String(error?.message ?? error)}` })
        return
      }
      try {
        const value = await handler(payload)
        sendJson(res, 200, { ok: true, value })
      } catch (error) {
        sendJson(res, 500, { ok: false, error: String(error?.message ?? error) })
      }
      return
    }

    sendJson(res, 404, { ok: false, error: `未知路径：${pathname}` })
  } catch (error) {
    try {
      sendJson(res, 500, { ok: false, error: `${String(error?.name ?? 'Error')}: ${String(error?.message ?? error)}` })
    } catch {
      /* 响应已发出 */
    }
  } finally {
    const ms = Date.now() - started
    if (ms > 3000) console.log(`[jethub] ${req.method} ${pathname} 用时 ${ms}ms`)
  }
})

// 端口占用是**异步 error 事件**：不注册 handler 会成为进程级 unhandled error。
server.on('error', (error) => {
  console.error(`[jethub] 服务错误：${String(error?.message ?? error)}`)
  if (String(error?.code) === 'EADDRINUSE') {
    console.error(`[jethub] 端口 ${PORT} 已被占用。若是本插件上一次的残留进程，可运行：`)
    console.error(`[jethub]   node ${join(HERE, 'server.mjs')} --port ${PORT}   （换端口）`)
    process.exit(2)
  }
})

server.listen(PORT, HOST, () => {
  console.log(`[jethub] 桥已监听 http://${HOST}:${PORT}  (root=${ROOT})`)
})

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => {
    console.log(`[jethub] 收到 ${signal}，正在关闭…`)
    server.close(() => process.exit(0))
    setTimeout(() => process.exit(0), 2000).unref()
  })
}
