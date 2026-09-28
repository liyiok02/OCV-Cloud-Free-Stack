/**
 * CodingPlan REST 客户端（Bearer token 认证）。
 *
 * 逆向自官方 atomcode（crates/atomcode-codingplan/src/client.rs + types.rs）：
 * - `POST /coding-plan/claim-v2` body `{"plan_type":"Max"|"Pro"|"Lite"}`，
 *   级联 Max→Pro→Lite；`duplicate=true` 视为成功（已持有）
 * - `GET /coding-plan/models-v2?plan_type=<tier>` → 模型目录
 * - `GET /coding-plan/status` → 套餐与用量
 * 仅 LLM 网关需要签名；本模块所有请求纯 Bearer OAuth token。
 * 401/403 → AuthExpired（需要刷新/重新登录）。
 */
import { ATOMGIT_CODINGPLAN_API_BASE, ATOMCODE_PLAN_CASCADE } from './atomcode-product.js';
const USER_AGENT = 'atomcode/5.1.0';
export class AuthExpiredError extends Error {
    status;
    constructor(status) {
        super(`AtomGit 认证失败 (HTTP ${status})，请重新登录`);
        this.status = status;
        this.name = 'AuthExpiredError';
    }
}
async function request(token, path, init = {}, fetcher = fetch) {
    const res = await fetcher(`${ATOMGIT_CODINGPLAN_API_BASE}${path}`, {
        ...init,
        headers: {
            authorization: `Bearer ${token}`,
            'user-agent': USER_AGENT,
            'content-type': 'application/json',
            ...(init.headers ?? {}),
        },
    });
    if (res.status === 401 || res.status === 403) {
        throw new AuthExpiredError(res.status);
    }
    if (!res.ok) {
        const body = await res.text().catch(() => '');
        throw new Error(`CodingPlan HTTP ${res.status}: ${body.slice(0, 200)}`);
    }
    return res.json();
}
/** 领取指定套餐；返回 duplicate 标志（true = 已持有，视为成功）。 */
export async function claimPlan(token, planType, options = {}) {
    const res = await request(token, '/coding-plan/claim-v2', {
        method: 'POST',
        body: JSON.stringify({ plan_type: planType }),
    }, options.fetcher);
    return { duplicate: res.duplicate === true, planName: res.plan_name };
}
/** 按级联顺序领取 CodingPlan 免费额度；返回命中的套餐名（未领取返回 null）。 */
export async function claimFreePlan(token, options = {}) {
    let lastError = null;
    for (const tier of ATOMCODE_PLAN_CASCADE) {
        try {
            const res = await claimPlan(token, tier, options);
            if (!res.duplicate)
                return tier;
            // duplicate=true：已持有该档（或更高档），停止级联。
            return res.planName ?? tier;
        }
        catch (error) {
            lastError = error;
        }
    }
    throw lastError ?? new Error('CodingPlan 领取失败');
}
/** 拉取模型目录（plan_type 用实际套餐档位，缺省 Max）。 */
export async function listModels(token, planType = 'Max', options = {}) {
    const entries = await request(token, `/coding-plan/models-v2?plan_type=${encodeURIComponent(planType)}`, {}, options.fetcher);
    return entries.map((e) => ({
        id: typeof e.id === 'number' ? e.id : 0,
        displayModelName: String(e.display_model_name ?? ''),
        baseUrl: typeof e.base_url === 'string' ? e.base_url : undefined,
        providerType: typeof e.type === 'string' ? e.type : undefined,
        contextWindow: typeof e.context_window === 'number' ? e.context_window : undefined,
        supportsVision: typeof e.supports_vision === 'boolean' ? e.supports_vision : undefined,
        planAvailable: e.plan_available === true,
        reasoningEffortLevels: Array.isArray(e.reasoning_effort_levels)
            ? e.reasoning_effort_levels
            : undefined,
        apiKey: typeof e.api_key === 'string' ? e.api_key : undefined,
    }));
}
/** 查询套餐状态。 */
export async function queryStatus(token, options = {}) {
    const res = await request(token, '/coding-plan/status', {}, options.fetcher);
    const plan = res.codingplan_free;
    if (!plan)
        return {};
    return {
        planName: typeof plan.plan_name === 'string' ? plan.plan_name : undefined,
        status: typeof plan.status === 'number' ? plan.status : undefined,
        claimedAt: typeof plan.claimed_at === 'string' ? plan.claimed_at : undefined,
        expiresAt: typeof plan.expires_at === 'string' ? plan.expires_at : undefined,
        remainingDays: typeof plan.remaining_days === 'number' ? plan.remaining_days : undefined,
        totalDays: typeof plan.total_days === 'number' ? plan.total_days : undefined,
    };
}
/** 判断凭据是否已失效（AuthExpiredError 且为 401/403）。 */
export function isAuthExpired(error) {
    return error instanceof AuthExpiredError;
}
//# sourceMappingURL=atomcode-rest.js.map