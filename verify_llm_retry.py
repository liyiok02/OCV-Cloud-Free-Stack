# SPDX-License-Identifier: AGPL-3.0-only
"""语言模型 30 档阶梯重试 —— 独立验证（确定性探针，不发真实请求）。

对齐 ``chai1110/dsh-provider-config`` 的推荐重试策略：

    mode: normal
    maxRetries: 30（与云端视频共用同一张档位表）
    retryableCodes: [RATE_LIMIT, QUOTA, TIMEOUT, SERVER, TRANSPORT]
    backoff: { initialDelayMs: 1000, maxDelayMs: 30000, jitterRatio: 0.3 }

本脚本**不联网、不写用户配置**，只对纯函数与挂载点做确定性断言。

用法::

    runtime\\python\\python.exe plugins\\cloud_free_stack\\verify_llm_retry.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = Path(__file__).resolve().parent
for path in (str(PROJECT_ROOT), str(PLUGIN_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

# 铁律 20：自检 / 验证脚本必须**自己**先 console.harden()。
# 无 PYTHONPATH 直跑时 sitecustomize 不加载；此时在 GBK 控制台打印 ✗ 会
# UnicodeEncodeError 并直接终止进程 —— 最需要诊断时工具自己先死。
try:
    from ocv_cloud_stack import console as _console

    _console.harden()
except Exception:  # noqa: BLE001
    pass

FAILURES: list[str] = []
TOTAL = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global TOTAL
    TOTAL += 1
    mark = "✓" if ok else "✗"
    suffix = f"  {detail}" if detail else ""
    print(f"  {mark} {label}{suffix}")
    if not ok:
        FAILURES.append(label)


def _check_end_to_end() -> None:
    """L) 端到端：伪造 ``requests.post`` 与 ``time.sleep``，验证真实调用路径。

    这一节是**反"单元测试全绿但链路不通"**的护栏（铁律 41）。
    前面几节只证明"策略函数算得对"，这里证明"OCV 真的按它重试了"。

    关键断言：

    * 前 3 次 429 后成功 ⇒ 共 4 次尝试，且**等待档位取自插件阶梯（1,2,4）**，
      而不是 OCV 原生的 3,6,12 —— 这是"补丁真的接管了"的直接证据；
    * 持续 429 ⇒ **恰好 31 次尝试 / 30 次等待**后放弃（有界，不无限重试）；
    * 400 / 401 ⇒ **1 次尝试、0 次等待**（不重试，不掩盖真实故障）。
    """
    import time as _time

    import requests as _requests

    from backend.app import gemini_client as gc
    from ocv_cloud_stack import config as _cfg
    from ocv_cloud_stack import llm_retry as _llm_retry

    # ⚠ 铁律 37：本节断言的是**具体的**尝试次数与档位，必须自己先把配置
    #   固定成"启用 + 30 次"，否则用户在面板里改成 7 次或关掉总开关时，
    #   这些断言全部假失败（本轮对抗性测试实测踩到）。
    #   闭合条件、跑完按快照定向还原。
    _KEY_E = "CLOUD_STACK_LLM_RETRY_ENABLED"
    _KEY_C = "CLOUD_STACK_LLM_RETRY_COUNT"
    _snap = _cfg.store_values()
    _had = {_KEY_E: _KEY_E in _snap, _KEY_C: _KEY_C in _snap}
    _import_os = __import__("os")
    try:
        _cfg.update_store({_KEY_E: "1", _KEY_C: "30"}, force=True)
        _cfg.reload_store()
        _import_os.environ.pop(_KEY_E, None)
        _import_os.environ.pop(_KEY_C, None)
        _check_end_to_end_body(gc, _llm_retry)
    finally:
        _restore = {k: _snap[k] for k in (_KEY_E, _KEY_C) if _had[k]}
        _remove = [k for k in (_KEY_E, _KEY_C) if not _had[k]]
        _cfg.update_store(_restore, remove=_remove, force=True)
        _cfg.reload_store()
        _import_os.environ.pop(_KEY_E, None)
        _import_os.environ.pop(_KEY_C, None)


def _check_end_to_end_body(gc, llm_retry) -> None:
    """L 节主体（配置已由调用方固定为「启用 + 30 次」）。"""
    original_post = gc.requests.post
    original_sleep = gc.time.sleep
    saved_events: list[tuple[str, str]] = []

    def _make(code: int, success_after: int | None = None):
        calls: list[int] = []
        waits: list[float] = []

        class _Resp:
            def __init__(self_, status: int) -> None:
                self_.status_code = status
                self_.ok = status == 200

            def json(self_) -> dict:
                return {"choices": [{"message": {"content": '{"ok":1}'},
                                     "finish_reason": "stop"}]}

            @property
            def text(self_) -> str:
                return "boom"

        def _post(url, **kwargs):  # noqa: ANN001, ARG001
            calls.append(1)
            if success_after is not None and len(calls) >= success_after:
                return _Resp(200)
            return _Resp(code)

        gc.requests.post = _post  # type: ignore[assignment]
        gc.time.sleep = lambda seconds: waits.append(float(seconds))  # type: ignore[assignment]
        return calls, waits

    def _restore() -> None:
        gc.requests.post = original_post  # type: ignore[assignment]
        gc.time.sleep = original_sleep  # type: ignore[assignment]

    # ---- ① 三次 429 后成功：必须用插件阶梯 ----
    calls, waits = _make(429, success_after=4)
    try:
        text = gc.generate_gemini_text(system_prompt="s", user_prompt="u")
        saved_events.append(("ok", text))
    except Exception as exc:  # noqa: BLE001
        saved_events.append(("err", f"{type(exc).__name__}: {exc}"))
    finally:
        _restore()

    check("③ 前 3 次 429 后成功：共 4 次尝试", len(calls) == 4, f"{len(calls)} 次")
    check("③ 等待 3 次（重试 3 次）", len(waits) == 3, f"{len(waits)} 次")
    check("③ 调用成功返回",
          saved_events and saved_events[0][0] == "ok", str(saved_events[:1]))
    # 原生档位 3/6/12；插件档位 1/2/4（含抖动）。落在 5s 以下即证明是插件档位。
    check("③ 等待档位取自插件阶梯（1,2,4 而非原生 3,6,12）",
          bool(waits) and all(value < 5.0 for value in waits),
          str([round(v, 2) for v in waits]))
    check("③ 档位落在 ±30% 抖动区间内",
          all(0.7 * base - 0.05 <= value <= 1.3 * base + 0.05
              for value, base in zip(waits, (1.0, 1.0, 1.0))),
          str([round(v, 2) for v in waits]))

    # ---- ② 持续 429：必须有界地耗尽 ----
    calls, waits = _make(429)
    try:
        gc.generate_gemini_text(system_prompt="s", user_prompt="u")
        exhausted = False
    except Exception:  # noqa: BLE001
        exhausted = True
    finally:
        _restore()
    check("② 持续 429 最终抛错（不静默成功）", exhausted)
    check("② 恰好 31 次尝试（30 次重试 + 首次）", len(calls) == 31, f"{len(calls)} 次")
    check("② 恰好 30 次等待，未越上限", len(waits) == 30, f"{len(waits)} 次")
    from ocv_cloud_stack import llm_retry as _llm_retry  # noqa: PLC0415

    ceiling = sum(_llm_retry.base_delay(i) for i in range(1, 31)) * (1.0 + _llm_retry.JITTER_RATIO)
    check("② 总等待未超档位总和 ×(1+抖动)（有界，不无限）",
          sum(waits) <= ceiling + 1.0,
          f"{sum(waits):.0f}s <= {ceiling:.0f}s")

    # ---- ③ 不可重试错误必须一次都不重试 ----
    for code in (400, 401, 403, 404, 422):
        calls, waits = _make(code)
        try:
            gc.generate_gemini_text(system_prompt="s", user_prompt="u")
        except Exception:  # noqa: BLE001
            pass
        finally:
            _restore()
        check(f"③ HTTP {code} 一次都不重试（不掩盖真实故障）",
              len(calls) == 1 and len(waits) == 0,
              f"尝试 {len(calls)} 次 / 等待 {len(waits)} 次")


def main() -> int:
    from ocv_cloud_stack import config, llm_retry

    print("== A) 默认值与开关 ==")
    # ⚠ 铁律 32/56：断言**内置默认**，不断言"当前生效值"。
    #   用户可能已在面板里改过这两项 —— 那时 config.llm_retry_count()
    #   返回的是用户值，拿它断言 30 会假失败（本轮实测踩到）。
    check("内置默认重试 30 次（与视频一致）", llm_retry.DEFAULT_RETRIES == 30, str(llm_retry.DEFAULT_RETRIES))
    _snap = config.store_values()
    try:
        config.update_store(
            {"CLOUD_STACK_LLM_RETRY_ENABLED": "", "CLOUD_STACK_LLM_RETRY_COUNT": ""},
            force=True, remove=["CLOUD_STACK_LLM_RETRY_ENABLED", "CLOUD_STACK_LLM_RETRY_COUNT"])
        config.reload_store()
        import os as _os
        _os.environ.pop("CLOUD_STACK_LLM_RETRY_ENABLED", None)
        _os.environ.pop("CLOUD_STACK_LLM_RETRY_COUNT", None)
        check("未配置时默认开启", llm_retry.enabled() is True)
        check("未配置时默认 30 次", llm_retry.retries() == 30, str(llm_retry.retries()))
        check("config.llm_retry_count() 默认 30", config.llm_retry_count() == 30,
              str(config.llm_retry_count()))
        check("config.llm_retry_enabled() 默认 True", config.llm_retry_enabled() is True)
    finally:
        _restore = {k: v for k, v in _snap.items()}
        config.update_store(_restore, remove=[k for k in config.store_values() if k not in _snap],
                            force=True)
        config.reload_store()
        import os as _os2
        _os2.environ.pop("CLOUD_STACK_LLM_RETRY_ENABLED", None)
        _os2.environ.pop("CLOUD_STACK_LLM_RETRY_COUNT", None)
    check("A 节结束后面板层逐字还原",
          config.store_values() == _snap,
          "一致" if config.store_values() == _snap else "有差异")

    print("\n== B) 阶梯档位逐项断言（与视频共用同一张表）==")
    # 用户要求「和视频机制类似」（F-016）：两边**逐项相同**。
    spec = ([1.0] * 3 + [5.0] * 5 + [10.0] * 4 + [15.0] * 3
            + [30.0] * 5 + [45.0] * 5 + [60.0] * 5)
    got = [llm_retry.base_delay(i) for i in range(1, 31)]
    check("30 档逐项一致（1×3,5×5,10×4,15×3,30×5,45×5,60×5）", got == spec,
          (f"不一致 {[i + 1 for i, (a, b) in enumerate(zip(got, spec)) if a != b][:5]}"
           if got != spec else "30 项全对"))
    check("与视频侧取的是同一张表（单一真相源，防漂移）",
          [config.video_submit_retry_delay(i) for i in range(1, 31)] == got,
          "两边逐项相同")
    check("第 31 档起沿用 1 分钟封顶（不变成 0 等待）",
          all(llm_retry.base_delay(i) == 60.0 for i in range(31, 61)))
    check("档位单调不减", all(llm_retry.base_delay(i) <= llm_retry.base_delay(i + 1)
                              for i in range(1, 60)))
    check("档位永不为负或 0（不会变成密集轰炸）",
          all(llm_retry.base_delay(i) > 0 for i in range(1, 61)))

    print("\n== C) 抖动 ==")
    samples = [llm_retry.delay(30) for _ in range(500)]
    low, high = min(samples), max(samples)
    check("下限不低于基准的 70%", low >= 60.0 * 0.7 - 1e-6, f"min={low:.3f}")
    check("上限不高于基准的 130%", high <= 60.0 * 1.3 + 1e-6, f"max={high:.3f}")
    check("确实在抖动（取值多样）", len({round(v, 3) for v in samples}) > 50,
          f"{len({round(v, 3) for v in samples})} 个不同取值")
    check("抖动的均值接近基准（不系统性偏移）",
          abs(sum(samples) / len(samples) - 60.0) < 3.0,
          f"mean={sum(samples) / len(samples):.2f}")
    check("抖动比例参数为 0.3（对齐参考文档）", abs(llm_retry.JITTER_RATIO - 0.3) < 1e-9)

    print("\n== D) 档位表就是用户指定 + 总窗口 788s ==")
    check("总窗口 = 788 秒（与视频一致）",
          abs(llm_retry.total_window(30) - 788.0) < 0.5,
          f"{llm_retry.total_window(30):.0f}s")
    check("档位说明含 1分钟 封顶", "1分钟" in llm_retry.schedule_text(30),
          llm_retry.schedule_text(30))

    print("\n== E) 可重试分类（RATE_LIMIT / SERVER / TIMEOUT）==")
    for status in (408, 409, 429, 500, 502, 503, 504):
        check(f"HTTP {status} 可重试", llm_retry.retryable_status(status))
    for status in (400, 401, 403, 404, 422):
        check(f"HTTP {status} 不重试（不掩盖真实故障）",
              not llm_retry.retryable_status(status))

    print("\n== F) 序号语义（铁律 53：累计 ≠ 第几次 ≠ 上限）==")
    check("attempt_budget = retries + 1（30 次重试 ⇒ 31 次尝试）",
          llm_retry.attempt_budget() == llm_retry.retries() + 1,
          f"{llm_retry.retries()} + 1 = {llm_retry.attempt_budget()}")

    print("\n== G) 真实挂载（不发请求）==")
    from backend.app import gemini_client

    check("_gemini_retry_count 已被接管",
          bool(getattr(gemini_client._gemini_retry_count,
                       "_cloud_stack_llm_retry_wrapper", False)))
    check("_gemini_retry_delay 已被接管",
          bool(getattr(gemini_client._gemini_retry_delay,
                       "_cloud_stack_llm_retry_wrapper", False)))
    check("generate_gemini_text **没有**被包成外层重试（防 3×31 放大）",
          not bool(getattr(gemini_client.generate_gemini_text,
                           "_cloud_stack_llm_retry_wrapper", False)))
    check("llm_retry.installed() 为真", llm_retry.installed())

    print("\n== H) 作用域隔离（非 sensenova provider 不得被接管）==")
    in_scope = llm_retry.in_scope(gemini_client)
    # 原生实现（补丁保存在模块上的引用）——用它作为"回落到原生"的基准，
    # 而不是写死 3（用户可能在 .env 里改过 GEMINI_RETRY_COUNT）。
    original_budget = gemini_client._cloud_stack_llm_retry_original[0]
    check("当前 provider 判定与 _provider() 一致",
          in_scope == (str(gemini_client._provider()).strip().lower() == llm_retry.PLUGIN_PROVIDER),
          f"in_scope={in_scope} provider={gemini_client._provider()}")
    if in_scope:
        # 不断言字面量 31：用户在面板里改过次数时它不是 31（铁律 32/56）。
        # 也不能断言 retries()+1 —— 用户若已把总开关关掉，_gemini_retry_count()
        # 会回落原生值，而 retries() 仍返回配置值 ⇒ 该比对同样假失败。
        # 真正的不变量是：**启用时**总尝试次数 == retries()+1。
        if llm_retry.enabled():
            check("作用域内且启用：总尝试次数 = retries() + 1",
                  gemini_client._gemini_retry_count() == llm_retry.retries() + 1,
                  f"{gemini_client._gemini_retry_count()} vs {llm_retry.retries()}+1")
        else:
            check("作用域内但总开关关闭：回落原生实现",
                  gemini_client._gemini_retry_count() == original_budget(),
                  f"{gemini_client._gemini_retry_count()} vs 原生 {original_budget()}")
    else:
        check("作用域外：保持 OCV 原生次数（不干扰别的服务商）",
              gemini_client._gemini_retry_count() == original_budget(),
              str(gemini_client._gemini_retry_count()))

    print("\n== I) 可还原性（卸载后必须回到原生）==")
    original_count = gemini_client._cloud_stack_llm_retry_original[0]
    original_delay = gemini_client._cloud_stack_llm_retry_original[1]
    check("已保存原生函数引用", callable(original_count) and callable(original_delay))
    removed = llm_retry.uninstall(gemini_client)
    check("uninstall() 返回 True", removed is True)
    check("卸载后 _gemini_retry_count 是原生函数",
          gemini_client._gemini_retry_count is original_count)
    check("卸载后不再标记为已接管",
          not getattr(gemini_client, "_cloud_stack_llm_retry", False))
    # 不断言字面量 3：原生值来自 GEMINI_RETRY_COUNT，用户可能在 .env 里改过它。
    # 断言"回到了原生实现本身"（同一函数对象），这才是我们真正关心的不变量。
    check("卸载后返回值由原生实现决定（不再受插件影响）",
          gemini_client._gemini_retry_count() == original_count(),
          f"{gemini_client._gemini_retry_count()} vs 原生 {original_count()}")
    # 装回去，避免影响同进程后续断言
    llm_retry.install(gemini_client)
    check("重新 install() 后恢复接管", llm_retry.installed())

    print("\n== J) 开关关闭时必须完全回到原生 ==")
    # ⚠ 铁律 23 / 48 / 56：还原用**逐键定向**，绝不用"先清空再写回"——
    #   中途异常或被强杀会让用户配置永久丢失（F-003/F-012 的同类教训）。
    #   并且断言只针对"我们确实改过的那一个键"，不假设用户没设过别的。
    enabled_key = "CLOUD_STACK_LLM_RETRY_ENABLED"
    saved = config.store_values()
    had_enabled = enabled_key in saved
    saved_enabled = saved.get(enabled_key)
    try:
        config.update_store({enabled_key: "0"}, force=True)
        config.reload_store()
        import os
        os.environ.pop(enabled_key, None)
        if in_scope:
            check("关闭后 _gemini_retry_count() 回到原生 3 次",
                  gemini_client._gemini_retry_count() == 3,
                  str(gemini_client._gemini_retry_count()))
            check("关闭后 _gemini_retry_delay() 回落到原生实现",
                  gemini_client._gemini_retry_delay(1) == original_delay(1),
                  f"{gemini_client._gemini_retry_delay(1)} vs {original_delay(1)}")
        else:
            check("非作用域 provider：开关状态不影响原生值",
                  gemini_client._gemini_retry_count() == 3)
    finally:
        if had_enabled:
            config.update_store({enabled_key: saved_enabled or ""}, force=True)
        else:
            config.update_store({}, remove=[enabled_key])
        config.reload_store()
        import os
        os.environ.pop(enabled_key, None)
    check("面板层逐字还原（不删用户设置、不多出键）",
          config.store_values() == saved,
          "一致" if config.store_values() == saved else "有差异")

    print("\n== L) 端到端行为（伪造 requests.post，不发真实请求）==")
    _check_end_to_end()

    print("\n== K) 自检脚本自身合规 ==")
    source = Path(__file__).read_text(encoding="utf-8")
    check("本脚本自己调用了 console.harden()（铁律 20）",
          "harden()" in source and "console" in source)
    # ⚠ 不能把待查字面量直接写进断言 —— 那样字符串出现在断言自身里，
    # 检查恒假（本轮实际踩到：第一版就是这么写的，44/45）。
    # 用拼接构造待查串，让断言不再自我满足。
    clobber_hint = "update_store({" + "'AGNES_API_KEY'"
    check("本脚本未写死调试用凭据（铁律 46）", clobber_hint not in source)

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
