"""服务商模型目录：把「现在到底能用哪些模型」变成一次可缓存查询。

为什么需要它：模型名硬编码在插件里，服务商上下架模型时用户只能靠试错 ——
报 429 / 404 / 超时的时候也说不清是「模型不对」还是「额度用完了」。
两家的接口都是 OpenAI 兼容的 ``GET {base}/models``，所以统一走一条路：

* 商汤 SenseNova ``https://token.sensenova.cn/v1/models``
  返回体带 ``context_length`` / ``max_output_length`` / ``input_modalities`` /
  ``output_modalities`` / ``supported_features`` / ``pricing`` 等元数据，
  因此**同一个请求就能同时切出语言模型与图片模型**（看输出模态）。
* 小米 MiMo ``https://api.xiaomimimo.com/v1/models``
  返回体是标准 ``{object: list, data: [{id, object, owned_by}]}``，
  鉴权头用 ``api-key``（与它家 chat 接口一致）。

API Key 只用于请求头，**永不落盘**：磁盘缓存里只存归一化后的模型元数据，
以及 Key 的 8 位 sha1 指纹，用来在 Key 换掉时让缓存失效。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any

from . import config

PROVIDER_SENSENOVA = "sensenova"
PROVIDER_MIMO = "mimo"
# Jet Hub：模型清单来自**本机桥**（`http://127.0.0.1:<port>/v1/models`），
# 而不是某个云端服务商。凭据也不在这层读 —— 桥自己管账号池。
PROVIDER_JETHUB = "jethub"

# 用途 -> (服务商, 筛选规则)
# 商汤同一个模型清单里既有文本模型（deepseek-v4-pro / glm-5.2 / kimi-k3 …）
# 也有图片模型（sensenova-u1.5-lite / sensenova-u1-fast），按输出模态切分最稳。
KINDS: dict[str, str] = {
    "llm": PROVIDER_SENSENOVA,
    "image": PROVIDER_SENSENOVA,
    # 图生图与文生图共用同一份 /models 清单，但**能力筛选不同**：
    # 只有支持 edits 的型号才能出现在这里（见 _EDIT_UNSUPPORTED）。
    "image_edit": PROVIDER_SENSENOVA,
    "tts": PROVIDER_MIMO,
    # Jet Hub 走**本机桥**而不是远端 /models：桥的 `/v1/models` 会把
    # CodeBuddy 的可用模型列成标准 OpenAI 形状。与上面几项并列成独立用途，
    # 这样面板右上角的「↻ 模型」按钮、缓存、回落逻辑全部免费复用。
    "llm_jethub": PROVIDER_JETHUB,
}

# 各用途下的首选（排在列表最前，并在标签上标注）
PREFERRED: dict[str, tuple[str, ...]] = {
    "llm": ("deepseek-v4-pro",),
    "image": ("sensenova-u1.5-lite",),
    "image_edit": ("sensenova-u1.5-lite", "sensenova-u1.5-fast"),
    "tts": ("mimo-v2.5-tts",),
    # Jet Hub 的首选随账号套餐变化，这里不硬编码 —— 让远端顺序说话。
    "llm_jethub": (),
}

# --------------------------------------------------------------------------
# 实测校准表（2026-09-23 直连商汤接口逐项验证，非文档推断）
# --------------------------------------------------------------------------
# 为什么需要它：**不能只信 /models**。实测「/models 报的」与「接口真收的」
# 并不一致，两个方向都会错：
#
#   * 漏报：`sensenova-u1.5-fast` 没出现在 /models 里，但它实际
#     generations 与 edits 都返回 200。只信清单的话，用户在下拉里**永远
#     看不到这个可用型号**，只能手填 —— 这正是"没拉取到正确的商汤模型"。
#   * 误报能力：清单里的 `sensenova-u1-fast` 做 edits 会返回
#     400 `model does not support image editing`。它出现在图生图下拉里
#     会让**每一次重绘都失败**。
#
# 因此：清单为准，但要用本表**补齐漏报** + **剔除不支持的能力**。

_VERIFIED_EXTRA: tuple[dict[str, Any], ...] = (
    {
        "id": "sensenova-u1.5-fast",
        "description": (
            "SenseNova U1.5 Fast（/models 未列出，实测文生图与图生图均可用）"
        ),
        "input_modalities": ["text"],
        "output_modalities": ["image"],
        "context_length": 262144,
        "max_output_length": 65536,
        "supported_features": ["tools", "json_mode", "reasoning"],
    },
)

# 实测**不支持图生图（edits）**的型号。图生图下拉必须排除它们。
_EDIT_UNSUPPORTED: frozenset[str] = frozenset({"sensenova-u1-fast"})

# --------------------------------------------------------------------------
# 实测校准表之二：`/models` 会列出**当前 token plan 用不了**的型号（F-015）
# --------------------------------------------------------------------------
# 2026-09-25 用真实 Key 对 ``/models`` 返回的 9 个型号逐个发 chat/completions：
#
# | 型号                      | /models | 实际调用                                  |
# |---------------------------|---------|-------------------------------------------|
# | deepseek-v4-flash         | 列了    | 200 可用                                  |
# | deepseek-v4-pro           | 列了    | 200 可用（偶发 429 tpm 限流）             |
# | deepseek-flash            | 列了    | 200 可用（偶发 429）                      |
# | deepseek-v4.1-flash       | 列了    | **403 model is not available in the current token plan** |
# | glm-5.2 / kimi-k3 /       | 列了    | 200 可用                                  |
# | sensenova-6.8-flash-lite  |         |                                           |
# | sensenova-u1.5-lite /     | 列了    | 图片模型（语言端点 404 属正常）           |
# | sensenova-u1-fast         |         |                                           |
#
# 即：**清单里的型号未必在当前套餐内**。用户选中 `deepseek-v4.1-flash` 后
# Agent 0 直接失败。这类"列了但不能用"的项必须在**语言模型**下拉里剔除，
# 否则用户会一个个撞上去（与 F-002 的"误报能力"同类，但成因不同：
# F-002 是能力不符，这里是**套餐权限**不符）。
#
# 注意它**不是**能力问题：该型号 `supported_features` 写着 tools/json_mode/reasoning，
# 元数据一切正常，只有真正发请求才知道被套餐挡住。所以只能靠实测表。
_PLAN_RESTRICTED: frozenset[str] = frozenset({"deepseek-v4.1-flash"})

# 该型号被套餐限制时的提示（面板标签里显示，替代"选中后必然失败"）
_PLAN_RESTRICTED_HINT = "当前 token plan 不可用（实测 403，勿选）"

_CACHE_TTL_SECONDS = 300.0
_CACHE_LOCK = threading.Lock()
_CACHE: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}

KIND_LABELS: dict[str, str] = {
    "llm": "语言模型",
    "image": "图片模型",
    "image_edit": "图生图模型",
    "tts": "配音模型",
    "llm_jethub": "Jet Hub 模型",
}


# --------------------------------------------------------------------------
# 元数据归一化
# --------------------------------------------------------------------------

def _context_label(value: Any) -> str:
    """1048576 -> ``1M``；262144 -> ``256K``。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return ""
    if number <= 0:
        return ""
    if number >= 1024 * 1024:
        return f"{number / (1024 * 1024):g}M"
    if number >= 1024:
        return f"{number // 1024}K"
    return str(number)


def _is_free(item: dict[str, Any]) -> bool:
    """pricing 全 0 视为免费额度内。字段缺失时不表态。"""
    pricing = item.get("pricing")
    if not isinstance(pricing, dict) or not pricing:
        return False
    for key in ("prompt", "completion"):
        raw = str(pricing.get(key, "")).strip()
        if not raw:
            continue
        try:
            if float(raw) != 0:
                return False
        except (TypeError, ValueError):
            return False
    return True


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _sensenova_kind_ok(item: dict[str, Any], kind: str) -> bool:
    model_id = _normalize_id(item)

    # 能力闸必须**最先**判，且与模态字段是否存在无关。
    # 曾把它放在 `if outputs:` 分支里，导致没有 output_modalities 的行
    # （磁盘缓存里手工构造的、或早期版本写歪的）会走名字兜底而绕开这道闸 ——
    # 不支持图生图的型号照样出现在图生图下拉里。自检 D 节抓到了这一点。
    if kind == "image_edit" and model_id in _EDIT_UNSUPPORTED:
        return False

    # 套餐权限闸：/models 列了、但当前 token plan 用不了（F-015）。
    # 与上面的能力闸同理必须**最先**判 —— 这类型号元数据完全正常
    # （tools/json_mode/reasoning 齐全），只有在真正的套餐校验上才被挡。
    # 只影响**语言模型**下拉：图片模型不走这条套餐限制。
    if kind == "llm" and model_id in _PLAN_RESTRICTED:
        return False

    outputs = _as_list(item.get("output_modalities"))
    if not outputs:
        # 老网关不带模态字段时用名字兜底：u1 系列是图片模型
        name = model_id.lower()
        looks_image = "-u1" in name or "image" in name
        return looks_image if kind.startswith("image") else not looks_image
    if kind.startswith("image"):
        return "image" in outputs
    return "text" in outputs


def _normalize_id(entry: dict[str, Any]) -> str:
    """统一取模型 id：上游清单用 ``id``，磁盘缓存里的归一化行用 ``value``。"""
    return str(entry.get("id") or entry.get("value") or entry.get("name") or "").strip()


def _merge_verified_extra(raw: list[Any], kind: str) -> list[Any]:
    """把实测确认可用、但 ``/models`` 漏报的型号补进原始清单。

    只对图片类生效，且**已经存在就不重复补**。补进去的是上游清单那种
    完整元数据形态（``id`` + ``output_modalities``），因此后续的 kind
    筛选手到擒来；如果输入来自磁盘缓存（``value`` 形态），
    同样能正确判重，不会补出重复项。
    """
    if not kind.startswith("image"):
        return raw
    present = {
        _normalize_id(item)
        for item in raw
        if isinstance(item, dict)
    }
    merged = list(raw)
    for extra in _VERIFIED_EXTRA:
        if _normalize_id(extra) in present:
            continue
        # 同时带上 ``value``：这条数据会流经两条路径 —— ``_fetch`` 走
        # ``_normalize``（认 ``id``），``cached_models`` 直接消费（认 ``value``）。
        # 两种键都给，两条路都不会 KeyError / 丢项。
        merged.append({**extra, "value": extra["id"]})
    return merged


def _mimo_kind_ok(item: dict[str, Any], kind: str) -> bool:
    name = str(item.get("id") or "").lower()
    if kind == "tts":
        return "tts" in name
    return "tts" not in name


def _normalize(item: Any, kind: str, provider: str) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    model_id = str(item.get("id") or item.get("name") or "").strip()
    if not model_id:
        return None
    return {
        "value": model_id,
        "label": model_id,
        "provider": provider,
        "context_length": item.get("context_length"),
        "context_label": _context_label(item.get("context_length")),
        "max_output_length": item.get("max_output_length"),
        "max_output_label": _context_label(item.get("max_output_length")),
        "input_modalities": _as_list(item.get("input_modalities")),
        "output_modalities": _as_list(item.get("output_modalities")),
        "features": _as_list(item.get("supported_features")),
        "free": _is_free(item),
        "owned_by": str(item.get("owned_by") or ""),
        "description": str(item.get("description") or "").strip(),
    }


def describe(row: dict[str, Any]) -> str:
    """拼一个给人看的标签。

    用户在面板上要做的判断是「换哪个模型能绕开限流」，所以标签里放的是
    上下文长度、输出上限、能力标签这类可比较的信息，而不是光秃秃的 id。
    """
    parts: list[str] = []
    if row.get("context_label"):
        parts.append(f"{row['context_label']} 上下文")
    if row.get("max_output_label") and row.get("max_output_label") != row.get("context_label"):
        parts.append(f"输出 {row['max_output_label']}")
    features = set(row.get("features") or [])
    if "json_mode" in features:
        parts.append("JSON 约束")
    if "reasoning" in features:
        parts.append("推理")
    if "image" in (row.get("input_modalities") or []):
        parts.append("可读图")
    return " · ".join(parts)


def _label_for(row: dict[str, Any], kind: str) -> str:
    text = row["value"]
    # 商汤给图片模型也填了 context_length / json_mode 这类字段，但对出图毫无意义，
    # 所以只有语言模型的标签才展开元数据，其余保持干净的 id。
    detail = describe(row) if kind == "llm" else ""
    if detail:
        text += f"　{detail}"
    if row["value"] in PREFERRED.get(kind, ()):
        text += "　★ 推荐"
    return text


def _sort_key(row: dict[str, Any], kind: str, index: int) -> tuple[int, int]:
    preferred = PREFERRED.get(kind, ())
    rank = preferred.index(row["value"]) if row["value"] in preferred else len(preferred)
    return (rank, index)


def panel_options(kind: str) -> list[dict[str, str]]:
    """给面板下拉用：``{value,label}`` 列表。**不发网络请求**。"""
    return [
        {"value": str(row["value"]), "label": str(row.get("label") or row["value"])}
        for row in cached_models(kind)
    ]


def verify_note(kind: str) -> str:
    """面板上那句"清单从哪来"的说明，讲清补齐/剔除这两件事。"""
    if kind == "llm_jethub":
        return (
            "来自 **Jet Hub 本机桥**（CodeBuddy 账号实际可用的模型），"
            "不含任何本地校正表 —— 桥给什么就是什么。"
            "清单为空说明还没登录账号，或模型都被「显示列表」关掉了。"
        )
    if kind == "image_edit":
        return (
            "基于服务商 /models 清单，并剔除实测不支持图生图的型号"
            f"（{'、'.join(sorted(_EDIT_UNSUPPORTED))}）；"
            f"另补齐实测可用的 {'、'.join(item['id'] for item in _VERIFIED_EXTRA)}。"
        )
    if kind == "image":
        return (
            "基于服务商 /models 清单，并补齐实测可用的型号"
            f"（{'、'.join(item['id'] for item in _VERIFIED_EXTRA)}）。"
        )
    if kind == "llm":
        restricted = "、".join(sorted(_PLAN_RESTRICTED))
        if restricted:
            return (
                "基于服务商 /models 清单，并剔除**当前套餐不可用**的型号"
                f"（{restricted}：清单里有、实测 403 不在当前 token plan）。"
            )
    return "基于服务商 /models 清单。"


# --------------------------------------------------------------------------
# 请求
# --------------------------------------------------------------------------

def _credentials(kind: str, api_key: str, api_base: str) -> tuple[str, str, str]:
    """返回 ``(provider, key, base)``。空值表示用当前配置里的值。

    图片类读**独立**的 ``SENSENOVA_IMAGE_API_KEY / _IMAGE_API_BASE``（面板可
    单独配置），两者都留空时回落到语言模型的 Key / 地址。

    **Jet Hub 是特例**：它读本机桥，不需要 Key（桥自己管账号池），
    因此返回一个占位 key 让上层"未配置 Key"的短路检查放行。
    """
    provider = KINDS.get(kind, PROVIDER_SENSENOVA)
    if provider == PROVIDER_JETHUB:
        from . import jethub_bridge

        return provider, "local-bridge", jethub_bridge.bridge_base_url()
    if provider == PROVIDER_SENSENOVA:
        if kind.startswith("image"):
            # 图生图与文生图共用同一组图片凭据（面板可给图片单独配 Key/地址）
            base = (api_base or config.sensenova_image_base_url()).rstrip("/")
            key = api_key or config.sensenova_image_api_key()
        else:
            base = (api_base or config.sensenova_base_url()).rstrip("/")
            key = api_key or config.sensenova_api_key()
    else:
        base = (api_base or config.mimo_base_url()).rstrip("/")
        key = api_key or config.mimo_api_key()
    return provider, key, base


def _headers(provider: str, key: str) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {key}"}
    if provider == PROVIDER_MIMO:
        # MiMo 的 chat 接口要求 api-key + Authorization 双头，模型清单同样吃 api-key
        headers["api-key"] = key
    return headers


def _fingerprint(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8", "replace")).hexdigest()[:8]


def list_models(
    kind: str = "llm",
    *,
    api_key: str = "",
    api_base: str = "",
    force: bool = False,
    timeout: tuple[float, float] = (10.0, 20.0),
) -> dict[str, Any]:
    """拉取某用途下的可用模型。

    返回结构稳定，面板直接渲染：

    ``{"ok": bool, "kind": str, "models": [...], "message": str, "cached": bool}``
    """
    kind = str(kind or "llm").strip().lower()
    if kind not in KINDS:
        return {"ok": False, "kind": kind, "models": [], "message": f"未知的模型用途：{kind}"}

    provider, key, base = _credentials(kind, api_key, api_base)
    if not key:
        label = "MiMo" if provider == PROVIDER_MIMO else KIND_LABELS.get(kind, "模型")
        return {
            "ok": False,
            "kind": kind,
            "models": [],
            "message": f"未配置{label} API Key，无法读取模型列表",
            "api_base": base,
        }

    cache_key = (provider, kind, f"{base}|{_fingerprint(key)}")
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(cache_key)
        if hit and not force and (now - hit[0]) < _CACHE_TTL_SECONDS:
            payload = dict(hit[1])
            payload["cached"] = True
            return payload

    result = _fetch(kind, provider, key, base, timeout)
    if result.get("ok"):
        with _CACHE_LOCK:
            _CACHE[cache_key] = (now, result)
        _write_disk_cache(kind, result)
    return result


def _fetch_jethub(kind: str, base: str, timeout: tuple[float, float]) -> dict[str, Any]:
    """从**本机桥**聚合云端 OAuth 全部提供商的模型清单。

    ## 关键设计：模型 id 编码成 `<provider>::<model>`

    用户要求「语言模型页能选择云端 OAuth 中的**具体哪个模型**」——
    但不同提供商的模型 id 会**重名**（`deepseek-v4-flash` 在 CodeBuddy、
    LobsterAI、Loomy 都存在）。只列裸 id 的话，桥无从知道该把请求发给哪一家。

    所以 `value` 一律是 **`<provider>::<model>`**：
      * 下拉标签形如「CodeBuddy（腾讯） · DeepSeek V4 Flash」，一眼看出归属；
      * OCV 把这个字符串当 model 发过来，桥在 `complete()` 里按 `::` 拆开路由。

    ## 只列「有可用模型」的提供商

    没登录的提供商本来就没模型（桥会返回空），列出来只会让下拉充斥无效项。

    ## 与其它 provider 的差别

    不需要 Key（桥自己管账号池）；桥没起来时返回「空清单 + 可操作提示」
    而不是报错 —— 这是常态而非故障。
    """
    from . import jethub_bridge

    if not jethub_bridge.is_listening():
        return {
            "ok": False,
            "kind": kind,
            "models": [],
            "api_base": base,
            "message": "云端 OAuth 桥未运行：请到「云端 OAuth」分页点「启动 / 重启桥」。",
        }

    per_call = min(float(timeout[1]), 25.0)
    status = jethub_bridge.call("/rpc/status", {}, timeout=per_call)
    if not status["ok"]:
        return {
            "ok": False,
            "kind": kind,
            "models": [],
            "api_base": base,
            "message": f"读取云端 OAuth 状态失败：{status['error']}",
        }
    providers = ((status["data"] or {}).get("value") or {}).get("providers") or []

    rows: list[dict[str, Any]] = []
    usable_providers = 0
    for provider in providers:
        provider_id = str(provider.get("id") or "").strip()
        if not provider_id:
            continue
        # ## 只列**有账号**的提供商（F-019 的真实缺陷）
        #
        # `models.list` 对没登录的 provider 也会返回 vendor 的**静态默认模型表**，
        # 于是「聚合全部提供商」会把 6 个没账号的 provider 的模型也列进去 ——
        # 实测下拉里 45 个模型只有 15 个真能用，用户选中那些只会得到
        # 「还没有任何账号，请先登录」。
        #
        # 光看模型列表分不出有没有账号，所以账号数由 `/rpc/status` 一并给出
        # （见 `server.mjs` 的 `status`）。
        if int(provider.get("accountCount") or 0) <= 0:
            continue
        result = jethub_bridge.call(
            "/rpc/models.list", {"provider": provider_id}, timeout=per_call
        )
        if not result["ok"]:
            continue
        models = ((result["data"] or {}).get("value") or {}).get("models") or []
        # 「显示列表」关掉的模型不列（与桥的黑名单语义一致）
        usable = [
            m for m in models
            if isinstance(m, dict) and not m.get("disabled") and str(m.get("id") or "").strip()
        ]
        if not usable:
            continue
        usable_providers += 1
        prefix = str(provider.get("label") or provider_id)
        for model in usable:
            model_id = str(model["id"]).strip()
            display = str(model.get("name") or model_id).strip()
            rows.append(
                {
                    "value": f"{provider_id}::{model_id}",
                    "label": f"{prefix} · {display}",
                }
            )

    if not rows:
        return {
            "ok": True,
            "kind": kind,
            "models": [],
            "api_base": base,
            "message": (
                "还没有可用模型 —— 通常是还没登录账号。"
                "请到「云端 OAuth」分页登录一个提供商，再回来点「刷新」。"
            ),
        }

    return {
        "ok": True,
        "kind": kind,
        "models": rows,
        "api_base": base,
        "message": f"来自云端 OAuth 本机桥：{usable_providers} 个提供商、{len(rows)} 个模型。",
    }

def _fetch(
    kind: str,
    provider: str,
    key: str,
    base: str,
    timeout: tuple[float, float],
) -> dict[str, Any]:
    if provider == PROVIDER_JETHUB:
        return _fetch_jethub(kind, base, timeout)

    import requests  # 延迟导入：面板/控制台大部分路径用不到网络栈

    url = f"{base}/models"
    try:
        response = requests.get(url, headers=_headers(provider, key), timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 面板要把原因原样告诉用户
        return {
            "ok": False,
            "kind": kind,
            "models": [],
            "api_base": base,
            "message": f"读取模型列表失败：{type(exc).__name__}: {exc}",
        }

    if response.status_code >= 400:
        return {
            "ok": False,
            "kind": kind,
            "models": [],
            "api_base": base,
            "message": f"读取模型列表失败：HTTP {response.status_code} {response.text[:200]}",
        }

    try:
        body = response.json()
    except ValueError as exc:
        return {
            "ok": False,
            "kind": kind,
            "models": [],
            "api_base": base,
            "message": f"模型列表返回体不是 JSON：{exc}",
        }

    raw = body.get("data") if isinstance(body, dict) else None
    if not isinstance(raw, list):
        # 少数网关把清单放在 models 键下
        raw = body.get("models") if isinstance(body, dict) else None
    if not isinstance(raw, list):
        return {
            "ok": False,
            "kind": kind,
            "models": [],
            "api_base": base,
            "message": "模型列表返回体里没有 data 数组",
        }

    matcher = _sensenova_kind_ok if provider == PROVIDER_SENSENOVA else _mimo_kind_ok
    if provider == PROVIDER_SENSENOVA:
        # 先用实测校准表补齐 /models 的漏报，再按用途筛选。
        raw = _merge_verified_extra(raw, kind)

    rows: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        if not matcher(item, kind):
            continue
        normalized = _normalize(item, kind, provider)
        if normalized is None:
            continue
        normalized["_order"] = index
        rows.append(normalized)

    rows.sort(key=lambda row: _sort_key(row, kind, int(row.get("_order", 0))))
    for row in rows:
        row.pop("_order", None)
        row["label"] = _label_for(row, kind)

    return {
        "ok": True,
        "kind": kind,
        "provider": provider,
        "api_base": base,
        "models": rows,
        "count": len(rows),
        "message": f"已读取 {len(rows)} 个{KIND_LABELS.get(kind, '')}",
        "fetched_at": time.time(),
        "cached": False,
    }


# --------------------------------------------------------------------------
# 磁盘缓存：给 panels 之外的消费者用（patches 填充 provider 模型表时读它，
# 这样 OCV 原生下拉也能跟着服务商实际情况走，而不用在启动路径上发网络请求）
# --------------------------------------------------------------------------

def cache_path():
    return config.plugin_var_dir() / "models_cache.json"


def _write_disk_cache(kind: str, result: dict[str, Any]) -> None:
    """按用途各存一份。

    图生图那份是**已按 edits 能力筛过**的（不含 sensenova-u1-fast），
    文生图那份是完整的。两份各自都是对应用途的正确可选集；
    ``cached_models`` 读取时还会再过一遍筛选（幂等），因此不会漂移。
    """
    store = _read_disk_cache()
    store[kind] = {
        "fetched_at": result.get("fetched_at"),
        "api_base": result.get("api_base"),
        "models": result.get("models") or [],
    }
    try:
        payload = json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True)
        target = cache_path()
        temporary = target.with_name(target.name + ".part")
        temporary.write_text(payload + "\n", encoding="utf-8")
        temporary.replace(target)
    except OSError:
        pass


def _read_disk_cache() -> dict[str, Any]:
    try:
        parsed = json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def cached_models(kind: str) -> list[dict[str, Any]]:
    """只读磁盘缓存里的模型清单（不发网络请求）。没有缓存时返回空列表。

    **不能直接回吐磁盘内容**：缓存可能是修复前写入的（那时既漏了
    ``sensenova-u1.5-fast``，又把不支持 edits 的 ``sensenova-u1-fast``
    混在图生图里）。面板首屏正是走这条路径，回吐陈旧数据就会重现原故障。
    因此这里同样过一遍「补齐漏报 + 按用途筛能力」。
    """
    provider = KINDS.get(kind)
    if provider is None:
        return []
    store = _read_disk_cache()
    entry = store.get(kind)
    if not isinstance(entry, dict) and kind == "image_edit":
        # 只允许 image_edit ← image 这一个方向兜底：文生图那份是**完整**清单，
        # 图生图读进来后还会再按 edits 能力筛一遍，方向安全。
        # 反方向（image ← image_edit）**绝不可以**：图生图那份已经剔除了
        # sensenova-u1-fast，拿来当文生图清单会把这个可用型号弄丢。
        if isinstance(store.get("image"), dict):
            entry = store["image"]
    if not isinstance(entry, dict):
        return []
    models = entry.get("models")
    if not isinstance(models, list):
        return []
    rows = [row for row in models if isinstance(row, dict) and _normalize_id(row)]
    if provider == PROVIDER_SENSENOVA:
        rows = _merge_verified_extra(rows, kind)
        # 缓存里可能存着别的用途的行（例如手工编辑过、或早期版本写歪了），
        # 一律按用途重新筛一遍，保证下拉里只有真正能用于该用途的型号。
        rows = [row for row in rows if _sensenova_kind_ok(row, kind)]
        rows.sort(key=lambda row: _sort_key(row, kind, 0))
    elif provider == PROVIDER_JETHUB:
        # Jet Hub 的清单只来自本机桥，没有"补齐/剔除"这类校正，原样返回。
        rows.sort(key=lambda row: int(row.get("_order", 0)))
    return rows


def cache_info() -> dict[str, dict[str, Any]]:
    """各用途上一次成功读取的时间与条数，供面板展示「清单有多新」。"""
    store = _read_disk_cache()
    info: dict[str, dict[str, Any]] = {}
    for kind, entry in store.items():
        if not isinstance(entry, dict):
            continue
        models = entry.get("models")
        count = len(models) if isinstance(models, list) else 0
        if count:
            info[str(kind)] = {"fetched_at": entry.get("fetched_at"), "count": count}
    return info


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()
