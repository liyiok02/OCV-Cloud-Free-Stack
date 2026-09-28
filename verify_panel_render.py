"""验证面板**前端**真的能渲染出后端下发的分页（防「后端加了、前端没跟」漂移）。

## 为什么需要这个测试

用户实测报「没有在云端免费栈面板看到 Agnes」。根因不是后端：

* 后端 `panel.snapshot()` 确实下发了 `groups` 里的 `video` 分页与 7 个字段；
* 但 `panel/index.html` 里**硬编码**了分页列表
  （`["mimo","llm","image","misc"].forEach(renderGroup)` 与 `var tabs = [...]`），
  没有 `video` ⇒ 前端根本不渲染那个分组，字段再全也看不见。

这类「两端各有一份清单」的漂移**只有真去渲染才能发现**，纯后端断言永远绿。
所以本测试同时做两件事：

  A. **结构断言**：前端必须从 `state.groups` 驱动分页，不得再有硬编码白名单；
     每个后端 `group` 都要有对应的 `<section id="tab-…">` 容器。
  B. **真渲染断言**：无头浏览器打开面板页，验证「云端视频」tab 真的出现、
     点开后有 Agnes API Key 输入框。（本机有 EdgeCore，用它。）

A 节永远可跑；B 节在找不到可用浏览器时**明确跳过**并说明原因，不伪装通过。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = PROJECT_ROOT / "plugins" / "cloud_free_stack"
PANEL_HTML = PLUGIN_DIR / "panel" / "index.html"
for entry in (str(PROJECT_ROOT), str(PLUGIN_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

try:
    from ocv_cloud_stack import console as _console

    _console.harden()
except Exception:  # noqa: BLE001
    pass

from ocv_cloud_stack import config, image_shim, panel  # noqa: E402

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
    """找可用的 Chromium 内核浏览器。

    实测本机的 Edge 装在 ``C:\\Program Files (x86)\\Microsoft\\EdgeCore\\<版本>\\``
    （不是常见的 ``Edge\\Application``），所以这里**按目录搜**版本号子目录，
    而不是写死路径 —— 写死过一次就找不到、测试静默跳过。
    """
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
        # 直接命中
        for name in ("msedge.exe", "chrome.exe"):
            direct = root / name
            if direct.is_file():
                return direct
        # 版本号子目录
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


html = PANEL_HTML.read_text(encoding="utf-8", errors="replace")
snapshot = panel.snapshot()
group_keys = [g["key"] for g in snapshot["groups"]]

print("== A) 前端结构：分页必须由后端下发驱动 ==")
print(f"    后端分页：{group_keys}")

# 1) 不得再出现硬编码的分页白名单
check("render() 不再硬编码 [\"mimo\",\"llm\",\"image\",\"misc\"]",
      not re.search(r'\[\s*"mimo"\s*,\s*"llm"\s*,\s*"image"\s*,\s*"misc"\s*\]\s*\.forEach\(renderGroup\)', html),
      "仍在使用硬编码列表" if re.search(r'\.forEach\(renderGroup\)', html) and '"mimo", "llm", "image", "misc"' in html else "")
check("render() 从 state.groups 取分页",
      "state.groups" in html and "keys.forEach(renderGroup)" in html)

# 2) 每个后端分页都要有对应的 section 容器
missing_sections = []
for key in group_keys:
    if f'id="tab-{key}"' not in html:
        missing_sections.append(key)
check("每个后端分页都有 <section id=\"tab-…\"> 容器",
      not missing_sections, f"缺失：{missing_sections}" if missing_sections else "全部齐备")

# 3) 视频分页的关键 UI 元素
check("视频分页有卡片容器 card-video", 'id="card-video"' in html)
check("视频分页有状态区 video-status", 'id="video-status"' in html)
check("视频分页向用户说明启用三步", "video-install" in html)
check("状态灯在视频开启后才显示（默认不多一个红灯）",
      "status.video && status.video.enabled" in html)

# 4) 后端确实下发了视频字段与分组
video_fields = [f for f in snapshot["fields"] if f.get("group") == "video"]
video_keys = {f["key"] for f in video_fields}
check("后端下发 video 分组", "video" in group_keys, str(group_keys))
# ⚠ 断言**必需的键都在**，而不是"正好 N 个"：
#   写死数字会在每次新增字段时假失败（本轮加两个重试字段就撞了一次），
#   而那种失败完全不反映真实缺陷，只会诱导人去改数字。
REQUIRED_VIDEO_KEYS = {
    "AGNES_API_KEY", "AGNES_API_BASE", "AGNES_VIDEO_MODEL",
    "CLOUD_STACK_VIDEO_ENABLED", "CLOUD_STACK_VIDEO_MODE",
    "CLOUD_STACK_VIDEO_SIZE", "CLOUD_STACK_VIDEO_INLINE_MEDIA",
    "CLOUD_STACK_VIDEO_SUBMIT_RETRIES",
}
missing = REQUIRED_VIDEO_KEYS - video_keys
check("视频分页含全部必需字段", not missing,
      f"缺 {sorted(missing)}" if missing else f"{len(video_keys)} 个字段齐全")
check("后端下发 status.video", isinstance(snapshot["status"].get("video"), dict))
check("后端下发 agnes_configured", "agnes_configured" in snapshot["status"])
check("视频分页标签含 Agnes",
      any("Agnes" in g["label"] for g in snapshot["groups"] if g["key"] == "video"))

print("\n== B) 真渲染：用无头浏览器打开面板页并检查 DOM ==")
browser = find_browser()
node_modules = PROJECT_ROOT / "node_modules" / "puppeteer-core"
if browser is None:
    skip("无头渲染验证", "本机未找到 Chromium 内核浏览器")
elif not node_modules.is_dir():
    skip("无头渲染验证", "未找到 puppeteer-core（node_modules）")
else:
    print(f"    浏览器：{browser}")
    port = 8961
    saved_store = config.store_values()
    config.update_store({"CLOUD_STACK_SHIM_PORT": str(port),
                         "CLOUD_STACK_VIDEO_ENABLED": "1"})
    server = image_shim.serve_in_thread(port=port)
    profile = PROJECT_ROOT / "dev" / f"_panelprofile_{os.getpid()}"
    script_path = PROJECT_ROOT / "dev" / f"_panel_render_{os.getpid()}.cjs"
    try:
        # 用 puppeteer-core 真开页面、真读 DOM。
        script_path.write_text(
            """
const puppeteer = require(process.argv[2]);
(async () => {
  const url = process.argv[3];
  const browser = await puppeteer.launch({
    executablePath: process.argv[4],
    headless: 'new',
    userDataDir: process.argv[5],
    args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'],
  });
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  await page.goto(url, { waitUntil: 'networkidle2', timeout: 30000 });
  // 等 tabs 渲染出来
  await page.waitForSelector('#tabs button', { timeout: 15000 });
  const out = await page.evaluate(() => {
    const tabs = Array.from(document.querySelectorAll('#tabs button')).map(b => b.textContent);
    const videoSection = document.getElementById('tab-video');
    const card = document.getElementById('card-video');
    // 卡片里应渲染出 Agnes API Key 字段（由后端 fields 驱动）
    const cardText = card ? card.textContent : '';
    const hasKeyInput = !!document.querySelector('#card-video input[type="password"]');
    const selects = card ? Array.from(card.querySelectorAll('select')).map(s => s.getAttribute('data-key')) : [];
    const inputs = card ? Array.from(card.querySelectorAll('input')).map(i => i.getAttribute('data-key')) : [];
    const videoStatus = document.getElementById('video-status');
    return {
      tabs: tabs,
      hasVideoSection: !!videoSection,
      hasCard: !!card,
      cardTextLen: cardText.length,
      hasKeyInput: hasKeyInput,
      fieldKeys: selects.concat(inputs).filter(Boolean),
      videoStatusText: videoStatus ? videoStatus.textContent : '',
    };
  });
  out.errors = errors;
  console.log(JSON.stringify(out));
  await browser.close();
})().catch((e) => { console.error('RENDER_FAILED ' + e.message); process.exit(3); });
""",
            encoding="utf-8",
        )
        proc = subprocess.run(
            ["node", str(script_path), str(node_modules), f"http://127.0.0.1:{port}/panel",
             str(browser), str(profile)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
            cwd=str(PROJECT_ROOT),
        )
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        if proc.returncode != 0 or not stdout:
            check("无头浏览器渲染面板", False,
                  f"exit={proc.returncode} {stderr[:200] or stdout[:200]}")
        else:
            info = json.loads(stdout.splitlines()[-1])
            tabs = info.get("tabs") or []
            print(f"    渲染出的分页：{tabs}")
            check("无头浏览器成功渲染面板", True, f"{len(tabs)} 个分页")
            check("页面无 JS 报错", not info.get("errors"), str(info.get("errors"))[:150])
            check("存在「云端视频」分页按钮（用户能看到）",
                  any("云端视频" in t for t in tabs), str(tabs))
            check("tab-video 区块存在", bool(info.get("hasVideoSection")))
            check("card-video 卡片存在", bool(info.get("hasCard")))
            check("视频卡片渲染出 Agnes API Key 输入框", bool(info.get("hasKeyInput")))
            keys = info.get("fieldKeys") or []
            check("渲染出 AGNES_API_KEY 字段", "AGNES_API_KEY" in keys, str(keys))
            check("渲染出视频模型下拉", "AGNES_VIDEO_MODEL" in keys, str(keys))
            check("渲染出接管开关", "CLOUD_STACK_VIDEO_ENABLED" in keys, str(keys))
            status_text = info.get("videoStatusText") or ""
            check("状态区渲染出内容", bool(status_text.strip()), status_text[:60])
    finally:
        _mine = set(config.store_values()) - set(saved_store)
        # force=True：还原时要把用户的长真 Key 写回，同属被保护的写入。
        config.update_store(saved_store, force=True)
        config.update_store({}, remove=_mine, force=True)
        try:
            server.shutdown()
        except Exception:  # noqa: BLE001
            pass
        import shutil

        shutil.rmtree(profile, ignore_errors=True)
        script_path.unlink(missing_ok=True)
        # 浏览器退出后可能残留的缓存目录
        shutil.rmtree(profile, ignore_errors=True)

print()
print(f"通过 {PASS} 项，失败 {FAIL} 项，跳过 {SKIP} 项")
sys.exit(1 if FAIL else 0)
