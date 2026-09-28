"""验证卸载不再冲掉用户设置（F-003 回归）。

故障实录（2026-09-23，我自己的卸载验证造成的真实损失）：
  早期 `cmd_uninstall` 是 `shutil.copy2(.env.cloud_stack_backup, .env)` ——
  **整文件覆盖**。而那份备份由 `_backup_env()` 在**首次安装时创建一次**
  （实测是 08-28，比会话开始早 26 天）。于是用户此后对 .env 的所有改动
  在卸载时被静默冲掉：

      ASR_DEVICE          cpu  -> auto     （无 N 卡机器会白试 CUDA）
      DASHSCOPE_API_KEY   真实Key -> 占位值
      IMAGE_MODEL_ID      u1.5-lite -> 快照里的旧值

本脚本做**真实的 install → uninstall → install 往返**（在 .env 的副本上做不了，
因为这些函数直接读项目根的 .env），因此它是**破坏性**的：
运行前先把 .env 完整备份，`finally` 里按字节还原。

断言的核心不变量：
  卸载只撤掉「插件块」与「插件顶掉的 4 个原生键」，
  **插件块外、且不是插件顶掉的键，一个字节都不许变。**
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = PROJECT_ROOT / "plugins" / "cloud_free_stack"
CTL = PLUGIN_DIR / "cloud_stack_ctl.py"
PYTHON = PROJECT_ROOT / "runtime" / "python" / "python.exe"
ENV = PROJECT_ROOT / ".env"

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
    proc = subprocess.run(
        [str(PYTHON), str(CTL), *args],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def parse_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        out[k.strip()] = v.strip()
    return out


BACKUP = ENV.with_name(".env.__f003_guard")
original = ENV.read_bytes()

# 插件块外、非插件顶掉的键：这些在卸载往返中必须**一字不变**
SENTINEL_KEYS = ["ASR_DEVICE", "ASR_MODEL", "ASR_LANGUAGE",
                 "RUNNINGHUB_ENDPOINT", "RUNNINGHUB_BASE_URL"]

try:
    BACKUP.write_bytes(original)
    before = parse_env(ENV)
    print("== 起点（会话当前 .env）==")
    for k in SENTINEL_KEYS:
        print(f"    {k} = {before.get(k, '<缺失>')}")

    print("\n== 1) install ==")
    code, out = run("install")
    check("install 退出码 0", code == 0, f"exit={code}")
    after_install = parse_env(ENV)
    check("install 未改动 ASR_DEVICE（块外键）",
          after_install.get("ASR_DEVICE") == before.get("ASR_DEVICE"),
          f"{before.get('ASR_DEVICE')} -> {after_install.get('ASR_DEVICE')}")

    print("\n== 2) uninstall ==")
    code, out = run("uninstall")
    check("uninstall 退出码 0", code == 0, f"exit={code}")
    after_uninstall = parse_env(ENV)

    print("\n== 3) 核心断言：块外用户设置必须原样存活 ==")
    for k in SENTINEL_KEYS:
        check(f"卸载后 {k} 未被冲掉",
              after_uninstall.get(k) == before.get(k),
              f"{before.get(k)!r} -> {after_uninstall.get(k)!r}")

    check("插件块已从 .env 移除",
          "cloud_free_stack BEGIN" not in ENV.read_text(encoding="utf-8", errors="replace"))
    check("DASHSCOPE_API_KEY 未被清空",
          bool(after_uninstall.get("DASHSCOPE_API_KEY")),
          f"值长度 {len(after_uninstall.get('DASHSCOPE_API_KEY') or '')}")
    check("卸载留存了 before 副本",
          bool(list(ENV.parent.glob(".env.uninstalled_*"))),
          "便于回溯")

    print("\n== 4) 重新 install 恢复可用状态 ==")
    code, out = run("install")
    check("重装退出码 0", code == 0, f"exit={code}")
    final = parse_env(ENV)
    check("重装后 ASR_DEVICE 仍为用户值",
          final.get("ASR_DEVICE") == before.get("ASR_DEVICE"),
          f"{final.get('ASR_DEVICE')!r}")
    check("重装后插件块存在",
          "cloud_free_stack BEGIN" in ENV.read_text(encoding="utf-8", errors="replace"))
finally:
    # 按字节还原：这些测试会改动真实 .env
    ENV.write_bytes(original)
    BACKUP.unlink(missing_ok=True)
    for stale in ENV.parent.glob(".env.uninstalled_*"):
        stale.unlink(missing_ok=True)
    restored = ENV.read_bytes()
    print(f"\n[cleanup] .env 已按字节还原：{'一致' if restored == original else '不一致!!'}")

print()
print(f"通过 {PASS} 项，失败 {FAIL} 项")
sys.exit(1 if FAIL else 0)
