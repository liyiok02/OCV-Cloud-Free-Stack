# SPDX-License-Identifier: AGPL-3.0-only
"""把「云端 OAuth」分页的 HTML / CSS / JS 打进 `panel/index.html`。

## 为什么需要这个脚本（F-001 事故的产物）

开发中我用 PowerShell 的 `Get-Content -Raw` + `Set-Content` 去改这个含中文的
**UTF-8 无 BOM** 文件，结果**整份中文乱码** —— PowerShell 5 默认按 ANSI(cp936)
解码，读进来就已经错了，再按 UTF-8 写回去就是两层损坏。教训已升格为铁律：

> **改含中文的 UTF-8 文件，一律用 Python（显式 `encoding="utf-8"`），
> 不要用 PowerShell 的 `Get-Content`/`Set-Content`。**

本脚本把「还原 + 打补丁」做成**可复现的一步**，且**幂等**：

1. 从 `plugins/cloud_free_stack.zip` 取原版（无损基线）；
2. 读 `panel/cloud-oauth.{css,html,js}` 三个资源文件；
3. 用 Python 字符串替换打上四处补丁；
4. 校验 12 项关键标记都在、中文没坏。

## 为什么资源放在独立文件而不是内嵌在脚本里

内嵌会让这个脚本膨胀到 1500+ 行、且改样式要改 Python 字符串字面量。
独立文件还能被编辑器正常高亮、被 `verify_panel_render.py` 直接断言。

用法：
```bat
runtime\\python\\python.exe plugins\\cloud_free_stack\\dev_patch_panel_html.py --check
runtime\\python\\python.exe plugins\\cloud_free_stack\\dev_patch_panel_html.py
```
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent
PANEL_DIR = PLUGIN_ROOT / "panel"
TARGET = PANEL_DIR / "index.html"
ZIP_PATH = PLUGIN_ROOT.parent / "cloud_free_stack.zip"
ZIP_MEMBER = "cloud_free_stack/panel/index.html"

CSS_FILE = PANEL_DIR / "cloud-oauth.css"
HTML_FILE = PANEL_DIR / "cloud-oauth.html"
JS_FILE = PANEL_DIR / "cloud-oauth.js"

CSS_MARK = ".jh-layout"
HTML_MARK = 'id="jethub-providers"'
JS_MARK = "function jhUnwrap"

# 备份弹窗**已删除**（用户要求：不要另外弹窗，直接分开放在面板中）。
# 备份与恢复现在是两张常驻卡片，见 `panel/cloud-oauth.html`。
#
# ⚠ 保留这个空常量只是为了标记「这里曾经有过一个 modal」，并提醒：
#   当年 modal 被插到 `</script>` **之后**，导致 `jhBind()` 在脚本执行时
#   找不到元素、**静默跳过绑定** —— 用户点「选择文件」毫无反应。
#   教训：**注入的 DOM 必须排在绑定它的脚本之前**，或者绑定要走
#   `jhBindWhenReady()` 这种「等元素就绪」的包装。
MODAL = ""

# render() 里接入本分页
RENDER_HOOK = """
    // 云端 OAuth 分页：状态是**异步**取的（要打桥），先渲染骨架
    // （此时 jethubState 可能还是 null，renderJetHub 会安全早退），
    // 再发起请求，回来后重渲染。
    renderJetHub();
    refreshJetHub();"""


# 「连通性测试」说明文案：必须让用户知道**测的是哪一路**（F-024）。
# 首版写死「验证 Key 有效」，而选云端 OAuth 时根本没有 Key —— 文案会误导。
LLM_TEST_SUB_OLD = '<span class="sub">验证 Key 有效且 JSON 强制输出真的生效</span>'
LLM_TEST_SUB_NEW = '<span class="sub">按当前启用的**模型来源**实测一次：自行填 API 或云端 OAuth，测试结果会标出打的是哪一路</span>'


def read_pristine() -> str:
    if not ZIP_PATH.is_file():
        raise SystemExit(f"找不到原版：{ZIP_PATH}")
    with zipfile.ZipFile(ZIP_PATH) as archive:
        return archive.read(ZIP_MEMBER).decode("utf-8")


def _find(text: str, needle: str) -> str | None:
    """兼容 LF / CRLF 地找一个锚点。"""
    if needle in text:
        return needle
    crlf = needle.replace("\n", "\r\n")
    return crlf if crlf in text else None


def _insert_after(text: str, anchor: str, block: str) -> tuple[str, bool]:
    found = _find(text, anchor)
    if found is None:
        return text, False
    newline = "\r\n" if "\r\n" in text else "\n"
    payload = block.replace("\r\n", "\n").replace("\n", newline)
    return text.replace(found, found + payload, 1), True


def _insert_before(text: str, anchor: str, block: str) -> tuple[str, bool]:
    """把 `block` 插到 `anchor` 的**前面**。

    与 `_insert_after` 配对存在：把代码插进某个函数**体内**时，
    用「下一个函数声明」当锚点、插在它前面，比「本函数最后一行 + 结束括号」
    更稳 —— 后者极易匹配到**函数自己的结束括号**而把代码甩到体外
    （F-020 就是这么来的）。
    """
    found = _find(text, anchor)
    if found is None:
        return text, False
    newline = "\r\n" if "\r\n" in text else "\n"
    payload = block.replace("\r\n", "\n").replace("\n", newline)
    return text.replace(found, payload + found, 1), True


def _nl(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def patch(text: str, css: str, html: str, js: str) -> tuple[str, list[str]]:
    applied: list[str] = []
    newline = _nl(text)

    # 1) CSS：插到最后一个 </style> 之前
    if CSS_MARK not in text:
        index = text.rfind("</style>")
        if index < 0:
            raise SystemExit("找不到 </style>")
        text = text[:index] + css.replace("\n", newline) + newline + text[index:]
        applied.append("css")

    # 2) HTML：写入 tab-jethub 分页。
    #
    # 两种基线都要能处理（脚本要能反复重跑、也要能在旧基线上跑）：
    #   * 基线里**已有** tab-jethub（我上一次的补丁产物）→ 整段替换；
    #   * 基线里**没有**（zip 里的原始版）→ 插到 tab-misc 之前。
    if HTML_MARK not in text:
        pattern = re.compile(r'[ \t]*<section id="tab-jethub">.*?</section>\s*', re.DOTALL)
        if pattern.search(text):
            text = pattern.sub(html.rstrip() + newline + newline, text, count=1)
            applied.append("html(替换)")
        else:
            anchor = '  <section id="tab-misc">'
            found = _find(text, anchor)
            if found is None:
                raise SystemExit('既没有 tab-jethub，也找不到 <section id="tab-misc"> 锚点')
            text = text.replace(found, html.rstrip() + newline + newline + found, 1)
            applied.append("html(插入)")

    # 3) JS：插在 btn-open-ocv 处理之后
    if JS_MARK not in text:
        anchor = """  el("btn-open-ocv").addEventListener("click", function () {
    window.open("http://127.0.0.1:5173", "_blank");
  });
"""
        text, ok = _insert_after(text, anchor, js)
        if not ok:
            raise SystemExit("找不到 JS 插入锚点（btn-open-ocv）")
        applied.append("js")

    # 4) 备份弹窗**已删除** —— 备份/恢复现在是 HTML 资源里的两张常驻卡片。
    #
    # 这里显式清掉历史版本可能残留在文件里的 modal（幂等迁移）：
    #   如果是基于**旧补丁产物**做增量，或文档里还留着 modal 片段，
    #   一律移除，避免「删了按钮但弹窗 DOM 还在」的中间态。
    if 'class="jh-modal" id="jh-backup-modal"' in text:
        pattern = re.compile(
            r'[ \t]*<div class="jh-modal" id="jh-backup-modal">.*?</div>\s*</div>\s*',
            re.DOTALL,
        )
        text, count = pattern.subn("", text, count=1)
        if count:
            applied.append("移除旧备份弹窗")

    # 4.5) 「连通性测试」的说明文案：跟随来源措辞
    if LLM_TEST_SUB_OLD in text:
        text = text.replace(LLM_TEST_SUB_OLD, LLM_TEST_SUB_NEW, 1)
        applied.append("测试文案")

    # 5) render() 里接入
    #
    # ## ⚠ 锚点必须落在 `render()` 的**函数体内**（F-020 的真实缺陷）
    #
    # 首版锚点写的是 `renderMisc(); renderVideoStatus(); }` —— 最后那个 `}`
    # 是 **render() 自己的结束括号**，`_insert_after` 于是把钩子插到了
    # **函数体外**，成了永不执行的死代码（`renderJetHub()` 从未被调用）。
    # 表现：**云端 OAuth 分页首屏空白**，要手动切一次左栏才出内容。
    #
    # 第二次改成「锚定下一个函数声明、插在它前面」也**不对** ——
    # 因为那个锚点字符串**带两空格缩进**，而 `render()` 的结束括号就在
    # 两段之间，钩子仍然落在 `}` 之后。
    #
    # 最终做法：锚定 `render()` 体内的**最后一条语句**，插在它**后面**。
    # 这条语句有一个天然优势 —— 它在函数体里唯一出现一次（`render()` 内），
    # 而 `renderVideoStatus()` 的**函数声明**在别处另有出现。
    if "renderJetHub();" not in text.replace("\r\n", "\n").split("function renderJetHub")[0]:
        anchor = """    renderMisc();
    renderVideoStatus();
  }"""
        text, ok = _insert_after(text, anchor, RENDER_HOOK)
        if not ok:
            raise SystemExit("找不到 render() 插入锚点")
        # 插在 `}` 之后会跑到函数体外 —— 把结束括号挪到钩子后面
        text = _move_brace_after_hook(text)
        applied.append("render")

    return text, applied


def _move_brace_after_hook(text: str) -> str:
    """把被 `_insert_after` 挤到钩子前面的 `render()` 结束括号挪到钩子之后。

    `_insert_after(anchor)` 的结果形如：

    ```js
        renderMisc();
        renderVideoStatus();
      }                        ← render() 的结束括号
        renderJetHub();        ← 钩子跑到了函数体外
        refreshJetHub();
    ```

    这里把那对「`}` + 钩子」的顺序对调，得到：

    ```js
        renderMisc();
        renderVideoStatus();
        renderJetHub();
        refreshJetHub();
      }                        ← 结束括号回到函数末尾
    ```

    只在**钩子紧随单个 `}`** 这种已知形态下生效，其它情况原样返回
    （宁可不动，也不要误改面板结构）。
    """
    newline = _nl(text)
    body = RENDER_HOOK.replace("\r\n", "\n").replace("\n", newline)
    # 形态：`<缩进>}` + 换行 + 钩子
    for brace in ("  }" + newline, "}" + newline):
        broken = brace + body.lstrip("\r\n")
        fixed = body.lstrip("\r\n").rstrip() + newline + brace.rstrip("\r\n")
        if broken in text:
            return text.replace(broken, fixed, 1)
    return text


CHECKS: list[tuple[str, str]] = [
    (CSS_MARK, "CSS 样式块"),
    ("jh-rail", "提供商左栏"),
    ('id="jethub-providers"', "提供商容器"),
    ("jh-card", "账号卡片样式"),
    (JS_MARK, "jhUnwrap 解包"),
    ("function renderJetHub", "renderJetHub"),
    ("btn-jethub-checkin", "一键签到按钮"),
    ("btn-jethub-balances", "刷新积分按钮"),
    # 备份/恢复：常驻卡片（不再弹窗）
    ("btn-jh-backup-pick", "备份「选择文件」按钮"),
    ("btn-jh-backup-import", "备份「开始恢复」按钮"),
    ("btn-jh-backup-export-download", "备份「下载到本地」按钮"),
    ("btn-jh-backup-export-server", "备份「留档」按钮"),
    ('id="jh-backup-file"', "隐藏的 file input"),
    ("jhBindWhenReady", "等元素就绪的绑定包装"),
    ("jh-switch", "模型开关控件"),
    ('id="tab-jethub"', "tab-jethub 容器"),
    ('id="card-jethub"', "card-jethub 字段卡片"),
    ("renderJetHub();", "render() 已接入"),
    ("按当前启用的", "连通性测试文案跟随来源"),
]

# 这些**必须不存在**（删除项 / 历史残留）
FORBIDDEN: list[tuple[str, str]] = [
    ('id="jh-backup-modal"', "备份弹窗（用户要求改为常驻卡片）"),
    ("btn-jethub-backup-open", "打开备份弹窗的按钮"),
    ('"key": "JETHUB_PROVIDER"', "重复的提供商下拉（用户要求删掉，左栏已足够）"),
]


def guard_raw_text(patched: str, css: str, js: str) -> list[str]:
    """守住「raw text 元素里不能出现字面量结束标签」这条硬约束。

    ## 为什么必须有这道闸（F-004 真实事故）

    `<style>` 与 `<script>` 是 HTML 的 **raw text 元素**：解析器在它们内部
    **不做标签解析、也不认 CSS/JS 注释**，一遇到 `</style>` / `</script>`
    就立刻关闭该元素。

    于是「在 CSS 注释里写一句 *注入到 `</style>` 之前*」这种看似无害的文档
    会产生致命后果：样式块在那个位置提前结束，**后面全部 CSS 变成页面可见
    文本** —— 用户看到的就是「面板出现一堆乱码」。本次开发真的踩了。

    所以这里做两道检查：
    1. 资源文件自身不得含字面量结束标签；
    2. 拼装后的结果里，`<style>` 与 `</style>` 必须**各只有一个**。
    """
    problems: list[str] = []
    # ⚠ 检查**开标签文本也要查**，不只查结束标签。
    #
    # 首版只查了 `</style>`，于是「修完第一次之后，把 `<style>` 写进说明注释」
    # 又触发了同一个 bug（第二次踩）—— 因为**开标签文本同样会被解析器当标签**。
    # 现在开闭两种写法都禁。
    for label, text, tags in (
        ("cloud-oauth.css", css, ("<style", "</style", "<script", "</script")),
        ("cloud-oauth.js", js, ("</script", "<script")),
    ):
        for danger in tags:
            if danger not in text:
                continue
            index = text.find(danger)
            line = text[:index].count("\n") + 1
            problems.append(
                f"{label} 第 {line} 行含标签文本 {danger!r} —— "
                f"它会在页面上提前关闭该元素，导致后续内容变成可见文本（F-004）"
            )
    return problems
    # 拼装后的整体检查。
    #
    # ⚠ 只检查 `<style>` —— 面板页里本来就有**一个** `<style>` 块，
    #   我们的 CSS 必须完整落在里面，不能多出第二个（那正是 F-004 的症状）。
    #   `<script>` **不检查数量**：页面本来就有多个（Vite 入口、面板注入、
    #   水印 boot 行），数量对不上不能说明有问题；脚本侧的防线是上面那条
    #   「资源文件不得含字面量结束标签」。
    lowered = patched.lower()
    opens = lowered.count("<style")
    closes = lowered.count("</style>")
    if opens != 1 or closes != 1:
        problems.append(
            f"结果里 <style> 数量异常：开 {opens} 处、闭 {closes} 处（各应恰好 1 处）"
            " —— 多于 1 处说明样式块被提前关闭（F-004）"
        )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="从 zip 还原并打「云端 OAuth」面板补丁")
    parser.add_argument("--check", action="store_true", help="只报告状态，不写文件")
    args = parser.parse_args()

    for path in (CSS_FILE, HTML_FILE, JS_FILE):
        if not path.is_file():
            print(f"✗ 缺少资源文件：{path}")
            return 1

    css = CSS_FILE.read_text(encoding="utf-8")
    html = HTML_FILE.read_text(encoding="utf-8")
    js = JS_FILE.read_text(encoding="utf-8")
    print(f"资源     : css={len(css)} html={len(html)} js={len(js)} 字符")

    # 资源文件自身的 raw-text 检查（在拼装前就拦下，报错更准）
    early = guard_raw_text("", css, js)
    resource_problems = [p for p in early if p.startswith("cloud-oauth.")]
    if resource_problems:
        print("✗ 资源文件不合格（raw text 冲突）：")
        for item in resource_problems:
            print(f"  ! {item}")
        return 1

    pristine = read_pristine()
    print(f"原版来自 : {ZIP_PATH.name}::{ZIP_MEMBER}（{len(pristine)} 字符）")

    current = TARGET.read_text(encoding="utf-8") if TARGET.is_file() else ""
    if current and not args.check:
        backup = TARGET.with_suffix(".html.before-repatch")
        backup.write_text(current, encoding="utf-8")
        print(f"已备份   : {backup.name}（{len(current)} 字符）")

    patched, applied = patch(pristine, css, html, js)
    print(f"应用补丁 : {applied if applied else '（无需改动）'}")
    print(f"结果大小 : {len(patched)} 字符")

    problems: list[str] = []
    for marker, label in CHECKS:
        if marker not in patched:
            problems.append(f"缺少{label}（{marker[:40]}）")
    for marker, label in FORBIDDEN:
        if marker in patched:
            problems.append(f"不该存在：{label}（{marker[:40]}）")
    if "账号" not in patched or "提供商" not in patched:
        problems.append("中文读不出来（编码损坏）")
    if "\ufffd" in patched:
        problems.append(f"含 {patched.count(chr(0xFFFD))} 个替换字符（编码损坏）")
    # raw text 元素护栏（F-004：CSS 注释里的 </style> 会让样式块提前关闭，
    # 后面全部 CSS 变成页面可见文本 —— 表现为「面板一堆乱码」）
    problems += guard_raw_text(patched, css, js)
    # 绑定时机护栏：脚本必须排在它要绑定的元素**之后**吗？不 ——
    # 脚本里的 `jhBind` 是**立即执行**的，所以被绑元素必须出现在脚本**之前**。
    # 「选择文件」原先就是踩了这个（modal 在 </script> 之后）而静默失效。
    # 现在备份/恢复卡片在 HTML 资源里、位于脚本之前；这里做一道位置断言。
    script_at = patched.rfind("jhBindWhenReady(\"btn-jh-backup-pick\"")
    file_at = patched.find('id="jh-backup-file"')
    if script_at > 0 and file_at > script_at:
        problems.append(
            "file input 出现在绑定脚本**之后** —— jhBindWhenReady 虽有兜底，"
            "但这是「选择文件点了没反应」的原始故障形态，必须避免"
        )

    if problems:
        print("✗ 结果不合格：")
        for item in problems:
            print(f"  ! {item}")
        return 1

    if args.check:
        print(f"✓ --check：{len(CHECKS)} 项内容齐备，未写文件")
        return 0

    TARGET.write_text(patched, encoding="utf-8")
    print(f"✓ 已写入 {TARGET}")
    cleanup = TARGET.with_suffix(".html.before-repatch")
    if cleanup.is_file():
        cleanup.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
