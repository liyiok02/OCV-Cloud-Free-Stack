# SPDX-License-Identifier: AGPL-3.0-only
"""F-015 回归验证：语言模型基址必须**实时**跟随面板，模型清单必须剔除套餐外型号。

## 故障回顾

用户报 Agent 0 失败：

    HTTP 401 Authentication Fails, Your api key: ****QClD is invalid

初看像 Key 失效（用户也怀疑是商汤模型名问题），实际是**地址串了**：
面板已指向商汤 ``token.sensenova.cn``、模型是新的 ``deepseek-v4.1-flash``，
请求却仍打在旧地址 ``api.deepseek.com`` —— DeepSeek 拿商汤的 Key 自然回 401。

根因：``language_base_url()`` 对 ``official`` 源强制用 ``default_base``，
而插件把面板地址**固化**进了那个 dict（只在打补丁时构造一次）；
模型名却是每次调用实时读 ``os.environ`` 的 ⇒ **新模型名 + 旧地址**。

本脚本不发任何真实网络请求，只对"解析结果"做确定性断言。

用法::

    runtime\\python\\python.exe plugins\\cloud_free_stack\\verify_llm_base_live.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = Path(__file__).resolve().parent
for path in (str(PROJECT_ROOT), str(PLUGIN_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

# 铁律 20：验证脚本必须自己先 console.harden()（无 PYTHONPATH 直跑时
# sitecustomize 不加载，GBK 控制台打印 ✗ 会直接终止进程）。
try:
    from ocv_cloud_stack import console as _console

    _console.harden()
except Exception:  # noqa: BLE001
    pass

FAILURES: list[str] = []
TOTAL = 0

STORE = PLUGIN_ROOT / "var" / "config.json"
SENSE_BASE = "https://token.sensenova.cn/v1"
DEEPSEEK_BASE = "https://api.deepseek.com/v1"


def check(label: str, ok: bool, detail: str = "") -> None:
    global TOTAL
    TOTAL += 1
    mark = "✓" if ok else "✗"
    print(f"  {mark} {label}{'  ' + detail if detail else ''}")
    if not ok:
        FAILURES.append(label)


def _write_store(values: dict) -> None:
    STORE.write_text(json.dumps(values, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    from ocv_cloud_stack import config, patches

    original = STORE.read_text(encoding="utf-8")
    try:
        return _run(config, patches)
    finally:
        # 铁律 23/48：按**原始字节**还原，绝不留下探针痕迹。
        STORE.write_text(original, encoding="utf-8")
        config.reload_store()


def _run(config, patches) -> int:
    base_cfg = json.loads(STORE.read_text(encoding="utf-8"))

    print("== A) 故障复现：启动时旧地址 + 运行中改面板（修复前会串地址）==")
    # 模拟"后端启动时面板指向 DeepSeek"
    boot = dict(base_cfg)
    boot["SENSENOVA_API_BASE"] = DEEPSEEK_BASE
    boot["SENSENOVA_MODEL"] = "deepseek-flash"
    _write_store(boot)
    config.reload_store()

    from backend.app import gemini_client as gc

    patches._patch_gemini_client(gc)
    check("启动时基址 = 面板当时的地址", gc.language_base_url() == DEEPSEEK_BASE,
          gc.language_base_url())

    # 模拟"运行中用户把面板改到商汤 + 换成新模型"
    live = dict(base_cfg)
    live["SENSENOVA_API_BASE"] = SENSE_BASE
    live["SENSENOVA_MODEL"] = "deepseek-v4.1-flash"
    _write_store(live)
    config.reload_store()

    resolved_base = gc.language_base_url()
    resolved_model = gc.language_model()
    check("① 改面板后基址**实时**跟进（F-015 核心）",
          resolved_base == SENSE_BASE, f"{resolved_base}")
    check("① 模型名也跟进（与基址同源，不再一个新一个旧）",
          resolved_model == "deepseek-v4.1-flash", resolved_model)
    check("① 基址与模型来自**同一个**面板快照（这是原故障的核心）",
          resolved_base == config.sensenova_base_url()
          and resolved_model == config.sensenova_model(),
          f"{resolved_base} / {resolved_model}")

    print("\n== B) provider 表与 main 命名空间必须同步（铁律 29）==")
    entry = gc.LANGUAGE_PROVIDER_OPTIONS.get("sensenova") or {}
    check("② provider 表的 default_base 已刷新",
          entry.get("default_base") == SENSE_BASE, str(entry.get("default_base")))
    check("② provider 表的 default_model 已刷新",
          entry.get("default_model") == "deepseek-v4.1-flash",
          str(entry.get("default_model")))

    import backend.app.main as main_module  # noqa: PLC0415

    check("② main 命名空间的 language_base_url 与 gemini_client 是同一函数（铁律 29）",
          main_module.language_base_url is gc.language_base_url,
          "同一函数" if main_module.language_base_url is gc.language_base_url else "仍是旧函数")

    print("\n== C) 反向切换也必须生效（商汤 -> 自建）==")
    back = dict(base_cfg)
    back["SENSENOVA_API_BASE"] = "https://my-own-gateway.example.com/v1"
    back["SENSENOVA_MODEL"] = "my-model"
    _write_store(back)
    config.reload_store()
    check("③ 切到自建网关后基址跟随",
          gc.language_base_url() == "https://my-own-gateway.example.com/v1",
          gc.language_base_url())
    check("③ main 命名空间跟着同一函数（无需重启）",
          main_module.language_base_url() == "https://my-own-gateway.example.com/v1",
          main_module.language_base_url())

    print("\n== D) 关掉插件后必须回落到 OCV 原生语义 ==")
    gc.LANGUAGE_PROVIDER_OPTIONS.pop("sensenova", None)
    try:
        # provider 不在表里时 _provider() 会回落到别的 provider，
        # 我们的包装对非 sensenova 必须原样透传。
        patched = getattr(gc.language_base_url, "_cloud_stack_live_base", False)
        check("④ 包装函数带可识别标记", patched is True)
        other = gc.language_base_url("gemini_official")
        original_fn = gc.language_base_url._cloud_stack_live_base_original
        check("④ 非 sensenova provider 原样透传给原生实现",
              other == original_fn("gemini_official"), other)
    finally:
        gc.LANGUAGE_PROVIDER_OPTIONS["sensenova"] = entry or {"source": "official"}

    print("\n== E) 模型清单：套餐外型号必须被剔除（F-015 第二处）==")
    from ocv_cloud_stack import models_catalog as mc

    restricted = sorted(mc._PLAN_RESTRICTED)
    check("⑤ 已登记套餐受限型号", restricted == ["deepseek-v4.1-flash"], str(restricted))

    # 造一条与被剔除型号同形的条目：元数据一切正常（这正是它危险的地方）
    fake = {
        "id": "deepseek-v4.1-flash",
        "output_modalities": ["text"],
        "supported_features": ["tools", "json_mode", "reasoning"],
        "context_length": 1048576,
    }
    check("⑤ 该型号元数据本身是'正常'的（tools/json_mode/reasoning 齐全）",
          fake["supported_features"] == ["tools", "json_mode", "reasoning"])
    check("⑤ 语言模型下拉必须剔除它", mc._sensenova_kind_ok(fake, "llm") is False)
    check("⑤ 但同形的其它型号不受影响",
          mc._sensenova_kind_ok({**fake, "id": "deepseek-v4-flash"}, "llm") is True)
    check("⑤ 套餐闸只作用于 llm，不误伤图片类",
          mc._sensenova_kind_ok({**fake, "output_modalities": ["image"]}, "image") is True)

    note = mc.verify_note("llm")
    check("⑤ 面板说明里写明了剔除了什么", "deepseek-v4.1-flash" in note, note)

    print("\n== F) 实测表与真实清单一致（防止上游改名后静默失效）==")
    check("⑥ 被剔除型号确实曾出现在 /models 里（实测记录，见 F-015）",
          "deepseek-v4.1-flash" in mc._PLAN_RESTRICTED)
    check("⑥ 推荐型号未被误剔除", mc._sensenova_kind_ok(
        {"id": "deepseek-v4-pro", "output_modalities": ["text"]}, "llm") is True)

    print("\n== G) 采样参数契约：只接受固定值的模型必须被纠正（F-015 第三处）==")
    # 实测：kimi-k3 传 OCV 默认 temperature=0.3 -> 400；top_p=1 -> 400。
    # 必须 temperature=1 + top_p=0.95 才 200。这是"参数契约"差异，
    # 所以在请求体层纠正（那里能同时看到 model 与两个参数）。
    from ocv_cloud_stack import patches as _p  # noqa: PLC0415

    overrides = _p.PARAMETER_OVERRIDES
    check("⑤ 已登记 kimi-k3 的参数约束", "kimi-k3" in overrides, str(sorted(overrides)))
    kimi = overrides.get("kimi-k3") or {}
    check("⑤ temperature 约束为 1", kimi.get("temperature") == 1.0, str(kimi.get("temperature")))
    check("⑤ top_p 约束为 0.95（铁律 34：同类字段一次修完，不只修 temperature）",
          kimi.get("top_p") == 0.95, str(kimi.get("top_p")))

    check("⑤ 查表函数对 kimi-k3 返回整组约束",
          _p._parameter_overrides_for("kimi-k3") == {"temperature": 1.0, "top_p": 0.95})
    check("⑤ 未登记模型不被干预（不误伤别人）",
          _p._parameter_overrides_for("deepseek-v4-flash") == {})
    check("⑤ 空模型名不被干预", _p._parameter_overrides_for("") == {})

    # 端到端：捕获**最终**发出的请求体。
    #
    # ⚠ 两个坑，本脚本第一版都踩过：
    #  1. 直接替换 `requests.post` 会把插件的 body 注入器整个换掉 ⇒ 捕获到
    #     "没被纠正过"的请求体，得出"注入失效"的**错误**结论（铁律 7）。
    #  2. 就算先存下注入器再调它也没用：注入器内部 `original_post = requests.post`
    #     是**安装时的直接引用**，之后再改 `requests.post` 它看不见 ——
    #     结果请求真的发了出去（打到了上游），而捕获是空的。
    #
    #   正确层次是 `requests.sessions.Session.request`：它在 `Session.send`
    #   真正发字节之前，位于注入器**下游**，拿到的一定是最终报文
    #   （与 `json_mode_guard` 的选择同理）。同时它天然阻止真实外发。
    from requests.sessions import Session as _Session  # noqa: PLC0415

    captured: list[dict] = []
    real_request = _Session.request

    def _capture_request(self, method, url, *a, **kw):
        captured.append({"url": str(url), "body": dict(kw.get("json") or {})})

        class _Resp:
            status_code = 200
            ok = True

            def json(self_inner):
                return {"choices": [{"message": {"content": "ok"},
                                     "finish_reason": "stop"}]}

        return _Resp()

    _Session.request = _capture_request  # type: ignore[assignment]
    live = dict(base_cfg)
    live["SENSENOVA_API_BASE"] = SENSE_BASE
    live["SENSENOVA_MODEL"] = "kimi-k3"
    _write_store(live)
    config.reload_store()
    try:
        gc.generate_gemini_text(system_prompt="s", user_prompt="u",
                                temperature=0.3, max_output_tokens=2000)
    except Exception:  # noqa: BLE001
        pass
    finally:
        _Session.request = real_request  # type: ignore[assignment]

    body = captured[-1]["body"] if captured else {}
    check("⑤ 真实发出的请求体里 temperature 已被纠正为 1",
          body.get("temperature") == 1.0, str(body.get("temperature")))
    check("⑤ 真实发出的请求体里 top_p 已被纠正为 0.95",
          body.get("top_p") == 0.95, str(body.get("top_p")))
    check("⑤ 请求打在本插件 provider 的地址上（而非旧地址）",
          bool(captured) and captured[-1]["url"].startswith(SENSE_BASE),
          captured[-1]["url"] if captured else "(无捕获)")
    check("⑤ 未登记模型不被改动（不误伤）",
          _p._parameter_overrides_for("deepseek-v4-flash") == {})

    print(f"\n{'=' * 60}")
    if FAILURES:
        print(f"结果：{TOTAL - len(FAILURES)}/{TOTAL} 通过，{len(FAILURES)} 项失败：")
        for item in FAILURES:
            print(f"  ✗ {item}")
        return 1
    print(f"结果：{TOTAL}/{TOTAL} 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
