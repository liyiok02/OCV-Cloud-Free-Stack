/**
 * AtomGit LLM 网关请求签名（X-AtomCode-* 头）。
 *
 * 逆向自官方 atomcode 5.1.0 二进制（2026-09），实测网关 200 确认：
 *
 * 签名链（4 层 HMAC，全部使用 RustCrypto sha2 crate 的 LE-state 语义）：
 * 1. key97 = uid_ASCII(24B) + 0x01 + hour_LE_u64(8B) + SHA256(token)(32B) + SHA256(ver)(32B)
 *    hour = floor(unixSec / 3600)
 * 2. K_first = HMAC_LE(key97, static32)
 *    static32 = e97250f05303162c8ecd68c688b2f55c1d81e508d243d88466472e7f54637123
 * 3. T1 = HMAC_LE(K_first, "atomcode-signing-v1" + 0x01)
 * 4. canonical = "v1\n" + method + "\n" + path + "\n" + str(unixSec) + "\n"
 *    + hex(nonce, 16B) + "\n" + hex(sha256(body), 32B)
 * 5. sig = HMAC_LE(T1, canonical)
 * 6. X-AtomCode-Sig = "v1:" + hex(sig)
 *
 * RustCrypto sha2 crate 的 LE-state 语义：
 * - SHA256 内部 state 以 LE u32 格式存储（每个 u32 的字节序与标准 BE 相反）
 * - compress 后 state 写回内存时为 LE
 * - final 输出时做一次额外 bswap 转成标准 BE
 * - 因此：直接用 Node.js crypto 的 HMAC 与之不等价（标准 HMAC 用 BE state）
 */
import { createHash, randomBytes } from 'node:crypto';
import { ATOMCODE_SIG_ALG } from './atomcode-product.js';
export const ATOMCODE_SIG_VER = '5.1.0';
/** 逆向确认的静态 32 字节常量（HMAC 第一层的 msg）。 */
export const ATOMCODE_STATIC32 = Buffer.from('e97250f05303162c8ecd68c688b2f55c1d81e508d243d88466472e7f54637123', 'hex');
/** SHA256 压缩函数的轮常量。 */
const KK = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);
const rotr = (x, n) => (x >>> n) | (x << (32 - n));
/** SHA256 标准 IV 的 LE 内存表示。 */
const IV_LE = Buffer.from('67e6096a85ae67bb72f36e3c3af54fa57f520e518c68059babd9831f19cde05b', 'hex');
/** 对 64 字节块执行一轮 SHA256 压缩。state 为 LE 格式 Buffer，原地更新。 */
function compressBlockLE(hLE, block64) {
    const h = new Uint32Array(8);
    for (let i = 0; i < 8; i++) {
        h[i] = hLE[i * 4] | (hLE[i * 4 + 1] << 8) | (hLE[i * 4 + 2] << 16) | (hLE[i * 4 + 3] << 24);
    }
    const dv = new DataView(block64.buffer, block64.byteOffset, 64);
    const w = new Uint32Array(64);
    for (let i = 0; i < 16; i++)
        w[i] = dv.getUint32(i * 4, false);
    for (let i = 16; i < 64; i++) {
        const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
        const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
        w[i] = (w[i - 16] + s0 + w[i - 7] + s1) | 0;
    }
    let a = h[0], b = h[1], c = h[2], d = h[3];
    let e = h[4], f = h[5], g = h[6], hh = h[7];
    for (let i = 0; i < 64; i++) {
        const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
        const ch = (e & f) ^ (~e & g);
        const t1 = (hh + S1 + ch + KK[i] + w[i]) | 0;
        const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
        const maj = (a & b) ^ (a & c) ^ (b & c);
        const t2 = (S0 + maj) | 0;
        hh = g;
        g = f;
        f = e;
        e = (d + t1) | 0;
        d = c;
        c = b;
        b = a;
        a = (t1 + t2) | 0;
    }
    h[0] = (h[0] + a) | 0;
    h[1] = (h[1] + b) | 0;
    h[2] = (h[2] + c) | 0;
    h[3] = (h[3] + d) | 0;
    h[4] = (h[4] + e) | 0;
    h[5] = (h[5] + f) | 0;
    h[6] = (h[6] + g) | 0;
    h[7] = (h[7] + hh) | 0;
    for (let i = 0; i < 8; i++) {
        const v = h[i] >>> 0;
        hLE[i * 4] = v & 0xff;
        hLE[i * 4 + 1] = (v >> 8) & 0xff;
        hLE[i * 4 + 2] = (v >> 16) & 0xff;
        hLE[i * 4 + 3] = (v >> 24) & 0xff;
    }
}
/**
 * RustCrypto sha2 语义的 finalize：从 LE 中间态继续处理 buffer 并输出最终 32 字节。
 * 输出为标准 BE 格式（与 sha256sum 一致）。
 */
function finalizeLE(stateLE, buffer, priorBlocks) {
    const hLE = Buffer.from(stateLE);
    const bitLen = BigInt(priorBlocks) * 512n + BigInt(buffer.length) * 8n;
    const withOne = Buffer.concat([buffer, Buffer.from([0x80])]);
    let total = withOne.length;
    while (total % 64 !== 56)
        total++;
    const buf = Buffer.alloc(total + 8);
    withOne.copy(buf);
    const dv = new DataView(buf.buffer);
    dv.setBigUint64(total, bitLen, false);
    for (let off = 0; off < buf.length; off += 64) {
        compressBlockLE(hLE, buf.subarray(off, off + 64));
    }
    // final 输出：LE → BE（标准 SHA256 摘要格式）
    const out = Buffer.alloc(32);
    for (let i = 0; i < 8; i++) {
        const b0 = hLE[i * 4], b1 = hLE[i * 4 + 1], b2 = hLE[i * 4 + 2], b3 = hLE[i * 4 + 3];
        out[i * 4] = b3;
        out[i * 4 + 1] = b2;
        out[i * 4 + 2] = b1;
        out[i * 4 + 3] = b0;
    }
    return out;
}
/**
 * SHA256 单块压缩的中间态（无 padding）：IV → compress(64B block) → LE 中间态。
 */
function intermediateLE(block64) {
    const hLE = Buffer.from(IV_LE);
    compressBlockLE(hLE, block64);
    return hLE;
}
/**
 * RustCrypto 语义的 HMAC-SHA256。
 * key > 64B → normKey = SHA256(key)；ipad/opad state 以 LE 中间态保存；
 * inner = finalize(ipadState, msg, 1)；outer = finalize(opadState, inner, 1)。
 */
function hmacLE(key, msg) {
    const nk = key.length > 64
        ? createHash('sha256').update(key).digest()
        : Buffer.concat([key, Buffer.alloc(64 - key.length)]);
    const ipad64 = Buffer.alloc(64);
    const opad64 = Buffer.alloc(64);
    for (let i = 0; i < 64; i++) {
        ipad64[i] = nk[i] ^ 0x36;
        opad64[i] = nk[i] ^ 0x5c;
    }
    const ipadState = intermediateLE(ipad64);
    const opadState = intermediateLE(opad64);
    const inner = finalizeLE(ipadState, msg, 1);
    return finalizeLE(opadState, inner, 1);
}
/**
 * 组装 97 字节 HMAC key（完整逆向确认）。
 * = uid_ASCII(24) + 0x01 + hour_LE_u64(8) + SHA256(token)(32) + SHA256(ver)(32)
 */
export function buildHmacKey(userId, token, ver, timestampSec) {
    const uidB = Buffer.from(userId, 'ascii');
    if (uidB.length !== 24) {
        throw new Error(`AtomGit user_id 长度应为 24，实际 ${uidB.length}`);
    }
    const hour = Math.floor(timestampSec / 3600);
    const fixedB = Buffer.alloc(8);
    fixedB.writeBigUInt64LE(BigInt(hour));
    const a = createHash('sha256').update(token, 'ascii').digest();
    const b = createHash('sha256').update(ver, 'ascii').digest();
    return Buffer.concat([uidB, Buffer.from([0x01]), fixedB, a, b]);
}
/** 计算 X-AtomCode-* 签名头（完整逆向实现，实测网关 200）。 */
export function sign(input) {
    const bodyBuf = Buffer.isBuffer(input.body) ? input.body : Buffer.from(input.body, 'utf8');
    const key97 = buildHmacKey(input.userId, input.token, ATOMCODE_SIG_VER, input.timestampSec);
    const kFirst = hmacLE(key97, ATOMCODE_STATIC32);
    const info = Buffer.concat([Buffer.from('atomcode-signing-v1', 'ascii'), Buffer.from([0x01])]);
    const t1 = hmacLE(kFirst, info);
    const bodyHash = createHash('sha256').update(bodyBuf).digest().toString('hex');
    const canonical = `v1\n${input.method}\n${input.path}\n${input.timestampSec}\n${input.nonce.toString('hex')}\n${bodyHash}`;
    const sig = hmacLE(t1, Buffer.from(canonical, 'binary'));
    return {
        sig: `v1:${sig.toString('hex')}`,
        ts: String(input.timestampSec),
        nonce: input.nonce.toString('hex'),
        alg: ATOMCODE_SIG_ALG,
        ver: ATOMCODE_SIG_VER,
    };
}
/** 便捷入口：生成随机 nonce 并签名。 */
export function signWithRandomNonce(method, path, body, token, userId, timestampSec) {
    return sign({
        method, path, body, token, userId, timestampSec,
        nonce: randomBytes(16),
    });
}
export { hmacLE as hmacRustCrypto };
//# sourceMappingURL=atomcode-sign.js.map