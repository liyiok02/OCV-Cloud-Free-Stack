"""shim 的探活与托管。

OCV 的图片阶段由 ``sys.executable`` 拉起独立子进程执行，而 shim 需要覆盖
整个图片阶段（任务状态、幂等键、结果图片都保存在它的内存与磁盘上）。

托管方式只有一个：**由长驻进程在自己进程里以守护线程托管**。

* 正常使用时是 OCV 的后端进程（见 ``patches._patch_main``）——
  与界面同生命周期；
* 自检 / 打开面板时是 ``cloud_stack_ctl.py`` 自己。

为什么不自动拉起分离进程（这里踩过坑）：分离想要真正独立，必须带
``CREATE_BREAKAWAY_FROM_JOB``，而该标志在受限环境（沙箱、部分启动器）
会被拒绝并抛 ``WinError 5``。降级之后子进程仍留在父进程的 job object 里，
父进程一退出就被连带杀掉 —— 表现是 shim 反复启动、又反复消失，日志里
能看到十几次「已启动」而端口上最终什么都没有。因此这条路径被彻底删掉，
宁可明确要求"由谁托管"，也不要一个看起来能自动修复、实际在制造僵尸的机制。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import urllib.request

from . import config


def is_listening(host: str | None = None, port: int | None = None, timeout: float = 0.35) -> bool:
    target_host = host or config.shim_host()
    target_port = int(port or config.shim_port())
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            return sock.connect_ex((target_host, target_port)) == 0
    except OSError:
        return False


def health() -> dict[str, object] | None:
    """问一下 shim 自己的身份（pid / 托管方式）。"""
    try:
        with urllib.request.urlopen(f"{config.shim_base_url()}/health", timeout=2.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, dict) else None


def ensure_in_process() -> bool:
    """在**当前进程**里托管 shim。

    只应该由长驻进程调用 —— 线程会随进程结束，短命进程调用它没有意义。
    """
    if is_listening():
        return True
    if not config.shim_autostart():
        return False
    try:
        # 延迟导入：image_shim 会拉起 requests/PIL 等依赖，注入路径不想付这个成本
        from . import image_shim

        return image_shim.serve_in_thread() is not None
    except Exception:  # noqa: BLE001 - 托管失败不能拖垮宿主进程
        return False


def stop() -> bool:
    """结束独立运行的 shim（供卸载脚本使用）。

    **必须先确认 pid 文件里那个 pid 真的是 shim**：pid 会被系统复用，而
    进程内托管模式下 shim 根本没有自己的 pid 文件。直接 taskkill 有把
    OCV 后端杀掉的真实风险，所以这里拿 ``/health`` 的回报做身份核对。
    """
    pid_path = config.plugin_var_dir() / "shim.pid"
    if not pid_path.is_file():
        return False
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pid_path.unlink(missing_ok=True)
        return False

    remote = health()
    remote_pid = remote.get("pid") if remote else None
    if not isinstance(remote_pid, int) or remote_pid != pid:
        # 端口上没有 shim，或 pid 对不上 —— 只清掉过期的 pid 文件，不杀任何进程
        pid_path.unlink(missing_ok=True)
        return False

    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True,
                check=False,
            )
        else:
            os.kill(pid, 15)
    except OSError:
        return False
    finally:
        try:
            pid_path.unlink(missing_ok=True)
        except OSError:
            pass
    return True
