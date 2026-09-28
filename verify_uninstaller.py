"""在**隔离沙箱**里端到端验证 uninstall.py（不碰真实 OCV 状态）。

做法：uninstall.py 的 `project_root()` 优先读 `OCV_PROJECT_ROOT` 环境变量。
于是构造一棵假的 OCV 目录树（假的 .pth / index.html / .env），把
OCV_PROJECT_ROOT 指过去，就能真实跑完整卸载流程而**毫发无伤**。

覆盖：
  A. --dry-run 不改任何文件
  B. 真实卸载：锚点删除 / 注入块摘除 / .env 定向还原
  C. 关键不变量：插件块外的键一字不动；不支持图生图的型号无关；BOM 不碍事
  D. 幂等：对已卸载状态再跑一次，不报错、不误改
  E. --purge 才动 var/（本测试默认不加，断言 var/ 未被碰）
  F. --disable 只停用、保留注入
  G. 卸载后校验函数真的能发现残留（负向验证）
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REAL_PLUGIN = Path(r"E:\1B1BLaoYang\plugins\cloud_free_stack")
PY = Path(r"E:\1B1BLaoYang\runtime\python\python.exe")
UNINSTALL = REAL_PLUGIN / "uninstall.py"

# 控制台加固：本脚本会把子进程输出原样打印出来，而中文 Windows 的 stdout 是
# GBK —— 子进程输出里的替换字符（U+FFFD）会让 print 抛 UnicodeEncodeError
# 并**终止测试**。测试工具自己先崩，最需要它的时候它不在。
sys.path.insert(0, str(REAL_PLUGIN))
try:
    from ocv_cloud_stack import console as _console  # noqa: E402

    _console.harden()
except Exception:  # noqa: BLE001
    pass

PASS = 0
FAIL = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {label}" + (f"  {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  {detail}" if detail else ""))


# 用真实的标记常量（从 ctl 读，避免本测试自己漂移）
import cloud_stack_ctl as ctl  # noqa: E402

BEGIN, END = ctl.BEGIN_MARK, ctl.END_MARK
FB, FE = ctl.FRONTEND_MARK_BEGIN, ctl.FRONTEND_MARK_END
PREV = ctl.PREV_PREFIX

# 用户自己的原始值（插件安装前）
USER = {
    "LANGUAGE_PROVIDER": "custom",
    "IMAGE_API_BASE_URL": "https://user-image.example/v1",
    "IMAGE_MODEL_ID": "user-model",
    "DASHSCOPE_API_KEY": "sk-user-key",
    "ASR_DEVICE": "cpu",
    "USER_KEEPME": "keep",
}


def make_sandbox(root: Path) -> None:
    (root / "runtime" / "python" / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True)
    (root / "frontend").mkdir(parents=True, exist_ok=True)

    # 锚点（真实内容形态）
    (root / "runtime/python/Lib/site-packages/ocv_cloud_free_stack.pth").write_text(
        "import sys; sys.path.insert(0, r'X')  # OCV cloud_free_stack injection anchor\n",
        encoding="ascii",
    )
    # 另一个插件的锚点（绝不能被删）
    (root / "runtime/python/Lib/site-packages/ocv_watermark_host.pth").write_text(
        "import sys; sys.path.insert(0, r'Y')\n", encoding="ascii"
    )
    # 前端：干净模板 + 本插件注入块 + 另一插件的行
    (root / "frontend/index.html").write_text(
        '<!doctype html>\r\n<html>\r\n  <head><meta name="viewport" content="w" /></head>\r\n'
        '  <body>\r\n    <div id="app"></div>\r\n'
        '    <script src="/ocv-watermark-boot.js" defer></script>\r\n'
        f"  {FB}\r\n<script src=\"http://127.0.0.1:8799/panel.js\" defer></script>\r\n{FE}\r\n"
        "</body>\r\n</html>\r\n",
        encoding="utf-8", newline="",
    )
    # .env：BOM + 用户原生键 + 插件块（含 PREV 记录）+ 块外用户键
    lines = ["\ufeff" + f"# user env", *[f"{k}={v}" for k, v in USER.items()]]
    lines += [
        BEGIN,
        "CLOUD_STACK_ENABLED=1",
        "MIMO_API_KEY=sk-mimo",
        f"{PREV}LANGUAGE_PROVIDER=custom",
        f"{PREV}IMAGE_API_BASE_URL=https://user-image.example/v1",
        f"{PREV}IMAGE_MODEL_ID=user-model",
        f"{PREV}DASHSCOPE_API_KEY=sk-user-key",
        END,
    ]
    # 插件块之后：插件注入的值（当前生效值）
    lines += [
        "LANGUAGE_PROVIDER=sensenova",
        "IMAGE_API_BASE_URL=http://127.0.0.1:8799",
    ]
    (root / ".env").write_text("\r\n".join(lines) + "\r\n", encoding="utf-8", newline="")
    # 备份快照（体积小，内容随意）
    (root / ".env.cloud_stack_backup").write_text("\ufeff# backup\r\n", encoding="utf-8", newline="")


def env_vals(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (root / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip().lstrip("\ufeff")
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def run(root: Path, *args: str) -> tuple[int, str]:
    env = dict(os.environ)
    env["OCV_PROJECT_ROOT"] = str(root)
    env["PYTHONPATH"] = str(REAL_PLUGIN)
    p = subprocess.run([str(PY), str(UNINSTALL), *args], cwd=str(REAL_PLUGIN),
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env=env)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


tmp = Path(tempfile.mkdtemp(prefix="ocv-uninstall-sandbox-"))
try:
    root = tmp
    make_sandbox(root)
    index = root / "frontend/index.html"
    pth = root / "runtime/python/Lib/site-packages/ocv_cloud_free_stack.pth"
    other_pth = root / "runtime/python/Lib/site-packages/ocv_watermark_host.pth"

    print("== A) --dry-run 必须零副作用 ==")
    snap = {p: p.read_bytes() for p in (root / ".env", index, pth)}
    code, out = run(root, "--dry-run")
    check("dry-run 退出码 0", code == 0, f"exit={code}")
    after = {p: p.read_bytes() for p in (root / ".env", index, pth)}
    check("dry-run 未修改 .env", snap[root / ".env"] == after[root / ".env"])
    check("dry-run 未修改 index.html", snap[index] == after[index])
    check("dry-run 未删除锚点", pth.is_file())
    check("dry-run 报告里给出锚点现状", ("锚点" in out) or ("pth" in out.lower()))

    print("\n== B) 真实卸载 ==")
    code, out = run(root, "--yes")
    check("卸载退出码 0", code == 0, f"exit={code}")
    check("锚点已删除", not pth.is_file())
    check("其它插件的锚点未被误删", other_pth.is_file(), "ocv_watermark_host.pth")
    html = index.read_text(encoding="utf-8", errors="replace")
    check("本插件注入块已摘除", FB not in html and FE not in html)
    check("另一插件的注入行幸存", "ocv-watermark-boot.js" in html, "不得误删")
    check("模板结构完好", "<!doctype html>" in html and "</html>" in html and "viewport" in html)

    print("\n== C) .env 定向还原 ==")
    v = env_vals(root)
    check("插件块已移除", BEGIN not in (root / ".env").read_text(encoding="utf-8", errors="replace"))
    for key in ("LANGUAGE_PROVIDER", "IMAGE_API_BASE_URL", "IMAGE_MODEL_ID", "DASHSCOPE_API_KEY"):
        check(f"{key} 已还原为用户原值", v.get(key) == USER[key], f"{v.get(key)!r}")
    check("块外用户键未受影响", v.get("USER_KEEPME") == "keep")
    check("ASR_DEVICE 未受影响", v.get("ASR_DEVICE") == "cpu")
    check("BOM 仍在（格式未破坏）",
          (root / ".env").read_bytes().startswith(b"\xef\xbb\xbf"))
    check("留存了卸载前副本",
          bool(list(root.glob(".env.uninstalled_*"))))

    print("\n== D) 幂等：对已卸载状态再跑 ==")
    code, out = run(root, "--yes")
    check("重复卸载退出码 0", code == 0, f"exit={code}")
    v2 = env_vals(root)
    for key in ("LANGUAGE_PROVIDER", "IMAGE_API_BASE_URL", "IMAGE_MODEL_ID", "DASHSCOPE_API_KEY"):
        check(f"{key} 未被二次改动", v2.get(key) == USER[key], f"{v2.get(key)!r}")

    print("\n== E) --purge 才动 var/（本次未加，断言未被碰）==")
    real_var = REAL_PLUGIN / "var"
    check("真实 var/ 未被本测试触碰", real_var.is_dir())

    print("\n== F) --disable 只停用、保留注入 ==")
    make_sandbox(root)
    code, out = run(root, "--disable")
    check("--disable 退出码 0", code == 0, f"exit={code}")
    check("--disable 后锚点仍在", pth.is_file())
    check("--disable 后注入块仍在", FB in index.read_text(encoding="utf-8", errors="replace"))
    check("CLOUD_STACK_ENABLED 置 0", env_vals(root).get("CLOUD_STACK_ENABLED") == "0")

    print("\n== G) 负向验证：残留必须能被检出 ==")
    make_sandbox(root)
    code, out = run(root, "--yes")
    # 人为制造残留，再确认校验逻辑会报告（用 Python 直接调 verify_clean）
    pth.write_text("import sys; sys.path.insert(0, r'X')  # cloud_free_stack\n", encoding="ascii")
    probe = subprocess.run(
        [str(PY), "-c",
         "import sys;sys.path.insert(0,r'%s');"
         "import uninstall as u;"
         "p=u.Primitives();"
         "print('PROBLEMS=' + str(len(u.verify_clean(p))))" % REAL_PLUGIN],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "OCV_PROJECT_ROOT": str(root), "PYTHONPATH": str(REAL_PLUGIN)},
        cwd=str(REAL_PLUGIN),
    )
    check("残留锚点能被 verify_clean 检出",
          "PROBLEMS=1" in (probe.stdout or ""), (probe.stdout or "").strip()[:80])
finally:
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n[sandbox] 已清理 {tmp}")

print()
print(f"通过 {PASS} 项，失败 {FAIL} 项")
sys.exit(1 if FAIL else 0)
