# SPDX-License-Identifier: AGPL-3.0-only
"""真实凭据端到端验收（**需要已登录账号**，会发出真实网络请求）。

用法：
```bat
:: 用当前插件 state/ 里已导入的账号
runtime\\python\\python.exe plugins\\cloud_free_stack\\verify_jethub_live.py

:: 顺带把一份 DSH 导出的备份导入后再验（会写 state/，谨慎）
runtime\\python\\python.exe plugins\\cloud_free_stack\\verify_jethub_live.py --import-dsh <备份.json>
```

## 与 `verify_jethub.py` 的分工

| 脚本 | 是否需要账号 | 是否发真实请求 | 覆盖 |
|---|---|---|---|
| `verify_jethub.py` | 不需要 | 只打本机桥 | 结构、契约、路由、配置 |
| **本脚本** | **需要** | **是**（真的调上游） | 凭据可用性、模型清单、真实补全、积分、签到 |

`verify_jethub.py` 验的是「装好了没」，本脚本验的是「真的能用吗」。
两者的断言**不重复** —— 本脚本的每一条都依赖真实凭据。

⚠ 本脚本**会真的签到**（一次写请求）。若不想触发，加 `--skip-checkin`。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent
OCV_ROOT = PLUGIN_ROOT.parents[1]
if str(OCV_ROOT) not in sys.path:
    sys.path.insert(0, str(OCV_ROOT))

FAILURES: list[str] = []
PASSED = 0
SKIPPED = 0


def check(label: str, ok: bool, detail: str = "") -> bool:
    global PASSED
    mark = "OK  " if ok else "FAIL"
    line = f"  [{mark}] {label}"
    if detail and not ok:
        line += f"\n         {detail}"
    print(line, flush=True)
    if ok:
        PASSED += 1
    else:
        FAILURES.append(label)
    return ok


def skip(label: str, why: str) -> None:
    global SKIPPED
    print(f"  [SKIP] {label} —— {why}", flush=True)
    SKIPPED += 1


def main() -> int:
    parser = argparse.ArgumentParser(description="云端 OAuth 真实凭据验收")
    parser.add_argument("--import-dsh", metavar="PATH",
                        help="先把一份 DSH 导出的备份导入 state/，再验收")
    parser.add_argument("--skip-checkin", action="store_true",
                        help="不执行真实签到（只查余额）")
    parser.add_argument("--model", default="", help="指定用于补全测试的 provider::model")
    args = parser.parse_args()

    try:
        from plugins.cloud_free_stack.ocv_cloud_stack import console

        console.harden()
    except Exception:  # noqa: BLE001
        pass

    print("=" * 68)
    print("云端 OAuth 真实凭据验收")
    print("=" * 68)

    from plugins.cloud_free_stack.ocv_cloud_stack import config, jethub_bridge

    # ------------------------------------------------------------------
    print("\n1) 桥与状态")
    # ------------------------------------------------------------------
    error = jethub_bridge.ensure_running()
    check("桥已就绪", error is None, str(error))
    if not jethub_bridge.is_listening():
        print("\n桥起不来，后续无法验证。")
        return 1

    status = jethub_bridge.call("/rpc/status", {}, timeout=10.0)
    value = (status.get("data") or {}).get("value") or {}
    print(f"         提供商 {len(value.get('providers') or [])} 家，"
          f"跳过 {len(value.get('skipped') or [])} 家")

    # ------------------------------------------------------------------
    print("\n2) 导入 DSH 备份（可选）")
    # ------------------------------------------------------------------
    if args.import_dsh:
        src = Path(args.import_dsh)
        if not src.is_file():
            check("DSH 备份文件存在", False, str(src))
        else:
            doc = json.loads(src.read_text(encoding="utf-8"))
            check("源文件是 DSH 格式", doc.get("format") == "dsh-codearts-auth/backup",
                  f"实际 format={doc.get('format')!r}")
            result = jethub_bridge.call(
                "/rpc/backup.import",
                {"document": doc, "mode": "merge", "file_name": src.name},
                timeout=90.0,
            )
            if check("导入成功（不再报 schema 不匹配）", result["ok"],
                     str(result.get("error"))[:200]):
                imported = (result["data"] or {}).get("value") or {}
                print(f"         source={imported.get('source')} "
                      f"账号={imported.get('accountCount')} "
                      f"凭据={imported.get('credentialsCount')} "
                      f"缺凭据={imported.get('missingCredentials')} "
                      f"过期={imported.get('expiredAccounts')}")
                check("被识别为 DSH 来源", imported.get("source") == "dsh",
                      str(imported.get("source")))
                check("账号与凭据都写入了",
                      int(imported.get("accountCount") or 0) > 0
                      and int(imported.get("credentialsCount") or 0) > 0,
                      str(imported))
    else:
        skip("导入 DSH 备份", "未传 --import-dsh")

    # ------------------------------------------------------------------
    print("\n3) 各提供商的账号与模型（真实凭据）")
    # ------------------------------------------------------------------
    accounts = jethub_bridge.call("/rpc/accounts.list", {"provider": "all"}, timeout=30.0)
    providers = value.get("providers") or []
    with_accounts: list[str] = []
    total_models = 0
    for provider in providers:
        pid = provider.get("id")
        if not pid:
            continue
        acc = jethub_bridge.call("/rpc/accounts.list", {"provider": pid}, timeout=20.0)
        rows = ((acc.get("data") or {}).get("value") or {}).get("accounts") or []
        if not rows:
            continue
        with_accounts.append(pid)
        mod = jethub_bridge.call("/rpc/models.list", {"provider": pid}, timeout=30.0)
        models = ((mod.get("data") or {}).get("value") or {}).get("models") or []
        usable = [m for m in models if not m.get("disabled")]
        total_models += len(usable)
        print(f"         {pid:<12} 账号={len(rows):<2} 模型={len(usable)}"
              f"  例：{', '.join(str(r.get('nickname') or r.get('id')) for r in rows[:3])}")

    check("至少有一个提供商有真实账号", len(with_accounts) > 0,
          "state/ 里没有账号 —— 先在面板登录，或用 --import-dsh 导入")
    if not with_accounts:
        print("\n没有账号，无法继续真实验收。")
        return 1
    check("从真实账号拿到了模型清单", total_models > 0, f"合计 {total_models} 个")

    # ------------------------------------------------------------------
    print("\n4) 真实对话补全")
    # ------------------------------------------------------------------
    target = args.model
    if not target:
        for pid in with_accounts:
            mod = jethub_bridge.call("/rpc/models.list", {"provider": pid}, timeout=30.0)
            models = ((mod.get("data") or {}).get("value") or {}).get("models") or []
            usable = [m for m in models if not m.get("disabled")]
            if usable:
                target = f"{pid}::{usable[0]['id']}"
                break
    if target:
        print(f"         选用模型：{target}")
        result = jethub_bridge.call(
            "/v1/chat/completions",
            {
                "model": target,
                "messages": [{"role": "user", "content": "只回复两个字：可用"}],
                # 给足预算：推理模型会先花掉一部分在 reasoning 上，
                # 预算太小会 finish_reason=length 且正文为空（这是**预期**行为，
                # 不是缺陷 —— 见 docs/JETHUB.md F-007）
                "max_tokens": 1024,
                "temperature": 0,
            },
            timeout=120.0,
        )
        if check("补全调用成功", result["ok"], str(result.get("error"))[:220]):
            body = result["data"] or {}
            choice = (body.get("choices") or [{}])[0]
            content = str((choice.get("message") or {}).get("content") or "").strip()
            finish = choice.get("finish_reason")
            check("拿到了非空正文", len(content) > 0, f"content={content!r} finish={finish!r}")
            check("finish_reason 是**字符串**而不是 '[object Object]'",
                  isinstance(finish, str) and "[object" not in finish,
                  f"实际 {finish!r} —— 这是 F-007 的回归点")
            print(f"         回复：{content[:60]!r}  finish={finish!r}  usage={body.get('usage')}")
    else:
        skip("真实补全", "没有可用模型")

    # ------------------------------------------------------------------
    print("\n5) 真实积分查询")
    # ------------------------------------------------------------------
    caps = jethub_bridge.call("/rpc/credits.capabilities", {}, timeout=10.0)
    cap_rows = ((caps.get("data") or {}).get("value") or {}).get("capabilities") or []
    balance_capable = [r["id"] for r in cap_rows if r.get("balance") and r["id"] in with_accounts]
    check("有支持查余额的提供商", len(balance_capable) > 0, str(balance_capable))
    ok_balances = 0
    for pid in balance_capable:
        res = jethub_bridge.call("/rpc/credits.balances", {"provider": pid}, timeout=60.0)
        rows = ((res.get("data") or {}).get("value") or {}).get("accounts") or []
        good = [r for r in rows if r.get("status") == "ok"]
        ok_balances += len(good)
        label = ", ".join(str((r.get("balance") or {}).get("label")) for r in good[:3])
        print(f"         {pid:<12} 成功={len(good)}/{len(rows)}  余额：{label[:60]}")
    check("至少查到一份真实余额", ok_balances > 0,
          "若全失败，检查凭据是否过期；`credits.balances` 的 product 参数缺失会静默失败")

    # ------------------------------------------------------------------
    print("\n6) 真实一键签到")
    # ------------------------------------------------------------------
    if args.skip_checkin:
        skip("一键签到", "传了 --skip-checkin")
    else:
        res = jethub_bridge.call("/rpc/credits.claimAll", {}, timeout=300.0)
        if check("签到调用成功", res["ok"], str(res.get("error"))[:220]):
            val = (res.get("data") or {}).get("value") or {}
            print(f"         {val.get('summary')}")
            print(f"         claimed={val.get('totalClaimed')} "
                  f"already={val.get('totalAlreadyClaimed')} "
                  f"inactive={val.get('totalInactive')} "
                  f"failed={val.get('totalFailed')} "
                  f"skipped={val.get('totalSkipped')}")
            # 「今日已领」与「暂无资格」都是正常状态，不该出现在 failure 里
            check("『今日已领』不计入失败",
                  int(val.get("totalFailed") or 0) == 0
                  or int(val.get("totalAlreadyClaimed") or 0) == 0,
                  f"already={val.get('totalAlreadyClaimed')} failed={val.get('totalFailed')}")
            for row in (val.get("results") or []):
                if row.get("failed"):
                    for msg in (row.get("messages") or [])[:3]:
                        print(f"         ! {row.get('provider')}: {str(msg)[:110]}")

    # ------------------------------------------------------------------
    print("\n" + "=" * 68)
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项 / 通过 {PASSED} 项 / 跳过 {SKIPPED} 项")
        for item in FAILURES:
            print(f"  - {item}")
        print("=" * 68)
        return 1
    print(f"全部通过（{PASSED} 项，跳过 {SKIPPED} 项）")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
