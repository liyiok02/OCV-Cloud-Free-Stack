/**
 * 账号积分与「一键签到」。
 *
 * ## 为什么单独成模块
 *
 * 签到要调**每个 provider 各自不同**的接口，参数形态也不一样
 * （`claimDailyCheckin(credential, product)` vs `claimTraeDailyCheckin(x, y)`
 * vs `claimCodeArtsDailyCheckin(credential)`）。把这层差异收敛到这里，
 * 上层（HTTP RPC / 面板）就只需要「给我一个 provider id」。
 *
 * ## 两条铁律（来自原 Jet Hub 的真实事故）
 *
 * ### 1. 跨渠道**必须串行**，绝不用 `Promise.all`
 *
 * 原实现注释（`jet-hub.js:1659-1661`）记录得很清楚：单渠道内部已经是
 * 逐账号顺序执行（避免风控），跨渠道并发会**同时发出多路真实的领积分写请求**。
 * 这是会造成账号被风控的写操作，不是查询。所以这里用 `for ... await`。
 *
 * ### 2. 能力门控的正确做法是「不请求」，不是「请求后吞错」
 *
 * `credits-capabilities.js:9-14` 记录了真实事故：无门控地调
 * `credits.balances` 让 CodeArts **每次开面板都报错**、卡片永远显示「查询失败」。
 * 所以每个 provider 支持什么，由 `runtime.mjs` 的 `PROVIDER_SPECS` 里的
 * `supportsCheckin` / `supportsBalance` 声明，**不支持就根本不发请求**。
 *
 * ## 三态语义（不要混淆）
 *
 * `CreditBalanceRow` 的注释（`jet-hub.js:232-238`）强调：
 * **查不到 ≠ 余额为 0 ≠ 还没查**。所以本模块的返回值里
 * `status` 明确区分 `'ok' | 'unsupported' | 'error' | 'pending'`，
 * 绝不把「查询失败」渲染成「0 积分」。
 */

/** 每个 provider 的积分实现。函数签名统一为 `({credential, product, mods, auth}) => Promise<...>`。
 *
 * ## ⚠ 参数必须与 vendor 的真实签名**逐一对应**（首版错了一半）
 *
 * 首版凭记忆写调用，结果 **5 个 provider 全部少传了 `product`**，
 * lobsterai 还少传了必填的 `clientVersion`。这类错误**不会在加载时报错**
 * —— 只会在真正签到/查余额时抛 `Cannot read properties of undefined`，
 * 被 catch 吞掉后表现为「签到失败但不知道为什么」。
 *
 * 权威签名（已在 vendor 源码逐个核对，`src/*-credits.ts`）：
 *
 * | 函数 | 签名 |
 * |---|---|
 * | `claimDailyCheckin` | `(credential, product, fetcher?)` |
 * | `fetchCreditBalance` | `(credential, product, fetcher?)` |
 * | `claimCodeArtsDailyCheckin` | `(credential, fetcher?)` ← **无 product** |
 * | `fetchCodeArtsAccountInfoDetailed` | `(credential, fetcher?)` ← **无 product** |
 * | `claimLobsteraiDailyCheckin` | `(credential, product, **clientVersion**, fetcher?)` |
 * | `fetchLobsteraiCreditBalance` | `(credential, product, fetcher?)` |
 * | `claimQoderDailyCheckin` | `(credential, product, fetcher?)` |
 * | `fetchQoderCreditBalance` | `(credential, product, fetcher?)` |
 * | `claimTraeDailyCheckin` | `(credential, product, fetcher?, generation?, …)` |
 * | `fetchTraeCreditBalance` | `(credential, _product, fetcher?)` |
 * | `fetchClineCreditBalance` | `(credential, product, fetcher?, options?)` |
 * | `claimLoomyDailyQuota` | `(credential, product, fetcher?)` |
 * | `fetchLoomyCreditBalance` | `(credential, product, fetcher?)` |
 * | `claimAllLoomyOnboardingTasks` | `(credential, product, fetcher?)` |
 */
const CREDITS_IMPL = {
  /** CodeBuddy / WorkBuddy：同一套协议，走 credits.js */
  buddy: {
    async balance({ credential, product, mods }) {
      const value = await mods.credits.fetchCreditBalance(credential, product)
      return normalizeBalance(value)
    },
    async checkin({ credential, product, mods }) {
      return asOutcome(await mods.credits.claimDailyCheckin(credential, product))
    },
  },

  /** WorkBuddy 与 buddy 同源同协议，但**不支持每日签到** */
  workbuddy: {
    async balance({ credential, product, mods }) {
      const value = await mods.credits.fetchCreditBalance(credential, product)
      return normalizeBalance(value)
    },
    // 无 checkin：能力表里 supportsCheckin=false，上层不会调到
  },

  lobsterai: {
    async balance({ credential, product, mods }) {
      const value = await mods['lobsterai-credits'].fetchLobsteraiCreditBalance(credential, product)
      return normalizeBalance(value)
    },
    async checkin({ credential, product, mods, auth }) {
      // ⚠ 第 3 个参数 `clientVersion` 是**必填**，缺了上游会拒绝。
      // 从 auth 取真实版本（`resolveClientVersion()` 带缓存），
      // 取不到时回退产品声明的兜底版本（vendor 自己也这么做，
      // 见 `lobsterai-adapter.ts:892-900`）。
      const clientVersion = await resolveClientVersion(auth, product)
      return asOutcome(
        await mods['lobsterai-credits'].claimLobsteraiDailyCheckin(
          credential, product, clientVersion,
        ),
      )
    },
  },

  qoder: {
    async balance({ credential, product, mods }) {
      const value = await mods['qoder-credits'].fetchQoderCreditBalance(credential, product)
      return normalizeBalance(value)
    },
    async checkin({ credential, product, mods }) {
      return asOutcome(await mods['qoder-credits'].claimQoderDailyCheckin(credential, product))
    },
  },

  trae: {
    async balance({ credential, product, mods }) {
      // 该函数第 2 参在 vendor 里叫 `_product`（未使用），但仍要传
      const value = await mods['trae-credits'].fetchTraeCreditBalance(credential, product)
      return normalizeBalance(value)
    },
    async checkin({ credential, product, mods }) {
      return asOutcome(await mods['trae-credits'].claimTraeDailyCheckin(credential, product))
    },
  },

  /** Cline 只有余额，没有每日签到 */
  cline: {
    async balance({ credential, product, mods }) {
      const value = await mods['cline-credits'].fetchClineCreditBalance(credential, product)
      // ⚠ Cline 返回的是 `{balance: {total, packages, expiredTotal}, rawBalance}`
      // —— 总额藏在 `balance` **子对象**里（`cline-credits.js:194-197`），
      // 而 `normalizeBalance` 的顶层字段白名单命中的 `balance` 是对象、
      // 数字分支不成立，嵌套白名单里又没有 `balance` → 恒得 `total:null`
      // → 面板永远显示「—」。这里先剥一层再归一化。
      const flat = value !== null && typeof value === 'object' && value.balance !== null
        && typeof value.balance === 'object' ? value.balance : value
      return normalizeBalance(flat)
    },
  },

  loomy: {
    async balance({ credential, product, mods }) {
      const value = await mods['loomy-credits'].fetchLoomyCreditBalance(credential, product)
      return normalizeBalance(value)
    },
    async checkin({ credential, product, mods }) {
      return asOutcome(await mods['loomy-credits'].claimLoomyDailyQuota(credential, product))
    },
    /** 新手任务（仅 loomy）—— 与每日签到是两条独立路径，不参与「一键签到」遍历 */
    async onboarding({ credential, product, mods }) {
      const value = await mods['loomy-onboarding'].claimAllLoomyOnboardingTasks(credential, product)
      return { claimed: true, detail: value }
    },
  },

  /** CodeArts 的凭据是华为云 AK/SK，**函数不接 product**（签名只有 credential） */
  codearts: {
    async balance({ credential, mods }) {
      const value = await mods['codearts-credits'].fetchCodeArtsAccountInfoDetailed(credential)
      // ⚠ 该函数返回的是 `{ok:true, info:{…, credit:{total,…}}}` 包装对象
      // （`codearts-credits.js:385-395`）。
      //   * 数字在 `info.credit.total`，而 `info` **不在** `normalizeBalance`
      //     的递归白名单里 → 首版恒得 `total:null` → 永远显示「—」；
      //   * 更糟的是**查询失败**时返回 `{ok:false, message}`，同样被归一化成
      //     `{total:null}` 而非 `null` → 「查询失败」被伪装成成功（显示「—」），
      //     违反了本模块自己声明的三态语义。
      // 这里显式解包：失败返回 `null`（上层据此判为 error），成功取 credit 子对象。
      if (value !== null && typeof value === 'object' && value.ok === false) return null
      const info = value?.info ?? value
      const credit = info?.credit ?? info
      return normalizeBalance(credit)
    },
    async checkin({ credential, mods }) {
      return asOutcome(await mods['codearts-credits'].claimCodeArtsDailyCheckin(credential))
    },
  },

  /** AtomCode 支持 claim()，但没有每日签到；余额也不在能力表内 */
  atomcode: {
    // 两者都不支持：能力表里都是 false，上层不会调到
  },
}

/**
 * 取 LobsterAI 的客户端版本号。
 *
 * `auth.resolveClientVersion()` 带缓存（`lobsterai-auth.ts:188`），
 * 拿不到时回退到产品声明的兜底版本 —— 但**签到必须用真版本号**
 * （`jet-hub-rpc.ts:1465-1478` 记录了用兜底版本的失败案例），
 * 所以这里优先真值，并在回退时把情况记进 detail 供排查。
 */
async function resolveClientVersion(auth, product) {
  if (auth !== undefined && typeof auth.resolveClientVersion === 'function') {
    try {
      const value = await auth.resolveClientVersion()
      if (typeof value === 'string' && value.trim() !== '') return value.trim()
    } catch {
      /* 落到兜底 */
    }
  }
  const fallback = product?.fallbackClientVersion
  if (typeof fallback === 'string' && fallback.trim() !== '') return fallback.trim()
  return '0.0.0'
}

/**
 * 把 vendor 的 `ClaimOutcome` 归一化。
 *
 * ## `ClaimOutcome` 有**四种** kind（`credits.ts:118-122` 是权威定义）
 *
 * ```ts
 * { kind: 'claimed';          credit; streakDays; isStreakDay }   // 领到了
 * { kind: 'already-claimed';  message }                            // 今天已领
 * { kind: 'inactive';         message; actionRequired? }           // **当前无领取资格**
 * { kind: 'failed';           code; message }                      // 真失败
 * ```
 *
 * 首版只认三种，把 **`inactive` 误判成失败** —— 那会让用户看到
 * 「失败 N 个」的假告警；而原 Jet Hub 的注释里也记录过同族缺陷
 * （「inactive 渠道整条消失」，`jet-hub.js:1684-1692`）。
 * 这里四种都显式分类，**绝不落进 else 当失败**。
 *
 * 另注意 kind 的字符串是 **`already-claimed`（带连字符）**，
 * 不是 `already`；首版把它拼错的话会掉进「失败」。两种都认。
 */
function asOutcome(result) {
  const kind = typeof result?.kind === 'string' ? result.kind.trim().toLowerCase() : ''
  const message = typeof result?.message === 'string' ? result.message : ''
  const actionRequired = result?.actionRequired === true

  if (kind === 'claimed' || kind === 'ok' || kind === 'success') {
    return {
      claimed: true,
      kind,
      message,
      // vendor 会给「连续签到天数」与「是否连续奖励日」，面板可展示
      credit: Number.isFinite(Number(result?.credit)) ? Number(result.credit) : undefined,
      streakDays: Number.isFinite(Number(result?.streakDays)) ? Number(result.streakDays) : undefined,
    }
  }
  if (kind === 'already-claimed' || kind === 'already' || kind === 'alreadyclaimed') {
    return { claimed: false, alreadyClaimed: true, kind: 'already-claimed', message }
  }
  if (kind === 'inactive') {
    // 「当前无领取资格」：既不是成功也不是失败 —— 活动未开始/已结束/账号不满足条件。
    // 单独一类，避免污染 failed 计数。
    return { claimed: false, inactive: true, kind: 'inactive', message, actionRequired }
  }
  if (kind === 'failed') {
    return { claimed: false, failed: true, kind: 'failed', message, code: result?.code }
  }
  if (kind === '') {
    // 个别实现直接返回业务对象（如 loomy onboarding 的 `{claimed:[],…}`），视为成功。
    // 但**必须排除 null/undefined**，否则「什么都没返回」会被当成功。
    if (result !== null && result !== undefined && typeof result === 'object') {
      return { claimed: true, kind: 'unknown', message }
    }
    return { claimed: false, failed: true, kind: 'empty', message: '上游没有返回结果' }
  }
  // 未知 kind：**不猜成成功**，但也标出原始 kind 便于排查
  return { claimed: false, failed: true, kind: kind || 'unknown', message }
}

/**
 * 把各 provider 五花八门的余额返回**归一化**成统一形状。
 *
 * 各家的字段名完全不同（`total` / `credits` / `remain` / 嵌套两层…），
 * 而面板只需要「显示一个数字 + 可选的明细」。归一化放在这一处，
 * 面板就不必知道任何 provider 的细节。
 */
export function normalizeBalance(raw) {
  if (raw === null || raw === undefined) return null
  if (typeof raw === 'number') return { total: raw, label: String(raw) }

  if (typeof raw !== 'object') return { total: null, label: String(raw) }

  // 逐个候选字段找「看起来像总额」的数字
  const candidates = ['total', 'credits', 'balance', 'remain', 'remaining', 'amount', 'quota']
  for (const key of candidates) {
    const value = raw[key]
    if (typeof value === 'number' && Number.isFinite(value)) {
      return { total: value, label: formatCredits(value), raw }
    }
    if (typeof value === 'string' && value.trim() !== '' && Number.isFinite(Number(value))) {
      const num = Number(value)
      return { total: num, label: formatCredits(num), raw }
    }
  }
  // 嵌套一层。白名单覆盖已知的包装层：
  //   * `data`/`Data`/`Response` —— buddy 系的两层包装（`credits.ts:476-480`）
  //   * `result`                  —— 通用结果包
  //   * `info`/`credit`           —— **CodeArts**：`{ok, info:{credit:{total}}}`
  //   * `balance`                 —— **Cline**：`{balance:{total,…}}`
  // 少了后两个，这两家的余额就恒为 null（首版就是这样）。
  for (const key of ['data', 'Data', 'Response', 'result', 'info', 'credit', 'balance']) {
    const nested = raw[key]
    if (nested !== null && typeof nested === 'object') {
      const inner = normalizeBalance(nested)
      if (inner !== null && inner.total !== null) return { ...inner, raw }
    }
  }
  return { total: null, label: '', raw }
}

/** 整数不补小数，有小数才保留两位（对齐原版 `jet-hub.js:214-218`）。 */
export function formatCredits(value) {
  const num = Number(value)
  if (!Number.isFinite(num)) return ''
  return Number.isInteger(num) ? String(num) : num.toFixed(2)
}

/**
 * 建一个积分/签到控制器。
 *
 * @param {object} deps
 * @param {Map} deps.entries runtime 的 provider 条目表
 * @param {object} deps.pool 账号池
 * @param {object} deps.credentials 凭据存储
 * @param {Function} deps.loadMods 惰性加载 credits 模块集合
 * @param {Function} deps.logger
 */
export function createCreditsController({ entries, pool, credentials, loadMods, logger = () => {} }) {
  /** 惰性加载并缓存各 provider 的 credits 模块（只有真用到时才付加载成本）。 */
  let modsPromise = null
  const mods = async () => {
    if (modsPromise === null) modsPromise = loadMods()
    return modsPromise
  }

  /** provider 是否声明了该能力。**不支持就根本不发请求**（见文件头铁律 2）。 */
  function supports(providerId, capability) {
    const entry = entries.get(providerId)
    if (entry === undefined) return false
    if (capability === 'checkin') return entry.spec.supportsCheckin === true
    if (capability === 'balance') return entry.spec.supportsBalance === true
    if (capability === 'onboarding') return providerId === 'loomy'
    return false
  }

  /** 列出所有「支持某能力」的 provider，顺序即执行顺序（串行遍历用）。 */
  function providersWith(capability) {
    return [...entries.keys()].filter((id) => supports(id, capability))
  }

  /**
   * 取某个账号的凭据对象（已解析）。
   * 取不到返回 undefined —— 调用方据此跳过该账号而不是报错。
   */
  async function credentialFor(providerId, accountRef) {
    const resolved = await credentials.resolve(accountRef)
    if (resolved === undefined) return undefined
    try {
      return JSON.parse(resolved.value)
    } catch {
      return undefined
    }
  }

  /**
   * 查一个 provider 下**所有账号**的余额。
   *
   * 返回逐账号的结果，每项都带 `status`，便于面板区分
   * 「不支持 / 查询失败 / 真为 0」三态。
   */
  async function balances(providerId) {
    if (!supports(providerId, 'balance')) {
      return { provider: providerId, supported: false, accounts: [] }
    }
    const impl = CREDITS_IMPL[providerId]
    if (impl?.balance === undefined) {
      return { provider: providerId, supported: false, accounts: [] }
    }
    const bundle = await mods()
    const rows = await pool.listAccounts(providerId)
    const entry = entries.get(providerId)
    const out = []
    for (const row of rows) {
      const ref = row.credentialRef
      if (typeof ref !== 'string' || ref.length === 0) continue
      const credential = await credentialFor(providerId, ref)
      if (credential === undefined) {
        out.push({ accountId: row.id, status: 'error', message: '凭据不可读' })
        continue
      }
      try {
        const value = await impl.balance({
          credential,
          product: entry?.product,
          mods: bundle,
          auth: entry?.auth,
        })
        out.push(
          value === null
            ? { accountId: row.id, status: 'error', message: '查询失败' }
            : { accountId: row.id, status: 'ok', balance: value },
        )
      } catch (error) {
        out.push({ accountId: row.id, status: 'error', message: String(error?.message ?? error) })
      }
    }
    return { provider: providerId, supported: true, accounts: out }
  }

  /**
   * **一键签到**：跨 provider 串行、每个 provider 内逐账号顺序执行。
   *
   * ⚠ 绝不改成 `Promise.all` —— 见文件头铁律 1。这是真实写请求。
   *
   * 返回每个 provider 的计数，**每个非零计数都要出现在结果里**
   * （原版 `jet-hub.js:1684-1692` 记录了「inactive 渠道整条消失」的真实缺陷，
   * 根因是用 else-if 短路了计数分支）。
   */
  async function claimAll({ providers = null } = {}) {
    const targets = (providers ?? providersWith('checkin')).filter((id) => supports(id, 'checkin'))
    const bundle = await mods()
    const results = []

    for (const providerId of targets) {
      const impl = CREDITS_IMPL[providerId]
      const entry = entries.get(providerId)
      if (impl?.checkin === undefined || entry === undefined) {
        results.push({ provider: providerId, status: 'unsupported', claimed: 0, failed: 0, skipped: 0 })
        continue
      }
      const rows = await pool.listAccounts(providerId)
      let claimed = 0
      let alreadyClaimed = 0
      let inactive = 0
      let failed = 0
      let skipped = 0
      const messages = []
      const inactiveMessages = []

      // 逐账号**顺序**执行（同一 provider 内也不并发）
      for (const row of rows) {
        if (row.enabled === false) {
          skipped += 1
          continue
        }
        const ref = row.credentialRef
        if (typeof ref !== 'string' || ref.length === 0) {
          skipped += 1
          continue
        }
        const credential = await credentialFor(providerId, ref)
        if (credential === undefined) {
          failed += 1
          messages.push(`${row.nickname || row.id}: 凭据不可读`)
          continue
        }
        try {
          const outcome = await impl.checkin({
            credential,
            product: entry.product,
            mods: bundle,
            auth: entry.auth,
            accountRef: ref,
          })
          // ⚠ 必须按 outcome 分类 —— `ClaimOutcome` 有四种 kind
          // （claimed / already-claimed / inactive / failed），
          // 直接当成功会把「今天已领过」与「无领取资格」都算成签到成功。
          if (outcome?.claimed === true) {
            claimed += 1
          } else if (outcome?.alreadyClaimed === true) {
            alreadyClaimed += 1
          } else if (outcome?.inactive === true) {
            // 「当前无领取资格」单独计数 —— 不是失败，不该出现在失败告警里
            inactive += 1
            if (outcome.message) {
              inactiveMessages.push(`${row.nickname || row.id}: ${String(outcome.message).slice(0, 100)}`)
            }
          } else {
            failed += 1
            if (outcome?.message) {
              messages.push(`${row.nickname || row.id}: ${String(outcome.message).slice(0, 100)}`)
            }
          }
        } catch (error) {
          failed += 1
          messages.push(`${row.nickname || row.id}: ${String(error?.message ?? error).slice(0, 120)}`)
        }
      }

      results.push({
        provider: providerId,
        label: entry.spec.label,
        status: failed === 0 ? 'ok' : 'partial',
        claimed,
        alreadyClaimed,
        inactive,
        failed,
        skipped,
        messages,
        inactiveMessages,
      })
    }

    // 汇总：**每个非零计数都单列**，不用 else-if 短路
    const totalClaimed = results.reduce((sum, r) => sum + r.claimed, 0)
    const totalAlready = results.reduce((sum, r) => sum + (r.alreadyClaimed || 0), 0)
    const totalInactive = results.reduce((sum, r) => sum + (r.inactive || 0), 0)
    const totalFailed = results.reduce((sum, r) => sum + r.failed, 0)
    const totalSkipped = results.reduce((sum, r) => sum + r.skipped, 0)
    const parts = []
    if (totalClaimed > 0) parts.push(`成功 ${totalClaimed} 个`)
    if (totalAlready > 0) parts.push(`今日已领 ${totalAlready} 个`)
    if (totalInactive > 0) parts.push(`暂无资格 ${totalInactive} 个`)
    if (totalFailed > 0) parts.push(`失败 ${totalFailed} 个`)
    if (totalSkipped > 0) parts.push(`跳过 ${totalSkipped} 个（已停用或无凭据）`)
    const summary = parts.length > 0 ? parts.join('，') : '没有可签到的账号'

    return {
      results,
      totalClaimed,
      totalAlreadyClaimed: totalAlready,
      totalInactive,
      totalFailed,
      totalSkipped,
      summary,
    }
  }

  /** 领取新手任务（目前只有 Loomy）。刻意不参与「一键签到」遍历。 */
  async function claimOnboarding(providerId) {
    const impl = CREDITS_IMPL[providerId]
    if (impl?.onboarding === undefined) {
      return { provider: providerId, status: 'unsupported', message: '该提供商没有新手任务' }
    }
    const entry = entries.get(providerId)
    const bundle = await mods()
    const rows = await pool.listAccounts(providerId)
    let claimed = 0
    const messages = []
    for (const row of rows) {
      if (row.enabled === false) continue
      const credential = await credentialFor(providerId, row.credentialRef)
      if (credential === undefined) continue
      try {
        await impl.onboarding({
          credential,
          product: entry?.product,
          mods: bundle,
          auth: entry?.auth,
        })
        claimed += 1
      } catch (error) {
        messages.push(`${row.nickname || row.id}: ${String(error?.message ?? error).slice(0, 120)}`)
      }
    }
    return { provider: providerId, status: 'ok', claimed, messages }
  }

  return {
    supports,
    providersWith,
    balances,
    claimAll,
    claimOnboarding,
    /** 能力矩阵（给面板渲染按钮用，**前端据此决定显不显示**）。 */
    capabilities() {
      return [...entries.keys()].map((id) => ({
        id,
        label: entries.get(id)?.spec.label ?? id,
        balance: supports(id, 'balance'),
        checkin: supports(id, 'checkin'),
        onboarding: supports(id, 'onboarding'),
      }))
    },
  }
}
