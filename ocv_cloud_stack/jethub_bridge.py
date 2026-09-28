# SPDX-License-Identifier: AGPL-3.0-only
"""Jet Hub 桥的 Python 侧托管：把 Node 桥拉起来，并暴露给 OCV 与插件面板。

## 为什么用子进程而不是进程内

桥的实现必须是 Node —— `dsh-codearts-auth` 的适配器是 ESM JS，且**不可重写**
（它封装了 CodeBuddy 的认证协议、多账号限流轮换、SSE 容错，是本插件最值钱的部分）。
Python 侧只做三件事：**拉起、探活、转发**。

## 为什么是「长驻单实例」

账号池的正确性前提是**进程内权威副本 + 读→改→整体写回**
（`account-pool.ts:48-56` 记录了多账号限流记录被互相覆盖的真实缺陷）。
多进程会各持一份内存状态、互相覆盖 `state.json`，所以：

* **一个 Node 进程**，由 OCV 后端进程内托管（守护线程 + `subprocess.Popen`）；
* 面板与 LLM 请求都打到**同一个**端口。

## 为什么不用分离进程

沿用 `cloud_free_stack` 既有结论（`shim_launcher.py:12-17`）：在受限环境里
`CREATE_BREAKAWAY_FROM_JOB` 会被拒绝，产物是一批随父进程消失的僵尸。
所以走「父进程守护线程 + 普通子进程」，与 OCV 后端同生命周期。

## 端口

默认 **8801**（与 cloud_free_stack 的 shim 8799、OCV 8010、Vite 5173 都错开）。
可用面板 `JETHUB_BRIDGE_PORT` 覆盖。
"""

from __future__ import annotations

import atexit
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
NODE_ENTRY = PLUGIN_ROOT / "jethub" / "server.mjs"
DEFAULT_PORT = 8801

_proc: subprocess.Popen | None = None
_lock = threading.Lock()
_started_at: float = 0.0


def _log(message: str) -> None:
    try:
        print(f"[jethub-bridge] {message}", flush=True)
    except Exception:  # noqa: BLE001 - 日志永不致命
        pass


# ---------------------------------------------------------------------------
# 解释器与端口
# ---------------------------------------------------------------------------


def node_executable() -> str | None:
    """优先用 OCV 自带的 Node，其次 PATH 里的。

    OCV 便携包自带 `runtime/node/node.exe`（实测 v24.14.0，满足 vendor 的
    `engines: ^22.19.0 || >=24.0.0`）。自带的好处是**目标机不用装 Node**。
    """
    candidates = [
        PLUGIN_ROOT.parents[1] / "runtime" / "node" / "node.exe",
        PLUGIN_ROOT.parents[1] / "runtime" / "node" / "bin" / "node",
    ]
    for path in candidates:
        if path.is_file():
            return str(path)
    import shutil

    return shutil.which("node")


def bridge_port() -> int:
    try:
        from . import config

        raw = config.get("JETHUB_BRIDGE_PORT", "").strip()
        if raw:
            return int(raw)
    except Exception:  # noqa: BLE001
        pass
    return DEFAULT_PORT


def bridge_base_url() -> str:
    return f"http://127.0.0.1:{bridge_port()}"


def _probe(port: int, timeout: float = 0.6) -> bool:
    """探活：能连上 TCP 即认为有人在监听。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex(("127.0.0.1", int(port))) == 0


def is_listening(port: int | None = None) -> bool:
    return _probe(port if port is not None else bridge_port())


# ---------------------------------------------------------------------------
# 拉起 / 停止
# ---------------------------------------------------------------------------


def ensure_running(timeout: float = 25.0) -> str | None:
    """确保桥在跑。返回 None 表示成功，否则返回错误说明。

    幂等：已在监听就直接返回。这是唯一被外部调用的入口
    （`patches._patch_main` 的后台线程、面板的 `/api/panel/jethub/ensure`）。
    """
    global _proc, _started_at

    with _lock:
        port = bridge_port()
        if is_listening(port):
            return None
        if not NODE_ENTRY.is_file():
            return f"缺少桥入口 {NODE_ENTRY}"
        node = node_executable()
        if node is None:
            return "找不到 Node 运行时（既无 runtime/node/node.exe，PATH 里也没有 node）"

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        # 桥自己会钉状态目录；这里额外传一份，便于排查
        env["JETHUB_PLUGIN_ROOT"] = str(PLUGIN_ROOT)

        try:
            creationflags = 0
            if os.name == "nt":
                # 只开新进程组（不脱离 job object）—— 见模块文档
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            _proc = subprocess.Popen(
                [node, str(NODE_ENTRY), "--port", str(port), "--plugin-root", str(PLUGIN_ROOT)],
                cwd=str(PLUGIN_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                creationflags=creationflags,
            )
            _started_at = time.time()
        except OSError as exc:
            return f"启动桥进程失败：{exc}"

        threading.Thread(target=_drain_output, name="jethub-bridge-log", daemon=True).start()

        deadline = time.time() + timeout
        while time.time() < deadline:
            if is_listening(port):
                _log(f"桥已就绪：{bridge_base_url()}")
                return None
            if _proc.poll() is not None:
                return f"桥进程启动后立即退出（退出码 {_proc.returncode}）"
            time.sleep(0.25)
        return f"等待桥监听 {port} 端口超时（{timeout:.0f} 秒）"


def _drain_output() -> None:
    """把子进程 stdout 转进本进程日志，避免管道塞满导致子进程阻塞。"""
    proc = _proc
    if proc is None or proc.stdout is None:
        return
    try:
        for line in proc.stdout:
            text = line.rstrip()
            if text:
                _log(text)
    except Exception:  # noqa: BLE001
        pass


def stop(timeout: float = 5.0) -> bool:
    """停掉桥进程（仅限本进程拉起的那个）。"""
    global _proc

    with _lock:
        proc = _proc
        _proc = None
    if proc is None or proc.poll() is not None:
        return False
    try:
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=timeout)
        return True
    except Exception:  # noqa: BLE001
        return False


atexit.register(lambda: stop(timeout=2.0))


# ---------------------------------------------------------------------------
# HTTP 转发：面板与 Python 侧都通过它访问桥
# ---------------------------------------------------------------------------


def call(path: str, payload: dict[str, Any] | None = None, *, timeout: float = 30.0) -> dict[str, Any]:
    """向桥发一个 JSON 请求。

    返回统一形状：`{"ok": bool, "status": int, "data": Any, "error": str}`。
    **绝不抛异常** —— 调用方（面板 handler、自检）都按返回值分支。
    """
    url = f"{bridge_base_url()}{path}"
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urlrequest.Request(url, data=body, headers=headers, method="POST" if body else "GET")
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urlerror.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        status = exc.code
    except (urlerror.URLError, OSError, TimeoutError) as exc:
        return {"ok": False, "status": 0, "data": None, "error": f"桥不可达：{exc}"}

    try:
        data = json.loads(raw) if raw.strip() else None
    except json.JSONDecodeError:
        return {"ok": False, "status": status, "data": None, "error": f"桥返回了非 JSON：{raw[:200]}"}

    if status >= 400:
        detail = ""
        if isinstance(data, dict):
            detail = str(data.get("error") or data.get("message") or "")
        return {"ok": False, "status": status, "data": data, "error": detail or f"HTTP {status}"}
    return {"ok": True, "status": status, "data": data, "error": ""}


def status_snapshot() -> dict[str, Any]:
    """给面板「运行状态」用的快照（**不含任何凭据**）。"""
    port = bridge_port()
    node = node_executable()
    snapshot: dict[str, Any] = {
        "port": port,
        "base_url": bridge_base_url(),
        "listening": is_listening(port),
        "node": node or "",
        "node_found": node is not None,
        "entry": str(NODE_ENTRY),
        "entry_found": NODE_ENTRY.is_file(),
        "pid": _proc.pid if _proc is not None and _proc.poll() is None else None,
        "started_at": _started_at or None,
    }
    if snapshot["listening"]:
        health = call("/health", timeout=3.0)
        snapshot["health"] = health.get("data") if health["ok"] else None
        snapshot["health_error"] = "" if health["ok"] else health["error"]
    return snapshot


def main(argv: list[str] | None = None) -> int:
    """命令行入口：`python -m ocv_cloud_stack.jethub_bridge start|stop|status`。"""
    action = (argv or sys.argv[1:] or ["status"])[0]
    if action == "start":
        err = ensure_running()
        print(f"✗ {err}" if err else f"✓ 桥已就绪：{bridge_base_url()}")
        return 1 if err else 0
    if action == "stop":
        print("✓ 已停止" if stop() else "（没有由本进程拉起的桥）")
        return 0
    snapshot = status_snapshot()
    print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    return 0 if snapshot["listening"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
