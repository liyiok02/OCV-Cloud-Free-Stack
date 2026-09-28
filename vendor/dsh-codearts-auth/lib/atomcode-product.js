/**
 * AtomCode / AtomGit CodingPlan 产品配置。
 *
 * 所有端点在 atomcode 官方仓库（crates/atomcode-config/src/endpoints.rs）中定义，
 * 本模块作为唯一真相源：
 *
 * - 认证 broker：`https://acs.atomgit.com`（OAuth：/auth/login → /auth/check →
 *   /auth/token；续期 /oauth/refresh）
 * - CodingPlan REST：`https://api.gitcode.com/api/v5`（claim-v2 / models-v2 / status，
 *   纯 Bearer token 认证）
 * - LLM 网关：`https://llm-api.atomgit.com/v1`（OpenAI 兼容 chat/completions，
 *   需 X-AtomCode-* 签名头）
 *
 * 签名协议（2026-09 逆向官方 5.1.0 二进制 + 真实请求确认）：
 * - X-AtomCode-Sig = "v1:" + hex(HMAC-SHA256)
 * - X-AtomCode-Ts / X-AtomCode-Nonce / X-AtomCode-Alg(=1) / X-AtomCode-Ver
 * - HMAC key = 97 字节（user_id(24) + 0x01 + 固定值 + 2×32B 派生块）
 * - canonical 由签名输入结构（method/path/ts/nonce 等）经自定义 SHA256 派生
 */
/** 认证 broker 端点。 */
export const ATOMGIT_PLATFORM_BASE = 'https://acs.atomgit.com';
/** CodingPlan REST 控制面（含 API 版本段）。 */
export const ATOMGIT_CODINGPLAN_API_BASE = 'https://api.gitcode.com/api/v5';
/** LLM 网关（OpenAI 兼容）。 */
export const ATOMGIT_LLM_BASE_URL = 'https://llm-api.atomgit.com/v1';
/** 单账号凭据 ref。 */
export const ATOMCODE_CREDENTIAL_REF = 'ATOMCODE_ACCESS_TOKEN';
/** provider id / 路由名。 */
export const ATOMCODE_ID = 'atomcode';
/** provider 配置 settings namespace（模型设置页按它派生 key ref）。 */
export const ATOMCODE_SETTINGS_NS = 'llm-atomcode';
/** 网关签名算法版本号（X-AtomCode-Alg）。 */
export const ATOMCODE_SIG_ALG = 1;
/** 网关签名算法标识（X-AtomCode-Ver 之外的 alg 值，用于 sig 前缀解析）。 */
export const ATOMCODE_SIG_ALG_NAME = 'atomcode-signing-v1';
/** 网关 ts 新鲜窗口（超出视为 SIG_STALE）。 */
export const ATOMCODE_TS_MAX_AGE_MS = 60 * 60 * 1000;
/** CodingPlan 套餐领取级联顺序（Max → Pro → Lite）。 */
export const ATOMCODE_PLAN_CASCADE = ['Max', 'Pro', 'Lite'];
/** CodingPlan 模型 context 窗口下限（models-v2 缺省或过小时抬高）。 */
export const ATOMCODE_MIN_CONTEXT_WINDOW = 128_000;
/** CodingPlan provider 前缀（账号池 provider 标识，与官方 "AtomGit" 前缀一致）。 */
export const ATOMCODE_PROVIDER_PREFIX = 'AtomGit';
/** 模型目录兜底（远端 models-v2 不可用时使用；远端可用时完全采信远端）。 */
export const ATOMCODE_FALLBACK_MODELS = [
    { id: 'glm5.3-flash', name: 'GLM-5.3 Flash', contextWindow: 1_000_000, supportsVision: true },
    { id: 'glm5.2', name: 'GLM-5.2', contextWindow: 262_144, supportsVision: true },
    { id: 'glm5.1', name: 'GLM-5.1', contextWindow: 262_144, supportsVision: true },
    { id: 'glm5', name: 'GLM-5', contextWindow: 262_144, supportsVision: true },
    { id: 'qwen3.8-27b', name: 'Qwen3.8-27B', contextWindow: 262_144, supportsVision: true },
    { id: 'deepseek-v4-flash', name: 'DeepSeek V4 Flash', contextWindow: 128_000 },
    { id: 'deepseek-v4-pro', name: 'DeepSeek V4 Pro', contextWindow: 128_000 },
];
//# sourceMappingURL=atomcode-product.js.map