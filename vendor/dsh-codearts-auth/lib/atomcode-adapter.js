import { LlmAdapter, LlmError, ToolCallId } from '@deepseek-ai/dsh-llm';
import { ATOMCODE_ID, ATOMCODE_MIN_CONTEXT_WINDOW, ATOMCODE_SETTINGS_NS, ATOMGIT_LLM_BASE_URL, ATOMCODE_FALLBACK_MODELS, } from './atomcode-product.js';
import { signWithRandomNonce } from './atomcode-sign.js';
export const PROVIDER = ATOMCODE_ID;
/** 把消息内容展平为文本。 */
function flattenContent(content) {
    if (typeof content === 'string')
        return content;
    if (!Array.isArray(content))
        return '';
    return content.map((b) => {
        if (typeof b === 'string')
            return b;
        if (b && typeof b === 'object' && typeof b.text === 'string')
            return b.text;
        return '';
    }).join('');
}
export class AtomcodeAdapter extends LlmAdapter {
    opts;
    remoteModels;
    loading;
    constructor(opts) {
        super();
        this.opts = opts;
    }
    get fetchImpl() {
        return this.opts.fetchImpl ?? fetch;
    }
    async loadRemote() {
        if (this.loading)
            return this.loading;
        this.loading = (async () => {
            try {
                const remote = await this.opts.fetchRemoteModels?.();
                if (remote && remote.length > 0)
                    this.remoteModels = remote;
            }
            catch { /* 回退兜底 */ }
        })();
        return this.loading;
    }
    modalitiesFor(model) {
        const m = (this.remoteModels ?? ATOMCODE_FALLBACK_MODELS).find((r) => r.id === model);
        return m?.supportsVision ? ['text', 'image'] : ['text'];
    }
    async listModels(_provider) {
        await this.loadRemote();
        return (this.remoteModels ?? ATOMCODE_FALLBACK_MODELS).map((m) => ({
            provider: PROVIDER,
            id: m.id,
            name: m.name,
            inputModalities: (m.supportsVision ? ['text', 'image'] : ['text']),
        }));
    }
    /**
     * 不套黑名单的全量目录（带最终展示名），供 Jet Hub「显示列表」使用。
     * 与上游各适配器约定一致：**同步**方法，读取已加载的目录；
     * 尚未加载完成时返回兜底表（`loadRemote` 的首次 `listModels` 调用会补齐远端目录）。
     */
    listAllModels() {
        return (this.remoteModels ?? ATOMCODE_FALLBACK_MODELS).map((m) => ({ id: m.id, name: m.name }));
    }
    async resolveModel(provider, model, _signal) {
        await this.loadRemote();
        const found = (this.remoteModels ?? ATOMCODE_FALLBACK_MODELS).find((r) => r.id === model);
        const resolved = {
            provider,
            id: model,
            name: found?.name ?? model,
            inputModalities: this.modalitiesFor(model),
        };
        const cw = Math.max(found?.contextWindow ?? ATOMCODE_MIN_CONTEXT_WINDOW, ATOMCODE_MIN_CONTEXT_WINDOW);
        resolved.context = { contextWindow: cw };
        return resolved;
    }
    async *stream(options) {
        const credential = await this.opts.resolveCredential();
        if (!credential) {
            throw new LlmError('atomcode: 凭据未配置，请先执行 /atomcode-login', 'MISSING_CREDENTIAL');
        }
        const url = `${ATOMGIT_LLM_BASE_URL}/chat/completions`;
        const body = JSON.stringify({
            model: options.model,
            messages: options.messages.map((m) => ({ role: m.role, content: flattenContent(m.content) })),
            stream: true,
            ...(typeof options.maxTokens === 'number' ? { max_tokens: options.maxTokens } : {}),
        });
        const signed = signWithRandomNonce('POST', '/v1/chat/completions', body, credential.access_token, credential.user.id, Math.floor(Date.now() / 1000));
        let response;
        try {
            response = await this.fetchImpl(url, {
                method: 'POST',
                headers: {
                    'content-type': 'application/json',
                    authorization: `Bearer ${credential.access_token}`,
                    'x-atomcode-sig': signed.sig,
                    'x-atomcode-ts': signed.ts,
                    'x-atomcode-nonce': signed.nonce,
                    'x-atomcode-alg': String(signed.alg),
                    'x-atomcode-ver': signed.ver,
                },
                body,
                signal: options.signal,
            });
        }
        catch (error) {
            throw new LlmError(`atomcode: 网关请求失败: ${String(error)}`, 'TRANSPORT', { cause: error });
        }
        if (!response.ok) {
            const text = await response.text().catch(() => '');
            if ((response.status === 401 || response.status === 403) && this.opts.refresh) {
                await this.opts.refresh().catch(() => { });
            }
            throw new LlmError(`atomcode: 网关 HTTP ${response.status}: ${text.slice(0, 200)}`, 'SERVER');
        }
        if (!response.body) {
            yield { type: 'finish', reason: { kind: 'stop' } };
            return;
        }
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let nextIndex = 0;
        const blocks = [];
        const toolBlocks = [];
        const toolOrder = [];
        let finishReason;
        try {
            for (;;) {
                const { done, value } = await reader.read();
                if (done)
                    break;
                buffer += decoder.decode(value, { stream: true });
                let nl;
                while ((nl = buffer.indexOf('\n')) >= 0) {
                    const line = buffer.slice(0, nl).trim();
                    buffer = buffer.slice(nl + 1);
                    if (!line.startsWith('data:'))
                        continue;
                    const payload = line.slice(5).trim();
                    if (payload === '[DONE]')
                        continue;
                    let parsed;
                    try {
                        parsed = JSON.parse(payload);
                    }
                    catch {
                        continue;
                    }
                    const choices = parsed.choices;
                    const choice = choices?.[0];
                    const delta = choice?.delta;
                    if (typeof delta?.content === 'string' && delta.content.length > 0) {
                        let block = blocks.find((b) => b.kind === 'text');
                        if (block === undefined) {
                            block = { index: nextIndex++, kind: 'text', text: '' };
                            blocks.push(block);
                            yield { type: 'block-start', index: block.index, blockType: 'text' };
                        }
                        block.text += delta.content;
                        yield { type: 'text-delta', index: block.index, text: delta.content };
                    }
                    if (typeof delta?.reasoning_content === 'string' && delta.reasoning_content.length > 0) {
                        let block = blocks.find((b) => b.kind === 'reasoning');
                        if (block === undefined) {
                            block = { index: nextIndex++, kind: 'reasoning', text: '' };
                            blocks.push(block);
                            yield { type: 'block-start', index: block.index, blockType: 'reasoning' };
                        }
                        block.text += delta.reasoning_content;
                        yield { type: 'reasoning-delta', index: block.index, text: delta.reasoning_content };
                    }
                    const tc = delta?.tool_calls;
                    if (Array.isArray(tc)) {
                        for (const call of tc) {
                            const idx = Number(call.index ?? 0);
                            let tb = toolBlocks.find((t) => t.index === idx);
                            if (tb === undefined) {
                                tb = { index: nextIndex++, callId: '', name: '', text: '' };
                                toolBlocks.push(tb);
                                toolOrder.push(tb.index);
                                yield { type: 'block-start', index: tb.index, blockType: 'tool-call' };
                            }
                            const fn = call.function;
                            if (fn) {
                                if (typeof fn.id === 'string')
                                    tb.callId = fn.id;
                                if (typeof fn.name === 'string')
                                    tb.name += fn.name;
                                if (typeof fn.arguments === 'string')
                                    tb.text += fn.arguments;
                            }
                            yield { type: 'tool-call-delta', index: tb.index, id: ToolCallId(tb.callId), name: tb.name, argumentsDelta: tb.text };
                        }
                    }
                    const fr = choice?.finish_reason;
                    if (typeof fr === 'string')
                        finishReason = fr;
                }
            }
        }
        finally {
            reader.releaseLock();
        }
        // 收尾：按创建顺序关闭每个块
        for (const index of toolOrder) {
            const tb = toolBlocks.find((t) => t.index === index);
            let parsedArgs;
            try {
                JSON.parse(tb.text);
                parsedArgs = tb.text;
            }
            catch {
                parsedArgs = tb.text || '{}';
            }
            yield {
                type: 'block-end',
                index,
                block: {
                    type: 'tool-call',
                    id: ToolCallId(tb.callId),
                    name: tb.name,
                    arguments: parsedArgs,
                },
            };
        }
        const textBlock = blocks.find((b) => b.kind === 'text');
        if (textBlock !== undefined) {
            yield { type: 'block-end', index: textBlock.index, block: { type: 'text', text: textBlock.text } };
        }
        const reasoningBlock = blocks.find((b) => b.kind === 'reasoning');
        if (reasoningBlock !== undefined && reasoningBlock.text !== '') {
            yield { type: 'block-end', index: reasoningBlock.index, block: { type: 'reasoning', text: reasoningBlock.text } };
        }
        const reason = finishReason === 'length'
            ? { kind: 'max-tokens' }
            : finishReason === 'tool_calls' || toolOrder.length > 0
                ? { kind: 'tool-calls' }
                : { kind: 'stop' };
        yield { type: 'finish', reason };
    }
}
/** 在 ctx.llm 上注册 atomcode 提供商路由和适配器。返回适配器实例（供 modelAdapters 登记）。 */
export function registerAtomcodeLlm(ctx, options) {
    ctx.llm.registerConfigurableProviders([
        { provider: PROVIDER, displayName: 'AtomCode (CodingPlan)', settingsNs: ATOMCODE_SETTINGS_NS, settingsPath: [] },
    ]);
    const adapter = new AtomcodeAdapter(options);
    ctx.llm.registerAdapter([PROVIDER], adapter);
    return adapter;
}
//# sourceMappingURL=atomcode-adapter.js.map