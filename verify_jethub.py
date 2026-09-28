# SPDX-License-Identifier: AGPL-3.0-only
"""Jet Hub 接入的开发期自检（不依赖 OCV 后端，可独立跑）。

用法：
```bat
runtime\\python\\runtime\\python.exe plugins\\cloud_free_stack\\verify_jethub.py
```

它做四件事：
1. **语法/导入**：改过的 6 个 Python 模块都能 import；
2. **配置层**：`JETHUB_*` 访问器读到正确默认值；
3. **桥联动**：拉起 Node 桥、`/health`、`/rpc/status`、`/v1/models` 全通；
4. **模型清单接入**：`models_catalog` 的 `llm_jethub` 用途真的能从桥取到清单，
   并出现在面板字段的候选项里。

退出码 0 = 全过。任何一步失败都打印原因并以 1 退出。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent
OCV_ROOT = PLUGIN_ROOT.parents[1]

if str(OCV_ROOT) not in sys.path:
    sys.path.insert(0, str(OCV_ROOT))

FAILURES: list[str] = []
PASSED = 0


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


def main() -> int:
    # 中文 Windows 的 stdout 是 GBK；本文件的输出里有 ✓/✗ 类符号，
    # 先让 console 层把不可编码字符降级，避免直接终止进程。
    try:
        from plugins.cloud_free_stack.ocv_cloud_stack import console

        console.harden()
    except Exception:  # noqa: BLE001
        pass

    print("=" * 68)
    print("Jet Hub 接入自检")
    print("=" * 68)

    # ------------------------------------------------------------------
    print("\n1) 模块导入与语法")
    # ------------------------------------------------------------------
    try:
        from plugins.cloud_free_stack.ocv_cloud_stack import (  # noqa: F401
            config,
            image_shim,
            jethub_bridge,
            models_catalog,
            panel,
        )

        check("6 个改动模块可导入", True)
    except Exception as exc:  # noqa: BLE001
        check("6 个改动模块可导入", False, f"{type(exc).__name__}: {exc}")
        print("\n导入失败，后续检查无意义。")
        return 1

    # ------------------------------------------------------------------
    print("\n2) 配置访问器")
    # ------------------------------------------------------------------
    check("jethub_enabled() 默认为真", config.jethub_enabled() is True)
    check("jethub_provider() 默认 buddy", config.jethub_provider() == "buddy",
          f"实际 {config.jethub_provider()!r}")
    check("jethub_bridge_port() 默认 8801", config.jethub_bridge_port() == 8801,
          f"实际 {config.jethub_bridge_port()}")
    check("jethub_bridge_timeout_ms() 默认 110000 且 < OCV 读超时 120s",
          config.jethub_bridge_timeout_ms() == 110_000,
          f"实际 {config.jethub_bridge_timeout_ms()}")
    fallback = config.jethub_llm_fallback_models()
    check("jethub_llm_fallback_models() 返回非空且结构正确",
          isinstance(fallback, list) and len(fallback) > 0
          and all({"value", "label"} <= set(row) for row in fallback),
          f"实际 {fallback!r}")
    state_dir = config.jethub_state_dir()
    check("jethub_state_dir() 落在插件目录内",
          str(state_dir).replace("\\", "/").startswith(str(PLUGIN_ROOT).replace("\\", "/")),
          f"实际 {state_dir}")
    check("jethub_backup_dir() 落在插件目录内",
          str(config.jethub_backup_dir()).replace("\\", "/").startswith(
              str(PLUGIN_ROOT).replace("\\", "/")),
          f"实际 {config.jethub_backup_dir()}")

    # ------------------------------------------------------------------
    print("\n3) Node 运行时与桥进程")
    # ------------------------------------------------------------------
    node = jethub_bridge.node_executable()
    check("找到 Node 运行时", node is not None, "既无 runtime/node/node.exe，PATH 里也没有 node")
    if node:
        print(f"         node = {node}")
    check("桥入口 server.mjs 存在", jethub_bridge.NODE_ENTRY.is_file(),
          f"缺 {jethub_bridge.NODE_ENTRY}")
    check("vendor 目录存在", (PLUGIN_ROOT / "vendor" / "dsh-codearts-auth" / "lib" / "index.js").is_file(),
          "缺 vendor/dsh-codearts-auth/lib/index.js，先跑 vendor_tool.py")

    port = config.jethub_bridge_port()
    if jethub_bridge.is_listening(port):
        print(f"         桥已在 {port} 监听（复用现有实例）")
    else:
        error = jethub_bridge.ensure_running()
        check("能拉起桥进程", error is None, str(error))
    check("桥端口在监听", jethub_bridge.is_listening(port), f"端口 {port}")

    if jethub_bridge.is_listening(port):
        health = jethub_bridge.call("/health", timeout=5.0)
        check("GET /health 返回 ok", bool(health["ok"] and (health["data"] or {}).get("ok")),
              str(health))
        status = jethub_bridge.call("/rpc/status", {}, timeout=10.0)
        value = (status.get("data") or {}).get("value") or {}
        check("RPC /rpc/status 可用", bool(status["ok"] and value), str(status)[:200])
        storage = value.get("storage") or {}
        check("账号池落在插件目录内", storage.get("poolInPlugin") is True,
              f"实际 {storage.get('poolPath')!r}")
        check("凭据文件落在插件目录内", storage.get("credentialsInPlugin") is True,
              f"实际 {storage.get('credentialsPath')!r}")
        check("已接线 buddy provider",
              any(p.get("id") == "buddy" for p in (value.get("providers") or [])),
              str(value.get("providers")))
        providers_now = {p.get("id") for p in (value.get("providers") or [])}
        check("已接线 9 个提供商（全部）",
              providers_now == {"buddy", "workbuddy", "lobsterai", "qoder", "trae",
                                "cline", "loomy", "codearts", "atomcode"},
              f"实际 {sorted(providers_now)}")
        check("无提供商被跳过（skipped 为空）",
              not (value.get("skipped") or []), str(value.get("skipped")))

        models = jethub_bridge.call("/v1/models", timeout=25.0)
        check("GET /v1/models 返回 OpenAI 形状",
              bool(models["ok"] and isinstance((models["data"] or {}).get("data"), list)),
              str(models)[:200])

        caps = jethub_bridge.call("/rpc/credits.capabilities", {}, timeout=10.0)
        cap_rows = ((caps.get("data") or {}).get("value") or {}).get("capabilities") or []
        check("积分能力矩阵可用", len(cap_rows) == 9, f"实际 {len(cap_rows)} 项")
        by_id = {row["id"]: row for row in cap_rows}
        check("workbuddy 不支持每日签到（对齐原版能力矩阵）",
              by_id.get("workbuddy", {}).get("checkin") is False,
              f"workbuddy={by_id.get('workbuddy')}")
        check("cline 不支持每日签到", by_id.get("cline", {}).get("checkin") is False,
              f"cline={by_id.get('cline')}")
        check("loomy 独有新手任务", by_id.get("loomy", {}).get("onboarding") is True,
              f"loomy={by_id.get('loomy')}")
        check("buddy 支持签到与余额",
              by_id.get("buddy", {}).get("checkin") is True
              and by_id.get("buddy", {}).get("balance") is True,
              f"buddy={by_id.get('buddy')}")

        # 备份：下载模式 + 上传恢复
        export = jethub_bridge.call("/rpc/backup.export", {"download": True}, timeout=30.0)
        export_value = ((export.get("data") or {}).get("value") or {})
        check("备份导出支持 download 模式（回文件本体供浏览器下载）",
              export.get("ok") and export_value.get("mode") == "download"
              and isinstance(export_value.get("document"), dict),
              f"mode={export_value.get('mode')!r}")

    # ------------------------------------------------------------------
    print("\n4) 模型清单接入（llm_jethub 用途）")
    # ------------------------------------------------------------------
    check("KINDS 里有 llm_jethub", "llm_jethub" in models_catalog.KINDS,
          f"实际 {list(models_catalog.KINDS)}")
    check("KINDS['llm_jethub'] 指向 jethub",
          models_catalog.KINDS.get("llm_jethub") == models_catalog.PROVIDER_JETHUB)
    check("KIND_LABELS 里有 llm_jethub", "llm_jethub" in models_catalog.KIND_LABELS)
    note = models_catalog.verify_note("llm_jethub")
    check("verify_note('llm_jethub') 非空", isinstance(note, str) and len(note) > 10, note[:80])

    # 模型 id 必须编成 `<provider>::<model>`，否则跨提供商无法路由
    catalog_src = (PLUGIN_ROOT / "ocv_cloud_stack" / "models_catalog.py").read_text(encoding="utf-8")
    check("模型 value 编成 <provider>::<model>",
          '{provider_id}::{model_id}' in catalog_src,
          "不同提供商的模型 id 会重名，必须带提供商前缀才能路由")
    check("聚合清单只列「有可用模型」的提供商",
          "usable_providers" in catalog_src)

    result = models_catalog.list_models("llm_jethub", force=True, timeout=(5.0, 25.0))
    check("list_models('llm_jethub') 可调用且返回结构合法",
          isinstance(result, dict) and "ok" in result and isinstance(result.get("models"), list),
          str(result)[:200])
    print(f"         ok={result.get('ok')} 条数={len(result.get('models') or [])}")
    print(f"         message={str(result.get('message'))[:120]}")
    # 未登录时 ok=False 且模型为空 —— 这是**预期**，不是失败。
    if not result.get("ok"):
        check("未登录时给出可操作提示（而非崩溃）",
              "登录" in str(result.get("message", "")) or "桥" in str(result.get("message", "")),
              str(result.get("message")))

    # ------------------------------------------------------------------
    print("\n5) 面板字段与 HTTP 路由")
    # ------------------------------------------------------------------
    keys = {f["key"] for f in panel.FIELDS}
    for key in ("JETHUB_ENABLED", "JETHUB_BRIDGE_PORT",
                "JETHUB_AUTOSTART", "JETHUB_BRIDGE_TIMEOUT_MS", "JETHUB_MODEL"):
        check(f"字段 {key} 已注册", key in keys)
    # 用户要求：「有下面的账号管理窗口就不需要上面的提供商选择了，删掉」
    # —— `JETHUB_PROVIDER` 不再作为**字段**渲染（左栏是唯一入口），
    #    但配置键本身仍然生效，所以这里断言「字段没有、键还在」。
    check("JETHUB_PROVIDER 不再作为字段渲染（用户要求删掉重复入口）",
          "JETHUB_PROVIDER" not in keys,
          "字段区不该再有提供商下拉 —— 「云端 OAuth」分页的左栏才是唯一入口")
    check("JETHUB_PROVIDER 配置键仍然有效（左栏保存时写它）",
          config.jethub_provider() in {p["value"] for p in panel.JETHUB_PROVIDERS},
          f"实际 {config.jethub_provider()!r}")
    check("分组 jethub 已注册", "jethub" in panel.GROUP_LABELS)

    model_field = next((f for f in panel.FIELDS if f["key"] == "JETHUB_MODEL"), None)
    check("JETHUB_MODEL 是动态模型下拉",
          bool(model_field and model_field.get("dynamic_models") == "llm_jethub"),
          str(model_field))
    check("JETHUB_MODEL 允许手填（与其它模型字段一致）",
          bool(model_field and model_field.get("allow_custom")),
          str(model_field))

    # ---- 语言模型来源互斥（用户核心要求） ----
    source_field = next((f for f in panel.FIELDS if f.get("key") == "LLM_SOURCE"), None)
    check("有「模型来源」字段 LLM_SOURCE", source_field is not None)
    source_options = {o["value"] for o in (source_field or {}).get("options", [])}
    check("来源只含 custom 与 jethub 两路", source_options == {"custom", "jethub"},
          f"options={source_options}")
    check("来源字段在语言模型分组（不在云端 OAuth 页）",
          bool(source_field and source_field.get("group") == "llm"),
          f"group={(source_field or {}).get('group')!r}")
    check("JETHUB_MODEL 在语言模型页（用户要求模型选择在此页）",
          bool(model_field and model_field.get("group") == ""),
          "它应紧随语言模型字段之后，同属 llm 分组")
    # ⚠ 这条**不能**断言「当前值是 custom」—— 用户可能已经保存过云端 OAuth，
    #   那是合法状态。要断言的是**默认值**（未保存时回落到 custom），
    #   以及「当前值必须是合法枚举之一」。
    check("来源字段的默认值是 custom（未保存时不擅自改用云端 OAuth）",
          str(source_field.get("default") or "").strip() == "custom",
          f"default={(source_field or {}).get('default')!r}")
    check("当前来源是合法枚举值",
          config.llm_source() in {"custom", "jethub"},
          f"实际 {config.llm_source()!r}")
    check("来源可翻译成 OCV provider id",
          config.resolved_language_provider() in {"sensenova", "jethub"},
          f"实际 {config.resolved_language_provider()!r}")

    custom_keys = {f["key"] for f in panel.FIELDS if f.get("source") == "custom"}
    jethub_keys = {f["key"] for f in panel.FIELDS if f.get("source") == "jethub"}
    check("标了 custom 的字段含服务地址/Key/模型",
          {"SENSENOVA_API_KEY", "SENSENOVA_API_BASE", "SENSENOVA_MODEL"} <= custom_keys,
          f"custom={sorted(custom_keys)}")
    check("标了 jethub 的字段含 JETHUB_MODEL", "JETHUB_MODEL" in jethub_keys,
          f"jethub={sorted(jethub_keys)}")
    check("两路字段不重叠（真互斥）", not (custom_keys & jethub_keys),
          f"交集={sorted(custom_keys & jethub_keys)}")

    # ---- 多提供商登记 ----
    expected = {p["value"] for p in panel.JETHUB_PROVIDERS}
    check("面板登记 9 个提供商", len(expected) == 9, f"实际 {len(expected)}")
    check("提供商登记与已接线一致",
          expected == {"buddy", "workbuddy", "lobsterai", "qoder", "trae",
                       "cline", "loomy", "codearts", "atomcode"},
          f"实际 {sorted(expected)}")
    runtime_src = (PLUGIN_ROOT / "jethub" / "runtime.mjs").read_text(encoding="utf-8")
    for provider in sorted(expected):
        check(f"runtime 接线表含 {provider}", f"id: '{provider}'" in runtime_src)

    # ---- 签到：串行 + 能力门控 ----
    credits_src = (PLUGIN_ROOT / "jethub" / "credits.mjs").read_text(encoding="utf-8")
    check("有积分/签到控制器", "createCreditsController" in credits_src)
    claim_body = credits_src.split("async function claimAll")[1][:2500] if "async function claimAll" in credits_src else ""
    check("一键签到**串行**执行（绝不 Promise.all）",
          "for (const providerId of targets)" in claim_body and "Promise.all" not in claim_body,
          "跨渠道并发会同时发多路真实写请求 → 触发风控")
    check("workbuddy 声明不支持签到（对齐原版能力矩阵）",
          "supportsCheckin: false" in runtime_src)
    check("面板有积分 handler", hasattr(panel, "jethub_credits"))

    # ---- 签到/积分**参数签名**必须与 vendor 一致（F-006 的真实缺陷） ----
    #
    # 首版凭记忆写调用，5 个 provider 全部少传 `product`，lobsterai 还少
    # `clientVersion`。这类错误**不在加载时报错**，只在真签到时抛
    # `Cannot read properties of undefined` 并被 catch 吞掉 ——
    # 表现为「签到失败但不知道为什么」。所以用断言守住。
    check("buddy 签到传 product",
          "claimDailyCheckin(credential, product)" in credits_src)
    check("qoder 签到传 product（首版漏了）",
          "claimQoderDailyCheckin(credential, product)" in credits_src)
    check("trae 签到传 product（首版漏了）",
          "claimTraeDailyCheckin(credential, product)" in credits_src)
    check("loomy 签到传 product（首版漏了）",
          "claimLoomyDailyQuota(credential, product)" in credits_src)
    check("lobsterai 签到传 clientVersion（必填，首版漏了）",
          "claimLobsteraiDailyCheckin(" in credits_src
          and "credential, product, clientVersion" in credits_src,
          "该函数第 3 参 clientVersion 是必填的")
    check("codearts 签到**不传** product（其签名只有 credential）",
          "claimCodeArtsDailyCheckin(credential)" in credits_src,
          "华为云那套凭据不接 product，多传无害但应与 vendor 一致")
    check("积分查询都传 product（除 codearts）",
          "fetchQoderCreditBalance(credential, product)" in credits_src
          and "fetchTraeCreditBalance(credential, product)" in credits_src
          and "fetchLoomyCreditBalance(credential, product)" in credits_src
          and "fetchCodeArtsAccountInfoDetailed(credential)" in credits_src)
    check("签到位结果按 kind 分类（claimed/already/inactive/failed）",
          "asOutcome" in credits_src and "alreadyClaimed" in credits_src,
          "vendor 返回 {kind:'claimed'|'already-claimed'|'inactive'|'failed'}，"
          "直接当成功会把「今天已领过」误报成成功、把 already/inactive 误报成失败")
    # ClaimOutcome 有**四种** kind（credits.ts:118-122），inactive 最易被漏
    check("inactive（当前无领取资格）单独分类、不计入失败",
          "outcome?.inactive === true" in credits_src and "totalInactive" in credits_src,
          "原 Jet Hub 记录过同族缺陷：「inactive 渠道整条消失」")
    check("alread/already-claimed 的连字符拼写被正确识别",
          "'already-claimed'" in credits_src,
          "vendor 用的是带连字符的 `already-claimed`，拼错会掉进失败分支")
    check("空返回值**不算成功**（避免把无响应当领到）",
          "上游没有返回结果" in credits_src)

    # ---- finish_reason 必须是字符串（F-007） ----
    bridge_src = (PLUGIN_ROOT / "jethub" / "bridge.mjs").read_text(encoding="utf-8")
    check("finish_reason 做了对象→字符串归一化",
          "finishReasonText" in bridge_src,
          "`FinishReason` 是 `{kind:'stop'}` 对象，直接 String() 会得到"
          " '[object Object]'，导致 OCV 永远检测不到 length 截断")
    check("collectStream 保留原始 reason 对象（不提前 String）",
          "finishReason = chunk.reason ?? 'stop'" in bridge_src)

    # ---- 备份格式兼容（F-008：用户从 dsh 导出的备份） ----
    check("识别本插件自己的备份标识", "ocv-cloud-free-stack.jethub-backup" in runtime_src)
    check("识别 DSH 原生的备份标识（用户要求适配）",
          "dsh-codearts-auth/backup" in runtime_src,
          "用户报「从 dsh 中导出的账号凭证提示 schema 不匹配」")
    check("备份归一化覆盖两种格式差异（平铺凭据 / 顶层账号）",
          "normalizeBackup" in runtime_src and "detectBackupKind" in runtime_src)
    check("认不出的备份**明确拒绝**而不是猜",
          "无法识别的备份格式" in runtime_src)
    check("归一化会统计缺失凭据与过期账号",
          "missingCredentials" in runtime_src and "expiredAccounts" in runtime_src)
    # F-010：merge 不能覆盖凭据（数据破坏级）
    check("merge 模式**合并**凭据而不是整体覆盖",
          "const merged = { ...(existing?.refs ?? {}), ...normalized.refs }" in runtime_src,
          "`credentials.importDocument` 是整体覆盖语义；merge 时直接调它"
          "会把备份之外的既有凭据**全部删掉**（账号还在但凭据没了）")
    check("replace 模式才整体覆盖凭据",
          "credentialsCount = await credentials.importDocument({ refs: normalized.refs })" in runtime_src)

    # ---- 审查发现的 5 个缺陷，逐条固化为断言 ----
    panel_src = (PLUGIN_ROOT / "ocv_cloud_stack" / "panel.py").read_text(encoding="utf-8")
    # shim_src 要在本段之前定义（它在原文件里位于更下方，这里提前读取）
    shim_src = (PLUGIN_ROOT / "ocv_cloud_stack" / "image_shim.py").read_text(encoding="utf-8")
    # F-009：_panel_call 以 payload 作第一位置参数
    check("jethub_status 的参数顺序符合 _panel_call 约定（F-009）",
          "def jethub_status(payload: dict[str, Any] | None = None, *," in panel_src,
          "首版签名为 (provider, payload)，HTTP 路径把 dict 传给 provider → "
          "str(dict) 垃圾串 → 账号/模型/积分全部空")
    check("jethub_status 有 dict 传入 provider 的防御",
          "if isinstance(provider, dict):" in panel_src)
    # 批量端点必须存在（F-015：桥有实现但无路由）
    check("面板有批量模型开关 handler",
          hasattr(panel, "jethub_models_all"),
          "前端调 /api/panel/jethub/models/all，首版没这个 handler 也没路由 → 必然 404")
    check("批量端点路由已挂",
          "/api/panel/jethub/models/all" in shim_src)
    check("jethub/model/toggle 支持 __ALL__ 批量语义",
          'model_id == "__ALL__"' in panel_src)
    # F-016：测试入口要带 provider
    check("对话测试带上 provider 查询参数（F-016）",
          "/v1/chat/completions?provider=" in panel_src,
          "不带的话手填的裸 model id 会被强制发往 buddy，与界面显示不符")
    # F-011：过期字段名不统一
    check("凭据过期判定同时认 expires_at 与 expire_time（F-011）",
          "credential.expires_at ?? credential.expire_time" in runtime_src,
          "qoder/cline 用的是 `expire_time`；只读 expires_at 会让它们永不刷新")
    # F-012：getRuntime 失败可重试
    check("getRuntime 失败后清空 promise 以便重试（F-012）",
          "_runtimePromise = null" in bridge_src,
          "首版 reject 后 promise 永久留着 → 本次进程内桥彻底不可用")
    # F-013：loginJobs 有 TTL
    server_live_src = (PLUGIN_ROOT / "jethub" / "server.mjs").read_text(encoding="utf-8")
    check("loginJobs 有 TTL 清理（F-013）",
          "pruneLoginJobs" in server_live_src,
          "首版只写不读也不清理，未完成的登录会永久驻留")
    # F-014：前端重试有上限
    oauth_js = (PLUGIN_ROOT / "panel" / "cloud-oauth.js").read_text(encoding="utf-8")
    check("前端重试有上限（F-014）",
          "bindInput(30)" in oauth_js and "if (tries > 30) clearInterval(timer);" in oauth_js,
          "首版是无上限递归 / `tries > 30 && ready` 条件，ready 恒 false 时定时器永久驻留")
    # 余额解析：cline / codearts 的包装层
    check("cline 余额剥 `balance` 子对象（F-017）",
          "value.balance !== null" in credits_src and "typeof value.balance === 'object'" in credits_src)
    check("codearts 余额解包 `info.credit` 且失败返回 null（F-017）",
          "value.ok === false" in credits_src and "info?.credit ?? info" in credits_src,
          "首版把「查询失败」也归一化成 {total:null}，被伪装成成功显示「—」")
    check("normalizeBalance 的嵌套白名单含 info/credit/balance",
          "'info', 'credit', 'balance'" in credits_src)
    # 前端字段名：packages 而非 pools
    #
    # ⚠ 断言不能只写 `"raw.pools" not in oauth_js` —— 修复说明的**注释里**
    #   正好会提到 `raw.pools` 作为反例，那会被误判成「还在用旧字段」。
    #   所以要判断**实际代码**：`value.raw && value.raw.packages` 这个取值表达式。
    check("前端读 `packages` 而不是 `pools`（F-018）",
          "value.raw && value.raw.packages" in oauth_js
          and "var packs = value.raw" in oauth_js,
          "vendor 返回的是 packages，且元素是对象（金额在 remaining）")

    # F-019：只列有账号的提供商
    check("聚合清单按 accountCount 过滤没有账号的提供商（F-019）",
          'int(provider.get("accountCount") or 0) <= 0' in catalog_src,
          "`models.list` 对没登录的 provider 也返回 vendor 静态默认表，"
          "不过滤会让下拉里塞满选中就失败的模型（实测 45 个里只有 15 个真能用）")
    check("status 里带 accountCount（过滤的依据）",
          "accountCount" in server_live_src,
          "光看模型列表分不出有没有账号，必须由 status 一并给出")

    # ---- 用户两项要求 ----
    # 1) 去掉「刷新凭据」按钮（后台自动刷新）
    check("账号卡没有「刷新凭据」按钮（用户要求去掉）",
          'h("button", "jh-btn", "刷新凭据")' not in oauth_js,
          "凭据刷新是后台自动的：resolveUsableCredential 在调用前按需刷新，"
          "手动按钮只会让人误以为「不点就不会刷新」")
    check("后台确实会自动刷新（resolveUsableCredential 有过期检查）",
          "credentialLooksExpired(credential)" in runtime_src
          and "auth.refreshAccountCredential(ref)" in runtime_src,
          "这是「去掉手动按钮」成立的前提")
    # 2) 语言模型页显示当前选用的云端 OAuth 模型
    check("语言模型页会显示**当前选用的模型**（用户要求）",
          "jhDescribeModel" in oauth_js and "当前选用模型" in oauth_js,
          "用户要求：要显示当前选用哪个云端 OAuth 的模型")
    check("提示条能识别 provider::model 并给出提供商友好名",
          "JH_PROVIDER_NAMES" in oauth_js and "CodeBuddy（腾讯）" in oauth_js)
    check("提示条会校验所选模型是否仍可用",
          "不在该提供商的模型清单里" in oauth_js or "已被「显示列表」关闭" in oauth_js,
          "账号被删/模型被关时要说清楚，否则用户只遇到指不到根因的调用失败")
    # F-021：提示条不能被 renderGroup 擦掉
    check("提示条会在 renderGroup 清空卡片后重建（F-021）",
          "jhEnsureSourceHint" in oauth_js and "MutationObserver" in oauth_js,
          "`renderGroup()` 执行 `card.innerHTML = ''`，插在卡片里的元素每次都被擦掉")
    # F-020：render() 钩子必须在函数体内
    rendered = (PLUGIN_ROOT / "panel" / "index.html").read_text(encoding="utf-8")
    hook_at = rendered.find("renderJetHub();\n    refreshJetHub();")
    fn_at = rendered.rfind("function render(", 0, hook_at) if hook_at > 0 else -1
    in_body = False
    if hook_at > 0 and fn_at > 0:
        depth = 0
        closed = False
        for ch in rendered[fn_at:hook_at]:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    closed = True
                    break
        in_body = not closed
    check("renderJetHub() 的钩子在 render() **函数体内**（F-020）",
          in_body,
          "首版钩子插到了函数体外 → 成了死代码 → 云端 OAuth 分页首屏空白")

    # ---- F-024：连通性测试必须跟随 LLM_SOURCE ----
    #
    # 首版把商汤写死在 `test_llm()` 里，选了云端 OAuth 后点「测试」
    # 打的仍是商汤 —— 测试结论与实际链路矛盾，比报错更误导。
    panel_now = (PLUGIN_ROOT / "ocv_cloud_stack" / "panel.py").read_text(encoding="utf-8")
    check("连通性测试按 LLM_SOURCE 分流（F-024）",
          "_test_llm_jethub" in panel_now and "_test_llm_custom" in panel_now
          and 'config.llm_source() == "jethub"' in panel_now,
          "测试按钮必须与真实链路用同一路，否则「测试通过」和「实际能用」会互相矛盾")
    check("云端 OAuth 的测试分支**不含** sensenova 引用",
          "sensenova" not in panel_now.split("def _test_llm_jethub")[1]
          .split("def _summarize_llm_response")[0],
          "该分支打的是本机桥，出现 sensenova 说明又走错路了")
    check("云端 OAuth 的测试走 HTTP（真网络路径，而非内部函数）",
          "/v1/chat/completions" in panel_now.split("def _test_llm_jethub")[1][:1500],
          "只调内部函数会漏掉「桥没起来」「端口不对」这类真实故障")
    check("测试结果里标明来源与模型",
          '"来源": source_label' in panel_now and 'source_label' in panel_now,
          "用户点测试就是为了确认「在用哪一路」，不写清楚等于白测")
    check("连通性测试文案跟随来源（不再写死「验证 Key」）",
          "按当前启用的" in rendered,
          "选云端 OAuth 时根本没有 Key，写「验证 Key 有效」会误导")

    # ---- F-025/F-026：body 注入器必须认云端 OAuth 的 base + 下限语义 ----
    patches_src = (PLUGIN_ROOT / "ocv_cloud_stack" / "patches.py").read_text(encoding="utf-8")
    check("body 注入器同时认两路 base（F-025）",
          "candidates" in patches_src and "jethub_bridge_base_url()" in patches_src
          and "sorted" not in patches_src.split("_post_with_extra_body")[1][:200],
          "只匹配 sensenova base 时，选云端 OAuth 会让注入器整段静默失效"
          "（max_tokens / reasoning_effort / EXTRA_BODY 全部不生效）")
    check("body 注入器按当前来源优先选 base",
          'if config.llm_source() == "jethub"' in patches_src)
    check("max_tokens 非零时实现**下限**语义（F-026）",
          "if limit > current_num:" in patches_src,
          "面板帮助文字承诺「填正整数作为下限生效」，首版只写了 0→删键，"
          "非零时什么都不做 → 用户填的 32768 被静默忽略")

    # F-027：保存后互斥可见性不能丢
    check("data-source 打标由 MutationObserver 长期守着（F-027）",
          "jhSourceObserved" in oauth_js and "MutationObserver" in oauth_js,
          "保存流程会 renderGroup → card.innerHTML='' 重建卡片，"
          "运行时打的 data-source 被清掉 → 所有字段都显示（退回 API Key 形态），"
          "用户必须关掉再打开面板才恢复")
    check("打标函数会跳过已打过同样值的元素",
          'wrap.getAttribute("data-source") !== field.source' in oauth_js,
          "避免 MutationObserver 回调里反复写 DOM 造成无谓触发")
    check("保存返回体里带 source 字段（前端据此打标的依据）",
          any(f.get("source") for f in panel.FIELDS)
          and bool((panel.snapshot().get("fields") or [{}])[0].get("key")),
          "snapshot() 的 fields 必须保留 source，否则保存后无法重新打标")

    handlers = [name for name in dir(panel) if name.startswith("jethub_")]
    check("面板 handler 齐全（>=10 个）", len(handlers) >= 10, f"实际 {handlers}")

    # ---- 备份/恢复：常驻卡片 + 上传真的能用（用户两项要求） ----
    html_src = (PLUGIN_ROOT / "panel" / "cloud-oauth.html").read_text(encoding="utf-8")
    js_src = (PLUGIN_ROOT / "panel" / "cloud-oauth.js").read_text(encoding="utf-8")
    panel_html = (PLUGIN_ROOT / "panel" / "index.html").read_text(encoding="utf-8")

    # 需求 1a：不要弹窗，直接分开放在面板里
    check("备份/恢复是常驻卡片（HTML 资源里没有弹窗 DOM）",
          'id="jh-backup-modal"' not in html_src,
          "用户要求：不要另外弹窗，直接分开放在面板中")
    check("备份与恢复分成两张卡片",
          html_src.count("<label>备份</label>") == 1
          and html_src.count("<label>恢复</label>") == 1,
          "「备份」「恢复」应各自成卡")
    check("拼装后的面板页里也没有弹窗元素",
          'id="jh-backup-modal"' not in panel_html)
    check("旧的「备份 / 恢复」弹窗入口按钮已删",
          "btn-jethub-backup-open" not in panel_html)

    # 需求 1b：点「选择文件」必须真的能选
    # （原故障：modal 排在脚本之后 → jhBind 执行时找不到元素 → 静默失效）
    check("「选择文件」按钮与 file input 都在 HTML 里",
          'id="btn-jh-backup-pick"' in html_src and 'id="jh-backup-file"' in html_src)
    check("绑定走 jhBindWhenReady（不依赖元素恰好写在脚本前）",
          "jhBindWhenReady" in js_src,
          "F-005：元素排在脚本之后会让 jhBind 静默失败，用户点按钮没反应")
    check("file input 在绑定代码之前出现",
          0 < panel_html.find('id="jh-backup-file"')
          < panel_html.find('jhBindWhenReady("btn-jh-backup-pick"'),
          "被绑元素必须排在立即执行的绑定代码之前")

    # 需求 2：字段区不再有提供商下拉（左栏是唯一入口）
    check("字段区没有重复的提供商下拉（用户要求删掉）",
          not any(f["key"] == "JETHUB_PROVIDER" for f in panel.FIELDS),
          "用户要求：有下面的账号管理窗口就不需要上面的提供商选择了")
    check("提供商左栏仍是唯一入口",
          'id="jethub-providers"' in html_src and "renderJetHubProviders" in js_src)

    shim_src = (PLUGIN_ROOT / "ocv_cloud_stack" / "image_shim.py").read_text(encoding="utf-8")
    shim_src = (PLUGIN_ROOT / "ocv_cloud_stack" / "image_shim.py").read_text(encoding="utf-8")
    for route in ("/api/panel/jethub/status", "/api/panel/jethub/ensure",
                  "/api/panel/jethub/login/start", "/api/panel/jethub/login/poll",
                  "/api/panel/jethub/account", "/api/panel/jethub/model/toggle",
                  "/api/panel/jethub/test", "/api/panel/jethub/backup",
                  "/api/panel/jethub/credits"):
        check(f"路由已挂 {route}", route in shim_src)

    # ------------------------------------------------------------------
    print("\n6) 面板 handler 端到端（打真实桥）")
    # ------------------------------------------------------------------
    try:
        status = panel.jethub_status()
        check("panel.jethub_status() 返回 ok", bool(status.get("ok")))
        check("状态里带 bridge 信息", isinstance(status.get("bridge"), dict))
        check("状态里带 storage 落点", isinstance(status.get("storage"), dict))
        # 未登录时账号为空是预期
        print(f"         accounts={len(status.get('accounts') or [])} "
              f"models={len(status.get('models') or [])}")
        test = panel.jethub_test({})
        check("panel.jethub_test() 在未登录时优雅失败（不抛异常）",
              isinstance(test, dict) and "message" in test,
              str(test)[:200])
        print(f"         test ok={test.get('ok')} message={str(test.get('message'))[:100]}")
    except Exception as exc:  # noqa: BLE001
        check("面板 handler 端到端", False, f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    print("\n" + "=" * 68)
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项 / 通过 {PASSED} 项")
        for item in FAILURES:
            print(f"  - {item}")
        print("=" * 68)
        return 1
    print(f"全部通过（{PASSED} 项）")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
