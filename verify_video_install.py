"""验证 video-install / video-uninstall 的两条关键路径。

必须同时成立，否则会伤到用户：

  A. **原本没有视频配置**（大多数情况）
     install → uninstall 必须**逐字节还原**（不能留下空键）

  B. **用户已有真实视频服务商配置**（很可能发生：用户早就配了 RunningHub）
     install 接管 → uninstall 必须**原样还原用户那套配置**，一个值都不能丢

两条都在真实 .env 上跑，finally 按字节还原。
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(r"E:\1B1BLaoYang")
PLUGIN_DIR = PROJECT_ROOT / "plugins" / "cloud_free_stack"
CTL = PLUGIN_DIR / "cloud_stack_ctl.py"
PY = PROJECT_ROOT / "runtime" / "python" / "python.exe"
ENV = PROJECT_ROOT / ".env"

sys.path.insert(0, str(PLUGIN_DIR))
from ocv_cloud_stack import console  # noqa: E402

console.harden()

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


def run(*args: str) -> tuple[int, str]:
    p = subprocess.run([str(PY), str(CTL), *args], cwd=str(PROJECT_ROOT),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def vals() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in ENV.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip().lstrip("\ufeff")
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        out[k.strip()] = v.strip()
    return out


ORIGINAL = ENV.read_bytes()
VIDEO_KEYS = ["VIDEO_API_BASE_URL", "VIDEO_SUBMIT_PATH", "VIDEO_QUERY_PATH",
              "VIDEO_UPLOAD_PATH", "VIDEO_API_KEY", "VIDEO_RESOLUTION"]

try:
    print("== A) 原本没有视频配置：往返必须逐字节还原 ==")
    ENV.write_bytes(ORIGINAL)
    run("video-install")
    after_install = vals()
    check("install 后指向本地 shim",
          after_install.get("VIDEO_API_BASE_URL", "").startswith("http://127.0.0.1"),
          str(after_install.get("VIDEO_API_BASE_URL")))
    check("install 后 submit_path 指向 shim 的视频路由",
          after_install.get("VIDEO_SUBMIT_PATH") == "/openapi/v2/video/agnes/multimodal-video")
    run("video-uninstall")
    check("uninstall 后逐字节还原（无空键残留）", ENV.read_bytes() == ORIGINAL)
    leftover = [k for k in VIDEO_KEYS if k in vals()]
    check("6 个视频键全部消失", not leftover, str(leftover))

    print("\n== B) 用户已有真实视频服务商配置：必须原样还原 ==")
    # 造一份"用户自己的" .env：插件块 + 真实的视频配置
    ctl_text = ORIGINAL.decode("utf-8", errors="replace")
    user_video = [
        "VIDEO_API_BASE_URL=https://www.runninghub.ai",
        "VIDEO_SUBMIT_PATH=/openapi/v2/rhart-video/sparkvideo-2.0-fast/multimodal-video",
        "VIDEO_QUERY_PATH=/openapi/v2/query",
        "VIDEO_UPLOAD_PATH=/openapi/v2/media/upload/binary",
        "VIDEO_API_KEY=sk-user-real-video-key-999",
        "VIDEO_RESOLUTION=1080p",  # 注意：OCV 只允许 480p/720p，这里故意用非常规值测保真
    ]
    marker = "# ===== OCV 全云端免费栈插件 cloud_free_stack BEGIN（由 cloud_stack_ctl.py 维护）====="
    # ⚠ 必须用 CRLF 拼接：本仓 .env 全文是 CRLF，而 `_write_lines` 会把 `\n`
    #   统一翻成平台换行。若这里用 LF 插入，往返后那几行被规范化成 CRLF，
    #   逐字节比对就会"失败"—— 那是**测试自己造出来的**假差异，不是代码问题。
    #   （首版就这么误报过；真实 .env 里不存在混合换行。）
    crlf = "\r\n"
    if marker in ctl_text:
        head, _, rest = ctl_text.partition(marker)
        ENV.write_text(head + crlf.join(user_video) + crlf + marker + rest,
                       encoding="utf-8", newline="")
    else:
        ENV.write_text(ctl_text + crlf + crlf.join(user_video) + crlf,
                       encoding="utf-8", newline="")
    baseline = ENV.read_bytes()
    before = vals()

    code, _ = run("video-install")
    check("install 退出码 0", code == 0)
    mid = vals()
    check("接管后 base 变成 shim", mid.get("VIDEO_API_BASE_URL", "").startswith("http://127.0.0.1"))
    check("接管后记录了用户原值（PREV）",
          "CLOUD_STACK_PREV_VIDEO_API_KEY" in mid,
          str(mid.get("CLOUD_STACK_PREV_VIDEO_API_KEY", "")[:20]))

    code, _ = run("video-uninstall")
    check("uninstall 退出码 0", code == 0)
    after = vals()
    for key in VIDEO_KEYS:
        check(f"{key} 恢复为用户原值", after.get(key) == before.get(key),
              f"{after.get(key)!r} (应为 {before.get(key)!r})")
    check("往返后逐字节还原", ENV.read_bytes() == baseline)
    check("PREV 记录已清理",
          not any(k.startswith("CLOUD_STACK_PREV_VIDEO") for k in after))
finally:
    ENV.write_bytes(ORIGINAL)
    print(f"\n[cleanup] .env 按字节还原：{'一致' if ENV.read_bytes() == ORIGINAL else '不一致!!'}")

print()
print(f"通过 {PASS} 项，失败 {FAIL} 项")
sys.exit(1 if FAIL else 0)
