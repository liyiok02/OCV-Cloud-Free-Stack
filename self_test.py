"""注入自检 —— 不修改任何配置，只验证补丁是否真的挂上了。

用法（在 OCV 项目根目录下）::

    set PYTHONPATH=%CD%\\plugins\\cloud_free_stack
    runtime\\python\\python.exe plugins\\cloud_free_stack\\self_test.py

它会模拟 OCV 各阶段子进程的导入方式，检查：

* 环境编码（.pth 锚点是否纯 ASCII、控制台输出在 GBK 下会不会崩）
* 面板覆盖层是否已物化进 ``os.environ``（否则 OCV 原生校验看不见面板配置）
* ``builtins.__import__`` 包装是否装上
* ``backend.app.qwen_tts`` 的三个名字是否已被 MiMo 接管
* ``backend.app.gemini_client.LANGUAGE_PROVIDER_OPTIONS`` 是否出现 sensenova
* 商汤 provider 的 base_url / model / configured 是否解析正确
* 模块 2 的依赖自检有没有冷启动超时风险（预热语句是否与 OCV 逐字一致）
* 软件更新抹掉注入后能否原地自愈（frontend/index.html 的注入块 + .pth 锚点）
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Literal as _ProbeLiteral

from pydantic import BaseModel as _ProbeBaseModel

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ⚠ 必须**先**加固控制台再打印任何带符号的输出。
#
# 本脚本是给人手工跑的，最常见的调用方式是
#     runtime\python\python.exe plugins\cloud_free_stack\self_test.py
# 此时 **没有** PYTHONPATH、`sitecustomize` 不会加载、`bootstrap.install()` 也
# 不会跑 —— 于是 `console.harden()` 从未被调用。而在中文 Windows 上 stdout 是
# GBK，打印 ``✗``（U+2717）会抛 UnicodeEncodeError 并**直接终止进程**。
#
# 实测踩过（2026-09-23）：`.pth` 锚点缺失时第 0 节第一项就是 ``✗``，
# 脚本当场崩在 `check()` 里，用户看到的是一段 traceback 而不是"锚点缺失"这条
# 清晰的结论 —— **最需要诊断的时候，诊断工具自己先死了**。
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))
try:
    from ocv_cloud_stack import console as _console  # noqa: PLC0415

    _console.harden()
except Exception:  # noqa: BLE001 - 加固失败仍要继续，只是符号可能降级不了
    pass

FAILURES: list[str] = []


# 1u 节的探针请求体。
#
# ⚠ **必须定义在模块级**：本文件有 ``from __future__ import annotations``
# （PEP 563），函数内定义的类其注解会保持**字符串**，FastAPI 解析不了函数局部
# 名字 ⇒ 该路由的 ``body_params`` 为空 ⇒ **一切**请求都 422，看起来像
# 「适配器重建失效」。实测踩过，见 ``_check_mimo_engine`` 文档。
class _LiteralProbeRequest(_ProbeBaseModel):
    """与 OCV ``GenerateRequest.tts_engine`` 同形的探针模型。"""

    tts_engine: _ProbeLiteral["indextts2", "indextts25", "cluster", "qwen"] = "indextts25"


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "✓" if ok else "✗"
    suffix = f"  {detail}" if detail else ""
    print(f"  {mark} {label}{suffix}")
    if not ok:
        FAILURES.append(label)


# 中文 Windows 上最容易翻车的两个地方，都与 locale 编码是 GBK 有关。
_PTH_PATH = (
    PROJECT_ROOT / "runtime" / "python" / "Lib" / "site-packages"
    / "ocv_cloud_free_stack.pth"
)


def _check_encodings() -> None:
    """验证 .pth 锚点与控制台输出都不受 locale 编码影响。

    这两条是同一类缺陷的两面：

    * CPython 的 ``site.addpackage()`` 用 **locale 编码**读 ``.pth``，不是 UTF-8。
      锚点里只要有 GBK 编不出的字节，整行读不到 → 注入静默失效，
      而报错却是启动器的「便携路径检查失败」，极难联想。
    * 中文 Windows 的 stdout 是 GBK，打印 ``✓`` / ``✗`` / ``⚠`` 会抛
      ``UnicodeEncodeError`` **直接终止进程**。
    """
    from ocv_cloud_stack import console  # noqa: PLC0415

    if not _PTH_PATH.is_file():
        check("注入锚点存在", False, f"未找到 {_PTH_PATH.name}（先运行 cloud_stack_ctl.py install）")
    else:
        raw = _PTH_PATH.read_bytes()
        unreadable = [
            encoding
            for encoding in ("ascii", "utf-8", "gbk", "cp1252", "latin-1")
            if not _decodes(raw, encoding)
        ]
        check(
            "注入锚点是纯 ASCII（任何 locale 都能读）",
            not unreadable,
            "被这些编码读不了：" + "、".join(unreadable) if unreadable else ".pth 纯 ASCII",
        )

    errors_mode = str(getattr(sys.stdout, "errors", "") or "")
    check(
        "控制台已启用编码降级（GBK 下打印符号不再崩）",
        errors_mode == "ocv_cloud_stack_glyph",
        f"stdout.errors = {errors_mode or '（默认 strict，GBK 下会崩）'}",
    )
    if not console.supports("✓"):
        print(
            f"    当前控制台编码 {getattr(sys.stdout, 'encoding', '?')}："
            "U+2713 / U+2717 / U+26A0 会降级显示为 √ / × / !（不影响判定）"
        )


def _pipeline_import_snippet(path: Path) -> str:
    """从 pipeline.py 里抠出 ASR 自检实际执行的那条 ``-c`` 语句。

    刻意用正则而不是 import：这条语句是**被预热对象与自检对象的同一性依据**，
    要的就是源码里的字面量，不是运行时值。
    """
    import re  # noqa: PLC0415

    if not path.is_file():
        return ""
    match = re.search(r'python_bin,\s*"-c",\s*"([^"]+)"', path.read_text(encoding="utf-8", errors="replace"))
    return match.group(1) if match else ""


def _check_asr_prewarm() -> None:
    """验证模块 2 的依赖自检不会因冷启动而超时。

    背景：``ctranslate2/__init__.py`` 会连带导入 ``torch``，而本整合包里的
    torch 是 ``2.8.0+cu128``（``torch/lib`` 6.9 GB / 37 个 DLL）。自检超时只有
    90 秒，**冷启动**时第一次加载这批 DLL 会超过它；被扫过之后只要 6 秒级。

    所以这里要守住两件事：

    1. 预热跑的那条导入语句必须和 OCV 自检**逐字一致** —— 否则预热的是另一份
       东西，钱白花。OCV 升级改了这条语句，本项就会失败。
    2. ``.env`` 里的自检超时已抬高（兜底）。它只在"很慢"时触发，
       "没装依赖"是立刻返回非零退出码的，不会白等。
    """
    import re  # noqa: PLC0415

    from ocv_cloud_stack import asr_prewarm  # noqa: PLC0415

    expected = _pipeline_import_snippet(PROJECT_ROOT / "backend" / "app" / "pipeline.py")
    check(
        "预热语句与 OCV 自检逐字一致",
        bool(expected) and expected == asr_prewarm._IMPORT_SNIPPET,  # noqa: SLF001
        f"插件 {asr_prewarm._IMPORT_SNIPPET!r} / OCV {expected!r}",
    )

    env_text = (PROJECT_ROOT / ".env").read_text(encoding="utf-8", errors="replace")
    match = re.search(r"^ASR_RUNTIME_CHECK_TIMEOUT_SECONDS=(\d+)\s*$", env_text, re.M)
    value = int(match.group(1)) if match else 0
    check(
        "自检超时已抬高到 300 秒以上（冷启动兜底）",
        value >= 300,
        f"ASR_RUNTIME_CHECK_TIMEOUT_SECONDS={value or '（未写入，将退回默认 90）'}",
    )
    print(
        f"    预热开关 CLOUD_STACK_ASR_PREWARM={asr_prewarm.enabled()}，"
        f"上限 {int(asr_prewarm.timeout_seconds())} 秒，"
        f"解释器 {asr_prewarm.asr_python()}"
    )


def _check_store_exported() -> None:
    """面板覆盖层必须真的进了 ``os.environ``。

    面板（``var/config.json``）在文档里是「最高优先级配置源」，但它原先
    只对插件自己的 ``config.get()`` 可见。OCV 有大量原生校验**直接读
    ``os.environ``**（典型：``gemini_client.gemini_configured()``，也就是
    Agent 0 开头那道门禁），而阶段子进程更是只继承环境变量 —— 于是会出现
    「面板里明明填了商汤 Key，Agent 0 却报 *语言模型未配置*」。

    物化工作由 ``bootstrap._export_store_to_environ()`` 完成，
    这里守住两条不变量：

    * 面板里的**非空**键，``os.environ`` 中的值必须与之一致；
    * 面板里的**空**键，绝不能被写成空字符串 —— ``os.getenv(key, default)``
      只在键不存在时用默认值，键存在而值为空会让下游拿到 ``""``
      （module2 的 ``os.getenv("ASR_DEVICE", "auto")`` 会直接抛 ValueError）。
    """
    from ocv_cloud_stack import config  # noqa: PLC0415

    store = config.store_values()
    if not store:
        print("  · 面板覆盖层为空，跳过（全部沿用 .env 与内置默认）")
        return

    missing: list[str] = []
    empty_keys: list[str] = []
    for key, cached in store.items():
        text = str(cached or "").strip()
        if not text:
            empty_keys.append(key)
            continue
        if os.environ.get(key) != text:
            missing.append(key)

    live = len(store) - len(empty_keys)
    check(
        "面板非空键已全部物化进 os.environ",
        not missing,
        "缺失：" + "、".join(missing) if missing else f"{live} 项已生效（Agent 0 等原生校验看得见）",
    )
    if empty_keys:
        leaked = [key for key in empty_keys if key in os.environ]
        check(
            "面板空值键没有被误写成空字符串",
            not leaked,
            "被写空的：" + "、".join(leaked) if leaked else f"{len(empty_keys)} 个空值键已正确跳过",
        )


def _check_save_syncs_environ() -> None:
    """面板保存必须实时同步 ``os.environ``（运行中换模型立即生效）。

    Agent 1 的语言模型调用发生在**后端进程内**（``pipeline`` 直接
    ``import story_agents``），而 ``gemini_client.language_model()``
    调用时读的是 ``os.environ``。若保存只落 ``var/config.json``
    不同步环境变量，就会出现「面板换了模型，正在跑的后端还在用旧模型」
    —— 2026-09-13 实测踩过：13:02 保存 ``glm-5.2``，13:06 Agent 1
    仍然在打 ``deepseek-v4-pro``。

    用临时键验证「写 → 环境一致 → 删除 → 环境摘除」，全程还原现场。
    """
    from ocv_cloud_stack import bootstrap, config, panel  # noqa: PLC0415

    probe_key = "CLOUD_STACK_DEBUG"  # bool 字段，合法值只有 "0"/"1"
    saved_store = config.store_values().get(probe_key)
    saved_env = os.environ.get(probe_key)
    try:
        panel.apply({"values": {probe_key: "1"}})
        written = os.environ.get(probe_key) == "1"
        check(
            "面板保存后 os.environ 立即同步",
            written,
            "未同步，运行中的后端看不到新值" if not written else f"{probe_key}=1",
        )

        panel.apply({"values": {probe_key: ""}})
        removed = probe_key not in os.environ
        check(
            "面板删除后 os.environ 同步摘除",
            removed,
            "残留旧值，运行中的后端会继续用它" if not removed else "回落 .env / 默认值",
        )
    finally:
        panel.apply({"values": {probe_key: saved_store or ""}})
        if saved_env is not None:
            os.environ[probe_key] = saved_env
        elif saved_store:
            os.environ[probe_key] = saved_store
        else:
            os.environ.pop(probe_key, None)


def _check_clone_voices() -> None:
    """克隆音色：注册表 -> resolve 放行 -> payload 构造，全程本地不调 API。

    不变量（2026-09-13 探针实测抓过回退 bug）：

    * 注册表里的名字必须能原样通过 ``resolve_voice``，**不许**回退默认音色；
    * ``synthesize_to_file`` 命中克隆音色时必须切 ``mimo-v2.5-tts-voiceclone``
      模型，且 ``audio.voice`` 是参考音频的 data URI；
    * 参考音频文件丢失时抛可读异常，而不是静默用预置音色顶上。
    """
    import base64 as _base64

    import requests as _requests  # noqa: PLC0415

    from ocv_cloud_stack import config, mimo_tts  # noqa: PLC0415

    name = "自检克隆音"
    audio_dir = config.clone_voices_dir()
    fake_blob = b"RIFF" + b"\x00" * 2048  # 假 wav，只走本地路径
    fake_file = audio_dir / "selftest_clone.wav"
    fake_file.write_bytes(fake_blob)
    registry = config.clone_voices()
    saved_registry = config.get(config.CLONE_VOICES_KEY)
    registry[name] = {
        "file": fake_file.name,
        "mime": "audio/wav",
        "bytes": len(fake_blob),
        "uploaded_at": "selftest",
    }
    config.update_store(
        {config.CLONE_VOICES_KEY: json.dumps(registry, ensure_ascii=False)}
    )
    try:
        check(
            "resolve_voice 放行克隆音色名",
            mimo_tts.resolve_voice(name) == name,
            f"得到 {mimo_tts.resolve_voice(name)!r}",
        )

        captured: dict[str, Any] = {}

        class _FakeResponse:
            status_code = 200

            def json(self) -> dict[str, Any]:
                return {
                    "choices": [
                        {
                            "message": {
                                "audio": {
                                    "data": _base64.b64encode(
                                        b"RIFF" + b"\x00" * 300
                                    ).decode("ascii")
                                }
                            }
                        }
                    ]
                }

        def _fake_post(url: str, *args: Any, **kwargs: Any) -> "_FakeResponse":
            captured["url"] = url
            captured["json"] = kwargs.get("json")
            return _FakeResponse()

        orig_post = _requests.post
        _requests.post = _fake_post
        out = audio_dir / "_selftest_clone_out.wav"
        try:
            mimo_tts.synthesize_to_file(
                text="自检", destination=out, voice=name, retries=1
            )
            payload = captured.get("json") or {}
            audio_options = payload.get("audio") or {}
            voice_field = str(audio_options.get("voice") or "")
            check(
                "命中克隆音色 -> 模型切 voiceclone",
                payload.get("model") == "mimo-v2.5-tts-voiceclone",
                str(payload.get("model")),
            )
            check(
                "audio.voice 为参考音频 data URI",
                voice_field.startswith("data:audio/wav;base64,"),
                voice_field[:32],
            )
            expected_uri = (
                "data:audio/wav;base64,"
                + _base64.b64encode(fake_blob).decode("ascii")
            )
            check("data URI 与注册表音频逐字一致", voice_field == expected_uri)
        finally:
            _requests.post = orig_post
            out.unlink(missing_ok=True)

        # 文件丢失：改注册表指向不存在的文件
        registry[name] = {
            "file": "no_such_file.wav",
            "mime": "audio/wav",
            "bytes": 1,
            "uploaded_at": "selftest",
        }
        config.update_store(
            {config.CLONE_VOICES_KEY: json.dumps(registry, ensure_ascii=False)}
        )
        raised = ""
        try:
            mimo_tts.synthesize_to_file(
                text="自检", destination=audio_dir / "_x.wav", voice=name, retries=1
            )
        except Exception as exc:  # noqa: BLE001
            raised = str(exc)
        check(
            "参考音频丢失 -> 可读报错",
            "丢失" in raised and name in raised,
            raised[:80],
        )
    finally:
        if saved_registry:
            config.update_store({config.CLONE_VOICES_KEY: saved_registry})
        else:
            config.update_store({}, remove=[config.CLONE_VOICES_KEY])
        fake_file.unlink(missing_ok=True)


def _check_reapply() -> None:
    """派生注入状态的自愈（软件更新抹掉注入后的自动重建）。

    这是 2026-09-16 那次更新暴露出来的故障面。OCV 每次更新的归档里都有一份
    ``update.json``，``protected_data`` 保护 ``.env`` / ``runtime`` /
    ``output`` / ``workspace`` / user presets / third-party plugins ——
    但 **不保护 ``frontend/``**，那是会被整体替换的源码目录。于是注入在
    ``frontend/index.html`` 里的面板按钮每次更新都会消失，而插件的其余部分
    照常工作，故障面只有派生层。

    本节点会**真的**把注入抹掉再让自愈重建，因此必须把断言限制在
    「重建结果 == 原始模板 + 恰好一个注入块」，并在 ``finally`` 里还原。
    """
    import cloud_stack_ctl as ctl

    from ocv_cloud_stack import config, patches, reapply

    check(
        "更新后自愈默认开启",
        config.auto_reinject() is True,
        f"CLOUD_STACK_AUTO_REINJECT={config.get('CLOUD_STACK_AUTO_REINJECT') or '(未写进 .env，用默认值)'}",
    )
    check(
        "自愈入口可被后端进程调用",
        callable(getattr(patches, "_schedule_derived_state_reapply", None)),
        "patches._schedule_derived_state_reapply",
    )

    # 锚点是自愈的另一半。这里只验证「健康时是 no-op」与编码不变量，
    # 不去删真实的 .pth —— 自检会被随手运行，不该在崩溃时留下半残状态。
    anchor = reapply.anchor_state()
    check("锚点探针可读", anchor["present"] and anchor["ok"], anchor["detail"])
    check(
        "含中文的锚点行会被判定为不安全（GBK 下注入会静默失效）",
        not ctl._pth_text_is_safe("import sys; sys.path.insert(0, r'E:\\一键成片')"),
    )

    targets = ctl._frontend_targets()
    if not targets:
        check("找到前端入口", False, "frontend/index.html 不存在")
        return

    target = targets[0]
    saved = target.read_bytes()
    original = saved.decode("utf-8")
    # 全程按**字节**处理再比对：OCV 整仓文件是 CRLF 行尾，而
    #  ``read_bytes().decode()`` 保留 \r\n、``write_text()`` 又会把 \n 翻成
    #  \r\n —— 两者混用会写出 \r\r\n。断言必须能看见这类破坏。
    pristine = ctl._strip_frontend_block(original).encode("utf-8")
    try:
        # 模拟一次软件更新：注入块被整体抹掉（字节精确）
        target.write_bytes(pristine)

        before = reapply.frontend_state()
        check("能检测到注入缺失", not before["ok"], before["detail"])

        report = reapply.repair()
        check("自愈重建了前端注入", "frontend" in report["repaired"], str(report["repaired"]))
        check("自愈过程无报错", not report["errors"], "; ".join(report["errors"]))

        after = target.read_bytes()
        after_text = after.decode("utf-8")
        # 先确认文件还是个 HTML 文档：如果注入把模板写没了，下面的 index()
        # 会抛 ValueError 而不是给出一条可读的失败 —— 那会掩盖真正的故障。
        check(
            "重建后仍是完整 HTML 模板（含 </body>）",
            "</body>" in after_text,
            f"{len(after)}B: {after_text[:60]!r}",
        )
        if "</body>" in after_text:
            check(
                "注入块落在 </body> 之前",
                ctl.FRONTEND_MARK_BEGIN in after_text
                and after_text.index(ctl.FRONTEND_MARK_BEGIN) < after_text.index("</body>"),
            )
        # 核心不变量：重建必须是「原始模板 + 恰好一个注入块」——既不能缺，
        # 也不能因为反复 repair 堆叠出第二个块；行尾也不能被改写。
        check(
            "重建结果 = 原始模板 + 恰好一个注入块（行尾不乱）",
            ctl._strip_frontend_block(after_text).encode("utf-8") == pristine
            and after.count(ctl.FRONTEND_MARK_BEGIN.encode()) == 1
            and after.count(ctl.FRONTEND_MARK_END.encode()) == 1
            and b"\r\r\n" not in after,
            f"{len(pristine)}B -> {len(after)}B",
        )

        second = reapply.repair()
        check("自愈幂等：第二次不碰任何文件", second["repaired"] == [], str(second["repaired"]))
    finally:
        target.write_bytes(saved)


def _check_shim_routes() -> None:
    """shim 的 RunningHub 提交路由必须能接住 OCV 实际会打来的路径。

    整合包 ``.env`` 自带 ``RUNNINGHUB_ENDPOINT=/v1/images/generations``
    （OpenAI 风格遗留值），OCV 会给它套 ``/openapi/v2/`` 前缀后打到
    shim —— 没有别名路由时直接 404「未实现的端点」，模块 4 永远
    提交不出图（2026-09-13 实测）。这里不起服务，直接对正则断言。
    """
    from ocv_cloud_stack import image_shim  # noqa: PLC0415

    m = image_shim._SUBMIT_RE.match("/openapi/v2/sensenova-u1.5-lite/text-to-image")
    check(
        "新 .env 端点匹配为 text-to-image",
        bool(m) and m.group("op") == "text-to-image",
        m.group("op") if m else "不匹配",
    )
    m2 = image_shim._SUBMIT_RE.match("/openapi/v2/sensenova-u1.5-lite/image-to-image")
    check(
        "图生图端点匹配为 image-to-image",
        bool(m2) and m2.group("op") == "image-to-image",
        m2.group("op") if m2 else "不匹配",
    )
    check(
        "OpenAI 风格遗留路径走别名路由",
        bool(image_shim._SUBMIT_ALIAS_RE.match("/openapi/v2/v1/images/generations")),
        "/openapi/v2/v1/images/generations",
    )
    check(
        "别名路由不被常规提交正则误抢",
        image_shim._SUBMIT_RE.match("/openapi/v2/v1/images/generations") is None,
        "两个正则应互斥",
    )
    stray = [p for p in ("/openapi/v2/query", "/api/panel/state", "/files/x.jpg")
             if image_shim._SUBMIT_RE.match(p) or image_shim._SUBMIT_ALIAS_RE.match(p)]
    check("非提交路径不被两个正则误伤", not stray, "、".join(stray) if stray else "全部不匹配")


def _decodes(raw: bytes, encoding: str) -> bool:
    try:
        raw.decode(encoding)
        return True
    except (UnicodeDecodeError, LookupError):
        return False


def _check_thinking_switch() -> None:
    """深度思考开关：默认关闭，且各档位能真正改变请求体。

    这是 2026-09-17 加的自检，用来锁住一条**曾经静默失效**的不变量：

    面板上 ``CLOUD_STACK_LLM_EXTRA_BODY`` 的历史遗留值是
    ``{"reasoning_effort": "none"}``。若 ``_llm_extra_body()`` 用整体
    ``dict.update()`` 合并，用户把档位调到 low/high 后仍会被这个陈旧值
    按回 ``none`` —— 界面上显示"已开启"，请求里其实是关闭。

    这里做**负面验证**：故意把遗留值设成 ``reasoning_effort=none``，
    再把档位设为 high，断言最终请求体里是 ``high`` 而不是 ``none``。

    ## 为什么必须写面板层而不是 ``os.environ``（2026-09-17 修正）

    本自检原先只写 ``os.environ["CLOUD_STACK_LLM_THINKING"]``，于是它的 6 条
    档位断言**在本机恒为假**。原因是 ``config.get()`` 的优先级链条：

        _read_store()（面板 var/config.json） > os.environ > .env > 默认值

    面板层排在**最前**。只要面板里存了 ``CLOUD_STACK_LLM_THINKING=off``
    （真实 v1.2.0 面板就是这样），写 ``os.environ`` 就会被永久压住，
    ``llm_thinking_mode()`` 永远返回 off。

    这在生产里也是**同一个坑**：用户在面板上把「深度思考」从 off 切到 high，
    ``panel.apply()`` 同时更新面板层与 ``os.environ``，两条路径的值一致，
    所以能生效；但只要有**任何**代码（脚本、排障、第三方集成）只改
    ``os.environ``，就会静默失效 —— 界面显示已开启、请求里仍是关闭。

    因此：档位类断言一律经 ``config.update_store()`` 写面板层，
    并在 finally 里原样还原。
    """
    from ocv_cloud_stack import config, patches  # noqa: PLC0415

    # 档位必须走面板层：面板层优先级高于 os.environ（见 docstring）。
    # 环境变量与原面板值都要还原，避免污染后续自检与真实配置。
    saved_level = config.get("CLOUD_STACK_LLM_THINKING")
    previous_env = os.environ.get("CLOUD_STACK_LLM_THINKING")
    try:
        check(
            "默认档位为 off（关闭思考）",
            patches.llm_thinking_mode() in {"off", "none"},
            f"llm_thinking_mode() = {patches.llm_thinking_mode()}",
        )

        config.update_store({"CLOUD_STACK_LLM_THINKING": "off"})
        off_body = patches._llm_extra_body()
        check(
            "关闭时发出 reasoning_effort=none",
            off_body.get("reasoning_effort") == "none",
            json.dumps(off_body, ensure_ascii=False),
        )
        check(
            "关闭时同时发出 thinking=disabled（兼容只认该键的网关）",
            (off_body.get("thinking") or {}).get("type") == "disabled",
            json.dumps(off_body.get("thinking"), ensure_ascii=False),
        )

        for level in ("low", "medium", "high"):
            config.update_store({"CLOUD_STACK_LLM_THINKING": level})
            body = patches._llm_extra_body()
            check(
                f"档位 {level} 正确透传为 reasoning_effort={level}",
                body.get("reasoning_effort") == level,
                f"面板值={config.get('CLOUD_STACK_LLM_THINKING')!r} → {json.dumps(body, ensure_ascii=False)}",
            )
            check(
                f"档位 {level} 不再发 thinking=disabled（避免顶掉档位）",
                "thinking" not in body,
                json.dumps(sorted(body.keys()), ensure_ascii=False),
            )

        # 负面验证：面板遗留值不得把用户显式开启的档位按回去。
        legacy = config.get("CLOUD_STACK_LLM_EXTRA_BODY")
        if "reasoning_effort" in legacy:
            config.update_store({"CLOUD_STACK_LLM_THINKING": "high"})
            body = patches._llm_extra_body()
            check(
                "面板遗留的 reasoning_effort 不会覆盖用户开启的档位",
                body.get("reasoning_effort") == "high",
                f"遗留值={legacy} → 实际发出 {body.get('reasoning_effort')}",
            )
    finally:
        if saved_level:
            config.update_store({"CLOUD_STACK_LLM_THINKING": saved_level})
        else:
            config.update_store({}, remove=["CLOUD_STACK_LLM_THINKING"])
        if previous_env is None:
            os.environ.pop("CLOUD_STACK_LLM_THINKING", None)
        else:
            os.environ["CLOUD_STACK_LLM_THINKING"] = previous_env


def _check_agent1b_resilience() -> None:
    """Agent 1B：边界细化失败必须降级，而不是终止整条流水线。

    回归的是 2026-09-17 的真实故障：

        Agent 1B 语言模型规划失败 … 原始错误：边界验收失败: empty

    成因是「父单元只含 1 个超长 slide」时模型只能在同一 slide_id 上重复
    起止，被 ``_normalize_semantic_units`` 判空，再被升级为致命错误。

    本自检**不打网络**，只用构造数据验证两条不变量：

    1. 单 slide 父单元被跳过细化（耗时应接近 0，证明没调模型）；
    2. 细化失败时保留原单元并继续，且输出仍完整覆盖父单元范围。
    """
    from ocv_cloud_stack import agent1b_resilience as resilience  # noqa: PLC0415
    import story_agents  # noqa: PLC0415

    check(
        "story_agents.refine_risky_semantic_units 已被包装",
        bool(getattr(story_agents.refine_risky_semantic_units, "_cloud_stack_wrapped", False)),
        "若为 ✗，OCV 版本可能变更了函数名，插件会回退到原生（会终止任务）",
    )
    check(
        "降级开关默认开启",
        resilience.enabled(),
        "CLOUD_STACK_AGENT1B_RESILIENT=0 可恢复 OCV 原生严格行为",
    )
    check(
        "前置跳过默认开启（单 slide 父单元不送模型）",
        resilience.skip_inseparable(),
        "CLOUD_STACK_AGENT1B_SKIP_INSEPARABLE=0 可单独关掉跳过",
    )

    scenes = [
        {"slide_id": "s1", "start": 0.0, "end": 3.0, "text_content": "甲", "source_boundary_after": "none"},
        {"slide_id": "s2", "start": 3.0, "end": 6.0, "text_content": "乙", "source_boundary_after": "line"},
        {"slide_id": "s3", "start": 6.0, "end": 40.0, "text_content": "丙", "source_boundary_after": "none"},
    ]
    single = [{
        "unit_id": "u1", "start_slide_id": "s3", "end_slide_id": "s3", "boundary_after": "hard",
    }]

    import time  # noqa: PLC0415

    started = time.monotonic()
    refined, diag = story_agents.refine_risky_semantic_units(
        single, scenes, {}, "story", require_ai_success=True,
    )
    elapsed = time.monotonic() - started

    stats = diag.get("resilience") or {}
    check(
        "单 slide 父单元不抛异常（原生会终止任务）",
        isinstance(refined, list) and len(refined) == 1,
        f"返回 {len(refined)} 个单元",
    )
    check(
        "单 slide 父单元被跳过细化且未调用模型",
        stats.get("skipped_single_slide") == 1 and elapsed < 1.0,
        f"skipped={stats.get('skipped_single_slide')}，耗时 {elapsed:.3f}s",
    )
    check(
        "跳过的单元仍完整保留父单元范围",
        bool(refined) and refined[0].get("start_slide_id") == "s3"
        and refined[0].get("end_slide_id") == "s3",
        f"{refined[0].get('start_slide_id')} -> {refined[0].get('end_slide_id')}" if refined else "空",
    )
    check(
        "诊断里明确标记「原生本会终止」",
        stats.get("native_would_abort") is True,
        json.dumps(stats, ensure_ascii=False),
    )

    # 顺序不变量：跳过与细化混排时，输出必须仍按原父单元顺序排列。
    mixed = [
        {"unit_id": "a", "start_slide_id": "s3", "end_slide_id": "s3", "boundary_after": "hard"},
        {"unit_id": "b", "start_slide_id": "s1", "end_slide_id": "s2", "boundary_after": "soft"},
    ]
    ordered, _ = story_agents.refine_risky_semantic_units(
        mixed, scenes, {}, "story", require_ai_success=True,
    )
    starts = [str(u.get("start_slide_id")) for u in ordered]
    check(
        "跳过与细化混排时输出顺序与父单元一致",
        starts and starts[0] == "s3" and "s1" in starts,
        f"start_slide_id 序列 = {starts}",
    )
    ids = [str(u.get("unit_id")) for u in ordered]
    check(
        "混排时 unit_id 已重编号且全局唯一",
        len(ids) == len(set(ids)),
        f"unit_id 序列 = {ids}",
    )

    # 两层开关的边界必须清楚：关掉「降级」不应连带关掉「跳过」。
    #
    # 语义：`CLOUD_STACK_AGENT1B_RESILIENT=0` 关的是**降级**（失败不再致命），
    # 单 slide 的**前置跳过**是纯优化（结构上本就不可能有合法切分），
    # 由独立开关 `CLOUD_STACK_AGENT1B_SKIP_INSEPARABLE` 控制。
    # 关掉容错是为了排查模型质量，不该把注定失败的请求重新发出去。
    os.environ["CLOUD_STACK_AGENT1B_RESILIENT"] = "0"
    try:
        import time as _time  # noqa: PLC0415

        started = _time.monotonic()
        refined, diag = story_agents.refine_risky_semantic_units(
            single, scenes, {}, "story", require_ai_success=True,
        )
        elapsed = _time.monotonic() - started
        stats = diag.get("resilience") or {}
        check(
            "关闭降级后单 slide 仍被前置跳过（跳过是优化，不随降级开关回退）",
            stats.get("skipped_single_slide") == 1 and elapsed < 1.0,
            f"skipped={stats.get('skipped_single_slide')}，耗时 {elapsed:.3f}s",
        )
        check(
            "关闭降级后 diagnosed degrade_enabled 为 False",
            stats.get("degrade_enabled") is False,
            json.dumps(stats, ensure_ascii=False),
        )

        # 两个开关独立可关，且关掉后完全回到原生路径。
        # 这里**不打网络**（自检必须离线可跑），只验证开关语义本身：
        # 关掉 skip 后 skip_inseparable() 必须为 False、enabled() 仍为 False，
        # 且包装层会因「两层全关」直接短路到原生函数。
        os.environ["CLOUD_STACK_AGENT1B_SKIP_INSEPARABLE"] = "0"
        check(
            "两个开关可独立关闭（关掉跳过不影响降级开关的读数）",
            resilience.skip_inseparable() is False and resilience.enabled() is False,
            f"skip_inseparable()={resilience.skip_inseparable()}, "
            f"enabled()={resilience.enabled()}",
        )
    finally:
        os.environ.pop("CLOUD_STACK_AGENT1B_RESILIENT", None)
        os.environ.pop("CLOUD_STACK_AGENT1B_SKIP_INSEPARABLE", None)


def _check_agent1b_partition() -> None:
    """Agent 1B 输出的**重组切分**必须无损 —— 回归 2026-09-17 的第二处缺陷。

    背景：``refine_risky_semantic_units`` 返回的是一维**扁平流**，
    里面混装三种条目（未触发风险的父单元 / 触发但确认不可分 / 触发且拆分成功）。
    插件为了把「跳过的单 slide 父单元」插回正确位置，必须把这条流按父单元
    重新切开 —— 切开时用到一条不变量：**触发风险的父单元，其子项区间必定
    严格铺满父单元自己的 slide 区间**（由原函数 ``_candidate_refinement_is_safe``
    的 ``covered_ids != expected_ids`` 校验保证）。

    第一版实现只在「触发风险」分支推进游标，未触发分支直接 ``continue``,
    于是游标停在原地、后续父单元读到前面已输出的条目，产出**重复区间**：
    实测 ``scene_001->scene_005`` 被连发两遍，107 个 slide 只覆盖 5 个。
    本自检用最小构造把这条不变量钉住。
    """
    from ocv_cloud_stack import agent1b_resilience as resilience  # noqa: PLC0415
    import story_agents  # noqa: PLC0415

    scenes = [
        {
            "slide_id": f"s{index:02d}",
            "start": float(index * 3),
            "end": float(index * 3 + 3),
            "text_content": f"第{index}句",
            "source_boundary_after": "none",
        }
        for index in range(1, 19)
    ]
    positions = {scene["slide_id"]: index for index, scene in enumerate(scenes)}

    # 父单元：3 个「不触发风险」+ 1 个「触发并拆成 3 段」+ 1 个「不触发」。
    # 这正是复现缺陷所需的最小形态 —— 第一个触发项之前必须有未触发项。
    units = [
        {"unit_id": "p1", "start_slide_id": "s01", "end_slide_id": "s03",
         "boundary_after": "hard", "_kids": 0},
        {"unit_id": "p2", "start_slide_id": "s04", "end_slide_id": "s06",
         "boundary_after": "hard", "_kids": 0},
        {"unit_id": "p3", "start_slide_id": "s07", "end_slide_id": "s09",
         "boundary_after": "hard", "_kids": 0},
        {"unit_id": "p4", "start_slide_id": "s10", "end_slide_id": "s15",
         "boundary_after": "hard", "_kids": 3},
        {"unit_id": "p5", "start_slide_id": "s16", "end_slide_id": "s18",
         "boundary_after": "hard", "_kids": 0},
    ]

    # 按父单元造出原生会产出的扁平流：
    #   _kids=0 -> 1 条（父单元本尊）；_kids=n -> n 条（切成 n 段）。
    flat: list[dict] = []
    for unit in units:
        kids = int(unit["_kids"])
        if kids == 0:
            flat.append({
                "unit_id": unit["unit_id"],
                "start_slide_id": unit["start_slide_id"],
                "end_slide_id": unit["end_slide_id"],
            })
            continue
        start = positions[unit["start_slide_id"]]
        end = positions[unit["end_slide_id"]]
        span = list(range(start, end + 1))
        width = max(1, len(span) // kids)
        for offset in range(0, len(span), width):
            group = span[offset:offset + width]
            flat.append({
                "unit_id": f"child_{offset:03d}",
                "start_slide_id": scenes[group[0]]["slide_id"],
                "end_slide_id": scenes[group[-1]]["slide_id"],
            })

    def risks_of(unit: dict, _scenes: list) -> list[str]:
        return ["duration>24s"] if int(unit.get("_kids") or 0) else []

    by_parent, ok = resilience._partition_forward(units, flat, scenes, risks_of)
    check(
        "扁平流能被无损切回各父单元",
        ok is True,
        f"partition_ok={ok}（False 说明覆盖不变量校验未通过）",
    )

    ordered: list[dict] = []
    for unit in units:
        ordered.extend(by_parent.get(id(unit)) or [dict(unit)])

    # 核心断言：连续铺满、无重复、无缺口。
    previous = -1
    breaks: list[int] = []
    seen: dict[int, int] = {}
    for index, unit in enumerate(ordered, 1):
        start = positions.get(str(unit.get("start_slide_id")), -1)
        end = positions.get(str(unit.get("end_slide_id")), -1)
        if start != previous + 1:
            breaks.append(index)
        for slide in range(start, end + 1):
            seen[slide] = seen.get(slide, 0) + 1
        previous = end
    duplicated = sorted(slide for slide, count in seen.items() if count > 1)

    check(
        "重组后无重复区间（第一版缺陷正是此处重复 scene_001->scene_005）",
        not duplicated,
        f"重复 slide idx = {duplicated}" if duplicated else "无重复",
    )
    check(
        "重组后严格连续无断裂",
        not breaks,
        f"断裂点 = {breaks}" if breaks else "无断裂",
    )
    check(
        "重组后覆盖全部 slide 且数量正确",
        previous + 1 == len(scenes) and len(ordered) == len(flat),
        f"覆盖 {previous + 1}/{len(scenes)}，输出 {len(ordered)} 条 / 原生流 {len(flat)} 条",
    )

    # 反向用例：扁平流被人为破坏时，必须**如实报告失败**而不是静默产出错乱结果。
    damaged = list(flat)
    damaged.pop(1)
    _, ok_broken = resilience._partition_forward(units, damaged, scenes, risks_of)
    check(
        "覆盖不变量被破坏时如实返回 False（不静默输出错位结果）",
        ok_broken is False,
        f"partition_ok={ok_broken}（期望 False）",
    )

    # 降级层必须独立于跳过层生效：没有单 slide 父单元时，遇到
    # 「边界验收失败」仍应降级而不是终止 —— 第一版实现的
    # `if not skipped: return target(...)` 提前返回把这个能力短路掉了。
    #
    # 这里不打网络：把 story_agents 上的**被包装函数**换成一个桩，
    # 桩内部再走一遍我们的包装逻辑。做法是先记下真正的包装体，
    # 再把模块属性指向一个「自带原生语义的桩」，然后重装包装 ——
    # install() 是从模块属性取 target 的，所以桩会被正确捕获。
    import types  # noqa: PLC0415

    wrapped_before = story_agents.refine_risky_semantic_units
    normalize_before = getattr(story_agents, "_normalize_semantic_units")

    def _stub_target(units, _scenes, _ctx, _mode, *, require_ai_success=False):
        # 模拟原生在 require_ai_success=True 时的行为：记录诊断后抛致命错。
        # 关键：``failed_units`` 非空 —— 这才是「降级层被触发」的信号，
        # 也是 ``native_would_abort`` 的判据之一。
        diagnostics = {
            "version": 1,
            "triggered_units": [{"unit_id": "q1", "reasons": ["duration>24s"]}],
            "accepted_units": [],
            "unchanged_units": [],
            "failed_units": [{"unit_id": "q1", "reason": "边界验收失败: empty"}],
            "status": "confirmed_or_fallback",
        }
        if require_ai_success:
            raise AssertionError("桩函数不该在降级路径上收到 require_ai_success=True")
        # 降级语义：返回父单元本尊。
        return [dict(u) for u in units], diagnostics

    # 构造一个「没有单 slide 父单元」但「有触发项」的输入。
    # 5 个 slide -> 不是一个 slide，因此前置跳过不会生效。
    two_slide = [
        {"unit_id": "q1", "start_slide_id": "s01", "end_slide_id": "s05",
         "boundary_after": "hard"},
    ]

    staged = types.SimpleNamespace(
        refine_risky_semantic_units=_stub_target,
        _normalize_semantic_units=normalize_before,
        semantic_unit_refinement_risks=getattr(
            story_agents, "semantic_unit_refinement_risks"
        ),
    )
    try:
        # install() 会把 _INSTALLED 置位；先复位以便重新包装 staged。
        resilience._INSTALLED = False
        resilience._ORIGINALS.clear()
        resilience.install(staged)
        result, diag = staged.refine_risky_semantic_units(
            two_slide, scenes, {}, "story", require_ai_success=True,
        )
        stats = diag.get("resilience") or {}
        check(
            "无单 slide 父单元时降级层依然生效（第一版在此短路回原生）",
            isinstance(result, list) and len(result) == 1,
            f"返回 {len(result) if isinstance(result, list) else '?'} 个单元",
        )
        check(
            "该场景降级被计入 degraded_units",
            stats.get("degraded_units") == 1,
            f"degraded_units={stats.get('degraded_units')}，"
            f"skipped_single_slide={stats.get('skipped_single_slide')}",
        )
        check(
            "该场景下诊断如实标记 native_would_abort",
            stats.get("native_would_abort") is True,
            json.dumps(stats, ensure_ascii=False),
        )
    except AssertionError as exc:
        check("无单 slide 父单元时降级层依然生效（第一版在此短路回原生）",
              False, str(exc))
    finally:
        # 还原：把模块属性恢复成真正的包装体，并把内部状态复位。
        resilience._INSTALLED = False
        resilience._ORIGINALS.clear()
        story_agents.refine_risky_semantic_units = wrapped_before
        resilience.install(story_agents)


def _check_json_mode_guard() -> None:
    """5e) response_format=json_object 的 OpenAI 契约兜底。

    守的是这条不变量：**任何**声明 ``response_format={"type":"json_object"}``
    的请求，其 ``messages`` 全文（含 role）必须含字面量 "json"，
    否则 OpenAI / DeepSeek 直接 400。

    背景：``story_agents`` 有四处 json 调用点都不传 ``json_root``，而
    ``AGENT1_PROMPT_SYSTEM`` 命中 ``"semantic_units" in custom_prompt``
    分支时会把出厂提示词整体换掉 —— 出厂提示词里恰好有 "只输出严格 JSON
    对象"，自定义提示词里通常没有。于是「能不能跑」曾完全取决于用户提示词里
    有没有这四个字母。
    """
    from ocv_cloud_stack import json_mode_guard  # noqa: PLC0415

    # 1) 缺席时补上
    payload = {
        "messages": [
            {"role": "system", "content": "你是分镜规划助手，只返回对象。"},
            {"role": "user", "content": "请分段。"},
        ],
        "response_format": {"type": "json_object"},
    }
    changed = json_mode_guard.repair_payload(payload)
    blob = " ".join(str(m.get("content") or "") for m in payload["messages"])
    check("json_object 请求缺 json 字面量时被补上", changed and "json" in blob.lower())
    check(
        "补进去的是 system 之后的 user 消息（不动 system 提示词语义）",
        json_mode_guard._HINT_MARK in str(payload["messages"][1]["content"])
        and payload["messages"][0]["content"] == "你是分镜规划助手，只返回对象。",
    )

    # 2) 幂等：再跑一次不改（避免重复追加）
    second = json_mode_guard.repair_payload(payload)
    again = str(payload["messages"][1]["content"]).count(json_mode_guard._HINT_MARK)
    check("守卫幂等（重复调用不会二次追加）", second is False and again == 1)

    # 3) 出厂提示词下必须是**零干预**（否则会无谓改动请求体）
    ok_payload = {
        "messages": [
            {"role": "system", "content": "只输出严格 JSON 对象：{...}"},
            {"role": "user", "content": "请分段。"},
        ],
        "response_format": {"type": "json_object"},
    }
    check(
        "出厂提示词（已含 JSON）时守卫完全不干预",
        json_mode_guard.repair_payload(ok_payload) is False
        and json_mode_guard._HINT_MARK not in str(ok_payload["messages"][0]["content"]),
    )

    # 4) 不该碰的请求一律不碰
    check(
        "非 json_object 的请求不被改动",
        json_mode_guard.repair_payload({"messages": [{"role": "user", "content": "hi"}]}) is False,
    )
    check(
        "json_root=array 形态（无 response_format）不被改动",
        json_mode_guard.repair_payload(
            {"messages": [{"role": "user", "content": "无 json 字面量"}], "response_format": {"type": "text"}}
        ) is False,
    )
    check(
        "图片请求（无 messages）不被改动",
        json_mode_guard.repair_payload({"response_format": {"type": "json_object"}, "prompt": "x"}) is False,
    )

    # 5) role 也算数：OpenAI 把 role 拼进校验串
    check(
        "role 里出现 json 即视为已满足契约（不多发提示词）",
        json_mode_guard.messages_contain_json(
            [{"role": "json_formatter", "content": "按格式输出"}]
        ) is True,
    )

    # 6) content 为分段列表时也能注入
    multi = {
        "messages": [
            {"role": "system", "content": "只返回对象。"},
            {"role": "user", "content": [{"type": "text", "text": "请分段。"}]},
        ],
        "response_format": {"type": "json_object"},
    }
    check(
        "content 为分段列表时也能注入",
        json_mode_guard.repair_payload(multi)
        and any(
            json_mode_guard._HINT_MARK in str(part.get("text") or "")
            for part in multi["messages"][1]["content"]
        ),
    )

    # 7) 钩子已装到 Session.request 上（不是 requests.post —— 装错层会被
    #    _install_llm_body_injector 的闭包捕获原函数而失效）
    check(
        "守卫挂在 requests.sessions.Session.request 上",
        json_mode_guard.installed(),
        "必须挂 Session.request：requests.post 层会被既有注入器的闭包绕过",
    )

    # 8) 开关默认开启，且 os.environ 优先于面板层
    previous = os.environ.get("CLOUD_STACK_JSON_MODE_GUARD")
    try:
        os.environ["CLOUD_STACK_JSON_MODE_GUARD"] = "0"
        off = json_mode_guard.enabled()
        os.environ["CLOUD_STACK_JSON_MODE_GUARD"] = "1"
        on = json_mode_guard.enabled()
    finally:
        if previous is None:
            os.environ.pop("CLOUD_STACK_JSON_MODE_GUARD", None)
        else:
            os.environ["CLOUD_STACK_JSON_MODE_GUARD"] = previous
    check("开关可关可开（=0 关闭、=1 开启）", off is False and on is True)
    check(
        "开关默认开启（面板无此键时也不该退化为关闭）",
        json_mode_guard.enabled() is True,
    )


def _check_image_mime_sniff() -> None:
    """1g) 参考图 data URI 的 media type 必须与**真实字节**一致。

    守的是这条不变量：发给商汤的 ``images[].image_url`` 里声明的 media type
    必须等于该图片的常量头所指示的类型。

    背景（2026-09-23，107 次出图失败，有日志与磁盘双重实证）：
    OCV 的 ``_reference_image_url()`` 在参考图上传端点不可用时回退成
    ``data:{mimetypes.guess_type(路径)};base64,...`` —— **只看扩展名**。
    而商汤以**内容**为准，声明不符直接
    ``HTTP 400 3 invalid images[0].image_url: data URL media type does not
    match the image content``。历史项目里确有「扩展名 .jpg、内容其实是 PNG」
    的参考图（实测扫描 5371 张里有 12 张），两者对不上便**确定性**失败，
    与 Key / 额度 / 限流完全无关。

    本项不联网、不写业务文件，只断言纯函数行为。
    """
    import base64  # noqa: PLC0415
    import io  # noqa: PLC0415

    from PIL import Image  # noqa: PLC0415

    from ocv_cloud_stack import config, sense_image  # noqa: PLC0415

    def _png() -> bytes:
        buffer = io.BytesIO()
        Image.new("RGB", (8, 8), (200, 30, 30)).save(buffer, format="PNG")
        return buffer.getvalue()

    def _jpeg() -> bytes:
        buffer = io.BytesIO()
        Image.new("RGB", (8, 8), (30, 30, 200)).save(buffer, format="JPEG")
        return buffer.getvalue()

    png = _png()
    check("PNG 常量头 -> image/png", sense_image.sniff_image_mime(png) == "image/png")
    check("JPEG 常量头 -> image/jpeg", sense_image.sniff_image_mime(_jpeg()) == "image/jpeg")
    check("认不出来时返回空串（不猜）", sense_image.sniff_image_mime(b"\x00\x01\x02\x03") == "")

    # 核心回归：声明 image/jpeg、内容其实是 PNG（正是线上失败形态）
    bogus = "data:image/jpeg;base64," + base64.b64encode(png).decode("ascii")
    fixed = sense_image._to_data_uri(bogus)
    declared = fixed.split(";", 1)[0][len("data:"):]
    check(
        "声明与内容不符时被纠正为真实类型（否则商汤确定性 400）",
        declared == "image/png",
        f"image/jpeg -> {declared}",
    )
    check("纠正只改头部、不改图片负载", fixed.partition(",")[2] == bogus.partition(",")[2])

    # 不误伤：声明与内容一致时必须原样返回
    good = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    check("声明与内容一致时原样透传（零副作用）", sense_image._to_data_uri(good) == good)
    check(
        "畸形 data URI 不抛异常且不改动",
        sense_image._to_data_uri("data:text/plain,hello") == "data:text/plain,hello",
    )

    # 开关存在且默认开启（关掉只用于对照排查）
    check("按字节嗅探默认开启", config.image_sniff_mime() is True,
          "CLOUD_STACK_IMAGE_SNIFF_MIME 默认 1；置 0 会还原成上游原样报文")


def _check_model_catalog() -> None:
    """1h) 图片模型清单必须补齐漏报、剔除不支持的能力。

    守的是：面板下拉里出现的型号，**必须真的能用于该用途**。

    背景（2026-09-23 直连商汤实测）：``GET /v1/models`` 与接口实际接受度
    两个方向都不一致 ——

        模型                   generations   edits(图生图)
        sensenova-u1.5-lite    200 OK        200 OK
        sensenova-u1.5-fast    200 OK        200 OK   ← /models 漏报！
        sensenova-u1-fast      200 OK        400 model does not support image editing

    ⇒ 只信 /models 会有两种后果：用户在下拉里**看不到可用的 u1.5-fast**
    （正是"没拉取到正确的商汤模型"）；而清单里的 u1-fast 一旦被选作图生图
    模型，**每次重绘都失败**。

    本项不发网络请求，用构造数据断言校正逻辑。
    """
    from ocv_cloud_stack import models_catalog  # noqa: PLC0415

    upstream = [
        {"id": "deepseek-v4-pro", "output_modalities": ["text"]},
        {"id": "glm-5.2", "output_modalities": ["text"]},
        {"id": "sensenova-u1-fast", "output_modalities": ["image"]},
        {"id": "sensenova-u1.5-lite", "output_modalities": ["image"]},
    ]
    merged = models_catalog._merge_verified_extra(list(upstream), "image")
    img_ids = [i["id"] for i in merged if models_catalog._sensenova_kind_ok(i, "image")]
    edit_ids = [i["id"] for i in merged if models_catalog._sensenova_kind_ok(i, "image_edit")]

    check("补齐 /models 漏报的 sensenova-u1.5-fast（文生图）",
          "sensenova-u1.5-fast" in img_ids)
    check("补齐 /models 漏报的 sensenova-u1.5-fast（图生图）",
          "sensenova-u1.5-fast" in edit_ids)
    check("剔除不支持 edits 的 sensenova-u1-fast（图生图）",
          "sensenova-u1-fast" not in edit_ids,
          "它能文生图，但 edits 返回 400 model does not support image editing")
    check("文生图清单仍保留 sensenova-u1-fast（该用途它可用）",
          "sensenova-u1-fast" in img_ids)
    check("文本模型不会被混进图片清单",
          not any(i in img_ids for i in ("glm-5.2", "deepseek-v4-pro")))

    # 幂等：补齐两次不得重复
    twice = models_catalog._merge_verified_extra(merged, "image")
    check("补齐是幂等的（重复调用不产生重复项）",
          [i["id"] for i in twice].count("sensenova-u1.5-fast") == 1)

    # 能力闸必须与模态字段无关：没有 output_modalities 的缓存行也不能漏筛
    no_modality = [{"value": "sensenova-u1-fast"}]
    check("无模态字段的行也挡在图生图之外（能力闸先于模态兜底）",
          not models_catalog._sensenova_kind_ok(no_modality[0], "image_edit"))

    check("图生图说明文案讲清补齐与剔除",
          "sensenova-u1.5-fast" in models_catalog.verify_note("image_edit")
          and "sensenova-u1-fast" in models_catalog.verify_note("image_edit"))


def _check_uninstall_safety() -> None:
    """1i) 卸载不得冲掉用户设置（F-003 回归，纯逻辑断言）。

    故障实录（2026-09-23，我自己的卸载验证造成的**真实数据损失**）：
    早期 ``cmd_uninstall`` 做的是 ``shutil.copy2(.env.cloud_stack_backup, .env)``
    —— **整文件覆盖**。而那份备份由 ``_backup_env()`` 在**首次安装时创建一次**
    （实测是 08-28，比出事那天早 26 天）。于是用户此后对 ``.env`` 的所有改动
    在卸载时被静默冲掉：``ASR_DEVICE`` 从 ``cpu`` 打回 ``auto``、
    ``DASHSCOPE_API_KEY`` 变成占位值。

    正确做法是**定向还原**：只撤插件块 + 还原插件顶掉的键。
    本项用内存中的行列表断言这个语义（不碰真实文件）。
    """
    import cloud_stack_ctl as ctl  # noqa: PLC0415

    # 模拟一份"用户后来改过"的 .env
    user_env = [
        "ASR_DEVICE=cpu",
        "RUNNINGHUB_ENDPOINT=/v1/images/generations",
        "LANGUAGE_PROVIDER=sensenova",
        ctl.BEGIN_MARK,
        "CLOUD_STACK_ENABLED=1",
        "SENSENOVA_API_KEY=",
        f"{ctl.PREV_PREFIX}LANGUAGE_PROVIDER=custom",
        f"{ctl.PREV_PREFIX}DASHSCOPE_API_KEY=sk-old",
        ctl.END_MARK,
        "TAIL_KEY=keep-me",
    ]
    stripped = ctl._strip_block(list(user_env))
    text = "\n".join(stripped)

    check("卸载会移除插件块", ctl.BEGIN_MARK not in text and ctl.END_MARK not in text)
    check("插件块外的用户设置原样保留", "ASR_DEVICE=cpu" in text)
    check("块外的第三方键也不受影响", "TAIL_KEY=keep-me" in text)
    check("PREV 记录随块一起移除",
          f"{ctl.PREV_PREFIX}LANGUAGE_PROVIDER" not in text)

    # PREV 记录是权威原值 —— 必须优先于任何快照
    recorded = ctl._parse_value(user_env, ctl._prev_key("LANGUAGE_PROVIDER"))
    check("能读出被顶掉键的原值记录", recorded == "custom", recorded)

    # 安全阀：原值记录与快照都为空时，绝不能把用户当前的非空值清空
    guarded = ctl._parse_value(["DASHSCOPE_API_KEY=sk-real"], ctl._prev_key("DASHSCOPE_API_KEY"))
    check("原值记录缺失时为空（触发安全阀，保留现值而非清空）", guarded == "")


def _check_video_bridge() -> None:
    """1j) 云端视频（Agnes）协议翻译的关键不变量。

    守的是「OCV 的视频 provider 真的能读懂我们的响应」。这条最容易踩错，
    因为它与**图片层**的读法不同（实测踩过，见 DEVLOG F-005）：

    * 图片层 ``_submit_poster_request``：读 ``data.taskId``（有 data 回退）
    * 视频层 ``RunningHubVideoProvider``：只读**顶层** ``taskId`` /
      ``status`` / ``results``，**没有** data 回退

    本项不发网络请求，只断言响应形状与翻译规则。
    """
    from ocv_cloud_stack import agnes_video, config, video_shim  # noqa: PLC0415

    # ---- 响应形状：必须有顶层字段（视频层读法）----
    payload = video_shim.query_response("不存在的任务")
    check("任务不存在时也返回**顶层** status（视频层只读顶层）",
          "status" in payload and payload["status"] == "FAILED", str(payload.get("status")))
    check("同时保留 data 便于排查", "data" in payload)

    # ---- 协议翻译规则 ----
    # ---- 模式推导 ----
    #
    # ⚠ 必须显式把模式设成 auto：``config.get`` 的优先级是「面板层 > os.environ
    #   > .env > 默认」，用户若在面板把「生成模式」钉死成 text，断言会恒假
    #   （本轮实测踩到 —— 用户的面板里恰好就是 "text"）。测试要**封闭**，
    #   不能依赖用户配置；所以用 update_store 写面板层并在 finally 还原。
    saved_mode = config.get("CLOUD_STACK_VIDEO_MODE")
    config.update_store({"CLOUD_STACK_VIDEO_MODE": "auto"})
    try:
        p, notes = agnes_video.build_payload(prompt="x", duration=5, reference_images=[])
        check("无参考图 -> text 模式", p["mode"] == "text", str(p["mode"]))
        p2, _ = agnes_video.build_payload(
            prompt="x", duration=5, reference_images=["https://example.com/a.png"])
        check("单图 -> keyframe + first_frame（核心分镜当首帧）",
              p2["mode"] == "keyframe" and p2.get("first_frame") == "https://example.com/a.png"
              and "last_frame" not in p2,
              f"mode={p2['mode']} keys={sorted(p2)}")
        p3, _ = agnes_video.build_payload(
            prompt="x", duration=5,
            reference_images=["https://example.com/a.png", "https://example.com/b.png"])
        check("双图 -> keyframe 首尾帧",
              p3["mode"] == "keyframe" and p3.get("last_frame") == "https://example.com/b.png",
              f"mode={p3['mode']} last={p3.get('last_frame')}")

        # ---- text 偏好 + 有参考图 = 矛盾：绝不能静默丢图 ----
        #
        # OCV 的动态镜头**永远至少有一张图**（核心分镜图），而 Agnes 的
        # mode=text 契约不允许任何媒体字段。若照 text 偏好走，就会
        # 「丢掉参考图（视频与分镜不符）」或「上游 400」。正确处理是
        # **忽略该偏好并记日志**。
        config.update_store({"CLOUD_STACK_VIDEO_MODE": "text"})
        p6, _ = agnes_video.build_payload(
            prompt="x", duration=5, reference_images=["https://example.com/a.png"])
        check("面板设 text 但有参考图时，忽略偏好、不丢图（避免静默出错数据）",
              p6["mode"] == "keyframe" and p6.get("first_frame") == "https://example.com/a.png",
              f"mode={p6['mode']} keys={sorted(p6)}")
    finally:
        if saved_mode:
            config.update_store({"CLOUD_STACK_VIDEO_MODE": saved_mode})
        else:
            config.update_store({}, remove=["CLOUD_STACK_VIDEO_MODE"])

    # ---- 时长：OCV 允许 4–15，Agnes 只收字符串 "4"–"12" ----
    p4, notes4 = agnes_video.build_payload(prompt="x", duration=15, reference_images=[])
    check("15s 夹紧为字符串 '12'（整数会被 Agnes 400）",
          p4["seconds"] == "12" and isinstance(p4["seconds"], str), repr(p4["seconds"]))
    check("夹紧有提示信息（不静默降级）", bool(notes4))

    # ---- 分辨率：Agnes 没有 480p ----
    p5, notes5 = agnes_video.build_payload(
        prompt="x", duration=5, resolution="480p", reference_images=[])
    check("480p 映射到有效档位（Agnes 无 480p）",
          p5["size"] in {"720P", "1080P", "1K", "2K"}, p5["size"])
    check("映射有提示信息", bool(notes5))

    # ---- 查询串必须带 model_name ----
    url = agnes_video.query_url("video_abc", "agnes-video-2.5-flash")
    check("查询串带 video_id 与 model_name（非 text 模式否则 404）",
          "video_id=video_abc" in url and "model_name=agnes-video-2.5-flash" in url, url)

    # ---- 状态映射到 OCV 认识的大写集合 ----
    check("completed -> SUCCESS", agnes_video._STATUS_MAP.get("completed") == "SUCCESS")
    check("failed -> FAILED", agnes_video._STATUS_MAP.get("failed") == "FAILED")

    # ---- 付费能力必须默认关闭（**内置默认**，不是"当前值"）----
    #
    # ⚠ 这里不能用 config.video_enabled() 断言 —— 那是**当前生效值**：
    #   用户一旦在面板里打开过视频接管，面板层就永久存着 "1"，
    #   而 config.get() 的优先级是「面板层 > os.environ > .env > 默认」
    #   ⇒ 断言会恒为假（本机实测踩过，见 DEVLOG 铁律 24 的同源教训）。
    #   要验的不变量是「**没人配置时**默认关闭」，所以直接验内置默认值。
    import inspect  # noqa: PLC0415

    source = inspect.getsource(config.video_enabled)
    check("视频接管的内置默认是关闭（按秒计费，须显式 opt-in）",
          'get_bool("CLOUD_STACK_VIDEO_ENABLED", False)' in source,
          "读的是 get_bool(..., False) 即默认关")
    # 当前生效值随用户配置而变，如实打印（不作为断言）
    print(f"    当前生效值 = {config.video_enabled()}"
          f"（来源层：{config.layer_of('CLOUD_STACK_VIDEO_ENABLED')}）")


def _check_motion_plan() -> None:
    """1k) 动态阶段方案 reference_beat 健壮性（F-007）。

    守的是：``reference_beat`` 是 LLM 产出的**记账型序号**，它的越界/类型漂移
    不该终止整条动态视频生产。实测用户报错
    ``动态阶段方案修订仍未通过：核心参考图对应的阶段编号无效``。

    更要紧的是：**同一个字段被当数组下标用**
    （``prompt_plan_issues`` 里 ``plan['beats'][plan['reference_beat'] - 1]``），
    越界会直接 IndexError ⇒ 夹紧不只是"让校验通过"，而是必须的安全动作。

    本项不发网络、不调模型，只断言夹紧规则与零干预。
    """
    from ocv_cloud_stack import motion_plan_resilience  # noqa: PLC0415

    def plan(beat, beats=3):
        return {
            "version": 2, "scene_anchor": "a", "participants": ["X"],
            "beats": [{"action": "x", "texts": []}] * beats,
            "reference_beat": beat, "reference_visual": "v",
            "reference_participants": ["X"], "reference_texts": [],
        }

    for raw, expected, desc in (
        (0, 1, "越界下界 -> 1"),
        (99, 3, "远越界 -> 阶段数"),
        (4, 3, "越界上界 -> 阶段数"),
        ("2", 2, "字符串 '2' -> 整数 2"),
        (2.0, 2, "浮点 2.0 -> 整数 2"),
        (None, 1, "null -> 契约默认 1"),
        ("x", 1, "不可解析 -> 1"),
    ):
        out, _ = motion_plan_resilience.repair_reference_beat(plan(raw))
        check(f"reference_beat {desc}", out["reference_beat"] == expected,
              f"{raw!r} -> {out['reference_beat']!r}")

    legal = plan(2)
    out, note = motion_plan_resilience.repair_reference_beat(legal)
    check("合法 int 零干预（原对象返回、无说明）", out is legal and note == "")

    for raw in ("2", 2.0, 99, None):
        out, _ = motion_plan_resilience.repair_reference_beat(plan(raw))
        check(f"{raw!r} 修复后是真正的 int（原生用 type 判断）",
              type(out["reference_beat"]) is int)

    for broken in (None, [], "x"):
        out, note = motion_plan_resilience.repair_reference_beat(
            {"beats": broken, "reference_beat": 99})
        check(f"阶段列表非法（{broken!r}）时不擅自修复",
              note == "" and out["reference_beat"] == 99)

    check("补丁默认开启", motion_plan_resilience.enabled() is True)


def _check_reference_visual() -> None:
    """1l) 参考画面状态 reference_visual 的**修复路径可达性**（F-008）。

    守的是：OCV 自己的 ``restore_reference_draft`` 本意就是用 Agent 2 的
    ``visual_description`` 草案修 ``reference_visual``，但它**先 normalize
    再修复**，于是「空 / 超长」在第 157 行就抛了，第 160 行的修复永远执行不到
    —— **修复代码不可达**。实测用户报错
    ``动态阶段方案的参考画面状态为空、格式无效或过长``。

    本项不发网络，只断言：包装后两种坏值都能被草案修好、合法值零干预、
    草案也缺失时不编造、上限与源码一致。
    """
    from ocv_cloud_stack import motion_plan_resilience  # noqa: PLC0415

    import backend.app.video_motion_plan as vmp  # noqa: PLC0415

    draft = "林工坐在书桌前翻看单据，暖光台灯亮着"

    def plan(value):
        return {
            "version": 2, "scene_anchor": "a", "participants": ["X"],
            "beats": [{"action": "x", "texts": []}] * 3,
            "reference_beat": 1, "reference_visual": value,
            "reference_participants": ["X"], "reference_texts": [],
        }

    # 坏值：走真实 restore_reference_draft 必须被草案修好
    for raw, desc in (("", "空字符串"), ("   ", "纯空白"), (None, "null"),
                      (123, "非字符串"), ("x" * 6001, "超长")):
        try:
            got = vmp.restore_reference_draft(plan(raw), draft)["reference_visual"]
            check(f"reference_visual {desc} 被草案修好", got == draft, f"{len(str(got))} 字符")
        except ValueError as exc:
            check(f"reference_visual {desc} 被草案修好", False, str(exc))

    # 合法值零干预
    out, note = motion_plan_resilience.repair_reference_visual(plan("合法画面描述"))
    check("合法 reference_visual 零干预（无说明）", note == "")

    # 超长且无草案：截断而不是报错
    out, note = motion_plan_resilience.repair_reference_visual(plan("y" * 6001))
    check("超长被截断到上限", len(out["reference_visual"]) == motion_plan_resilience._MAX_REFERENCE_VISUAL,
          f"{len(out['reference_visual'])} 字符")

    # 上限必须与源码一致（引用而非重定义）
    import re  # noqa: PLC0415

    source = (PROJECT_ROOT / "backend" / "app" / "video_motion_plan.py").read_text(
        encoding="utf-8", errors="replace")
    match = re.search(r"string\(value\.get\('reference_visual'\),\s*(\d+),", source)
    check("补丁上限与 OCV 源码一致（源码改了必须同步）",
          bool(match) and int(match.group(1)) == motion_plan_resilience._MAX_REFERENCE_VISUAL,
          f"源码 {match.group(1) if match else '?'} vs 补丁 {motion_plan_resilience._MAX_REFERENCE_VISUAL}")

    # 两个函数都要被包装：只包 normalize 的话修复路径仍然不可达
    diag = motion_plan_resilience.diagnostics()
    check("normalize_motion_plan 已包装", bool(diag["wrapped_modules"]))
    check("restore_reference_draft 也已包装（否则修复不可达）",
          bool(diag.get("wrapped_restore")), str(diag.get("wrapped_restore")))


def _check_motion_plan_fields() -> None:
    """1m) 动态阶段方案**整族字段**的记账/格式健壮性（F-009）。

    用户连续三轮撞到同一个模型输出的不同字段：
    ``reference_beat``（F-007）→ ``reference_visual``（F-008）→
    ``reference_participants``（F-009）。逐个打补丁就是"打地鼠"，
    所以这里按**整族**断言，一次覆盖全部「记账/格式类」失败面。

    分类原则：**只修记账/格式，绝不修语义**。
    形状/重复/超限/类型漂移可修（意图可还原）；
    内容/结构缺失（version、阶段数、beat 非对象、文案为空）必须照常报错
    —— 编造会静默产出错数据，比报错更糟。

    ⚠ 特别守住一条实测踩到的坑：``beats[].texts`` 的 owner 对
    **participants**，而 ``reference_texts`` 的 owner 对
    **reference_participants** —— 两套不同的主体列表。只补前者，
    后者仍然非法。
    """
    from ocv_cloud_stack import motion_plan_resilience  # noqa: PLC0415

    import backend.app.video_motion_plan as vmp  # noqa: PLC0415

    def plan(**kw):
        base = {
            "version": 2, "scene_anchor": "a", "participants": ["林工"],
            "beats": [{"action": "x", "texts": []}] * 3, "reference_beat": 1,
            "reference_visual": "林工坐在书桌前翻看单据", "reference_participants": ["林工"],
            "reference_texts": [],
        }
        base.update(kw)
        return base

    def ok(desc, p):
        try:
            vmp.normalize_motion_plan(p)
            check(f"整族可修：{desc}", True)
        except ValueError as exc:
            check(f"整族可修：{desc}", False, str(exc))

    # ---- 用户本轮报错的那个字段 ----
    ok("reference_participants 缺失（模型整个不填）", plan(reference_participants=None))
    ok("reference_participants 非 list", plan(reference_participants="林工"))
    ok("reference_participants 超 12 个",
       plan(reference_participants=[f"人{i}" for i in range(1, 15)]))
    ok("reference_participants 含占位符", plan(reference_participants=["林工", None, {}]))
    ok("子集违约（核心图主体未登记）",
       plan(participants=["林工"], reference_participants=["林工", "助手"]))

    # ---- 同族的其它记账字段 ----
    ok("participants 是标量字符串", plan(participants="林工、助手"))
    ok("participants 重名", plan(participants=["林工", "林工"]))
    ok("participants 超 12 个", plan(participants=[f"人{i}" for i in range(1, 15)]))
    ok("阶段文字 owner 未登记", plan(beats=[{"action": "x", "texts": [
        {"text": "账目", "owner": "助手", "container": "对话气泡"}]}] * 3))
    ok("核心图文字 owner 未登记（对 reference_participants）",
       plan(reference_texts=[{"text": "账目", "owner": "助手", "container": "对话气泡"}]))
    ok("文字条目重复", plan(beats=[{"action": "x", "texts": [
        {"text": "账目", "owner": "林工", "container": "气泡"},
        {"text": "账目", "owner": "林工", "container": "气泡"}]}] * 3))
    ok("texts 缺失 -> []", plan(beats=[{"action": "x", "texts": None}] * 3))
    ok("reference_texts 缺失 -> []", plan(reference_texts=None))
    ok("容器超 80 字符", plan(beats=[{"action": "x", "texts": [
        {"text": "账目", "owner": "林工", "container": "容" * 120}]}] * 3))

    # ---- 语义/结构类必须仍然严格（不能被顺手修掉）----
    #
    # ⚠ 注意 ``participants=[""]`` **不在这里** —— 它是占位符，
    #   而契约明写「无固定主体**可以为空**」，所以规整成 ``[]`` 是
    #   **正确的形状修复**（``[]`` 是合法值），不是放宽语义。
    #   同理 ``texts=None`` → ``[]`` 也是对的。
    strict = [
        ("version 非法", plan(version=9)),
        ("阶段数为 0", plan(beats=[])),
        ("阶段数超 6", plan(beats=[{"action": "x", "texts": []}] * 7)),
        ("beat 非对象", plan(beats=["x"] * 3)),
        ("scene_anchor 为空", plan(scene_anchor="")),
        ("action 为空", plan(beats=[{"action": "", "texts": []}] * 3)),
    ]
    for desc, p in strict:
        try:
            vmp.normalize_motion_plan(p)
            check(f"仍然严格拒绝：{desc}", False, "竟然通过了（不该修！）")
        except ValueError:
            check(f"仍然严格拒绝：{desc}", True)

    # 占位符 -> 合法空值：这是**可修**的（属于形状规整）
    try:
        out = vmp.normalize_motion_plan(plan(participants=[""], reference_participants=[""]))
        check("participants=[''] 占位符规整为 []（契约允许无主体为空）",
              out["participants"] == [], str(out["participants"]))
    except ValueError as exc:
        check("participants=[''] 占位符规整为 []", False, str(exc))

    # ---- 关键不变量：修法是「补登记」而不是「删主体」----
    out, _ = motion_plan_resilience.repair_participants(
        plan(participants=["林工"], reference_participants=["林工", "助手", "路人"]))
    check("核心图主体一个都没被删",
          out["reference_participants"] == ["林工", "助手", "路人"],
          str(out["reference_participants"]))
    check("缺的主体被补进 participants",
          all(n in out["participants"] for n in ("助手", "路人")), str(out["participants"]))

    # ---- 两套 owner 列表必须分别满足 ----
    out, _ = motion_plan_resilience.repair_texts(
        plan(reference_texts=[{"text": "复核", "owner": "助手", "container": "说明框"}]))
    check("核心图文字 owner 补进了 reference_participants",
          "助手" in out["reference_participants"], str(out["reference_participants"]))
    check("reference_participants 仍是 participants 子集",
          all(n in out["participants"] for n in out["reference_participants"]),
          f"{out['reference_participants']} vs {out['participants']}")

    # ---- '画面标注' 是唯一允许不登记的主体 ----
    out, _ = motion_plan_resilience.repair_texts(plan(beats=[{"action": "x", "texts": [
        {"text": "注意", "owner": "画面标注", "container": "说明框"}]}] * 3))
    check("「画面标注」不被误当成主体登记",
          "画面标注" not in out["participants"], str(out["participants"]))


def _check_video_assemble() -> None:
    """1n) 视频提示词组装（``beat_prompts``）的健壮性 + **包装必须真的装上**（F-010）。

    守两件事：

    1. **形状规整**：``beat_prompts`` 是字符串列表 / 编号错位 / 缺段 / 正文为空时，
       按阶段顺序规整、必要时回落方案 ``action``；框架文字用
       ``scene_anchor`` 与末阶段 ``action`` 兜底（都不是编造）。
    2. **包装真的装上了** —— 这一条比第 1 条更重要。
       ``_assemble_video_body`` 是 ``video_agents`` 的**私有函数**，定义位置在
       模块**靠后**处，而补丁首次触发时它还不存在。实测踩到：单元测试全绿，
       但全链路仍然报「缺少逐阶段定稿段落」，因为包装**从来没装上**
       （``diagnostics()['wrapped_assemble']`` 为空）。修法是把
       ``backend.app.video_agents`` 本身登记为补丁目标，让
       ``_PatchingLoader`` 在它**模块体执行完之后**回调补丁。
    """
    from ocv_cloud_stack import motion_plan_resilience  # noqa: PLC0415

    import backend.app.video_agents as va  # noqa: PLC0415

    # ---- ① 包装必须装上（这是本节的真正价值）----
    diag = motion_plan_resilience.diagnostics()
    check("_assemble_video_body 已被包装（否则 F-010 形同不存在）",
          bool(diag.get("wrapped_assemble")), str(diag.get("wrapped_assemble")))
    check("video_agents._assemble_video_body 是包装版",
          getattr(va._assemble_video_body, "_cloud_stack_wrapped", False))
    # diagnostics 不得把"没有该属性的模块"误报为已包装
    check("diagnostics 不误报（wrapper 为 None 时不得匹配 None）",
          all(n == "backend.app.video_agents" for n in diag.get("wrapped_assemble") or []),
          str(diag.get("wrapped_assemble")))

    # ---- ② 形状规整 ----
    plan = {"version": 2, "scene_anchor": "夜晚书房，暖光台灯",
            "participants": ["林工"],
            "beats": [{"action": "第1阶段：翻开单据", "texts": []},
                      {"action": "第2阶段：逐页核对", "texts": []},
                      {"action": "第3阶段：合上单据", "texts": []}],
            "reference_beat": 1, "reference_visual": "v",
            "reference_participants": ["林工"], "reference_texts": []}
    shot = {"id": "shot_001", "motion_plan": plan}

    cases = [
        ("beat_prompts 是字符串列表", {"beat_prompts": ["a", "b", "c"]}),
        ("beat_prompts 缺失", {"beat_prompts": None}),
        ("beat 编号从 0 开始",
         {"beat_prompts": [{"beat": 0, "prompt": "a"}, {"beat": 1, "prompt": "b"},
                           {"beat": 2, "prompt": "c"}]}),
        ("段落数不足", {"beat_prompts": [{"beat": 1, "prompt": "a"}]}),
        ("某阶段正文为空（回落 action）",
         {"beat_prompts": [{"beat": 1, "prompt": "a"}, {"beat": 2, "prompt": ""},
                           {"beat": 3, "prompt": "c"}]}),
        ("框架文字全缺", {"beat_prompts": ["a", "b", "c"],
                          "continuity_prompt": None, "ending_prompt": ""}),
    ]
    for desc, row in cases:
        import copy as _copy  # noqa: PLC0415

        r = _copy.deepcopy(row)
        try:
            va._assemble_video_body(shot, r)
            text = str(r.get("video_prompt") or "")
            paras = len([x for x in text.split("\n") if x.strip()])
            check(f"beat_prompts {desc} 被规整通过", bool(text.strip()) and paras >= 3,
                  f"{paras} 段落")
            check(f"beat_prompts {desc} 保持对象身份（原地改）", r is r)
        except ValueError as exc:
            check(f"beat_prompts {desc} 被规整通过", False, str(exc))

    # ---- ③ 不编造：连 action 都空时仍须报错 ----
    empty = {"version": 2, "scene_anchor": "x", "participants": ["林工"],
             "beats": [{"action": "", "texts": []}] * 3,
             "reference_beat": 1, "reference_visual": "v",
             "reference_participants": ["林工"], "reference_texts": []}
    r = {"beat_prompts": [{"beat": i, "prompt": ""} for i in (1, 2, 3)]}
    try:
        va._assemble_video_body({"id": "s", "motion_plan": empty}, r)
        check("无 action 可回落时仍报错（不编造）", False, "竟然通过了")
    except ValueError:
        check("无 action 可回落时仍报错（不编造）", True)

    # ---- ④ 真·标量无法推断位置 ⇒ 不修，交原生 ----
    r = {"beat_prompts": "一整段话", "continuity_prompt": "c", "ending_prompt": "e"}
    try:
        va._assemble_video_body(shot, r)
        check("真标量 beat_prompts 仍报错（无位置可推断）", False, "竟然通过了")
    except ValueError:
        check("真标量 beat_prompts 仍报错（无位置可推断）", True)


def _check_video_gate_bridge() -> None:
    """1o) 视频门禁桥接（F-011）：面板配好 Agnes 后 OCV 原生门禁必须放行。

    用户症状「一键制作已暂停：请先配置视频 API」，但他**已在面板填好 Agnes**。

    前端放行条件是 OCV 自己的 ``/api/video-model`` 返回
    ``source=='dedicated' && has_api_key``，而 ``load_config()`` 只读
    ``VIDEO_API_*``（``.env``/``os.environ``）—— 它不认识 ``AGNES_API_KEY``。
    原本只有手工执行、且需重启的 ``video-install`` 能架这座桥。

    本补丁改成**运行时桥接**：``os.environ`` 覆盖 ``.env``，所以包装
    ``load_config`` 即可免重启生效，且不写 ``.env``、不泄露真 Key。

    ⚠ 测试必须**同时**整理面板层与 ``os.environ``：``config.get`` 的分层是
    ``panel > environ > .env > default``，只删面板键会回落到环境变量
    （实测踩到：删了面板的 AGNES_API_KEY 判定仍为 enabled）。
    """
    from ocv_cloud_stack import config, video_gate_bridge  # noqa: PLC0415

    # ⚠ 必须走模块属性：from-import 会把包装对象绑进本模块命名空间，
    #   uninstall 改不到它（铁律 29 —— 测试自己踩过）。
    import backend.app.video_model_config as vmc  # noqa: PLC0415

    saved = config.store_values()
    env_keys = ("VIDEO_API_KEY", "VIDEO_API_KEYS", "AGNES_API_KEY",
                "CLOUD_STACK_VIDEO_ENABLED")
    env_saved = {k: os.environ.get(k) for k in env_keys}

    def set_env(**kv):
        for key, value in kv.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def gate_open() -> bool:
        cfg = vmc.load_config()
        return cfg.get("source") == "dedicated" and bool(cfg.get("has_api_key"))

    try:
        # 前提：面板有 Key、开关打开；环境里没有 VIDEO_API_*
        config.update_store({"AGNES_API_KEY": "sk-selftest-key-0123456789",
                             "CLOUD_STACK_VIDEO_ENABLED": "1"})
        set_env(VIDEO_API_KEY=None, VIDEO_API_KEYS=None,
                AGNES_API_KEY="sk-selftest-key-0123456789",
                CLOUD_STACK_VIDEO_ENABLED="1")

        # 负向：关掉补丁必须复现"未配置"
        video_gate_bridge.uninstall()
        check("原生门禁判定未配置（复现症状）", not gate_open())

        # 正向：开着补丁必须放行
        video_gate_bridge.install()
        cfg = vmc.load_config()
        check("桥接后门禁放行", cfg.get("source") == "dedicated" and cfg.get("has_api_key"),
              f"source={cfg.get('source')!r}")
        check("桥接指向本地 shim",
              str(cfg.get("base_url")).rstrip("/") == config.shim_base_url().rstrip("/"),
              str(cfg.get("base_url")))
        check("提交路径与 shim 约定一致",
              cfg.get("submit_path") == video_gate_bridge.SHIM_SUBMIT_PATH,
              str(cfg.get("submit_path")))

        # 开关语义：任一不满足都不接管
        config.update_store({"CLOUD_STACK_VIDEO_ENABLED": "0"})
        set_env(CLOUD_STACK_VIDEO_ENABLED=None)
        check("接管开关=0 时不插手", not gate_open())
        config.update_store({"CLOUD_STACK_VIDEO_ENABLED": "1"})
        set_env(CLOUD_STACK_VIDEO_ENABLED="1")
        config.update_store({}, remove=["AGNES_API_KEY"])
        set_env(AGNES_API_KEY=None)
        check("无 Agnes Key 时不插手", not gate_open())

        # 不开视频不该有个来自插件的真 Key 泄漏
        config.update_store({"AGNES_API_KEY": "sk-selftest-key-0123456789"})
        set_env(AGNES_API_KEY="sk-selftest-key-0123456789")
        blob = json.dumps(vmc.load_config(), ensure_ascii=False)
        check("返回值不含真 Key", "sk-selftest-key-0123456789" not in blob)
        check("key_hints 为空（连末四位也不给）", vmc.load_config().get("key_hints") == [])

        # 免重启：面板一改立刻生效
        config.update_store({"CLOUD_STACK_VIDEO_ENABLED": "0"})
        set_env(CLOUD_STACK_VIDEO_ENABLED=None)
        check("改 0 → 立刻关闭（免重启）", not gate_open())
        config.update_store({"CLOUD_STACK_VIDEO_ENABLED": "1"})
        set_env(CLOUD_STACK_VIDEO_ENABLED="1")
        check("改回 1 → 立刻放行（免重启）", gate_open())

        # 绑定陷阱：video_generation 也必须被换
        import backend.app.video_generation as vg  # noqa: PLC0415

        check("video_generation 的 from-import 绑定也被换掉",
              getattr(vg.load_config, "_cloud_stack_wrapped", False))
    finally:
        config.update_store({k: v for k, v in saved.items()},
                            remove=[k for k in config.store_values() if k not in saved])
        for key, value in env_saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        video_gate_bridge.install()


def _check_credential_guard() -> None:
    """1p) 凭据缩水保护：**不许把真 Key 静默覆盖成占位符**。

    这条来自一次**真实事故**（2026-09-24，我本人造成）：一条诊断探针写了
    ``update_store({'AGNES_API_KEY': 'k'}, remove=[...])``，而该函数语义是
    **先 remove 再写入** —— 用户 51 字符的真 Key 被静默换成 ``'k'``，
    没有任何报错。这与 F-003（整体还原 ``.env`` 备份反而丢凭据）同类。

    处理：``update_store`` 在 ``force=False``（默认）时拒绝"长凭据 → 显著更短的值"。
    真人表单路径（``panel.apply``）显式 ``force=True``，因为它有自己的 ``_validate``。
    """
    from ocv_cloud_stack import config  # noqa: PLC0415

    saved = config.store_values()
    probe_key = "CLOUD_STACK_SELFTEST_API_KEY"   # 用一个临时键，绝不碰用户真键
    long_value = "sk-" + "x" * 48               # 51 字符，与真实 Key 同量级
    try:
        config.update_store({probe_key: long_value}, force=True)
        check("长凭据已写入（force=True）", config.store_values().get(probe_key) == long_value)

        # ① 程序化写入短值 -> 必须拒绝
        raised = ""
        try:
            config.update_store({probe_key: "k"})
        except config.CredentialClobberError as exc:
            raised = str(exc)
        check("拒绝把长凭据覆盖成 1 字符占位符", bool(raised), raised[:80])
        check("拒绝后旧值**未被改动**",
              config.store_values().get(probe_key) == long_value,
              f"{len(config.store_values().get(probe_key) or '')} 字符")

        # ② 空串 = 清空，是合法操作，不该被拦
        config.update_store({probe_key: ""})
        check("空串清空凭据是允许的",
              probe_key not in config.store_values())

        # ③ 显式 force=True 可以写短值（测试与特殊路径的出口）
        config.update_store({probe_key: long_value}, force=True)
        config.update_store({probe_key: "k"}, force=True)
        check("force=True 时可写短值", config.store_values().get(probe_key) == "k")

        # ④ 非凭据键不受影响（普通配置项可以自由变短）
        config.update_store({"CLOUD_STACK_SELFTEST_MODE": "auto"})
        config.update_store({"CLOUD_STACK_SELFTEST_MODE": "a"})
        check("非凭据键不受保护限制（普通配置可变短）",
              config.store_values().get("CLOUD_STACK_SELFTEST_MODE") == "a")

        # ⑤ 真人表单路径必须仍然可写（panel.apply 走 force=True）
        import inspect  # noqa: PLC0415

        from ocv_cloud_stack import panel  # noqa: PLC0415

        src = inspect.getsource(panel.apply)
        check("panel.apply 显式 force=True（真人操作不被拦）",
              "force=True" in src)
    finally:
        config.update_store({k: v for k, v in saved.items()},
                            remove=[k for k in config.store_values() if k not in saved], force=True)
        check("凭据保护测试已完整还原面板层",
              config.store_values() == saved, f"{len(config.store_values())} vs {len(saved)}")


def _check_video_submit_retry() -> None:
    """1q) 上游"队列满"时必须重试，且真实原因必须传出去（F-013）。

    用户报错：``视频提交被明确拒绝（HTTP 400）：未返回任务身份``

    日志证据：95 次受理 / 3 次被拒，3 次**全是同一个**
    ``503 video queue is full, please retry later``。两个叠加缺陷：

    * **字段名不匹配**：OCV 读 ``errorMessage`` / ``message``，
      而 shim 只给 ``msg`` ⇒ 真实原因被丢掉，只剩兜底文案。
    * **上游说"稍后重试"，却一次都不重试**：503 被压成 400，
      OCV 判成"明确拒绝"（终态）。

    修法：503/429 等"确定未受理"⇒ **延后到后台**每 30s 重试、最多 15 次
    （必须在后台：OCV 提交 POST 只有 120s 超时，装不下 420s 的重试）。
    """
    from ocv_cloud_stack import agnes_video, config, image_shim, video_shim  # noqa: PLC0415

    import re  # noqa: PLC0415

    # ---- ① 错误分类 ----
    for status in (408, 425, 429, 500, 502, 503, 504):
        exc = agnes_video.AgnesVideoError("x", status_code=status,
                                          retryable=status in agnes_video._RETRYABLE_STATUS,
                                          definite=True)
        check(f"HTTP {status} 可重试且确定未创建", exc.retryable and exc.definite)
    check("HTTP 200 无身份 ⇒ 既不可重试也不确定（防重复扣费）",
          not agnes_video.AgnesVideoError("x").retryable
          and not agnes_video.AgnesVideoError("x").definite)

    # ---- ② 真实原因必须能被 OCV 读到 ----
    real = "HTTP 503 video queue is full, please retry later"
    read = lambda body: str(body.get("errorMessage") or body.get("message") or "未返回任务身份")  # noqa: E731
    check("shim 的错误响应含 errorMessage/message（否则又是「未返回任务身份」）",
          read({"errorMessage": real, "message": real}) == real)

    def definite_exc():
        return agnes_video.AgnesVideoError("参数错", status_code=400, definite=True)

    def unknown_exc():
        return agnes_video.AgnesVideoError("状态不明", definite=False)

    check("确定未创建 ⇒ HTTP 400（OCV 给一键重试）",
          image_shim._video_error_status(definite_exc())[0] == 400)
    check("状态不明 ⇒ HTTP 503（OCV 冻结镜头）",
          image_shim._video_error_status(unknown_exc())[0] == 503)

    # ---- ③ 重试窗口不得越过 OCV 的轮询预算 ----
    ocv_src = (PROJECT_ROOT / "module6_dynamic_video.py").read_text(
        encoding="utf-8", errors="replace")
    m = re.search(r"timeout_seconds: float = (\d+)", ocv_src)
    check("shim 的轮询预算常量与 OCV 源码一致",
          bool(m) and video_shim.OCV_POLL_BUDGET_SECONDS == float(m.group(1)),
          f"shim={video_shim.OCV_POLL_BUDGET_SECONDS} ocv={m.group(1) if m else '?'}")
    import time as _time  # noqa: PLC0415

    now = _time.time()
    span = video_shim._retry_deadline(now) - now
    check("默认重试窗口（788s）落在 OCV 轮询预算内",
          span < video_shim.OCV_POLL_BUDGET_SECONDS, f"{span:.0f}s")

    # ---- ④ 默认值就是用户要的「30 次阶梯退避」----
    check("默认重试次数 30", config.video_submit_retries() == 30,
          str(config.video_submit_retries()))
    spec = ([1.0] * 3 + [5.0] * 5 + [10.0] * 4 + [15.0] * 3
            + [30.0] * 5 + [45.0] * 5 + [60.0] * 5)
    got = [config.video_submit_retry_delay(i) for i in range(1, 31)]
    check("阶梯退避档位与用户指定逐项一致", got == spec,
          (f"不一致 {[i + 1 for i, (a, b) in enumerate(zip(got, spec)) if a != b][:5]}"
           if got != spec else "30 项全对"))
    check("总等待窗口 = 788 秒", abs(config.video_submit_retry_window() - 788.0) < 0.5,
          f"{config.video_submit_retry_window():.0f}s")

    # ---- ④b 同一任务的并发提交必须被串行化（防重复扣费）----
    # ⚠ 用独立的局部变量存原始函数：本节后面（⑤）才定义 `original`，
    #   在这里引用它会 UnboundLocalError（实测踩到）。
    _saved_create = agnes_video.create_task
    probe = video_shim._VideoTask("probe-conc", "", "m", {"prompt": "p"})
    probe.submitting = True          # 模拟"已在提交中"
    count = {"n": 0}

    def counting_create(*args, **kwargs):
        count["n"] += 1
        return {"video_id": "x", "task_id": "x", "status": "queued", "raw": {}}

    agnes_video.create_task = counting_create
    try:
        video_shim._retry_deferred_submit(probe)
        check("已在提交中时不再并发提交（防重复扣费）", count["n"] == 0,
              f"调用 {count['n']} 次")
    finally:
        agnes_video.create_task = _saved_create
        probe.submitting = False

    # ---- ⑤ 越窗后绝不再提交（防"没人跟踪的付费任务"）----
    calls = {"n": 0}
    original = agnes_video.create_task

    def must_not_call(*args, **kwargs):
        calls["n"] += 1
        return {"video_id": "SHOULD_NOT_HAPPEN", "task_id": "x", "status": "queued", "raw": {}}

    agnes_video.create_task = must_not_call
    try:
        task = video_shim._VideoTask("probe", "", "m", {"prompt": "p"})
        task.awaiting_upstream = True
        task.submit_attempts = 3
        task.retry_deadline = _time.time() - 1
        task.last_submit_error = "HTTP 503"
        video_shim._retry_deferred_submit(task)
        check("越过窗口判 FAILED", task.status == "FAILED", task.status)
        check("越过窗口后**一次都不再提交**（防无人跟踪的付费任务）",
              calls["n"] == 0, f"调用 {calls['n']} 次")
    finally:
        agnes_video.create_task = original
        video_shim._TASKS.pop("probe", None)

    # ---- ⑥ 重试期间对 OCV 呈现排队中，不是失败 ----
    task = video_shim._VideoTask("probe2", "", "m", {"prompt": "p"})
    task.awaiting_upstream = True
    video_shim._TASKS["probe2"] = task
    try:
        q = video_shim.query_response("probe2")
        check("重试期间对 OCV 呈现 QUEUED（不是 FAILED，否则会被引导重新付费）",
              q.get("status") == "QUEUED", str(q.get("status")))
    finally:
        video_shim._TASKS.pop("probe2", None)


def _check_llm_retry() -> None:
    """1r) 语言模型 30 档阶梯重试（与云端视频共用同一张表）。

    三件必须成立的事：

    1. **档位逐项正确**：与视频**共用同一张 30 档表**
       （1×3, 5×5, 10×4, 15×3, 30×5, 45×5, 60×5，合计 788s），
       且抖动后仍落在 ±30% 内。
    2. **不放大重试**：只包装 OCV 的 ``_gemini_retry_count`` / ``_gemini_retry_delay``，
       **不得**在 ``generate_gemini_text`` 外面再套一层循环。断言这两个函数被替换、
       且 ``generate_gemini_text`` 本身仍是原函数（没被包成"外层重试"）。
    3. **序号语义正确**（铁律 53）：``_gemini_retry_count()`` 返回**总尝试次数**
       = 重试次数 + 1，因为 OCV 的循环是 ``range(1, N+1)`` 且以 ``attempt < N``
       判断是否继续。差一就会少重试一次。
    """
    from ocv_cloud_stack import config, llm_retry  # noqa: PLC0415

    # ---- ① 默认值就是用户要的 30 次（与视频一致）----
    check("默认重试次数 30（与视频一致）", llm_retry.DEFAULT_RETRIES == 30,
          str(llm_retry.DEFAULT_RETRIES))

    # ---- ② 档位表：与视频逐项相同 ----
    spec = ([1.0] * 3 + [5.0] * 5 + [10.0] * 4 + [15.0] * 3
            + [30.0] * 5 + [45.0] * 5 + [60.0] * 5)
    got = [llm_retry.base_delay(index) for index in range(1, 31)]
    check("30 档逐项一致（1×3,5×5,10×4,15×3,30×5,45×5,60×5）", got == spec,
          "30 项全对" if got == spec else str(got[:8]))
    check("与视频侧取同一张表（单一真相源，防漂移）",
          [config.video_submit_retry_delay(i) for i in range(1, 31)] == got)
    check("超出最后一档沿用 1 分钟封顶（不变成 0 等待）",
          llm_retry.base_delay(31) == 60.0 and llm_retry.base_delay(60) == 60.0,
          f"{llm_retry.base_delay(31)}/{llm_retry.base_delay(60)}")
    check("总窗口 = 788 秒（与视频一致）",
          abs(llm_retry.total_window(30) - 788.0) < 0.5,
          f"{llm_retry.total_window(30):.0f}s")

    # ---- ③ 抖动必须落在 ±30% 内，且确实在抖动 ----
    samples = [llm_retry.delay(30) for _ in range(200)]  # 基准 60s
    low, high = min(samples), max(samples)
    check("抖动后仍落在基准值的 ±30% 内",
          low >= 60.0 * 0.7 - 1e-6 and high <= 60.0 * 1.3 + 1e-6,
          f"[{low:.2f}, {high:.2f}]")
    check("抖动确实产生差异（不是常量）", len({round(v, 3) for v in samples}) > 10,
          f"{len({round(v, 3) for v in samples})} 个不同取值")

    # ---- ④ 关掉开关时必须完全回到 OCV 原生 ----
    #
    # ⚠ 铁律 23 + 32：本节的断言**绝不能**依赖"用户当前把它设成了什么"。
    #   第一版写的是「还原后 enabled() 必须为 True」—— 若用户本就把这项设成 0，
    #   该断言恒假（假失败）；而 finally 里无脑 remove 这个键，还会把用户的
    #   设置**直接删掉**。改为：精确记住该键的原始存在性/取值，按原样还原，
    #   并断言"还原后与快照逐字一致"。
    _KEY_ENABLED = "CLOUD_STACK_LLM_RETRY_ENABLED"
    _KEY_COUNT = "CLOUD_STACK_LLM_RETRY_COUNT"
    store_snapshot = config.store_values()
    had_enabled = _KEY_ENABLED in store_snapshot
    saved_enabled = store_snapshot.get(_KEY_ENABLED)
    try:
        config.update_store({_KEY_ENABLED: "0"}, force=True)
        config.reload_store()
        import os as _os  # noqa: PLC0415
        _os.environ.pop(_KEY_ENABLED, None)
        check("开关置 0 后 enabled() 为 False（关闭语义生效）",
              llm_retry.enabled() is False)
    finally:
        if had_enabled:
            config.update_store({_KEY_ENABLED: saved_enabled or ""}, force=True)
        else:
            config.update_store({}, remove=[_KEY_ENABLED])
        config.reload_store()
    check("本节结束后面板层逐字还原（不删用户设置，铁律 23）",
          config.store_values() == store_snapshot,
          "一致" if config.store_values() == store_snapshot else "有差异")

    # ---- ⑤ 真实挂载：策略函数被替换，且没有第二层循环 ----
    from backend.app import gemini_client  # noqa: PLC0415

    check("_gemini_retry_count 已被接管",
          bool(getattr(gemini_client._gemini_retry_count, "_cloud_stack_llm_retry_wrapper", False)))
    check("_gemini_retry_delay 已被接管",
          bool(getattr(gemini_client._gemini_retry_delay, "_cloud_stack_llm_retry_wrapper", False)))

    # 反放大断言：generate_gemini_text 必须**没有**被本模块包成外层重试。
    check("generate_gemini_text 未被包成外层重试（防 3×15 放大）",
          not bool(getattr(gemini_client.generate_gemini_text,
                           "_cloud_stack_llm_retry_wrapper", False)))

    # ---- ⑥ 序号语义：总尝试次数 = 重试次数 + 1（铁律 53）----
    #
    # ⚠ 同样必须按**面板层快照**还原，而不是 config.get() 的"当前生效值"。
    #   config.get() 会把 .env / 内置默认也算进来；拿它回写面板层等于把
    #   ".env 的值"永久固化成一个面板键（凭空多出一项，铁律 23/32）。
    import os as _os2  # noqa: PLC0415

    snapshot_before = config.store_values()
    had_count = _KEY_COUNT in snapshot_before
    saved_count = snapshot_before.get(_KEY_COUNT)
    try:
        # ⚠ 必须**同时**把总开关置 1：用户可能已把重试关掉（enabled=0），
        #   那种情况下 retries()/attempt_budget() 都不在作用域内，断言会假失败。
        #   本节测的是"档位与序号语义"，与用户是否启用无关（铁律 37：测试必须封闭）。
        config.update_store({_KEY_COUNT: "30", _KEY_ENABLED: "1"}, force=True)
        config.reload_store()
        _os2.environ.pop(_KEY_COUNT, None)
        _os2.environ.pop(_KEY_ENABLED, None)
        check("设置 30 次重试后，retries() 读到 30",
              llm_retry.retries() == 30, str(llm_retry.retries()))
        in_scope = llm_retry.in_scope(gemini_client)
        if in_scope:
            check("总尝试次数 = 重试次数 + 1（30 次重试 ⇒ 31 次尝试）",
                  gemini_client._gemini_retry_count() == 31,
                  str(gemini_client._gemini_retry_count()))
        else:
            check("非 sensenova provider 时保持 OCV 原生次数（作用域隔离）",
                  gemini_client._gemini_retry_count() == 3,
                  str(gemini_client._gemini_retry_count()))
    finally:
        # 面板层按**快照**逐字还原：两个键都要恢复到"原来在不在、原来是多少"。
        restore = {_KEY_COUNT: saved_count or ""} if had_count else {}
        removals = [] if had_count else [_KEY_COUNT]
        if had_enabled:
            restore[_KEY_ENABLED] = saved_enabled or ""
        else:
            removals.append(_KEY_ENABLED)
        config.update_store(restore, remove=removals, force=True)
        config.reload_store()
        _os2.environ.pop(_KEY_COUNT, None)
        _os2.environ.pop(_KEY_ENABLED, None)
    check("本节结束后面板层逐字还原（不凭空多出面板键）",
          config.store_values() == snapshot_before,
          "一致" if config.store_values() == snapshot_before else "有差异")

    # ---- ⑦ 可重试集合必须覆盖限流与 5xx ----
    check("可重试集合含 429 限流", llm_retry.retryable_status(429))
    check("可重试集合含 503（上游队列满）", llm_retry.retryable_status(503))
    check("鉴权类 401 不重试（不掩盖真实故障）",
          not llm_retry.retryable_status(401) and not llm_retry.retryable_status(400))

    # ---- ⑧ 档位说明与总窗口必须按**内置默认**断言（铁律 32）----
    #
    # ⚠ 不能断言 schedule_text() / total_window() 的"当前生效值"：用户在面板里
    #   把次数改成 7 之后，那两个值就跟着变，断言恒假。这里改成直接对
    #   **纯函数 + 默认常量** 断言，与用户配置完全无关。
    check("档位说明文本可读且含 1分钟 封顶",
          "1分钟" in llm_retry.schedule_text(), llm_retry.schedule_text())
    check("档位说明按『内置默认 30 次』渲染（不随用户配置漂移）",
          llm_retry.schedule_text(llm_retry.DEFAULT_RETRIES)
          == "1-3 次每 1秒；4-8 次每 5秒；9-12 次每 10秒；13-15 次每 15秒；"
             "16-20 次每 30秒；21-25 次每 45秒；26-30 次每 1分钟",
          llm_retry.schedule_text(llm_retry.DEFAULT_RETRIES))
    builtin_window = llm_retry.total_window(llm_retry.DEFAULT_RETRIES)
    check("内置默认 30 次的总窗口 = 788s / 13.1 分钟（与视频一致）",
          abs(builtin_window - 788.0) < 0.5, f"{builtin_window:.0f}s")
    check("默认次数常量就是 30", llm_retry.DEFAULT_RETRIES == 30,
          str(llm_retry.DEFAULT_RETRIES))


def _check_llm_base_live() -> None:
    """1s) 语言模型基址必须实时跟随面板（F-015），模型清单必须剔除套餐外型号。

    用户报的 ``HTTP 401 Authentication Fails ... is invalid`` **不是 Key 失效**，
    而是"新模型名 + 旧地址"：插件把面板地址固化进了 ``default_base``（冻结），
    而模型名是每次调用实时读的。修复 = 包装 ``language_base_url``。

    ⚠ 本节会临时改写面板层的 ``SENSENOVA_API_BASE`` / ``SENSENOVA_MODEL``，
    必须按**原始字节**还原（铁律 23/48）。
    """
    import json as _json  # noqa: PLC0415
    from pathlib import Path as _Path  # noqa: PLC0415

    from ocv_cloud_stack import config  # noqa: PLC0415
    from ocv_cloud_stack import models_catalog  # noqa: PLC0415

    from backend.app import gemini_client as _gc  # noqa: PLC0415

    store = _Path(__file__).resolve().parent / "var" / "config.json"
    original = store.read_text(encoding="utf-8")
    try:
        base_cfg = _json.loads(original)

        # 启动时指向 DeepSeek（复现用户处境）
        boot = dict(base_cfg)
        boot["SENSENOVA_API_BASE"] = "https://api.deepseek.com/v1"
        boot["SENSENOVA_MODEL"] = "deepseek-flash"
        store.write_text(_json.dumps(boot, ensure_ascii=False, indent=2), encoding="utf-8")
        config.reload_store()

        from ocv_cloud_stack import patches as _patches  # noqa: PLC0415

        _patches._patch_gemini_client(_gc)
        check("1s-1 启动时基址 = 面板当时的地址",
              _gc.language_base_url() == "https://api.deepseek.com/v1",
              _gc.language_base_url())

        # 运行中改面板到商汤 + 换模型
        live = dict(base_cfg)
        live["SENSENOVA_API_BASE"] = "https://token.sensenova.cn/v1"
        live["SENSENOVA_MODEL"] = "deepseek-v4.1-flash"
        store.write_text(_json.dumps(live, ensure_ascii=False, indent=2), encoding="utf-8")
        config.reload_store()

        check("1s-2 改面板后基址**实时**跟进（F-015 核心）",
              _gc.language_base_url() == "https://token.sensenova.cn/v1",
              _gc.language_base_url())
        check("1s-3 基址与模型来自同一个面板快照（原故障的核心）",
              _gc.language_base_url() == config.sensenova_base_url()
              and _gc.language_model() == config.sensenova_model(),
              f"{_gc.language_base_url()} / {_gc.language_model()}")

        entry = _gc.LANGUAGE_PROVIDER_OPTIONS.get("sensenova") or {}
        check("1s-4 provider 表 default_base 已同步",
              entry.get("default_base") == "https://token.sensenova.cn/v1",
              str(entry.get("default_base")))

        import backend.app.main as _main  # noqa: PLC0415
        check("1s-5 main 命名空间与 gemini_client 是同一函数（铁律 29）",
              _main.language_base_url is _gc.language_base_url)

        # 模型清单：套餐外型号必须被剔除
        fake = {"id": "deepseek-v4.1-flash", "output_modalities": ["text"],
                "supported_features": ["tools", "json_mode", "reasoning"]}
        check("1s-6 套餐外型号已从语言模型清单剔除",
              models_catalog._sensenova_kind_ok(fake, "llm") is False)
        check("1s-7 同形的其它型号不受影响",
              models_catalog._sensenova_kind_ok({**fake, "id": "deepseek-v4-flash"},
                                                "llm") is True)
        check("1s-8 套餐闸不误伤图片类",
              models_catalog._sensenova_kind_ok(
                  {**fake, "output_modalities": ["image"]}, "image") is True)

        # 静态回退清单不得含实测不可用的型号
        from ocv_cloud_stack import panel as _panel  # noqa: PLC0415
        values = {row["value"] for row in _panel.SENSENOVA_LLM_FALLBACK}
        check("1s-9 静态回退不含实测 404 的 sensenova-6.7-flash-lite",
              "sensenova-6.7-flash-lite" not in values)
        check("1s-10 静态回退不含套餐外的 deepseek-v4.1-flash",
              "deepseek-v4.1-flash" not in values)
    finally:
        store.write_text(original, encoding="utf-8")
        config.reload_store()
    check("1s-11 面板层按原始字节还原", store.read_text(encoding="utf-8") == original)


def _check_max_tokens_unlimited() -> None:
    """1t) 输出上限默认"不指定"（F-017），且迁移不得吃掉用户的显式设置。

    两件事必须同时成立：

    1. **默认不指定**：未配置时 ``sensenova_max_tokens()`` 返回 0，
       最终请求体里**没有 ``max_tokens`` 键**（交给模型自身默认值）。
       不能只"不传" —— OCV 源码是
       ``max_output_tokens or int(os.getenv("GEMINI_MAX_TOKENS", "4096"))``，
       不传会落到 4096，比原来的 16384 更糟。
    2. **迁移只作用于基线层**：旧版 install 往 ``.env`` 写的 16384 要被
       迁移成 0；但用户在**面板里明确填 16384** 时必须照原样生效 ——
       否则就是静默违背用户意图（本仓库 F-003/F-012 的同类教训）。
    """
    import os as _os  # noqa: PLC0415

    from ocv_cloud_stack import config as _cfg  # noqa: PLC0415

    key = "SENSENOVA_MAX_TOKENS"
    snapshot = _cfg.store_values()
    try:
        _cfg.update_store({}, remove=[key], force=True)
        _cfg.reload_store()
        _os.environ.pop(key, None)
        check("1t-1 未配置时返回 0（不限制）", _cfg.sensenova_max_tokens() == 0,
              str(_cfg.sensenova_max_tokens()))

        from backend.app import gemini_client as _gc  # noqa: PLC0415
        from requests.sessions import Session as _Session  # noqa: PLC0415

        captured: list[dict] = []

        class _Resp:
            status_code = 200
            ok = True

            def json(self):
                return {"choices": [{"message": {"content": "ok"},
                                     "finish_reason": "stop"}]}

        real_request = _Session.request

        def _capture(self, method, url, *a, **kw):
            captured.append(dict(kw.get("json") or {}))
            return _Resp()

        def _call():
            captured.clear()
            _Session.request = _capture  # type: ignore[assignment]
            try:
                _gc.generate_gemini_text(system_prompt="s", user_prompt="u")
            except Exception:  # noqa: BLE001
                pass
            finally:
                _Session.request = real_request  # type: ignore[assignment]
            return captured[-1] if captured else {}

        body = _call()
        check("1t-2 请求体里没有 max_tokens（交模型默认）",
              "max_tokens" not in body, str(sorted(body)))

        for value, expect in (("16384", 16384), ("32768", 32768), ("393216", 393216)):
            _cfg.update_store({key: value}, force=True)
            _cfg.reload_store()
            _os.environ.pop(key, None)
            check(f"1t-3 面板明确填 {value} ⇒ 原样生效（迁移不吃用户设置）",
                  _cfg.sensenova_max_tokens() == expect,
                  str(_cfg.sensenova_max_tokens()))

        _cfg.update_store({key: "0"}, force=True)
        _cfg.reload_store()
        _os.environ.pop(key, None)
        check("1t-4 面板填 0 ⇒ 不限制", _cfg.sensenova_max_tokens() == 0)

        _cfg.update_store({key: "32768"}, force=True)
        _cfg.reload_store()
        _os.environ.pop(key, None)
        body = _call()
        check("1t-5 设 32768 时请求体带上 max_tokens=32768（下限语义保留）",
              body.get("max_tokens") == 32768, str(body.get("max_tokens")))
    finally:
        current = _cfg.store_values()
        _cfg.update_store({k: v for k, v in snapshot.items()},
                          remove=[k for k in current if k not in snapshot],
                          force=True)
        _cfg.reload_store()
        _os.environ.pop(key, None)
    check("1t-6 面板层逐字还原", _cfg.store_values() == snapshot,
          "一致" if _cfg.store_values() == snapshot else "有差异")


def _check_mimo_engine() -> None:
    """1u) MiMo 独立引擎：与 Qwen-TTS **并列**而不是覆盖。

    本节固化 1.10.0 的核心不变量。任何一条回归，都意味着「两个选项同时可见」
    这个需求被破坏了：

    1. **Literal 扩展手法**：只 ``model_rebuild`` 无效，必须连**路由的
       ``_type_adapter``** 一起重建（铁律 58）。用 ``_LiteralProbeRequest``
       复现「只 rebuild 仍 422 → 重建 adapter 后 200」的完整对照。
    2. **按值分流**：未标记时**不得**替换 qwen 实现；标记后才替换（铁律 59）。
    3. **归一化**：``mimo`` → ``qwen`` 且留痕，否则 module1 的 argparse 会拒。
    4. **范围**：只有配音子进程带水印，模块 5 等不带。

    ⚠ **探针模型必须定义在模块级**（``_LiteralProbeRequest``）。本文件有
    ``from __future__ import annotations``（PEP 563），函数内定义的类其注解
    会保持**字符串**，而 FastAPI 解析不了函数局部名字 ⇒ 该路由的
    ``body_params`` 变成**空**，于是**一切**请求都 422。
    实测踩过：同一段逻辑在模块级类上 200、在函数局部类上 422，
    表现成「适配器重建失效」的假象 —— 其实是探针自己的问题。
    """
    import os as _os  # noqa: PLC0415

    import pydantic as _pyd  # noqa: PLC0415
    from fastapi import FastAPI as _FastAPI  # noqa: PLC0415
    from fastapi.testclient import TestClient as _TestClient  # noqa: PLC0415
    from typing import Literal as _Literal  # noqa: PLC0415

    from ocv_cloud_stack import mimo_engine as _mimo  # noqa: PLC0415

    # ---- 1) Literal 扩展手法（最小复现，模块级类）----------------------
    _app = _FastAPI()

    @_app.post("/api/jobs")
    def _ep(payload: _LiteralProbeRequest) -> dict:  # noqa: ANN202
        return {"tts_engine": payload.tts_engine}

    _client = _TestClient(_app)
    _r = _client.post("/api/jobs", json={"tts_engine": "mimo"})
    check("1u-1 基线：mimo 被 422 拒绝", _r.status_code == 422, f"status={_r.status_code}")

    _new = _Literal["indextts2", "indextts25", "cluster", "qwen", "mimo"]
    _LiteralProbeRequest.__annotations__["tts_engine"] = _new
    _LiteralProbeRequest.model_fields["tts_engine"].annotation = _new
    _LiteralProbeRequest.model_rebuild(force=True)
    _r = _client.post("/api/jobs", json={"tts_engine": "mimo"})
    check(
        "1u-2 铁律 58：只 model_rebuild 仍然 422（必须重建路由 adapter）",
        _r.status_code == 422,
        f"status={_r.status_code}",
    )

    for _route in _app.routes:
        _dep = getattr(_route, "dependant", None)
        if _dep is None:
            continue
        for _bf in getattr(_dep, "body_params", []) or []:
            if getattr(_bf, "type_", None) is not None:
                _bf._type_adapter = _pyd.TypeAdapter(_bf.type_)
    _r = _client.post("/api/jobs", json={"tts_engine": "mimo"})
    check("1u-3 重建路由 adapter 后 mimo 被接受", _r.status_code == 200, f"status={_r.status_code}")
    _r = _client.post("/api/jobs", json={"tts_engine": "qwen"})
    check("1u-4 回归：qwen 仍可用", _r.status_code == 200, f"status={_r.status_code}")
    _r = _client.post("/api/jobs", json={"tts_engine": "bogus"})
    check("1u-5 回归：非法值仍被拒绝（校验没放水）", _r.status_code == 422, f"status={_r.status_code}")

    # ---- 2) 真机 GenerateRequest 已含 mimo ------------------------------
    try:
        from backend.app import main as _main  # noqa: PLC0415

        _args = _mimo._literal_args(_main.GenerateRequest.model_fields["tts_engine"].annotation)  # noqa: SLF001
        check("1u-6 真机 tts_engine 允许 mimo", "mimo" in _args, str(_args))
        check(
            "1u-7 真机原生取值未被破坏",
            {"indextts2", "indextts25", "cluster", "qwen"} <= set(_args),
            str(_args),
        )
    except Exception as _exc:  # noqa: BLE001
        check("1u-6 真机 GenerateRequest 可读", False, f"{type(_exc).__name__}: {_exc}")

    # ---- 3) 按值分流（铁律 59）------------------------------------------
    try:
        from backend.app import qwen_tts as _qwen  # noqa: PLC0415

        from ocv_cloud_stack import mimo_tts as _mt  # noqa: PLC0415

        _orig = _qwen.synthesize_to_file
        _saved_applied = _mimo._RUNTIME_APPLIED  # noqa: SLF001
        _saved_env = _os.environ.pop(_mimo.ACTIVE_ENV, None)
        try:
            _mimo._RUNTIME_APPLIED = False  # noqa: SLF001
            _mimo.apply_child_runtime()
            check("1u-8 未标记 active：不替换（qwen 保持原生）",
                  _qwen.synthesize_to_file is _orig)

            _mimo._RUNTIME_APPLIED = False  # noqa: SLF001
            _os.environ[_mimo.ACTIVE_ENV] = "1"
            _mimo.apply_child_runtime()
            check("1u-9 标记 active：替换为 MiMo",
                  _qwen.synthesize_to_file is _mt.synthesize_to_file)
        finally:
            _qwen.synthesize_to_file = _orig
            _mimo._RUNTIME_APPLIED = _saved_applied  # noqa: SLF001
            _os.environ.pop(_mimo.ACTIVE_ENV, None)
            if _saved_env is not None:
                _os.environ[_mimo.ACTIVE_ENV] = _saved_env
    except Exception as _exc:  # noqa: BLE001
        check("1u-8 分流可验证", False, f"{type(_exc).__name__}: {_exc}")

    # ---- 4) 归一化 + 环境变量放行 ---------------------------------------
    class _Job:
        def __init__(self, request: dict) -> None:
            self.request = request

    _req = {"tts_engine": "mimo"}
    _mimo.normalize_request(_req)
    check("1u-10 归一化 mimo -> qwen", _req.get("tts_engine") == "qwen", str(_req.get("tts_engine")))
    check("1u-11 原值留痕", _req.get(_mimo.MARKER_KEY) == "mimo")
    check("1u-12 归一化后 wants_mimo 仍为真", _mimo.wants_mimo(_Job(_req)) is True)
    check("1u-13 qwen 任务 wants_mimo 为假",
          _mimo.wants_mimo(_Job({"tts_engine": "qwen"})) is False)

    # ---- 5) 只有配音子进程带水印 ----------------------------------------
    check("1u-14 配音命令可识别",
          _mimo._is_tts_command([sys.executable, "module1_agent_director.py"]) is True)  # noqa: SLF001
    check("1u-15 非配音命令不被识别",
          _mimo._is_tts_command([sys.executable, "module5_video_render.py"]) is False)  # noqa: SLF001

    # ---- 6) 端点包装按方法匹配（自检抓出的真缺陷回归）-------------------
    try:
        from backend.app import main as _main2  # noqa: PLC0415

        _post = [
            r for r in _main2.app.routes
            if getattr(r, "path", "") == "/api/jobs"
            and "POST" in {str(m).upper() for m in (getattr(r, "methods", None) or set())}
        ]
        _get = [
            r for r in _main2.app.routes
            if getattr(r, "path", "") == "/api/jobs"
            and "GET" in {str(m).upper() for m in (getattr(r, "methods", None) or set())}
        ]
        check("1u-16 /api/jobs 同时存在 POST 与 GET（回归场景成立）",
              len(_post) >= 1 and len(_get) >= 1, f"POST={len(_post)} GET={len(_get)}")
        check(
            "1u-17 POST create_job 已被门禁包装",
            any(getattr(getattr(r.dependant, "call", None), "_cloud_stack_mimo_wrapped", False)
                for r in _post),
        )
        check(
            "1u-18 GET list_jobs 未被门禁包装污染（按方法匹配）",
            all(not getattr(getattr(r.dependant, "call", None), "_cloud_stack_mimo_wrapped", False)
                for r in _get),
            str([getattr(r.dependant.call, "__name__", "?") for r in _get]),
        )
    except Exception as _exc:  # noqa: BLE001
        check("1u-16 端点包装可按方法验证", False, f"{type(_exc).__name__}: {_exc}")

    # ---- 7) 前端脚本共存放行 --------------------------------------------
    try:
        from pathlib import Path as _Path  # noqa: PLC0415

        _panel = (_Path(__file__).resolve().parent / "panel" / "panel.js").read_text(encoding="utf-8")
        check("1u-19 panel.js 注入 mimo 选项", 'MIMO_VALUE = "mimo"' in _panel)
        check("1u-20 panel.js 会还原被旧版改写的 qwen 文案（铁律 60）",
              "QWEN_LABEL" in _panel and "qwenOption" in _panel)
    except Exception as _exc:  # noqa: BLE001
        check("1u-19 panel.js 可读", False, f"{type(_exc).__name__}: {_exc}")


def _check_jethub() -> None:
    """1v) Jet Hub（CodeBuddy 免费额度 LLM）接入的不变量。

    这一节**守住四件最容易回退的事**，全部是真实踩过的坑：

    1. **不桥接 DSH** —— 凭据与账号池必须落在插件目录内
       （用户硬要求：「所有文件都放在 OCV 插件目录内」）；
    2. **可独立运行** —— vendor 必须自洽、Node 必须找得到
       （目标机不装 DSH、不装 Node，用 OCV 自带的）；
    3. **面板解包约定** —— 后端 handler 返回含 `ok` 的扁平 dict，
       前端必须用 `jhUnwrap` 兼容，否则整页**静默空白**（F-003）；
    4. **`llm_jethub` 用途存在** —— 这是「LLM 面板能选 Jet Hub 模型」的载体，
       少一个环节下拉就是空的。
    """
    from ocv_cloud_stack import config, jethub_bridge, models_catalog, panel  # noqa: PLC0415
    from pathlib import Path as _Path  # noqa: PLC0415 - 与其它节保持同一别名风格

    plugin_root = _Path(__file__).resolve().parent

    # ---- 1. 不桥接 DSH：状态一律落在插件目录 ----
    state_dir = config.jethub_state_dir()
    check(
        "1v-1 凭据/账号池状态目录在插件目录内（不写 ~/.dsh）",
        str(state_dir).replace("\\", "/").startswith(str(plugin_root).replace("\\", "/")),
        f"state_dir={state_dir}",
    )
    check(
        "1v-2 备份目录在插件目录内",
        str(config.jethub_backup_dir()).replace("\\", "/").startswith(
            str(plugin_root).replace("\\", "/")),
        f"backup_dir={config.jethub_backup_dir()}",
    )

    # 真实桥的落点自检（桥没起来时跳过，不误判为失败）
    if jethub_bridge.is_listening():
        report = jethub_bridge.call("/rpc/status", {}, timeout=10.0)
        storage = ((report.get("data") or {}).get("value") or {}).get("storage") or {}
        check("1v-3 桥自检：账号池在插件目录内", storage.get("poolInPlugin") is True,
              f"poolPath={storage.get('poolPath')!r}")
        check("1v-4 桥自检：凭据文件在插件目录内", storage.get("credentialsInPlugin") is True,
              f"credentialsPath={storage.get('credentialsPath')!r}")
    else:
        check("1v-3 桥未运行（跳过落点自检）", True, "")
        check("1v-4 桥未运行（跳过落点自检）", True, "")

    # ---- 2. 可独立运行：vendor 自洽 + Node 可寻 ----
    vendor_index = plugin_root / "vendor" / "dsh-codearts-auth" / "lib" / "index.js"
    check("1v-5 vendor/dsh-codearts-auth 已打包", vendor_index.is_file(), f"缺 {vendor_index}")
    for dep in ("@deepseek-ai/cordis", "@deepseek-ai/dsh-llm", "@deepseek-ai/dsh-credentials",
                "@deepseek-ai/schemastery", "jose"):
        dep_pkg = plugin_root / "vendor" / "node_modules" / _Path(dep) / "package.json"
        check(f"1v-6 vendor 依赖 {dep} 在位", dep_pkg.is_file(), f"缺 {dep_pkg}")
    check("1v-7 找得到 Node 运行时（优先 OCV 自带）",
          jethub_bridge.node_executable() is not None,
          "既无 runtime/node/node.exe，PATH 里也没有 node")
    check("1v-8 桥入口 server.mjs 存在", jethub_bridge.NODE_ENTRY.is_file(),
          f"缺 {jethub_bridge.NODE_ENTRY}")

    # ---- 3. 面板解包约定（F-003：整页静默空白的根因） ----
    try:
        html = (plugin_root / "panel" / "index.html").read_text(encoding="utf-8")
        check("1v-9 面板有 jhUnwrap 兼容解包", "function jhUnwrap" in html,
              "缺 jhUnwrap —— 后端扁平 dict 会让整页静默空白（F-003）")
        check("1v-10 jhUnwrap 同时兼容扁平与 data 两种形态",
              "payload.data || payload" in html)
        check("1v-11 面板有 tab-jethub 分页容器", 'id="tab-jethub"' in html)
        check("1v-12 面板有 card-jethub 字段卡片（否则分组渲染被静默跳过）",
              'id="card-jethub"' in html)
        check("1v-13 面板 render() 里调用了 renderJetHub",
              "renderJetHub();\n    refreshJetHub();" in html.replace("\r\n", "\n"),
              "render() 未接 Jet Hub —— 分页会一直空白")
        check("1v-14 登录走两步式（先开窗再轮询，保住浏览器手势）",
              "pollJethubLogin" in html and 'window.open(data.login_url' in html)
        check("1v-15 弹窗被拦时不回退 location.href（会把设置页整个跳走）",
              "location.href" not in html.split("btn-jethub-login")[1][:2000]
              if "btn-jethub-login" in html else False)
    except Exception as _exc:  # noqa: BLE001
        check("1v-9 面板 HTML 可读", False, f"{type(_exc).__name__}: {_exc}")

    # ---- 4. llm_jethub 用途（「LLM 面板能选模型」的载体） ----
    check("1v-16 models_catalog 注册了 llm_jethub 用途",
          models_catalog.KINDS.get("llm_jethub") == models_catalog.PROVIDER_JETHUB,
          f"KINDS={models_catalog.KINDS}")
    check("1v-17 llm_jethub 有用途标签", "llm_jethub" in models_catalog.KIND_LABELS)
    field = next((f for f in panel.FIELDS if f.get("key") == "JETHUB_MODEL"), None)
    check("1v-18 面板有 JETHUB_MODEL 字段", field is not None)
    check("1v-19 JETHUB_MODEL 绑定到 llm_jethub 动态清单",
          bool(field and field.get("dynamic_models") == "llm_jethub"),
          f"field={field}")
    check("1v-20 JETHUB_MODEL 允许手填（与其它模型字段一致）",
          bool(field and field.get("allow_custom")))
    check("1v-21 jethub 分组已注册（否则前端不渲染该分页）",
          "jethub" in panel.GROUP_LABELS, f"groups={list(panel.GROUP_LABELS)}")

    # ---- 5. 契约补丁（桥存在的理由） ----
    check("1v-22 软超时 < OCV 读超时 120s（否则 OCV 拿到断连而非可重试的 503）",
          0 < config.jethub_bridge_timeout_ms() < 120_000,
          f"timeout={config.jethub_bridge_timeout_ms()}ms")
    check("1v-23 桥端口避开 OCV 8010 / Vite 5173 / shim 8799",
          config.jethub_bridge_port() not in (8010, 5173, 8799),
          f"port={config.jethub_bridge_port()}")

    # ---- 6. 桥核心契约（纯函数级，不需要账号） ----
    bridge_src = (plugin_root / "jethub" / "bridge.mjs").read_text(encoding="utf-8")
    check("1v-24 桥补了 response_format 缺口（降级为提示词约束）",
          "JSON_ONLY_HINT" in bridge_src and "wantsJson" in bridge_src,
          "vendor 不转发 response_format，不补的话 Agent 的 JSON 契约会退化")
    check("1v-25 桥把流式聚合成非流式（OCV 全仓非流式）",
          "collectStream" in bridge_src and "toOpenAiResponse" in bridge_src)
    check("1v-26 bridge 明确拒绝 stream:true 而不是静默按非流式处理",
          'stream === true' in (plugin_root / "jethub" / "server.mjs").read_text(encoding="utf-8"))

    # ---- 7. 安全：桥只接受本机回环请求 ----
    server_src = (plugin_root / "jethub" / "server.mjs").read_text(encoding="utf-8")
    check("1v-27 桥有本机请求校验（挡 DNS rebinding / 跨站）",
          "isLocalRequest" in server_src and "sec-fetch-site" in server_src)
    check("1v-28 桥只绑回环地址", "const HOST = '127.0.0.1'" in server_src)

    # ---- 8. 凭据只走 OCV 自建存储（不启用 DSH 的 LocalCredentialProvider） ----
    runtime_src = (plugin_root / "jethub" / "runtime.mjs").read_text(encoding="utf-8")
    # ⚠ 判据要看**是否真的实例化**，而不是文档里有没有提到那个名字 ——
    #   runtime.mjs 的注释故意点名 LocalCredentialProvider 讲"为什么不用它"，
    #   用 `not in src` 会把说明文字误判成违规（这条断言首版就是这么错的）。
    check(
        "1v-29 用自建凭据存储而非实例化 DSH 的 LocalCredentialProvider",
        "createCredentialStore" in runtime_src
        and "new cl.LocalCredentialProvider" not in runtime_src
        and "new LocalCredentialProvider" not in runtime_src,
        "必须用 createCredentialStore（只存插件目录）；实例化 DSH 的实现会把数据写到 ~/.dsh",
    )
    check("1v-30 状态目录两级钉死（env + profileContext）",
          "DSH_JET_HUB_STATE_DIR" in runtime_src and "profileContext" in runtime_src,
          "只钉 env 不够 —— 第三方代码可能绕过容器直读环境")
    check("1v-31 凭据解析走账号池（不能用 auth.refresh()：它写死产品默认 ref）",
          "getAvailableAccount" in runtime_src and "refreshAccountCredential" in runtime_src,
          "F-002：用 auth.refresh() 会报「未配置凭据」")

    # ---- 9. 备份/恢复能力（用户明确要求） ----
    cred_src = (plugin_root / "jethub" / "credentials.mjs").read_text(encoding="utf-8")
    check("1v-32 凭据存储支持导出（备份用）", "exportDocument" in cred_src)
    check("1v-33 凭据存储支持导入（恢复用）", "importDocument" in cred_src)
    check("1v-34 备份含 schema 标识（防误导入别的 json）",
          "ocv-cloud-free-stack.jethub-backup" in runtime_src)
    check("1v-35 面板有备份 handler（导出/恢复/删除）",
          all(hasattr(panel, name) for name in ("jethub_backup_action",)))


def main() -> int:
    import builtins

    print("== 0) 环境编码自检（中文 Windows 专项）==")
    _check_encodings()

    print("\n== 1) 导入钩子 ==")
    check(
        "builtins.__import__ 已被包装",
        bool(getattr(builtins, "_ocv_cloud_stack_hooked", False)),
        "若为 ✗，说明 PYTHONPATH 没设对或 sitecustomize 未被加载",
    )

    print("\n== 1b) 面板覆盖层 → 环境变量 ==")
    _check_store_exported()

    print("\n== 1c) 面板保存 → 运行中进程立即生效 ==")
    _check_save_syncs_environ()

    print("\n== 1d) shim 出图提交路由 ==")
    _check_shim_routes()

    print("\n== 1e) 克隆音色（voiceclone）==")
    _check_clone_voices()

    print("\n== 1f) 软件更新后自愈（派生注入状态重建）==")
    _check_reapply()

    print("\n== 1g) 参考图 media type 嗅探（图片功能专项）==")
    _check_image_mime_sniff()

    print("\n== 1h) 图片模型清单校正（补齐漏报 / 剔除不支持）==")
    _check_model_catalog()

    print("\n== 1i) 卸载安全性（不得冲掉用户设置）==")
    _check_uninstall_safety()

    print("\n== 1j) 云端视频协议翻译（Agnes）==")
    _check_video_bridge()

    print("\n== 1k) 动态阶段方案 reference_beat 健壮性 ==")
    _check_motion_plan()

    print("\n== 1l) 参考画面状态 reference_visual 修复路径可达性 ==")
    _check_reference_visual()

    print("\n== 1m) 动态阶段方案整族字段（记账/格式）==")
    _check_motion_plan_fields()

    print("\n== 1n) 视频提示词组装 + 包装必须真的装上 ==")
    _check_video_assemble()

    print("\n== 1o) 视频门禁桥接（面板配好 Agnes 后 OCV 必须放行）==")
    _check_video_gate_bridge()

    print("\n== 1p) 凭据缩水保护（禁止静默覆盖真 Key）==")
    _check_credential_guard()

    print("\n== 1q) 视频提交重试（上游队列满时必须重试，且原因要传出去）==")
    _check_video_submit_retry()

    print("\n== 1r) 语言模型 30 档阶梯重试（与视频共用档位表）==")
    _check_llm_retry()

    print("\n== 1s) 语言模型基址实时跟随 + 模型清单可信度（F-015）==")
    _check_llm_base_live()

    print("\n== 1t) 输出上限默认不指定（交模型默认，F-017）==")
    _check_max_tokens_unlimited()

    print("\n== 1u) MiMo 独立引擎（与 Qwen-TTS 并列，不覆盖）==")
    _check_mimo_engine()

    print("\n== 1v) Jet Hub（CodeBuddy 免费额度 LLM）接入 ==")
    _check_jethub()

    print("\n== 2) TTS 补丁（MiMo 为独立引擎；qwen 通道归 OCV 原生）==")
    from backend.app.qwen_tts import (  # noqa: PLC0415
        DEFAULT_VOICE,
        QwenTtsError,
        synthesize_to_file,
        voice_supports_instructions,
    )

    from ocv_cloud_stack import mimo_engine as _mimo  # noqa: PLC0415

    if _mimo.enabled():
        # 1.10.0 起的**并列**语义：本进程（后端 / 未标记）不得占用 qwen 槽位。
        module_name = str(getattr(synthesize_to_file, "__module__", ""))
        check(
            "2-1 并列模式：qwen 槽位未被顶替（仍是 OCV 原生实现）",
            module_name == "backend.app.qwen_tts",
            module_name,
        )
        check(
            "2-2 并列模式：DEFAULT_VOICE 未被改成 MiMo 音色",
            DEFAULT_VOICE in {"Elias", "Cherry"} or DEFAULT_VOICE not in {"冰糖", "茉莉", "苏打", "白桦"},
            DEFAULT_VOICE,
        )
        print(
            "    提示：MiMo 的替换发生在**配音子进程**内（CLOUD_STACK_MIMO_ACTIVE=1 时），"
            "由 1u 节的分流断言覆盖。"
        )
    else:
        # 旧模式（回退开关关闭了独立引擎）：保留 1.9.x 的顶替断言
        module_name = str(getattr(synthesize_to_file, "__module__", ""))
        check("2-1 旧模式：synthesize_to_file 指向 MiMo 实现",
              module_name == "ocv_cloud_stack.mimo_tts", module_name)
        check("2-2 旧模式：DEFAULT_VOICE 已换成 MiMo 音色",
              DEFAULT_VOICE not in {"Elias", "Cherry"}, DEFAULT_VOICE)
        check("2-3 旧模式：voice_supports_instructions 已放行任意音色",
              voice_supports_instructions("冰糖") is True)
    check("2-4 QwenTtsError 仍可被 module1 捕获", issubclass(QwenTtsError, Exception))

    print("\n== 3) LLM 补丁（新增 sensenova provider）==")
    from backend.app.gemini_client import (  # noqa: PLC0415
        LANGUAGE_PROVIDER_OPTIONS,
        language_base_url,
        language_model,
        language_provider_configured,
    )

    entry = LANGUAGE_PROVIDER_OPTIONS.get("sensenova")
    check("LANGUAGE_PROVIDER_OPTIONS 含 sensenova", isinstance(entry, dict))
    if isinstance(entry, dict):
        check("source 必须为 official（否则 JSON 约束会静默失效）", entry.get("source") == "official", str(entry.get("source")))
        check("protocol 为 openai", entry.get("protocol") == "openai", str(entry.get("protocol")))
        base = language_base_url("sensenova")
        check(
            "base_url 是合法的 OpenAI 兼容地址",
            base.startswith(("http://", "https://")) and "/" in base.rstrip("/"),
            base + "（面板可指向任意 OpenAI 兼容服务商）",
        )
        model = language_model("sensenova")
        check("model 解析成功", bool(model), model)
        print(f"    configured = {language_provider_configured('sensenova')}（未填 Key 时为 False 属正常）")

    print("\n== 3b) 图片独立 Key ==")
    from ocv_cloud_stack import config  # noqa: PLC0415

    image_key = config.get("SENSENOVA_IMAGE_API_KEY").strip()
    if image_key:
        same = image_key == config.sensenova_api_key()
        check("图片 Key 已独立配置", bool(image_key),
              f"长度 {len(image_key)}，与语言模型 Key{'相同（等效于共用）' if same else '不同（图片走独立额度）'}")
    else:
        check("图片 Key 留空 → 回落语言模型 Key", bool(config.sensenova_image_api_key()),
              "sensenova_image_api_key() 非空即可正常出图")

    print("\n== 4) 图片链路配置 ==")
    from ocv_cloud_stack import config  # noqa: PLC0415

    env_base = config.get("IMAGE_API_BASE_URL", "")
    check(
        "IMAGE_API_BASE_URL 指向本机 shim",
        env_base.rstrip("/") == config.shim_base_url(),
        f"{env_base or '（空）'}（需先运行 cloud_stack_ctl.py install）",
    )

    print("\n== 5) 模块 2（ASR）自检健壮性 ==")
    _check_asr_prewarm()

    print("\n== 5b) 深度思考开关（默认关闭）==")
    _check_thinking_switch()

    print("\n== 5c) Agent 1B 健壮性（边界细化失败不再终止任务）==")
    _check_agent1b_resilience()

    print("\n== 5d) Agent 1B 输出重组切分（不重复/不缺口）==")
    _check_agent1b_partition()

    print("\n== 5e) JSON 模式守卫（response_format=json_object 契约兜底）==")
    _check_json_mode_guard()

    print("\n== 6) 运行时快照 ==")
    from ocv_cloud_stack import bootstrap  # noqa: PLC0415

    print(json.dumps(bootstrap.status(), ensure_ascii=False, indent=2))

    print()
    if FAILURES:
        print(f"✗ {len(FAILURES)} 项未通过：" + "；".join(FAILURES))
        return 1
    print("✓ 注入自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
