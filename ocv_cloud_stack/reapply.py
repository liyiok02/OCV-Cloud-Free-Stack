"""重建被 OCV 软件更新抹掉的「派生注入状态」。

## 为什么需要这个模块

插件的落地状态分两层，性质完全不同：

* **基线层** —— 插件目录本身、面板覆盖层 ``var/config.json``、``runtime`` 里的
  ``.pth`` 锚点。OCV 启动器每次更新的归档里都有一份 ``update.json``，其
  ``protected_data`` 明确列出 ``.env`` / ``runtime`` / ``output`` / ``workspace`` /
  ``runtime_logs`` / user presets / third-party plugins，所以这一层
  **天然扛得住更新**。
* **派生层** —— 从基线层推导出来的注入物：``frontend/index.html`` 里那一行
  ``<script>``（以及锚点自身）。``frontend/`` 是会被更新**整体替换**的源码目录
  （用户数据在 ``workspace/``，不在 ``frontend/``），所以每次软件更新都会把注入
  抹掉。

2026-09-16 那次更新（``release_id 2026.09.16.1``，「图像配置与画面编辑更新」）正是
如此：``frontend/index.html`` 被换回 413 字节的干净 Vite 模板，界面右下角的
「云端免费栈」按钮消失，而插件的其余部分（MiMo 配音 / 商汤 LLM / 商汤出图接管）
全部照常工作 —— 故障面**只有**派生层。前端确认由 Vite dev server 直接提供
（``start_windows.bat`` 用 ``node vite.js frontend --port 5173``），
``frontend/dist/`` 是过期残留、不参与运行，所以注入目标就是 ``frontend/index.html``。

## 结论

派生层是**可重建**的：只要基线层还在，原地就能补回来。本模块就是重建器。
逻辑只此一份，被两个入口复用：

* **自动** —— :func:`patches._patch_main` 在 OCV 后端进程启动时后台调一次。
  后端是「``runtime`` 受保护 → 锚点必然存活 → 必然会导入 ``backend.app.main``」
  这条链上唯一长驻的进程，也就是更新之后唯一还能自动跑起来的时机。
* **手工** —— ``cloud_stack_ctl.py reinject`` 或插件目录下的
  ``重新注入云端免费栈.bat``。兜底自动化够不到的场景：``runtime`` 被整体删除、
  锚点被手工清掉、或换机器重新部署。

设计约束与 ``sitecustomize`` 一致：**绝不抛异常**。补不上最多是按钮不出现，
绝不能让 OCV 起不来。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

# 允许本模块被直接 `python ocv_cloud_stack/reapply.py` 跑起来 —— 这条路径
# 恰恰用于「锚点已灭、自动注入根本不会发生」的兜底场景，所以不能依赖包上下文。
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

try:  # 包内导入（被 bootstrap / patches / cloud_stack_ctl 调用）
    from . import console  # noqa: E402
except ImportError:  # 直接当脚本运行：__package__ 为空，相对导入无宿主
    from ocv_cloud_stack import console  # type: ignore[no-redef]  # noqa: E402


def _ctl() -> Any:
    """延迟导入控制台脚本。

    ``cloud_stack_ctl`` 在模块级就 ``from ocv_cloud_stack import ...``，与这里
    形成包 ↔ 脚本的环，只能在函数里导入。注入原语（标记、目标、锚点写入）
    全部复用它的实现，**不在这里复制一份** —— 两边标记一旦漂移，
    ``_strip_frontend_block`` 就会删不掉本模块写进去的块。
    """
    import cloud_stack_ctl

    return cloud_stack_ctl


# --------------------------------------------------------------------------
# 只读探针
# --------------------------------------------------------------------------

def anchor_state() -> dict[str, Any]:
    """``.pth`` 锚点现状。只读，不修复。"""
    ctl = _ctl()
    pth = ctl._pth_path()
    if pth is None:
        return {
            "present": False,
            "ok": False,
            "path": None,
            "detail": "找不到 runtime/python/Lib/site-packages（非便携安装？）",
        }
    if not pth.is_file():
        return {"present": False, "ok": False, "path": pth, "detail": "锚点文件不存在"}
    try:
        # 用 ascii 解码而不是 utf-8：锚点必须纯 ASCII，非 ASCII 会让
        # CPython 按 locale(GBK) 读 .pth 时整行丢失 —— 注入静默失效。
        text = pth.read_text(encoding="ascii")
    except OSError as exc:
        return {"present": True, "ok": False, "path": pth, "detail": f"锚点读不了：{exc}"}
    except UnicodeDecodeError:
        return {
            "present": True,
            "ok": False,
            "path": pth,
            "detail": "锚点含非 ASCII 字节，中文 Windows 下注入会静默失效",
        }
    if ctl._pth_line(str(_PLUGIN_ROOT)) not in text:
        return {
            "present": True,
            "ok": False,
            "path": pth,
            "detail": "锚点指向的不是当前插件目录（整合包被移动过？）",
        }
    return {"present": True, "ok": True, "path": pth, "detail": "锚点正常"}


def frontend_state() -> dict[str, Any]:
    """前端注入现状。只读，不修复。"""
    ctl = _ctl()
    targets = ctl._frontend_targets()
    if not targets:
        return {
            "targets": [],
            "missing": [],
            "ok": True,
            "detail": "找不到前端入口，跳过（纯后端部署？）",
        }
    missing: list[Path] = []
    for path in targets:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            missing.append(path)
            continue
        if ctl.FRONTEND_MARK_BEGIN not in text:
            missing.append(path)
    ok = not missing
    return {
        "targets": targets,
        "missing": missing,
        "ok": ok,
        "detail": "注入完好" if ok else f"{len(missing)}/{len(targets)} 个前端入口缺少注入块",
    }


# --------------------------------------------------------------------------
# 修复（全部幂等，可反复调用）
# --------------------------------------------------------------------------

def ensure_anchor() -> dict[str, Any]:
    """确保 ``.pth`` 锚点存在且指向本插件。"""
    ctl = _ctl()
    before = anchor_state()
    if before["ok"]:
        return {**before, "changed": False}
    if before.get("path") is None:
        return {**before, "changed": False}
    written = ctl.install_injection()
    after = anchor_state()
    return {**after, "changed": bool(written is not None and after["ok"])}


def ensure_frontend() -> dict[str, Any]:
    """确保每个前端入口都带注入块。"""
    ctl = _ctl()
    before = frontend_state()
    touched: list[Path] = ctl.inject_frontend() if before["missing"] else []
    after = frontend_state()
    return {**after, "changed": bool(touched), "touched": touched}


def repair() -> dict[str, Any]:
    """执行全部可自动完成的修复，返回结构化报告。绝不抛异常。"""
    console.harden()
    report: dict[str, Any] = {
        "anchor": None,
        "frontend": None,
        "repaired": [],
        "errors": [],
    }
    for name, step in (("anchor", ensure_anchor), ("frontend", ensure_frontend)):
        try:
            result = step()
        except Exception as exc:  # noqa: BLE001 - 修复失败不得影响 OCV
            report["errors"].append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        report[name] = result
        if result.get("changed"):
            report["repaired"].append(name)
    return report


# --------------------------------------------------------------------------
# 对外入口
# --------------------------------------------------------------------------

_LABELS = {"anchor": ".pth 自动注入锚点", "frontend": "前端面板入口（右下角按钮）"}


def run(
    verbose: bool = True,
    emit: Callable[[str], None] = print,
) -> int:
    """修复并报告。返回进程退出码（0 = 派生状态齐全）。"""
    console.harden()
    before = {"anchor": anchor_state(), "frontend": frontend_state()}
    was_ok = all(state["ok"] for state in before.values())

    report = repair()

    if verbose:
        emit("== 派生注入状态自检 ==")
        for name in ("anchor", "frontend"):
            state = before[name]
            emit(f"  修复前 · {_LABELS[name]:<24} : {'正常' if state['ok'] else state['detail']}")
        emit("")

        if report["repaired"]:
            emit("== 已修复 ==")
            for name in report["repaired"]:
                emit(f"  已重建 {_LABELS[name]}")
        elif was_ok:
            emit("== 无需修复 ==")
            emit("  派生注入状态完好，本次未改动任何文件。")
        else:
            emit("== 修复未完成 ==")
            for name in ("anchor", "frontend"):
                state = report[name]
                if state is None or not state.get("ok"):
                    detail = state["detail"] if state else "步骤未执行"
                    emit(f"  ! {_LABELS[name]} 仍不正常：{detail}")

        for err in report["errors"]:
            emit(f"  ! 修复过程出错：{err}")

        emit("")
        emit("== 修复后 ==")
        for name in ("anchor", "frontend"):
            state = report[name] or before[name]
            emit(f"  {_LABELS[name]:<24} : {'正常' if state.get('ok') else '仍异常'}  —— {state.get('detail', '')}")

        if report["repaired"]:
            emit("")
            emit("  提示：前端由 Vite dev server 直接读取，刷新浏览器页面即可看到按钮。")

    ok = all((report[name] or before[name]).get("ok") for name in ("anchor", "frontend"))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    """``python ocv_cloud_stack/reapply.py`` 独立入口（不经过 cloud_stack_ctl）。"""
    del argv
    return run(verbose=True)


if __name__ == "__main__":
    raise SystemExit(main())
