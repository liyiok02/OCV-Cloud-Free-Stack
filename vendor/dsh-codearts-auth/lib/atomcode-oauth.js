/**
 * AtomGit OAuth 登录（broker 轮询模式，不起本地端口）。
 *
 * 逆向自官方 atomcode（crates/atomcode-auth/src/oauth.rs）：
 * 1. `GET /auth/login?provider=atomgit` → `{login_url, state}`
 * 2. 浏览器打开 login_url（可剥离 `&force_login=true` 让已登录用户免确认）
 * 3. 轮询 `GET /auth/check?state=<state>` → `{valid}`，2 秒间隔直到 true
 * 4. `GET /auth/token?state=<state>` → `{access_token, token_type, expires_in,
 *    refresh_token, user:{id, username, name, email, avatar_url}}`
 * 5. 续期：`POST /oauth/refresh` body `{"refresh_token": "..."}` → 新凭据
 *    （refresh_token 轮换，用一次即作废，必须立即回写）
 */
import { spawn } from 'node:child_process';
import { ATOMGIT_PLATFORM_BASE } from './atomcode-product.js';
const USER_AGENT = 'atomcode/5.1.0';
async function http(fetcher, url, init = {}) {
    const res = await fetcher(url, {
        ...init,
        headers: {
            'user-agent': USER_AGENT,
            ...(init.headers ?? {}),
        },
    });
    if (!res.ok) {
        const body = await res.text().catch(() => '');
        throw new Error(`AtomGit broker HTTP ${res.status}: ${body.slice(0, 200)}`);
    }
    return res.json();
}
/** 打开浏览器（尽力而为，失败静默）。 */
export function openBrowser(url) {
    try {
        if (process.platform === 'win32') {
            spawn('cmd', ['/c', 'start', '', url], { stdio: 'ignore', detached: true }).unref();
        }
        else if (process.platform === 'darwin') {
            spawn('open', [url], { stdio: 'ignore' }).unref();
        }
        else {
            spawn('xdg-open', [url], { stdio: 'ignore' }).unref();
        }
    }
    catch { /* 静默 */ }
}
/** 发起 OAuth 登录：返回登录会话（loginUrl + state），不含等待。 */
export async function startLogin(options = {}) {
    const fetcher = options.fetcher ?? fetch;
    const res = await http(fetcher, `${ATOMGIT_PLATFORM_BASE}/auth/login?provider=atomgit`);
    // 剥离 force_login=true：已登录 atomgit.com 的用户可自动授权，跳过确认页。
    const loginUrl = res.login_url
        .replace('&force_login=true', '')
        .replace('?force_login=true&', '?')
        .replace('?force_login=true', '');
    return { loginUrl, state: res.state };
}
/** 轮询 /auth/check 直到授权完成，返回 true（已授权）。 */
export async function pollAuthorized(session, options = {}) {
    const fetcher = options.fetcher ?? fetch;
    const interval = options.pollIntervalMs ?? 2000;
    const timeout = options.pollTimeoutMs ?? 5 * 60 * 1000;
    const deadline = Date.now() + timeout;
    for (;;) {
        if (Date.now() > deadline) {
            throw new Error('AtomGit 登录等待超时，请重新发起登录');
        }
        const res = await http(fetcher, `${ATOMGIT_PLATFORM_BASE}/auth/check?state=${encodeURIComponent(session.state)}`);
        if (res.valid === true)
            return;
        await new Promise((resolve) => setTimeout(resolve, interval));
    }
}
/** 用 state 换取 token 凭据。 */
export async function exchangeToken(session, options = {}) {
    const fetcher = options.fetcher ?? fetch;
    const res = await http(fetcher, `${ATOMGIT_PLATFORM_BASE}/auth/token?state=${encodeURIComponent(session.state)}`);
    if (!res.access_token || !res.user?.id) {
        throw new Error('AtomGit /auth/token 响应缺少 access_token 或 user.id');
    }
    return {
        access_token: res.access_token,
        refresh_token: res.refresh_token,
        token_type: res.token_type ?? 'Bearer',
        expires_in: res.expires_in,
        created_at: Math.floor(Date.now() / 1000),
        user: {
            id: res.user.id,
            username: res.user.username,
            name: res.user.name,
            email: res.user.email,
            avatar_url: res.user.avatar_url,
        },
    };
}
/** 完整登录流：start → 浏览器 → 轮询 → 换 token。 */
export async function runLoginFlow(options = {}) {
    const session = await startLogin(options);
    openBrowser(session.loginUrl);
    await pollAuthorized(session, options);
    return exchangeToken(session, options);
}
/** 静默续期：POST /oauth/refresh。refresh_token 一次性轮换，返回必须回写。 */
export async function refreshToken(credential, options = {}) {
    const fetcher = options.fetcher ?? fetch;
    if (!credential.refresh_token) {
        throw new Error('无 refresh_token，请重新登录');
    }
    const res = await http(fetcher, `${ATOMGIT_PLATFORM_BASE}/oauth/refresh`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ refresh_token: credential.refresh_token }),
    });
    if (!res.access_token) {
        throw new Error('AtomGit /oauth/refresh 响应缺少 access_token');
    }
    return {
        access_token: res.access_token,
        // 服务端未回发新 refresh_token 时沿用旧的（官方实现同此语义）。
        refresh_token: res.refresh_token ?? credential.refresh_token,
        token_type: res.token_type ?? credential.token_type,
        expires_in: res.expires_in ?? credential.expires_in,
        created_at: Math.floor(Date.now() / 1000),
        user: res.user
            ? {
                id: res.user.id,
                username: res.user.username,
                name: res.user.name,
                email: res.user.email,
                avatar_url: res.user.avatar_url,
            }
            : credential.user,
    };
}
/** 凭据是否过期（含 5 分钟安全边际）。 */
export function isExpired(credential, nowSec = Math.floor(Date.now() / 1000)) {
    if (typeof credential.expires_in !== 'number')
        return credential.created_at === 0;
    return nowSec >= credential.created_at + credential.expires_in - 300;
}
//# sourceMappingURL=atomcode-oauth.js.map