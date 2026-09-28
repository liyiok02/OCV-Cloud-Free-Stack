/**
 * AtomCode（AtomGit CodingPlan）认证服务。
 *
 * 单账号版：OAuth 登录（broker 轮询）→ 凭据存储（ATOMCODE_ACCESS_TOKEN）→
 * 静默续期（/oauth/refresh）→ CodingPlan 目录/领取/状态。
 * 登录流程与续期语义对齐官方 atomcode（RefreshScheduler 由本插件现有
 * src/refresh.ts 提供）。
 */
import { Service } from '@deepseek-ai/cordis';
import { credentialRef } from '@deepseek-ai/dsh-credentials';
import { ATOMCODE_CREDENTIAL_REF, ATOMCODE_ID } from './atomcode-product.js';
import { isExpired, refreshToken, runLoginFlow, startLogin as startLoginSession, pollAuthorized, exchangeToken } from './atomcode-oauth.js';
import { AuthExpiredError, claimFreePlan, listModels, queryStatus, } from './atomcode-rest.js';
function parseCredential(value) {
    try {
        const parsed = JSON.parse(value);
        return typeof parsed === 'object' && parsed !== null && typeof parsed.access_token === 'string'
            ? parsed
            : undefined;
    }
    catch {
        return undefined;
    }
}
/** 从存储值解析最新有效凭据；无/过期时静默刷新，失败抛错。 */
export class AtomcodeAuth extends Service {
    refreshTokenInvalid = false;
    lastRefreshError;
    timer;
    constructor(ctx) {
        super(ctx, `${ATOMCODE_ID}Auth`);
    }
    get ref() {
        return credentialRef(ATOMCODE_CREDENTIAL_REF);
    }
    async readCredential() {
        const resolved = await this.ctx.credentials.resolve(this.ref);
        if (!resolved)
            return undefined;
        return parseCredential(resolved.value);
    }
    /** 完整登录：打开浏览器授权 → 轮询 → 换 token → 存凭据 → 武装续期。 */
    async login() {
        const credential = await runLoginFlow();
        const access = JSON.stringify(credential);
        await this.ctx.credentials.set(this.ref, access);
        this.refreshTokenInvalid = false;
        this.lastRefreshError = undefined;
        this.scheduleRefresh();
        const expires = credential.expires_in
            ? (credential.created_at + credential.expires_in) * 1000
            : 0;
        return {
            access,
            expires,
            ref: this.ref,
            refreshable: Boolean(credential.refresh_token),
        };
    }
    /** 报告状态（含 CodingPlan 套餐名，查询失败不阻塞）。 */
    async status() {
        const info = await this.ctx.credentials.describe(this.ref);
        if (!info.configured)
            return { configured: false, refreshable: false };
        let expiresAt;
        let refreshable = false;
        let planName;
        const credential = await this.readCredential();
        if (credential) {
            if (credential.expires_in) {
                expiresAt = (credential.created_at + credential.expires_in) * 1000;
            }
            refreshable = Boolean(credential.refresh_token) && !this.refreshTokenInvalid;
            if (refreshable && isExpired(credential)) {
                await this.refresh().catch(() => { });
                const fresh = await this.readCredential();
                if (fresh?.expires_in)
                    expiresAt = (fresh.created_at + fresh.expires_in) * 1000;
            }
            try {
                const st = await queryStatus(credential.access_token);
                planName = st.planName;
            }
            catch { /* 查询失败不阻塞状态展示 */ }
        }
        return {
            configured: true,
            source: info.source,
            expiresAt,
            refreshable,
            refreshError: this.lastRefreshError,
            planName,
        };
    }
    /** 静默续期（refresh_token 一次性轮换，立即回写）。 */
    async refresh() {
        const credential = await this.readCredential();
        if (!credential)
            throw new Error('未配置 AtomGit 凭据，请先执行 /atomcode-login');
        if (!credential.refresh_token) {
            this.refreshTokenInvalid = true;
            this.lastRefreshError = '无 refresh_token，请重新登录';
            throw new Error(this.lastRefreshError);
        }
        try {
            const next = await refreshToken(credential);
            await this.ctx.credentials.set(this.ref, JSON.stringify(next));
            this.refreshTokenInvalid = false;
            this.lastRefreshError = undefined;
        }
        catch (error) {
            this.refreshTokenInvalid = true;
            this.lastRefreshError = error instanceof Error ? error.message : String(error);
            throw error;
        }
    }
    /**
     * 按凭据 ref 刷新指定账号的凭据（Jet Hub「刷新」按钮入口）。
     * 与 refresh() 的差异：refresh() 只操作默认 ref（ATOMCODE_ACCESS_TOKEN），
     * 本方法可操作任意 ref（ATOMCODE_ACCOUNT_XXX）。
     *
     * AtomCode 的凭据来自 OAuth broker，refresh_token 是一次性轮换：
     * 读旧凭据 → refreshToken() → 写新凭据到同一个 ref。
     */
    async refreshAccountCredential(ref) {
        const refStr = credentialRef(ref);
        const resolved = await this.ctx.credentials.resolve(refStr);
        if (!resolved)
            throw new Error('未配置 AtomGit 凭据');
        let credential;
        try {
            credential = JSON.parse(resolved.value);
        }
        catch {
            throw new Error('AtomGit 凭据数据损坏，请重新登录');
        }
        if (!credential.refresh_token)
            throw new Error('无 refresh_token，请重新登录');
        const next = await refreshToken(credential);
        await this.ctx.credentials.set(refStr, JSON.stringify(next));
    }
    /**
     * 将凭据写入指定 ref 并返回凭据数据（Jet Hub account.create 导入入口）。
     * 来源优先级：官方 CLI 登录态 > 已有默认 ref 凭据。
     * 返回 undefined 表示两种来源都不可用。
     */
    async importToRef(ref) {
        const refStr = credentialRef(ref);
        // 先尝试从官方 CLI 导入
        const imported = await this.importFromOfficialCli({ targetRef: refStr });
        if (imported) {
            const resolved = await this.ctx.credentials.resolve(refStr);
            if (resolved)
                return JSON.parse(resolved.value);
        }
        // 回退：从默认 ref 复制已有凭据
        const existing = await this.ctx.credentials.resolve(this.ref);
        if (existing) {
            try {
                const cred = JSON.parse(existing.value);
                await this.ctx.credentials.set(refStr, JSON.stringify(cred));
                return cred;
            }
            catch { /* 复制失败 */ }
        }
        return undefined;
    }
    /** 领取 CodingPlan 免费额度（级联 Max→Pro→Lite）。 */
    async claim() {
        const credential = await this.ensureValidCredential();
        return claimFreePlan(credential.access_token);
    }
    /** 拉取模型目录（纯 Bearer；远端不可用时回退内置兜底表）。 */
    async fetchModels() {
        const credential = await this.ensureValidCredential();
        try {
            const models = await listModels(credential.access_token);
            const available = models.filter((m) => m.planAvailable && m.displayModelName);
            if (available.length > 0)
                return available;
            // 远端返回空：套餐未生效，先尝试领取再拉一次。
            await claimFreePlan(credential.access_token).catch(() => { });
            const retry = await listModels(credential.access_token);
            const availableRetry = retry.filter((m) => m.planAvailable && m.displayModelName);
            if (availableRetry.length > 0)
                return availableRetry;
        }
        catch (error) {
            if (error instanceof AuthExpiredError)
                throw error;
        }
        return [];
    }
    async ensureValidCredential() {
        let credential = await this.readCredential();
        if (!credential)
            throw new Error('未配置 AtomGit 凭据，请先执行 /atomcode-login');
        if (isExpired(credential)) {
            if (!credential.refresh_token) {
                throw new Error('AtomGit 凭据已过期且无 refresh_token，请重新登录');
            }
            await this.refresh();
            credential = await this.readCredential();
            if (!credential)
                throw new Error('AtomGit 凭据刷新失败');
        }
        return credential;
    }
    /** 登录后每 30 分钟静默续期（unref，卸载时清理）。 */
    scheduleRefresh() {
        if (this.timer)
            return;
        this.timer = setInterval(() => {
            void this.refresh().catch((error) => {
                this.lastRefreshError = error instanceof Error ? error.message : String(error);
            });
        }, 30 * 60 * 1000);
        this.timer.unref?.();
    }
    /** 清除续期定时器。 */
    stop() {
        if (this.timer) {
            clearInterval(this.timer);
            this.timer = undefined;
        }
    }
    /**
     * 两步式登录（Jet Hub 弹窗模式）——第一步：获取登录 URL。
     * 不起本地端口、不阻塞，前端立刻 window.open(authUrl)，
     * 后台用同一个 session 轮询直到授权完成。
     */
    async startLogin(options = {}) {
        const session = await startLoginSession();
        const refName = options.refName ?? this.ref;
        const result = (async () => {
            await pollAuthorized(session);
            const credential = await exchangeToken(session);
            const access = JSON.stringify(credential);
            await this.ctx.credentials.set(credentialRef(refName), access);
            this.refreshTokenInvalid = false;
            this.lastRefreshError = undefined;
            this.scheduleRefresh();
            const expires = credential.expires_in
                ? (credential.created_at + credential.expires_in) * 1000
                : 0;
            return {
                access,
                expires,
                ref: credentialRef(refName),
                refreshable: Boolean(credential.refresh_token),
            };
        })();
        return { loginUrl: session.loginUrl, result };
    }
    /** 领取 CodingPlan（按指定 ref 凭据），Jet Hub 一键领取入口。 */
    async claimByRef(ref) {
        const refStr = credentialRef(ref);
        const resolved = await this.ctx.credentials.resolve(refStr);
        if (!resolved)
            throw new Error('未配置 AtomGit 凭据');
        let credential;
        try {
            credential = JSON.parse(resolved.value);
        }
        catch {
            throw new Error('AtomGit 凭据数据损坏');
        }
        return claimFreePlan(credential.access_token);
    }
    /**
     * 从官方 atomcode CLI 的登录态导入凭据（~/.atomcode/auth.toml）。
     * 用户已在官方 CLI 登录过时，无需重新 OAuth；导入后立即武装续期。
     * 返回 true = 导入成功，false = 官方登录态不存在或不可用。
     */
    async importFromOfficialCli(options = {}) {
        const home = options.homeDir ?? process.env.USERPROFILE ?? process.env.HOME ?? '';
        const authPath = `${home}/.atomcode/auth.toml`;
        let content;
        try {
            content = await import('node:fs/promises').then((fs) => fs.readFile(authPath, 'utf8'));
        }
        catch {
            return false;
        }
        const get = (key) => {
            // TOML 标量：字符串带引号，数字/布尔无引号——统一匹配。
            const m = content.match(new RegExp(`^${key}\\s*=\\s*"?([^"\\n]*)"?`, 'm'));
            return m?.[1];
        };
        const accessToken = get('access_token');
        const id = get('id');
        if (!accessToken || !id)
            return false;
        const credential = {
            access_token: accessToken,
            refresh_token: get('refresh_token'),
            token_type: get('token_type') ?? 'Bearer',
            expires_in: Number(get('expires_in')) || undefined,
            created_at: Number(get('created_at')) || Math.floor(Date.now() / 1000),
            user: {
                id,
                username: get('username') ?? id,
                name: get('name'),
                email: get('email'),
                avatar_url: get('avatar_url'),
            },
        };
        const writeTarget = options.targetRef !== undefined ? credentialRef(options.targetRef) : this.ref;
        await this.ctx.credentials.set(writeTarget, JSON.stringify(credential));
        this.refreshTokenInvalid = false;
        this.lastRefreshError = undefined;
        this.scheduleRefresh();
        return true;
    }
}
//# sourceMappingURL=atomcode-service.js.map