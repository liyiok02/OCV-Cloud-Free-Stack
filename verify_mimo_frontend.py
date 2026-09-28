# SPDX-License-Identifier: AGPL-3.0-only
"""真渲染验证：`panel.js` 注入的 MiMo 选项在**真实浏览器**里确实出现，且 qwen 文案被还原。

## 为什么要单独测这个（铁律 27）

1.10.0 的需求是「选择时可以**同时看见两个选项**」。这句话的验收标准在 **DOM** 里，
不在字符串里：

* `panel.js` 是靠 `MutationObserver` 在**运行时**往 OCV 的 `<select>` 里插 option 的；
* 纯字符串断言（"panel.js 里有 MIMO_VALUE"）**证明不了**它真的插进去了 ——
  选择器写错、`isEngineSelect` 判错、脚本在 `document.body` 就绪前跑，
  字符串断言全都是绿的（本仓库 F-006 就是这么栽的）。

所以本测试**真的用无头浏览器**加载一个模拟 OCV「执行方式」下拉的页面，
装进真实 `panel.js`，然后读 DOM 断言：

  A. 出现了 **两个** 云端选项：`mimo` 与 `qwen`；
  B. `qwen` 的文案是**原生** "Qwen-TTS"（说明"还原旧改写"生效了，铁律 60）；
  C. `mimo` 的文案是 "MiMo TTS（云端免费栈）"；
  D. 两个选项**同时可见**（这正是用户要的效果）；
  E. 页面无 JS 报错；
  F. 幂等：DOM 再变动一轮后不会插出第二个 mimo option。

找不到浏览器/puppeteer 时**明确跳过并说明**，不伪装通过。

    runtime\\python\\python.exe plugins\\cloud_free_stack\\verify_mimo_frontend.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PLUGIN_DIR.parents[1]

try:
    from plugins.cloud_free_stack.ocv_cloud_stack import console as _console

    _console.harden()
except Exception:  # noqa: BLE001
    pass

PASS = 0
FAIL = 0
SKIP = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {label}" + (f"  {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  {detail}" if detail else ""))


def skip(label: str, why: str) -> None:
    global SKIP
    SKIP += 1
    print(f"  [SKIP] {label}  {why}")


def find_browser() -> Path | None:
    """按**目录搜**版本号子目录找 Chromium 内核浏览器（与 verify_panel_render.py 同法）。"""
    roots = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/EdgeCore",
        Path(r"C:\Program Files (x86)\Microsoft\EdgeCore"),
        Path(r"C:\Program Files\Microsoft\EdgeCore"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application"),
        Path(r"C:\Program Files\Microsoft\Edge\Application"),
        Path(r"C:\Program Files\Google\Chrome\Application"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application"),
    ]
    for root in roots:
        if not root.is_dir():
            continue
        for name in ("msedge.exe", "chrome.exe"):
            direct = root / name
            if direct.is_file():
                return direct
        try:
            for child in sorted(root.iterdir(), reverse=True):
                if not child.is_dir():
                    continue
                for name in ("msedge.exe", "chrome.exe"):
                    candidate = child / name
                    if candidate.is_file():
                        return candidate
        except OSError:
            continue
    return None


PANEL_JS = PLUGIN_DIR / "panel" / "panel.js"


def main() -> int:
    panel_js = PANEL_JS.read_text(encoding="utf-8")

    print("== A) 结构断言（便宜的快检，先排除明显错误）==")
    check("panel.js 定义了 mimo 取值", 'MIMO_VALUE = "mimo"' in panel_js)
    check("panel.js 会创建 option 元素", 'document.createElement("option")' in panel_js)
    check("panel.js 用 indextts25 识别引擎下拉（不误伤别的 select）",
          'option[value="indextts25"]' in panel_js)
    check("panel.js 有还原 qwen 文案的分支", "QWEN_LABEL" in panel_js and "qwenOption" in panel_js)
    check("panel.js 不再无条件把 qwen 写成 MiMo",
          'option.textContent = "MiMo TTS（云端免费栈）";' not in panel_js)

    print("\n== B) 真渲染：无头浏览器加载 panel.js 并读 DOM ==")
    browser = find_browser()
    node_modules = PROJECT_ROOT / "node_modules" / "puppeteer-core"
    if browser is None:
        skip("真渲染验证", "本机未找到 Chromium 内核浏览器")
    elif not node_modules.is_dir():
        skip("真渲染验证", "未找到 puppeteer-core（node_modules）")
    else:
        print(f"    浏览器：{browser}")
        _run_render(browser, node_modules, panel_js)

    print()
    print(f"通过 {PASS} 项，失败 {FAIL} 项，跳过 {SKIP} 项")
    if FAIL:
        return 1
    if SKIP:
        print("（有跳过项：真渲染未执行，结论不完整）")
        return 0 if FAIL == 0 else 1
    return 0


def _run_render(browser: Path, node_modules: Path, panel_js: str) -> None:
    """搭一个模拟 OCV 执行方式下拉的页面，装真 panel.js，读 DOM。"""
    workdir = Path(tempfile.mkdtemp(prefix="ocv_mimo_fe_"))
    html_path = workdir / "index.html"
    js_copy = workdir / "panel.js"
    profile = workdir / "profile"
    script_path = workdir / "render.mjs"

    # panel.js 会去 fetch {API_BASE}/api/panel/state 探活。这里让它指向一个
    # **必然连不上**的端口 —— 面板服务不可用时，按钮状态点会变黄，
    # 但**选项注入逻辑必须照常工作**（那条路径不依赖面板服务）。
    # 这本身就是一条有价值的健壮性断言。
    js_copy.write_text(panel_js, encoding="utf-8")

    # 模拟 OCV 的执行方式下拉：三个原生选项，正是 Studio.vue:1089-1093 的形状
    html_path.write_text(
        """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>probe</title></head>
<body>
  <label class="tts-engine-select">
    <span>执行方式</span>
    <select id="engine">
      <option value="indextts25">本地 GPU · IndexTTS-2.5</option>
      <option value="cluster">集群 GPU · IndexTTS-2.5</option>
      <option value="qwen">Qwen-TTS</option>
    </select>
  </label>
  <select id="unrelated">
    <option value="a">无关下拉 A</option>
    <option value="qwen">无关下拉里的 qwen 文案</option>
  </select>
  <script src="./panel.js" data-api="http://127.0.0.1:1"></script>
</body></html>
""",
        encoding="utf-8",
    )

    script_path.write_text(
        """
// ⚠ ESM **不认 NODE_PATH**，必须用**绝对路径**导入 puppeteer-core
// （`.mjs` 下 `import 'puppeteer-core'` 会 ERR_MODULE_NOT_FOUND，
//   哪怕 NODE_PATH 指对了目录 —— 实测踩过）。
const puppeteerModule = await import(process.env.OCV_PUPPETEER_ENTRY);
const puppeteer = puppeteerModule.default ?? puppeteerModule;

const [url, browserPath, profileDir] = process.argv.slice(2);
const browser = await puppeteer.launch({
  executablePath: browserPath,
  headless: 'new',
  userDataDir: profileDir,
  args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'],
});
const page = await browser.newPage();
const errors = [];
page.on('pageerror', (e) => errors.push(String(e)));
await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
// panel.js 的注入是 debounce 250ms 的，等它跑完
await new Promise((r) => setTimeout(r, 1600));

const read = () => {
  const sel = document.getElementById('engine');
  const opts = sel ? Array.from(sel.options) : [];
  return opts.map((o) => ({ value: o.value, text: o.textContent.trim() }));
};

const first = await page.evaluate(read);

// 触发一次 DOM 变动，验证注入幂等（不会插出第二个 mimo）
await page.evaluate(() => {
  const d = document.createElement('div');
  d.id = 'tickle';
  document.body.appendChild(d);
});
await new Promise((r) => setTimeout(r, 900));
const second = await page.evaluate(read);

const unrelated = await page.evaluate(() =>
  Array.from(document.getElementById('unrelated').options).map((o) => o.textContent.trim()));

const btn = await page.evaluate(() => !!document.getElementById('ocvcs-btn'));

console.log(JSON.stringify({ first, second, unrelated, btn, errors }));
await browser.close();
""",
        encoding="utf-8",
    )

    try:
        entry = node_modules / "lib" / "cjs" / "puppeteer" / "puppeteer-core.js"
        if not entry.is_file():
            entry = node_modules / "lib" / "esm" / "puppeteer" / "puppeteer-core.js"
        proc = subprocess.run(
            ["node", str(script_path), html_path.as_uri(), str(browser), str(profile)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=180, cwd=str(PROJECT_ROOT),
            env={**os.environ, "OCV_PUPPETEER_ENTRY": entry.as_uri()},
        )
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        if proc.returncode != 0 or not stdout:
            check("无头浏览器渲染 panel.js", False,
                  f"exit={proc.returncode} {(stderr or stdout)[:300]}")
            return
        info = json.loads(stdout.splitlines()[-1])
    except Exception as exc:  # noqa: BLE001
        check("无头浏览器渲染 panel.js", False, f"{type(exc).__name__}: {exc}")
        return
    finally:
        import shutil

        shutil.rmtree(workdir, ignore_errors=True)

    first = info.get("first") or []
    second = info.get("second") or []
    values = [o["value"] for o in first]
    by_value = {o["value"]: o["text"] for o in first}

    print(f"    渲染出的选项：{[(o['value'], o['text']) for o in first]}")
    check("页面无 JS 报错", not info.get("errors"), str(info.get("errors"))[:200])
    check("原生三个选项都还在", {"indextts25", "cluster", "qwen"} <= set(values), str(values))
    check("新增了 mimo 选项", "mimo" in values, str(values))
    check(
        "mimo 文案正确",
        by_value.get("mimo") == "MiMo TTS（云端免费栈）",
        repr(by_value.get("mimo")),
    )
    check(
        "qwen 文案已还原为原生 Qwen-TTS（铁律 60）",
        by_value.get("qwen") == "Qwen-TTS",
        repr(by_value.get("qwen")),
    )
    check(
        "★ 两个云端选项同时可见（用户要的效果）",
        "mimo" in values and "qwen" in values,
        f"mimo + qwen 并存",
    )
    check(
        "mimo 排在 qwen 之后（两个云端选项相邻）",
        values.index("mimo") == values.index("qwen") + 1,
        str(values),
    )
    # 幂等：DOM 变动后再扫一轮，不能出现第二个 mimo
    check(
        "幂等：DOM 变动后仍只有一个 mimo option",
        [o["value"] for o in second].count("mimo") == 1,
        str([o["value"] for o in second]),
    )
    # 不误伤：无关下拉里的 qwen 文案不该被动
    unrelated = info.get("unrelated") or []
    check(
        "不误伤：无关下拉未被注入 mimo",
        not any("MiMo" in t for t in unrelated),
        str(unrelated),
    )
    check("悬浮球已挂载（面板服务不可用时也应挂上）", bool(info.get("btn")))


if __name__ == "__main__":
    raise SystemExit(main())
