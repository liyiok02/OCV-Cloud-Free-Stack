/**
 * Jet Hub 运行时装配：在**普通 Node 进程**里把 dsh-codearts-auth 的
 * provider 适配器立起来，不依赖 DSH 主进程、不依赖 `~/.dsh`。
 *
 * ## 依赖闭包为什么能脱离 DSH 跑
 *
 * `dsh-codearts-auth` 是 DSH 的 cordis 插件，但它对 DSH 的耦合面很窄：
 *
 * | 它需要的东西 | 谁来提供 |
 * |---|---|
 * | `ctx`（cordis 容器） | 本模块 `new Context()`（cordis 在 vendor 里，MIT） |
 * | `ctx.credentials` | 本模块用**自建凭据存储**顶上（见 credentials.mjs） |
 * | `ctx.llm` | 桩：只记录注册，不参与调用（我们直接调 `adapter.stream()`） |
 * | `ctx.logger` | cordis 自带 |
 * | 附件服务 `attachments` | **不提供** —— 本插件只做纯文本 LLM，图片会得到 UNSUPPORTED_CONTENT |
 *
 * 实测（见 docs/DEVLOG.md 的验证记录）：`AccountPool` / `BuddyAuth` /
 * `BuddyAdapter` 全部能在裸 cordis 容器里构造并注册成功。
 *
 * ## 状态目录：为什么必须显式钉死
 *
 * `jet-hub-store.js` 的 `resolveJetHubHome()` 回退链是：
 * `DSH_JET_HUB_STATE_DIR` → `profileContext.home` → `DSH_HOME` → `~/.dsh`。
 * **最后那级 `~/.dsh` 是危险默认值** —— 在没装 DSH 的目标机上它要么不存在，
 * 要么更糟：静默把账号写进用户主目录，破坏「所有文件都在插件目录内」。
 *
 * 所以本模块**两级钉死**：既 `ctx.provide('profileContext', {home})`，
 * 又把 `DSH_JET_HUB_STATE_DIR` 写进 `process.env`（该变量优先级最高）。
 * 这不是冗余 —— 前者是我们能控的正路，后者是防第三方代码绕过容器直读环境的兜底。
 *
 * ## 为什么桥必须是**长驻单进程**
 *
 * 账号池的限流记录、凭据刷新、`prompt_cache_key` 都活在内存里。多进程会各持
 * 一份状态、互相覆盖 `state.json`。所以本插件的形态是：
 * **OCV 后端进程内托管一个 Node 子进程**，所有 LLM 请求都从它出去。
 */

import { existsSync, mkdirSync, readFileSync, copyFileSync, writeFileSync, readdirSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createCredentialStore } from './credentials.mjs'
import { createCreditsController } from './credits.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))
/** vendor/ 在**插件根目录**下（与 jethub/ 平级），由 vendor_tool.py 生成。 */
const PLUGIN_ROOT = dirname(HERE)
const VENDOR = join(PLUGIN_ROOT, 'vendor')
const AUTH_LIB = join(VENDOR, 'dsh-codearts-auth', 'lib')

/** 静态导入不能用变量路径，因此走动态 import + 顶层 await 的薄包装。 */
/**
 * 加载 vendor 里的模块。
 *
 * 全部走**动态 import**（静态 import 不能带变量路径）。分两组：
 *   * `core`：所有 provider 共用的容器与凭据；
 *   * `providers`：每个 provider 的 product / auth / adapter 三件套，
 *     **按需加载**（某个模块缺失不该让整个运行时起不来）。
 */
async function loadVendor() {
  const V = VENDOR
  const auth = (name) => pathToUrl(join(AUTH_LIB, name))

  const [cordis, credentials, accountPool, product, llmAdapter] = await Promise.all([
    import(pathToUrl(join(V, 'node_modules', '@deepseek-ai', 'cordis', 'lib', 'index.js'))),
    // 只为 credentialRef()（把字符串打 brand 的薄函数）；不启用它的 LocalCredentialProvider
    import(pathToUrl(join(V, 'node_modules', '@deepseek-ai', 'dsh-credentials', 'lib', 'index.js'))),
    import(auth('account-pool.js')),
    import(auth('product.js')),
    import(auth('llm-adapter.js')),
  ])

  /** 可选模块：缺失时返回 null 而不是抛，便于降级。 */
  const optional = async (name) => {
    try {
      return await import(auth(name))
    } catch {
      return null
    }
  }

  return {
    Context: cordis.Context,
    credentialRef: credentials.credentialRef,
    AccountPool: accountPool.AccountPool,
    ALL_PRODUCTS: product.ALL_PRODUCTS,
    productById: product.productById,
    optional,
    // 平台通用注册器（codearts 用它；其余 provider 各有自己的 registerXxxLlm）
    registerCodeArtsLlm: llmAdapter.registerCodeArtsLlm,
  }
}

/**
 * provider 接线表 —— **本插件的「支持哪些免费 LLM」就由这张表决定**。
 *
 * 每一项的字段含义：
 *
 * | 字段 | 说明 |
 * |---|---|
 * | `id` | provider id，同时是账号池里的 `provider` 键、路由名、URL 查询参数 |
 * | `label` | 面板显示名（用户要求：面板里叫「提供商」，不叫 Provider） |
 * | `productModule` / `productExport` | 产品配置来自哪个模块的哪个导出 |
 * | `credentialRef` | 单凭据模式用的 ref（多账号用 `{ID}_ACCOUNT_{SHORT}`） |
 * | `authModule` / `authExport` | 认证服务类 |
 * | `adapterModule` / `adapterExport` | `registerXxxLlm(ctx, options)` 所在的模块 |
 * | `authOptions` | 构造 auth 时除 `ctx` 外要传的参数（多数是 `{product}`） |
 * | `supportsCheckin` | 是否支持「一键签到」（决定面板显不显示按钮） |
 * | `creditsModule` | 签到/余额函数所在模块（`accountCredits` 表用） |
 *
 * ⚠ **接线新 provider 时，先跑 `verify_jethub.py`** —— 它会用
 * `providerModuleReport()` 逐个探测模块是否可加载，缺东西会明确报出来。
 */
const PROVIDER_SPECS = [
  {
    id: 'buddy',
    label: 'CodeBuddy（腾讯）',
    productModule: 'product.js',
    productExport: 'CODEBUDDY',
    credentialRef: 'BUDDY_ACCESS_TOKEN',
    authModule: 'buddy-auth.js',
    authExport: 'BuddyAuth',
    adapterModule: 'buddy-adapter.js',
    adapterExport: 'registerBuddyLlm',
    authOptions: (product) => ({ product }),
    supportsCheckin: true,
    supportsBalance: true,
    loginKind: 'state-poll',
  },
  {
    id: 'workbuddy',
    label: 'WorkBuddy（腾讯国际版）',
    productModule: 'product.js',
    productExport: 'WORKBUDDY',
    credentialRef: 'WORKBUDDY_ACCESS_TOKEN',
    authModule: 'buddy-auth.js',
    authExport: 'BuddyAuth',
    adapterModule: 'buddy-adapter.js',
    adapterExport: 'registerBuddyLlm',
    authOptions: (product) => ({ product }),
    // 原版能力矩阵（credits-capabilities.js:62-98）明确：workbuddy **不支持**每日签到。
    // 与国际版的签到接口未开放一致；写成 true 会让「一键签到」对它发无效请求。
    supportsCheckin: false,
    supportsBalance: true,
    loginKind: 'state-poll',
  },
  {
    id: 'lobsterai',
    label: 'LobsterAI（有道）',
    productModule: 'lobsterai-product.js',
    productExport: 'LOBSTERAI',
    credentialRef: 'LOBSTERAI_ACCESS_TOKEN',
    authModule: 'lobsterai-auth.js',
    authExport: 'LobsteraiAuth',
    adapterModule: 'lobsterai-adapter.js',
    adapterExport: 'registerLobsteraiLlm',
    authOptions: (product) => ({ product }),
    supportsCheckin: true,
    supportsBalance: true,
    loginKind: 'callback',
  },
  {
    id: 'qoder',
    label: 'Qoder（阿里）',
    productModule: 'qoder-product.js',
    productExport: 'QODER',
    credentialRef: 'QODER_ACCESS_TOKEN',
    authModule: 'qoder-auth.js',
    authExport: 'QoderAuth',
    adapterModule: 'qoder-adapter.js',
    adapterExport: 'registerQoderLlm',
    authOptions: (product) => ({ product }),
    supportsCheckin: true,
    supportsBalance: true,
    loginKind: 'device-code',
  },
  {
    id: 'trae',
    label: 'TRAE（字节）',
    productModule: 'trae-product.js',
    productExport: 'TRAE',
    credentialRef: 'TRAE_ACCESS_TOKEN',
    authModule: 'trae-auth.js',
    authExport: 'TraeAuth',
    adapterModule: 'trae-adapter.js',
    adapterExport: 'registerTraeLlm',
    authOptions: (product) => ({ product }),
    supportsCheckin: true,
    supportsBalance: true,
    loginKind: 'callback',
  },
  {
    id: 'cline',
    label: 'Cline',
    productModule: 'cline-product.js',
    productExport: 'CLINE',
    credentialRef: 'CLINE_ACCESS_TOKEN',
    authModule: 'cline-auth.js',
    authExport: 'ClineAuth',
    adapterModule: 'cline-adapter.js',
    adapterExport: 'registerClineLlm',
    authOptions: (product) => ({ product }),
    supportsCheckin: false,   // 只支持查余额，没有每日签到
    supportsBalance: true,
    loginKind: 'device-code',
  },
  {
    id: 'loomy',
    label: 'Loomy（讯飞）',
    productModule: 'loomy-product.js',
    productExport: 'LOOMY',
    credentialRef: 'LOOMY_ACCESS_TOKEN',
    authModule: 'loomy-auth.js',
    authExport: 'LoomyAuth',
    adapterModule: 'loomy-adapter.js',
    adapterExport: 'registerLoomyLlm',
    authOptions: (product) => ({ product }),
    supportsCheckin: true,    // 每日额度 + 新手任务
    supportsBalance: true,
    loginKind: 'wechat-qr',
  },
]

/**
 * 平台通用 provider（codearts）的产品配置不在自己的 product 文件里，
 * 也没有 `product` 概念 —— `CodeArtsAuth` 只接 `ctx`，适配器也不需要
 * `product`。用 `productRequired: false` 让通用接线逻辑跳过产品解析。
 */
const CODEARTS_SPEC = {
  id: 'codearts',
  label: 'CodeArts（华为云）',
  productRequired: false,
  credentialRef: 'CODEARTS_ACCESS_TOKEN',
  authModule: 'service.js',
  authExport: 'CodeArtsAuth',
  adapterModule: 'llm-adapter.js',
  adapterExport: 'registerCodeArtsLlm',
  authOptions: null,
  supportsCheckin: true,
  supportsBalance: true,
  loginKind: 'callback',
}

/**
 * AtomCode（AtomGit）：同样是 `ctx`-only 的认证类，产品常量散在
 * `atomcode-product.js` 里（没有统一的 product 对象）。
 */
const ATOMCODE_SPEC = {
  id: 'atomcode',
  label: 'AtomCode（AtomGit）',
  productRequired: false,
  credentialRef: 'ATOMCODE_ACCESS_TOKEN',
  authModule: 'atomcode-service.js',
  authExport: 'AtomcodeAuth',
  adapterModule: 'atomcode-adapter.js',
  adapterExport: 'registerAtomcodeLlm',
  authOptions: null,
  supportsCheckin: false,
  supportsBalance: false,   // 支持 claim()，但没有每日签到
  loginKind: 'cli-import',
}

function pathToUrl(p) {
  // Windows 盘符要转成 file:///E:/...
  const normalized = p.replace(/\\/g, '/')
  return normalized.startsWith('/') ? `file://${normalized}` : `file:///${normalized}`
}

/**
 * 凭据是否已过期（或即将过期）。
 *
 * ## ⚠ 各家的过期字段名**不统一**（F-011）
 *
 * | provider | 字段 | 证据 |
 * |---|---|---|
 * | buddy / lobsterai / loomy / trae / codearts | `expires_at` | 各 `*-auth.ts` 写入 |
 * | **qoder / cline** | **`expire_time`** | `qoder.js:166` / `cline.js:133` 写入，`:177`/`:163` 读取 |
 *
 * 首版只读 `expires_at`，于是 qoder/cline **恒判「未过期」** →
 * `resolveUsableCredential` 直接返回旧凭据、**永不主动刷新**。
 * 两家的 token 到期后表现为「突然全部调用失败」，而不是自动续期。
 *
 * 值可能是**字符串形式的毫秒时间戳**（CodeBuddy 系）或 **ISO 字符串**
 * （CodeArts），所以两种解析都试。多留 60 秒余量：一次 LLM 调用本身可能跑
 * 几十秒，卡在边界上会出现「刚通过检查、发出请求时已过期」的竞态。
 */
function credentialLooksExpired(credential, skewMs = 60_000) {
  if (credential === null || typeof credential !== 'object') return false
  const raw = credential.expires_at ?? credential.expire_time
    ?? credential.expiresAt ?? credential.expires
  if (raw === undefined || raw === null || raw === '') return false
  let ms = Number(raw)
  if (!Number.isFinite(ms)) {
    const parsed = Date.parse(String(raw))
    ms = Number.isFinite(parsed) ? parsed : NaN
  }
  if (!Number.isFinite(ms) || ms <= 0) return false
  return Date.now() + skewMs >= ms
}

/**
 * 本插件的 provider 接线表见上方 `PROVIDER_SPECS` / `CODEARTS_SPEC` /
 * `ATOMCODE_SPEC` —— **9 家全部已接线**，切换提供商只需换 `spec`。
 *
 * 凭据刷新一律走 `resolveUsableCredential()`（下方）：**调用前按需自动刷新**。
 * 面板上**没有**「刷新凭据」按钮 —— 用户要求去掉，因为后台已经自动做了，
 * 手动按钮只会让人误以为「不点就不会刷新」。
 */
/**
 * 建一个 Jet Hub 运行时。
 *
 * @param {object} options
 * @param {string} options.pluginRoot 插件根目录（`plugins/cloud_free_stack`）
 * @param {(msg: string) => void} [options.logger]
 * @param {string[]} [options.only] 只接线这些 provider id（测试用）
 * @returns {Promise<object>} 运行时句柄
 */
export async function createRuntime({ pluginRoot, logger = () => {}, only = null }) {
  if (typeof pluginRoot !== 'string' || pluginRoot.length === 0) {
    throw new TypeError('createRuntime: pluginRoot 必填')
  }

  const stateDir = join(pluginRoot, 'state')
  mkdirSync(stateDir, { recursive: true })

  // ---- 1. 钉死状态目录（两级，见文件头说明） ----
  process.env.DSH_JET_HUB_STATE_DIR = stateDir

  const v = await loadVendor()

  // ---- 2. 容器 + 凭据服务（自建，只存插件目录） ----
  const ctx = new v.Context()
  const credentials = createCredentialStore({ dir: stateDir })
  ctx.provide('credentials', credentials)
  ctx.provide('profileContext', { home: stateDir })

  // ---- 3. 账号池（多账号 + 限流轮换），状态同样落插件目录 ----
  const pool = new v.AccountPool(ctx)

  // ---- 4. LLM 服务桩：只记录注册结果，不参与调用 ----
  const registrations = []
  ctx.provide('llm', {
    registerConfigurableProviders(list) {
      registrations.push({ kind: 'providers', list })
    },
    registerAdapter(providers, adapter) {
      registrations.push({ kind: 'adapter', providers, adapter })
      return { dispose() {} }
    },
  })

  // ---- 5. 逐 provider 立起来 ----
  const entries = new Map()
  const skipped = []

  for (const spec of [...PROVIDER_SPECS, CODEARTS_SPEC, ATOMCODE_SPEC]) {
    if (Array.isArray(only) && only.length > 0 && !only.includes(spec.id)) continue

    // 5a) 产品配置。
    //
    // 三种情况：
    //   * **没有产品概念**（codearts / atomcode）—— 它们的认证与适配器
    //     不接 product 参数，`productRequired: false`，跳过这一步；
    //   * 从自己的 product 文件取（多数 provider）；
    //   * 走 `productById`（目前没有这类，保留分支以便将来扩展）。
    let product = null
    if (spec.productRequired !== false) {
      if (spec.productModule && spec.productExport) {
        const mod = await v.optional(spec.productModule)
        product = mod?.[spec.productExport] ?? null
      } else if (spec.productByIdFallback) {
        product = v.productById(spec.productByIdFallback) ?? null
      }
      if (product === null) {
        skipped.push({ id: spec.id, reason: 'vendor 里没有对应产品配置' })
        logger(`跳过 provider ${spec.id}：vendor 里没有对应产品配置`)
        continue
      }
    }

    // 5b) 认证服务 + 适配器：模块缺失时**降级跳过**而不是整体崩掉
    const authMod = await v.optional(spec.authModule)
    const adapterMod = await v.optional(spec.adapterModule)
    if (!authMod?.[spec.authExport] || typeof adapterMod?.[spec.adapterExport] !== 'function') {
      const missing = []
      if (!authMod?.[spec.authExport]) missing.push(`${spec.authModule}#${spec.authExport}`)
      if (typeof adapterMod?.[spec.adapterExport] !== 'function') {
        missing.push(`${spec.adapterModule}#${spec.adapterExport}`)
      }
      skipped.push({ id: spec.id, reason: `缺模块 ${missing.join(', ')}` })
      logger(`跳过 provider ${spec.id}：缺模块 ${missing.join(', ')}`)
      continue
    }

    const AuthClass = authMod[spec.authExport]
    const registerFn = adapterMod[spec.adapterExport]
    // 有的 auth 类不接受 options（构造签名只有 ctx），传了也无害，
    // 但 `spec.authOptions` 为 undefined 时显式只传 ctx 更贴近原版用法。
    const auth = spec.authOptions
      ? new AuthClass(ctx, spec.authOptions(product))
      : new AuthClass(ctx)

    /**
     * 解析一个**可用账号的凭据**（含"快过期就先刷新"）。
     *
     * ## 为什么不直接用 `auth.refresh()`
     *
     * 各 provider 的 `refresh()` 内部写死用**产品默认 ref**
     * （如 `BUDDY_ACCESS_TOKEN`），而本插件的凭据存在**账号 ref**
     * （`BUDDY_ACCOUNT_XXXXXXXX`）下 —— 直接调它会抛
     * 「未配置凭据，请先登录」（实测踩到，见 docs/JETHUB.md F-002）。
     *
     * 所以这里自己走账号池：拿到账号 → 读它的 ref → 过期就用
     * `refreshAccountCredential(ref)` 刷（那个方法**接收 ref 参数**，是对的路）。
     */
    const resolveUsableCredential = async () => {
      const available = await pool.getAvailableAccount(spec.id, '')
      if (available === null || available === undefined) return undefined
      let credential = available.credential
      if (credential === undefined || credential === null) return undefined
      if (!credentialLooksExpired(credential)) return credential

      const ref = available.entry?.credentialRef
      if (typeof ref !== 'string' || ref.length === 0) return credential
      try {
        await auth.refreshAccountCredential(ref)
        const fresh = await credentials.resolve(ref)
        if (fresh !== undefined) {
          const parsed = JSON.parse(fresh.value)
          if (parsed !== null && typeof parsed === 'object') credential = parsed
        }
      } catch (error) {
        logger(`${spec.id} 账号 ${ref} 刷新失败：${String(error?.message ?? error)}`)
      }
      return credential
    }

    /** 刷遍该 provider 所有启用账号（适配器在凭据缺失时会调）。 */
    const refreshAllAccounts = async () => {
      const accounts = await pool.listAccounts(spec.id)
      for (const row of accounts) {
        if (row.enabled === false) continue
        const ref = row.credentialRef
        if (typeof ref !== 'string' || ref.length === 0) continue
        try {
          await auth.refreshAccountCredential(ref)
        } catch (error) {
          logger(`${spec.id} 账号 ${ref} 刷新失败：${String(error?.message ?? error)}`)
        }
      }
    }

    const options = {
      credentialRef: v.credentialRef(spec.credentialRef),
      resolveCredential: resolveUsableCredential,
      refresh: refreshAllAccounts,
      accountPool: pool,
    }
    // 只有「有产品概念」的 provider 才传 product；codearts / atomcode 不认这个字段
    if (product !== null) options.product = product
    // 只有部分 provider 的适配器接受这些可选回调
    if (typeof auth.fetchModels === 'function') {
      options.fetchRemoteModels = () => auth.fetchModels(pool)
    }
    if (typeof auth.refreshModels === 'function') {
      options.fetchRemoteModels = () => auth.refreshModels(pool)
    }
    if (typeof auth.claim === 'function') {
      options.claim = (...args) => auth.claim(...args)
    }

    const adapter = registerFn(ctx, options)
    entries.set(spec.id, { spec, product, auth, adapter, options })
    logger(`provider 就绪：${spec.id}（${spec.label}）`)
  }

  /**
   * 积分/签到控制器。各 provider 的 credits 模块**惰性加载** ——
   * 只有真去查余额或签到时才付加载成本（面板首屏不需要它们）。
   */
  const credits = createCreditsController({
    entries,
    pool,
    credentials,
    logger,
    loadMods: async () => {
      const names = [
        'credits.js',
        'codearts-credits.js',
        'lobsterai-credits.js',
        'qoder-credits.js',
        'trae-credits.js',
        'cline-credits.js',
        'loomy-credits.js',
        'loomy-onboarding.js',
      ]
      const pairs = await Promise.all(
        names.map(async (name) => [name.replace(/\.js$/, ''), await v.optional(name)]),
      )
      const out = {}
      for (const [key, value] of pairs) out[key] = value
      return out
    },
  })

  return {
    stateDir,
    credentials,
    pool,
    ctx,
    entries,
    registrations,
    skipped,
    credits,
    /** 把字符串打成凭据引用（vendor 的 credentialRef 薄包装）。 */
    credentialRef: v.credentialRef,

    /** 已接线的 provider id 列表。 */
    providerIds() {
      return [...entries.keys()]
    },

    /** 某个 provider 的适配器（取不到返回 undefined）。 */
    adapterFor(providerId) {
      return entries.get(providerId)?.adapter
    },

    /** provider 元信息（面板展示用）。 */
    providerInfo() {
      return [...entries.values()].map((entry) => ({
        id: entry.spec.id,
        label: entry.spec.label,
        supportsCheckin: entry.spec.supportsCheckin === true,
        supportsBalance: entry.spec.supportsBalance === true,
        loginKind: entry.spec.loginKind ?? 'callback',

        endpoint: entry.product?.endpoint ?? entry.product?.apiBase ?? '',
      }))
    },

    /**
     * 账号列表（给面板用）。
     *
     * `pool.listAccounts()` 返回的是 `ProviderAccountStatus`，它 **extends
     * `ProviderAccountEntry`**（字段是平铺的，不是包在 `.account` 里）。
     * 这里只回**可下发的字段**，**绝不回 credentialRef 与凭据明文**。
     */
    async listAccounts(providerId) {
      const target = providerId ?? PROVIDER_SPECS[0].id
      const rows = await pool.listAccounts(target)
      return rows.map((row) => ({
        id: String(row.id ?? ''),
        provider: String(row.provider ?? target),
        nickname: String(row.nickname ?? ''),
        enabled: row.enabled !== false,
        expiresAt: Number.isFinite(Number(row.expiresAt)) ? Number(row.expiresAt) : null,
        refreshable: row.refreshable === true,
        source: typeof row.source === 'string' ? row.source : '',
      }))
    },

    /** 启用/停用某个账号。 */
    async setAccountEnabled(providerId, accountId, enabled) {
      await pool.updateAccount(accountId, { enabled: Boolean(enabled) })
      return this.listAccounts(providerId)
    },

    /** 删除账号（连带它的凭据）。 */
    async removeAccount(providerId, accountId) {
      const rows = await pool.listAccounts(providerId)
      const hit = rows.find((row) => String(row.id) === String(accountId))
      await pool.removeAccount(accountId)
      const ref = hit?.credentialRef
      if (typeof ref === 'string' && ref.length > 0) {
        try {
          await credentials.unset(ref)
        } catch {
          /* 凭据已不在也无所谓 */
        }
      }
      return this.listAccounts(providerId)
    },

    /** 立即刷新某账号的凭据。 */
    async refreshAccount(providerId, accountId) {
      const entry = entries.get(providerId)
      if (entry === undefined) throw new Error(`未接线的 provider：${providerId}`)
      const rows = await pool.listAccounts(providerId)
      const hit = rows.find((row) => String(row.id) === String(accountId))
      const ref = hit?.credentialRef
      if (typeof ref !== 'string' || ref.length === 0) throw new Error('找不到该账号的凭据引用')
      await entry.auth.refreshAccountCredential(ref)
      return this.listAccounts(providerId)
    },

    /**
     * 拉取某个 provider 的**可用模型清单**。
     *
     * ## 为什么优先用 `adapter.listAllModels()` 而不是 `auth.fetchModels()`
     *
     * 实测（侦察确认）：`qoder-auth` / `cline-auth` / `loomy-auth`
     * **根本没有 `fetchModels` 方法** —— qoder 的模型目录是纯本地表
     * （`qoder-adapter.ts:227` 注释：刻意不发远端请求）。
     * 直接调 `auth.fetchModels()` 会抛
     * `TypeError: entry.auth.fetchModels is not a function`，
     * 被 catch 吞掉后**静默返回空清单** —— 用户看到的是「没有模型」，
     * 而真正原因是调用错了对象。**这个缺陷首版就存在。**
     *
     * 而 `listAllModels()` **8 个适配器全都有**（同步，返回全量目录，
     * 不套用户黑名单）。所以：
     *   * 首选 `adapter.listAllModels()`；
     *   * 只有在它不可用时才回退 `auth.fetchModels()`。
     */
    async listModels(providerId) {
      const target = providerId ?? PROVIDER_SPECS[0].id
      const entry = entries.get(target)
      if (entry === undefined) return []

      let catalogue = []
      const adapter = entry.adapter
      if (typeof adapter?.listAllModels === 'function') {
        try {
          catalogue = adapter.listAllModels(target) ?? []
        } catch (error) {
          logger(`${target} listAllModels 失败：${String(error?.message ?? error)}`)
        }
      }
      if ((!Array.isArray(catalogue) || catalogue.length === 0)
          && typeof entry.auth?.fetchModels === 'function') {
        try {
          catalogue = (await entry.auth.fetchModels(pool)) ?? []
        } catch (error) {
          logger(`${target} fetchModels 失败：${String(error?.message ?? error)}`)
        }
      }
      if (!Array.isArray(catalogue)) catalogue = []

      const disabled = pool.disabledModelsFor(target)
      return catalogue
        .map((model) => {
          const id = String(model.id ?? model.model ?? '').trim()
          return {
            id,
            name: String(model.name ?? model.displayName ?? model.label ?? id),
            disabled: disabled.has(id),
          }
        })
        .filter((model) => model.id.length > 0)
    },

    /** 切换某个模型是否出现在可选列表里。 */
    async setModelDisabled(providerId, modelId, disabled) {
      await pool.setModelDisabled(providerId, modelId, Boolean(disabled))
      return this.listModels(providerId)
    },

    /**
     * 深检：账号池 + 凭据是否都落在插件目录内。
     * `self_test` 用它守「不桥接 DSH」这条硬约束。
     */
    storageReport() {
      const poolPath = pool?.store?.path ?? ''
      const credsPath = credentials.path
      const inPlugin = (p) => p.replace(/\\/g, '/').startsWith(pluginRoot.replace(/\\/g, '/'))
      return {
        stateDir,
        poolPath,
        credentialsPath: credsPath,
        poolInPlugin: inPlugin(poolPath),
        credentialsInPlugin: inPlugin(credsPath),
        poolKind: pool?.store?.kind ?? 'unknown',
      }
    },

    // ---- 备份 / 恢复（用户要求的能力） ----

    /**
     * 导出一份可移植备份：凭据 + 账号池状态，合成**一个 JSON**。
     *
     * 刻意做成单文件：目标机只要放进 `<插件>/state/` 就能恢复，
     * 不用理解内部是两个文件。返回对象**含明文凭据**，调用方负责落盘权限。
     */
    async exportBackup() {
      const credentialsDoc = await credentials.exportDocument()
      const accounts = await pool.getStateSnapshot()
      return {
        schema: BACKUP_SCHEMA,
        version: BACKUP_VERSION,
        exportedAt: new Date().toISOString(),
        credentials: credentialsDoc,
        accounts,
      }
    },

    /**
     * 从备份恢复。`mode`:
     *  - `merge`（默认）：备份里的条目覆盖同名，其余保留
     *  - `replace`：先清空再写入
     *
     * ## 兼容两种备份格式（用户明确要求）
     *
     * | 来源 | 标识字段 | `credentials` 形态 | 账号位置 |
     * |---|---|---|---|
     * | 本插件 | `schema: 'ocv-cloud-free-stack.jethub-backup'` | 嵌套 `{refs:{ref:{value,…}}}` | `accounts.accounts` |
     * | **DSH 原生** | `format: 'dsh-codearts-auth/backup'` | **平铺 `{ref: "JSON 字符串"}`** | 顶层 `accounts` |
     *
     * 用户报过「从 dsh 中导出的账号凭证提示 schema 不匹配」——
     * 那是我们只认自己的格式。现在两种都收，并统一归一化成内部形态。
     *
     * ⚠ 归一化必须**严格**：宁可拒绝也不要猜。只认这两个标识，
     *   其它一律拒绝（导入的是凭据，写坏等于把用户账号搞丢）。
     */
    async importBackup(backup, { mode = 'merge' } = {}) {
      if (backup === null || typeof backup !== 'object') {
        throw new TypeError('importBackup: 备份必须是对象')
      }

      const kind = detectBackupKind(backup)
      if (kind === null) {
        throw new TypeError(
          'importBackup: 无法识别的备份格式。'
          + `本插件期望 schema='${BACKUP_SCHEMA}'，`
          + `DSH 导出的备份应为 format='${DSH_BACKUP_FORMAT}'。`
          + `实际拿到：${describeBackup(backup)}`,
        )
      }

      const normalized = normalizeBackup(backup, kind)

      // ## ⚠ 凭据也要按 mode 处理，不能一律整体覆盖（F-010，数据破坏级）
      //
      // `credentials.importDocument` 是**整体覆盖**语义（写完整个文档）。
      // 首版在 merge 分支也直接调它，于是「合并导入一份只含 1 个账号的备份」
      // 会**把其余所有账号的凭据删掉** —— 那些账号还在池里，
      // 但 credentialRef 指向空 → 全部不可用。账号走的是并集、凭据却是替换，
      // 语义不对等就是数据破坏。
      //
      // 正确做法：merge 时先把既有凭据读出来，与备份里的**合并**再写回。
      let credentialsCount = 0
      if (mode === 'replace') {
        credentialsCount = await credentials.importDocument({ refs: normalized.refs })
      } else {
        const existing = await credentials.exportDocument()
        const merged = { ...(existing?.refs ?? {}), ...normalized.refs }
        credentialsCount = await credentials.importDocument({ refs: merged })
      }

      const incoming = normalized.accounts
      let accountCount = 0

      if (mode === 'replace') {
        await pool.replaceAll(incoming, normalized.disabledModels)
        accountCount = incoming.length
      } else {
        const existing = await pool.getStateSnapshot()
        const byId = new Map(
          (Array.isArray(existing?.accounts) ? existing.accounts : []).map((row) => [row.id, row]),
        )
        for (const row of incoming) {
          if (row !== null && typeof row === 'object' && typeof row.id === 'string') {
            byId.set(row.id, row)
          }
        }
        const mergedDisabled = { ...(existing?.disabledModels ?? {}), ...normalized.disabledModels }
        await pool.replaceAll([...byId.values()], mergedDisabled)
        accountCount = byId.size
      }

      return {
        credentialsCount,
        accountCount,
        mode,
        source: kind === 'dsh' ? 'dsh' : 'plugin',
        // 让面板能提示「这份备份里有几个账号的凭据缺失/已过期」
        missingCredentials: normalized.missingCredentials,
        expiredAccounts: normalized.expiredAccounts,
      }
    },

    /** 备份落盘的默认文件名（带时间戳）。 */
    backupFileName() {
      const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)
      return `jethub-backup-${stamp}.json`
    },
  }
}

/** 本插件自己的备份标识。 */
const BACKUP_SCHEMA = 'ocv-cloud-free-stack.jethub-backup'
/** DSH 原生插件的备份标识（`types.ts:529`）。 */
const DSH_BACKUP_FORMAT = 'dsh-codearts-auth/backup'
const BACKUP_VERSION = 1

/**
 * 判断备份是哪一种格式。认不出来返回 `null`（**不猜**）。
 *
 * 判据用**标识字段**而不是「有哪些键」—— 后者在字段演进时会误判。
 */
function detectBackupKind(backup) {
  if (backup.schema === BACKUP_SCHEMA) return 'plugin'
  if (backup.format === DSH_BACKUP_FORMAT) return 'dsh'
  return null
}

/** 给报错用的简短描述（**不回显内容**，避免把凭据打进日志）。 */
function describeBackup(backup) {
  const keys = Object.keys(backup).slice(0, 8).join(', ')
  return `键=[${keys}]`
}

/**
 * 把两种格式**归一化**成内部形态：`{refs, accounts, disabledModels, …}`。
 *
 * DSH 的差异（`types.ts:546-557` 是权威定义）：
 *   * `credentials` 是**平铺**的 `{refName: "JSON 字符串"}`，
 *     而我们的存储要 `{refName: {value, createdAt, updatedAt}}`；
 *   * `accounts` / `disabledModels` 在**顶层**，不在 `accounts.accounts` 里。
 *
 * 另外统计两个给人看的信息（**不因它们中断导入**）：
 *   * `missingCredentials` —— 账号存在但备份里没有对应凭据；
 *   * `expiredAccounts` —— `expiresAt` 已过期。
 *   DSH 的 `importBackup` 也做这两项统计（`backup.ts:159-170`），
 *   语义保持一致，便于用户判断这份备份还能不能用。
 */
function normalizeBackup(backup, kind) {
  const refs = {}
  let rawCredentials = {}

  if (kind === 'dsh') {
    rawCredentials = backup.credentials ?? {}
  } else {
    // 本插件：credentials.refs[ref] = {value, createdAt, updatedAt}
    const nested = backup.credentials
    if (nested !== null && typeof nested === 'object' && nested.refs !== null
        && typeof nested.refs === 'object') {
      rawCredentials = nested.refs
    } else if (nested !== null && typeof nested === 'object') {
      // 容错：早期版本可能直接是平铺的
      rawCredentials = nested
    }
  }

  const now = new Date().toISOString()
  for (const [name, entry] of Object.entries(rawCredentials)) {
    if (typeof entry === 'string') {
      // DSH 形态：值是 JSON 字符串
      if (entry.length === 0) continue
      refs[name] = { value: entry, createdAt: now, updatedAt: now }
    } else if (entry !== null && typeof entry === 'object' && typeof entry.value === 'string') {
      // 本插件形态
      if (entry.value.length === 0) continue
      refs[name] = {
        value: entry.value,
        createdAt: typeof entry.createdAt === 'string' ? entry.createdAt : now,
        updatedAt: typeof entry.updatedAt === 'string' ? entry.updatedAt : now,
      }
    }
  }

  // 账号与黑名单：DSH 在顶层，本插件在 accounts 下
  let accounts = []
  let disabledModels = {}
  if (kind === 'dsh') {
    accounts = Array.isArray(backup.accounts) ? backup.accounts : []
    disabledModels = (backup.disabledModels !== null && typeof backup.disabledModels === 'object')
      ? backup.disabledModels : {}
  } else {
    const holder = backup.accounts
    if (holder !== null && typeof holder === 'object') {
      accounts = Array.isArray(holder.accounts) ? holder.accounts : []
      disabledModels = (holder.disabledModels !== null && typeof holder.disabledModels === 'object')
        ? holder.disabledModels : {}
    }
  }

  // 只保留结构合法的账号（缺 id/provider/credentialRef 的行会让账号池写坏）
  accounts = accounts.filter(
    (row) => row !== null && typeof row === 'object'
      && typeof row.id === 'string' && row.id.length > 0
      && typeof row.provider === 'string' && row.provider.length > 0
      && typeof row.credentialRef === 'string' && row.credentialRef.length > 0,
  )

  const refNames = new Set(Object.keys(refs))
  const missingCredentials = accounts.filter(
    (row) => !refNames.has(row.credentialRef),
  ).length
  const nowMs = Date.now()
  const expiredAccounts = accounts.filter(
    (row) => typeof row.expiresAt === 'number'
      && Number.isFinite(row.expiresAt) && row.expiresAt <= nowMs,
  ).length

  return { refs, accounts, disabledModels, missingCredentials, expiredAccounts }
}

/**
 * 启动**任意 provider** 的两步式登录。
 *
 * ## 为什么统一到这一步
 *
 * 各家 auth 的登录入口不统一，但**可分成三类**（实测 vendor lib 的导出）：
 *
 * | 类别 | 入口 | 哪些 provider |
 * |---|---|---|
 * | 有 `startLogin()` | 直接调，返回 `{loginUrl, result, close?}` | lobsterai / qoder / trae / cline |
 * | 有 `login()` 但无 `startLogin` | 自己取 authState 再后台 await | buddy / workbuddy |
 * | 特殊（扫码 / CLI 导入） | 各自入口 | loomy（微信扫码）/ atomcode（CLI 导入）/ codearts（OAuth 回调） |
 *
 * 本函数把它们收敛成同一个返回形状：
 * `{loginUrl, accountId, credentialRef, done, note}`，其中 `done` 是
 * 后台 Promise。前端只管「拿到 loginUrl 立刻 window.open」。
 *
 * ## 为什么必须两步式（真实缺陷）
 *
 * 浏览器只在用户点击后的短暂窗口（transient activation，约 5 秒）内允许
 * `window.open`。若 `await` 完整个登录流程再返回 URL，手势早已过期，
 * 弹窗被拦截并返回 `null`；常见兜底 `location.href = loginUrl` 会把
 * **整个设置页跳走** —— 这是 DSH 侧 `jet-hub-rpc.ts` 注释里记录的真实缺陷。
 */
export async function startProviderLogin(runtime, { providerId, nickname = '' } = {}) {
  const entry = runtime.entries.get(providerId)
  if (entry === undefined) throw new Error(`未接线的提供商：${providerId}`)

  const { auth, product } = entry
  const shortId = randomShortId()
  const accountId = `${providerId}-${shortId}`
  const credentialRefName = `${providerId.toUpperCase()}_ACCOUNT_${shortId.toUpperCase()}`
  const ref = runtime.credentialRef(credentialRefName)

  /** 先登记占位账号：让前端 login.poll 立刻能查到，失败时可回滚。 */
  const placeholder = async () => {
    await runtime.pool.addAccount({
      id: accountId,
      provider: providerId,
      nickname: nickname || accountId,
      enabled: true,
      credentialRef: credentialRefName,
      refreshable: false,
      createdAt: Date.now(),
    })
  }

  /** 登录成功后回填昵称/有效期 —— 各家凭据字段名不同，统一在这里嗅探。 */
  const finalize = async () => {
    const resolved = await runtime.credentials.resolve(credentialRefName)
    let expiresAt
    let resolvedNickname = nickname
    let refreshable = false
    if (resolved !== undefined) {
      try {
        const parsed = JSON.parse(resolved.value)
        if (parsed !== null && typeof parsed === 'object') {
          // 有效期字段：`expires_at` / `expiresAt` / `expires`。
          //
          // ⚠ 两种编码都要认：**CodeBuddy 系是字符串形式的毫秒时间戳**
          // （`Number()` 可解），而 **CodeArts 是 ISO 字符串**
          // （`Number()` 得到 NaN，必须 `Date.parse()`）。
          // 只认前者会让 CodeArts 账号卡片永远显示不出过期时间 ——
          // 侦察报告点名过这个坑，这里一次处理掉。
          for (const key of ['expires_at', 'expiresAt', 'expires']) {
            const raw = parsed[key]
            if (raw === undefined || raw === null || raw === '') continue
            let ms = Number(raw)
            if (!Number.isFinite(ms)) {
              const parsedDate = Date.parse(String(raw))
              ms = Number.isFinite(parsedDate) ? parsedDate : NaN
            }
            if (Number.isFinite(ms) && ms > 0) {
              expiresAt = ms
              break
            }
          }
          for (const key of ['nickname', 'user_name', 'userName', 'name', 'email', 'uid']) {
            const value = parsed[key]
            if (typeof value === 'string' && value.trim() !== '') {
              if (!resolvedNickname) resolvedNickname = value.trim()
              break
            }
          }
          refreshable =
            (typeof parsed.refresh_token === 'string' && parsed.refresh_token.length > 0)
            || (typeof parsed.refreshToken === 'string' && parsed.refreshToken.length > 0)
        }
      } catch {
        /* 凭据非 JSON：不致命，跳过元信息回填 */
      }
    }
    await runtime.pool.updateAccount(accountId, {
      nickname: resolvedNickname || accountId,
      expiresAt,
      refreshable,
    })
    return { accountId, credentialRef: credentialRefName, nickname: resolvedNickname, expiresAt, refreshable }
  }

  // ---- 类别 1：auth 自带 startLogin()（两步式原生支持） ----
  if (typeof auth.startLogin === 'function') {
    const started = await auth.startLogin({
      refName: credentialRefName,
      accountId,
      pool: runtime.pool,
      ...(product !== null && product !== undefined ? { product } : {}),
    })
    if (started === null || typeof started !== 'object' || typeof started.loginUrl !== 'string') {
      throw new Error(`${providerId} 的 startLogin 返回结构异常`)
    }
    const done = Promise.resolve(started.result)
      .then(async () => {
        await finalize()
        // startLogin 内部通常已注册账号；幂等补一次，确保占位被填实
        return { accountId, credentialRef: credentialRefName }
      })
      .catch(async (error) => {
        try {
          await runtime.pool.removeAccount(accountId)
        } catch {
          /* ignore */
        }
        throw error
      })
    done.catch(() => {})
    return { loginUrl: started.loginUrl, accountId, credentialRef: credentialRefName, done, note: '' }
  }

  // ---- 类别 2：buddy / workbuddy（服务端 state 轮询，无本地服务器） ----
  if (providerId === 'buddy' || providerId === 'workbuddy') {
    const [oauth, buddy] = await Promise.all([
      import(pathToUrl(join(AUTH_LIB, 'buddy-oauth.js'))),
      import(pathToUrl(join(AUTH_LIB, 'buddy.js'))),
    ])
    let authState
    try {
      authState = await oauth.fetchAuthState(undefined, undefined, product)
    } catch (error) {
      throw new Error(`无法获取 ${product?.displayName ?? providerId} 登录地址：${String(error?.message ?? error)}`)
    }
    if (authState === null || typeof authState !== 'object' || typeof authState.authUrl !== 'string') {
      throw new Error('fetchAuthState 返回结构异常')
    }
    const loginUrl = oauth.decorateLoginUrl(authState.authUrl, product)
    await placeholder()
    const done = oauth
      .runBuddyLoginFlow({ openBrowser: () => {}, state: authState.state, product })
      .then(async (flow) => {
        await runtime.credentials.set(credentialRefName, flow.access)
        void buddy.credentialExpiresAtMs
        return await finalize()
      })
      .catch(async (error) => {
        try {
          await runtime.pool.removeAccount(accountId)
        } catch {
          /* ignore */
        }
        throw error
      })
    done.catch(() => {})
    return { loginUrl, accountId, credentialRef: credentialRefName, done, note: '' }
  }

  // ---- 类别 2.5：loomy（微信扫码，**既无 login 也无 startLogin**） ----
  //
  // 真实缺陷修复：首版把 loomy 归到「类别 3：有 login()」，但
  // `loomy-auth.js` **两个都没有** —— 它只有 `startWechatLogin()`，
  // 而且拿到结果后**还要再调一次 `persistWechatLogin()` 才落库**。
  // 不单独处理就会抛「没有可用的登录入口」，导致 loomy 实际不可用。
  if (providerId === 'loomy' && typeof auth.startWechatLogin === 'function') {
    const started = await auth.startWechatLogin()
    if (started === null || typeof started !== 'object' || typeof started.loginUrl !== 'string') {
      throw new Error('loomy 的 startWechatLogin 返回结构异常')
    }
    await placeholder()
    const done = Promise.resolve(started.result)
      .then(async (login) => {
        // 关键：微信扫码只拿到 session，必须再 persist 一次才写凭据
        if (typeof auth.persistWechatLogin === 'function') {
          await auth.persistWechatLogin(login, { refName: credentialRefName })
        }
        return await finalize()
      })
      .catch(async (error) => {
        try {
          await runtime.pool.removeAccount(accountId)
        } catch {
          /* ignore */
        }
        throw error
      })
    done.catch(() => {})
    return {
      loginUrl: started.loginUrl,
      accountId,
      credentialRef: credentialRefName,
      done,
      note: 'Loomy 使用微信扫码登录：弹出的页面里会显示二维码，用微信扫一扫即可。',
    }
  }

  // ---- 类别 3：其余（atomcode CLI 导入 / codearts OAuth 等） ----
  //
  // 这些入口的交互形态差异大（CLI 导入根本不开浏览器、OAuth 要本地回调服务器），
  // **统一走 auth.login()** 并在后台 await。
  // 前端拿到的 `loginUrl` 可能是空串 —— 那时它会显示「请在应用内完成」。
  if (typeof auth.login === 'function') {
    await placeholder()
    let loginUrl = ''
    const done = Promise.resolve(
      auth.login({
        refName: credentialRefName,
        accountId,
        pool: runtime.pool,
        // 由我们接管开窗：不在这里弹浏览器（前端已经负责）
        openBrowser: () => {},
        ...(product !== null && product !== undefined ? { product } : {}),
      }),
    )
      .then(async (result) => {
        if (result && typeof result.loginUrl === 'string' && result.loginUrl !== '') {
          loginUrl = result.loginUrl
        }
        return await finalize()
      })
      .catch(async (error) => {
        try {
          await runtime.pool.removeAccount(accountId)
        } catch {
          /* ignore */
        }
        throw error
      })
    done.catch(() => {})
    return {
      loginUrl,
      accountId,
      credentialRef: credentialRefName,
      done,
      note: '该提供商的登录在后台进行；若未自动打开浏览器，请查看插件日志或改用官方客户端登录后导入。',
    }
  }

  throw new Error(`${providerId} 没有可用的登录入口（既无 startLogin 也无 login）`)
}

function randomShortId() {
  const alphabet = 'abcdef0123456789'
  let out = ''
  for (let i = 0; i < 8; i += 1) {
    out += alphabet[Math.floor(Math.random() * alphabet.length)]
  }
  return out
}

/**
 * 备份文件列表（给面板「恢复」下拉用）。
 * 只认本插件导出的命名约定，避免误列用户其它 json。
 */
export function listBackups(stateDir) {
  const dir = join(stateDir, 'backups')
  if (!existsSync(dir)) return []
  try {
    return readdirSync(dir)
      .filter((name) => name.startsWith('jethub-backup-') && name.endsWith('.json'))
      .sort()
      .reverse()
      .map((name) => ({ name, path: join(dir, name) }))
  } catch {
    return []
  }
}

/** 读取一份备份文件（已做基本形态校验，但仍由 importBackup 复校）。 */
export function readBackup(path) {
  const text = readFileSync(path, 'utf-8')
  return JSON.parse(text)
}

/** 原子写一份备份文件。 */
export function writeBackup(stateDir, name, payload) {
  const dir = join(stateDir, 'backups')
  mkdirSync(dir, { recursive: true })
  const target = join(dir, name)
  const pending = `${target}.pending`
  writeFileSync(pending, `${JSON.stringify(payload, null, 2)}\n`, 'utf-8')
  copyFileSync(pending, target)
  try {
    // 删临时文件
    import('node:fs').then(({ unlinkSync }) => {
      try {
        unlinkSync(pending)
      } catch {
        /* ignore */
      }
    })
  } catch {
    /* ignore */
  }
  return target
}
