/**
 * Jet Hub 凭据存储（**只存 OCV 插件目录，不桥接 DSH**）。
 *
 * ## 为什么自己写而不用 DSH 的 `dsh-credentials-local`
 *
 * 用户明确要求：凭据只存 OCV、不桥接 dsh，但必须支持**备份与恢复**。
 * DSH 的实现把数据放在 `$DSH_HOME/.credentials.yaml`，且配置项里的
 * `dshHome` **默认回退 `~/.dsh`** —— 在没装 DSH 的目标机上会失败，或者
 * 更糟：静默写到用户主目录去，破坏「所有文件都在插件目录内」这条硬约束。
 *
 * 自建实现只有 ~120 行，换来三件确定性：
 *
 * 1. **路径可控**：永远 <插件目录>/state/credentials.json，无任何回退分支；
 * 2. **格式可控**：单文件 JSON，`备份 = 复制该文件`，恢复 = 反向复制；
 * 3. **接口最小**：只需 `resolve / set / unset / describe` 四个方法
 *    （`buddy-auth.js` 内部实际调用的就这四个）。
 *
 * ## 为什么不能删掉 `@deepseek-ai/dsh-credentials` 这个依赖
 *
 * `buddy-auth.js` 仍然 `import { credentialRef } from '@deepseek-ai/dsh-credentials'`。
 * 那只是把字符串打上 brand 的薄函数（实测 `lib/index.js` 里只有 3 行），
 * 但既然它要 import，包就必须在场 —— 所以 vendor 保留，我们只是**不启用**
 * 它的 `LocalCredentialProvider`，改用本模块提供的服务。
 *
 * ## 凭据值形态
 *
 * 值一律是**字符串**。CodeBuddy 系存的是 `JSON.stringify(BuddyCredential)`
 * （见 `buddy-auth.ts` 的 `saveCredential`），刷新时再 `JSON.parse` 回来。
 * 本模块不解释内容，只做读写 —— 这样将来接别的 provider 不用改这里。
 *
 * ## 并发
 *
 * 同一个宿主进程内用一把内存锁串行化「读-改-写」。跨进程不设锁：本插件的
 * 设计是**只有一个长驻桥进程**持有状态（见 runtime.mjs 的说明），
 * 多进程同时写属于配置错误，不在本模块兜底范围内。
 */

import { existsSync, mkdirSync, readFileSync, renameSync, unlinkSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'

const DOCUMENT_VERSION = 1
const FILE_NAME = 'credentials.json'

/** 凭据引用名必须匹配 POSIX 标识符（与 DSH 的约束保持一致）。 */
const REF_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*$/

/** 原子写：先写临时文件再 rename，避免读到半截文档。 */
function writeAtomic(path, text) {
  const dir = dirname(path)
  mkdirSync(dir, { recursive: true })
  const pending = `${path}.${process.pid}.${Date.now().toString(36)}.pending`
  writeFileSync(pending, text, 'utf-8')
  try {
    renameSync(pending, path)
  } catch (error) {
    try {
      unlinkSync(pending)
    } catch {
      /* 清理失败无所谓 */
    }
    throw error
  }
}

function emptyDocument() {
  return { version: DOCUMENT_VERSION, updatedAt: new Date().toISOString(), refs: {} }
}

/**
 * 创建一个凭据存储。
 *
 * @param {object} options
 * @param {string} options.dir 状态目录（插件目录内的 `state/`）
 * @returns 带 resolve/set/unset/describe/list 的存储对象
 */
export function createCredentialStore({ dir }) {
  if (typeof dir !== 'string' || dir.length === 0) {
    throw new TypeError('createCredentialStore: dir 必填')
  }
  const path = join(dir, FILE_NAME)
  let queue = Promise.resolve()

  function readDocument() {
    if (!existsSync(path)) return emptyDocument()
    try {
      const parsed = JSON.parse(readFileSync(path, 'utf-8'))
      if (parsed === null || typeof parsed !== 'object') return emptyDocument()
      const refs = parsed.refs
      if (refs === null || typeof refs !== 'object' || Array.isArray(refs)) {
        return { ...emptyDocument(), refs: {} }
      }
      return {
        version: Number(parsed.version) || DOCUMENT_VERSION,
        updatedAt: typeof parsed.updatedAt === 'string' ? parsed.updatedAt : '',
        refs: { ...refs },
      }
    } catch {
      // 文档损坏时**不抛**：当作空文档继续，避免一个坏文件让整个桥起不来。
      // 原文件保留在原地，用户可以从备份恢复。
      return emptyDocument()
    }
  }

  function writeDocument(document) {
    document.version = DOCUMENT_VERSION
    document.updatedAt = new Date().toISOString()
    writeAtomic(path, `${JSON.stringify(document, null, 2)}\n`)
  }

  /** 所有读写都排进同一条 Promise 链，保证「读-改-写」不会被交错。 */
  function serialize(task) {
    const next = queue.then(task, task)
    // 吞掉拒绝以免污染后续任务；调用方拿到的仍是同一个 Promise。
    queue = next.then(
      () => undefined,
      () => undefined,
    )
    return next
  }

  function normalizeRef(ref) {
    const name = String(ref ?? '').trim()
    if (!REF_PATTERN.test(name)) {
      throw new TypeError(`凭据引用名不合法："${name}"（须匹配 ${String(REF_PATTERN)}）`)
    }
    return name
  }

  return {
    /** 数据文件绝对路径（备份/恢复与诊断都用它）。 */
    path,

    /** @returns {Promise<{value: string} | undefined>} 与 DSH 的 resolve 同形。 */
    async resolve(ref) {
      const name = normalizeRef(ref)
      return serialize(async () => {
        const entry = readDocument().refs[name]
        if (entry === null || typeof entry !== 'object') return undefined
        const value = entry.value
        if (typeof value !== 'string' || value.length === 0) return undefined
        return { value }
      })
    },

    /** 写入或覆盖一个凭据。 */
    async set(ref, value) {
      const name = normalizeRef(ref)
      const text = String(value ?? '')
      if (text.length === 0) throw new TypeError(`凭据值为空：${name}`)
      return serialize(async () => {
        const document = readDocument()
        const previous = document.refs[name]
        const now = new Date().toISOString()
        document.refs[name] = {
          value: text,
          createdAt:
            previous !== null && typeof previous === 'object' && typeof previous.createdAt === 'string'
              ? previous.createdAt
              : now,
          updatedAt: now,
        }
        writeDocument(document)
      })
    },

    /** 删除一个凭据；不存在时静默成功。 */
    async unset(ref) {
      const name = normalizeRef(ref)
      return serialize(async () => {
        const document = readDocument()
        if (!(name in document.refs)) return
        delete document.refs[name]
        writeDocument(document)
      })
    },

    /**
     * 描述一个凭据的元信息（**绝不返回明文**）。
     * `buddy-auth.js` 用它判断「是否已配置」。
     */
    async describe(ref) {
      const name = normalizeRef(ref)
      return serialize(async () => {
        const entry = readDocument().refs[name]
        const configured = entry !== null && typeof entry === 'object' && typeof entry.value === 'string'
        return {
          ref: name,
          configured,
          updatedAt: configured && typeof entry.updatedAt === 'string' ? entry.updatedAt : undefined,
        }
      })
    },

    /** 列出全部引用名及其元信息（不含明文），供面板展示与备份校验。 */
    async list() {
      return serialize(async () => {
        const refs = readDocument().refs
        return Object.entries(refs)
          .map(([ref, entry]) => ({
            ref,
            configured: entry !== null && typeof entry === 'object' && typeof entry.value === 'string',
            updatedAt:
              entry !== null && typeof entry === 'object' && typeof entry.updatedAt === 'string'
                ? entry.updatedAt
                : undefined,
          }))
          .sort((a, b) => a.ref.localeCompare(b.ref))
      })
    },

    /** 整份文档快照（**含明文**）。只给备份导出用，不要下发到前端。 */
    async exportDocument() {
      return serialize(async () => readDocument())
    },

    /** 用一份文档整体覆盖（恢复用）。返回写入的引用数。 */
    async importDocument(document) {
      if (document === null || typeof document !== 'object') {
        throw new TypeError('importDocument: 文档必须是对象')
      }
      const refs = document.refs
      if (refs === null || typeof refs !== 'object' || Array.isArray(refs)) {
        throw new TypeError('importDocument: 文档缺少 refs 对象')
      }
      const cleaned = {}
      for (const [name, entry] of Object.entries(refs)) {
        if (!REF_PATTERN.test(name)) continue
        if (entry === null || typeof entry !== 'object') continue
        const value = entry.value
        if (typeof value !== 'string' || value.length === 0) continue
        const now = new Date().toISOString()
        cleaned[name] = {
          value,
          createdAt: typeof entry.createdAt === 'string' ? entry.createdAt : now,
          updatedAt: typeof entry.updatedAt === 'string' ? entry.updatedAt : now,
        }
      }
      return serialize(async () => {
        writeDocument({ ...emptyDocument(), refs: cleaned })
        return Object.keys(cleaned).length
      })
    },
  }
}
