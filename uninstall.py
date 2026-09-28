"""OCV 全云端免费栈插件 —— 独立卸载脚本（standalone uninstaller）。

用法（插件目录下）::

    runtime\\python\\python.exe plugins\\cloud_free_stack\\uninstall.py            # 交互确认后卸载
    runtime\\python\\python.exe plugins\\cloud_free_stack\\uninstall.py --dry-run  # 只报告，不改任何东西
    runtime\\python\\python.exe plugins\\cloud_free_stack\\uninstall.py --yes      # 不询问（供 .bat 调用）
    runtime\\python\\python.exe plugins\\cloud_free_stack\\uninstall.py --purge    # 连 var/ 运行期数据一起清
    runtime\\python\\python.exe plugins\\cloud_free_stack\\uninstall.py --disable  # 只停用，不卸载

## 为什么单独写一个（而不是只用 cloud_stack_ctl.py uninstall）

`cloud_stack_ctl.py` 是**装了才跑**的安装器，它模块级就
`from ocv_cloud_stack import ...`。而卸载的典型场景恰恰是「插件已经坏了 /
装了一半 / 包导入不起来」—— 这时让用户去跑一个依赖插件包的脚本，等于
「要先能跑，才能卸掉」。**卸载器不能依赖它要卸载的东西。**

因此本脚本的设计约束是：

* **只用标准库**，不 `import ocv_cloud_stack.*`。
* 自带的注入原语在**可用时**延迟导入 `cloud_stack_ctl`（避免标记字符串
  两处漂移 —— 漂移会让 `_strip_frontend_block` 删不掉注入块），
  **不可用时**回落到内置常量并把这件事**明确报告**出来。
* 每个动作前先「探针」，只动**确认属于本插件**的东西。

## 它会清理什么

| # | 对象 | 说明 |
|---|---|---|
| 1 | `runtime/python/Lib/site-packages/ocv_cloud_free_stack.pth` | 导入锚点（**基线层**）。删前校验内容确实是我们的 |
| 2 | `frontend/index.html` 里的标记块 | 面板按钮注入（**派生层**）。与 ocv_watermark 共用一把锁 |
| 3 | `.env` 的插件块 + 被顶掉的 4 个原生键 | **定向还原**，其余内容一字不动 |
| 4 | 独立运行的 shim 进程 | 先核对 `/health` 的 pid，**绝不误杀 OCV 后端** |
| 5 | `__pycache__` | 派生物，可重建 |
| 6 | `var/`（仅 `--purge`） | 出图缓存 / 面板预览 / 模型缓存 / 日志 |

## 它**不会**碰什么

* OCV 源码（`module*.py` / `backend/` / `story_agents.py` / …）——
  这是本插件的立身之本，也写进了开发台账的铁律 A。
* 别的插件的注入（`ocv_watermark_host.pth`、水印在 `index.html` 里的那一行）。
* `.env` 里插件块之外、且不是本插件顶掉的任何键。

## 关于「卸载后 shim 还在跑」

shim 默认由 **OCV 后端进程内**的守护线程托管（`mode: inproc`）。卸载只能
删掉它的 pid 文件与注入；**那个线程会随 OCV 下次重启一起消失**。
所以脚本最后会提示重启 OCV —— 这是预期行为，不是没卸干净。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# --------------------------------------------------------------------------
# 常量（与 cloud_stack_ctl.py 对齐；可用时会优先从那里导入，避免漂移）
# --------------------------------------------------------------------------

SITECUSTOMIZE_PTH_NAME = "ocv_cloud_free_stack.pth"

# 回落用的标记。**只在 import cloud_stack_ctl 失败时使用**，并且会明确告警。
FALLBACK_ENV_BEGIN = "# ===== OCV 全云端免费栈插件 cloud_free_stack BEGIN（由 cloud_stack_ctl.py 维护）====="
FALLBACK_ENV_END = "# ===== OCV 全云端免费栈插件 cloud_free_stack END ====="
FALLBACK_FRONTEND_BEGIN = "<!-- cloud_free_stack BEGIN（由 cloud_stack_ctl.py 维护，卸载时自动移除） -->"
FALLBACK_FRONTEND_END = "<!-- cloud_free_stack END -->"
FALLBACK_PREV_PREFIX = "CLOUD_STACK_PREV_"
FALLBACK_OVERRIDDEN = ("LANGUAGE_PROVIDER", "IMAGE_API_BASE_URL", "IMAGE_MODEL_ID", "DASHSCOPE_API_KEY")
FALLBACK_BACKUP_SUFFIX = ".cloud_stack_backup"

# 与本插件共用 frontend/index.html 的另一个插件所持有的锁（不可擅动）
SHARED_FRONTEND_LOCK = ".ocv_frontend_inject.lock"
LOCK_STALE_SECONDS = 20.0

PLUGIN_DIR = Path(__file__).resolve().parent


# --------------------------------------------------------------------------
# 控制台加固（内联，刻意不 import ocv_cloud_stack.console）
#
# 中文 Windows 的 stdout 是 GBK，打印 ✓ / ✗ / ⚠ 会抛 UnicodeEncodeError 并
# **终止进程**。卸载脚本尤其不能因为一行输出就半途而废 —— 那会留下「卸了一半」
# 的状态。所以这里内联一份降级映射，不依赖插件包。
# --------------------------------------------------------------------------

_GLYPH_FALLBACK = {"✓": "√", "✗": "×", "⚠": "!", "→": "->"}


def harden_console() -> None:
    """把 stdout/stderr 的编码错误策略换成可降级，避免中文 Windows 下崩掉。"""
    import codecs

    def _handler(exc: UnicodeError) -> tuple[str, int]:
        text = exc.object[exc.start:exc.end] if isinstance(exc, UnicodeError) else ""
        return _GLYPH_FALLBACK.get(text, "?"), exc.end

    try:
        codecs.register_error("ocv_uninstall_glyph", _handler)
    except (LookupError, TypeError):
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="ocv_uninstall_glyph")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


def say(text: str = "") -> None:
    try:
        print(text, flush=True)
    except Exception:  # noqa: BLE001 - 输出失败绝不能中断卸载
        pass


# --------------------------------------------------------------------------
# 项目根定位
# --------------------------------------------------------------------------

def project_root() -> Path:
    """插件在 ``<OCV 根>/plugins/cloud_free_stack/``，上溯三级。"""
    configured = os.getenv("OCV_PROJECT_ROOT", "").strip()
    if configured and Path(configured).is_dir():
        return Path(configured).resolve()
    return PLUGIN_DIR.parent.parent


def site_packages() -> Path:
    return project_root() / "runtime" / "python" / "Lib" / "site-packages"


def env_path() -> Path:
    return project_root() / ".env"


# --------------------------------------------------------------------------
# 原语加载：优先复用 cloud_stack_ctl，失败则回落
# --------------------------------------------------------------------------

class Primitives:
    """卸载所需的注入原语。

    ``source`` 为 ``"ctl"`` 表示复用了 ``cloud_stack_ctl`` 的实现（推荐，
    标记字符串只有一份）；``"fallback"`` 表示内置回落（会在报告里告警）。
    """

    def __init__(self) -> None:
        self.source = "fallback"
        self.reason = ""
        self.ctl = None
        self._load()

    def _load(self) -> None:
        if str(PLUGIN_DIR) not in sys.path:
            sys.path.insert(0, str(PLUGIN_DIR))
        try:
            import cloud_stack_ctl as ctl  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001 - 包坏了也要能卸载
            self.reason = f"{type(exc).__name__}: {exc}"
            return
        self.ctl = ctl
        self.source = "ctl"

    # ---- 标记 ----
    @property
    def env_begin(self) -> str:
        return getattr(self.ctl, "BEGIN_MARK", FALLBACK_ENV_BEGIN)

    @property
    def env_end(self) -> str:
        return getattr(self.ctl, "END_MARK", FALLBACK_ENV_END)

    @property
    def frontend_begin(self) -> str:
        return getattr(self.ctl, "FRONTEND_MARK_BEGIN", FALLBACK_FRONTEND_BEGIN)

    @property
    def frontend_end(self) -> str:
        return getattr(self.ctl, "FRONTEND_MARK_END", FALLBACK_FRONTEND_END)

    @property
    def prev_prefix(self) -> str:
        return getattr(self.ctl, "PREV_PREFIX", FALLBACK_PREV_PREFIX)

    @property
    def overridden(self) -> tuple[str, ...]:
        return tuple(getattr(self.ctl, "_OVERRIDDEN_KEYS", FALLBACK_OVERRIDDEN))

    @property
    def backup_suffix(self) -> str:
        return getattr(self.ctl, "BACKUP_SUFFIX", FALLBACK_BACKUP_SUFFIX)

    @property
    def frontend_targets(self) -> tuple[str, ...]:
        return tuple(getattr(self.ctl, "FRONTEND_TARGETS", ("frontend/index.html",)))


# --------------------------------------------------------------------------
# .env 读取小工具（本脚本自用，避免依赖 ctl 的私有函数）
# --------------------------------------------------------------------------

def read_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def parse_value(lines: list[str], key: str) -> str:
    prefix = f"{key}="
    for line in lines:
        # 剥掉行首 BOM（本仓 .env 是 UTF-8 with BOM，decode 后 BOM 会成为
        # 第一行的普通字符，导致**首行的键**匹配不到 —— 见 DEVLOG F-004）。
        stripped = line.strip().lstrip("\ufeff")
        if stripped.startswith("#") or not stripped.startswith(prefix):
            continue
        value = stripped[len(prefix):].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value
    return ""


def strip_env_block(lines: list[str], begin: str, end: str) -> list[str]:
    kept: list[str] = []
    inside = False
    for line in lines:
        # 同样剥 BOM：万一插件块标记落在首行，带 BOM 就匹配不上、块摘不干净。
        marker = line.strip().lstrip("\ufeff")
        if marker == begin:
            inside = True
            continue
        if marker == end:
            inside = False
            continue
        if not inside:
            kept.append(line)
    return kept


# --------------------------------------------------------------------------
# 探针（只读，供 plan 与 verify 使用）
# --------------------------------------------------------------------------

def pth_probe(p: Primitives) -> tuple[Path | None, str]:
    """返回 ``(锚点路径, 说明)``。路径为 None 表示没有 / 不是我们的。"""
    target = site_packages() / SITECUSTOMIZE_PTH_NAME
    if not target.is_file():
        return None, "不存在"
    try:
        text = target.read_text(encoding="ascii", errors="replace")
    except OSError as exc:
        return None, f"读取失败 {type(exc).__name__}"
    # 只认**确认属于本插件**的锚点：文件名 + 内容双重校验，绝不误删别人的 .pth
    if "cloud_free_stack" not in text:
        return None, "内容不像本插件的锚点（已跳过，未删除）"
    return target, "存在"


def frontend_probe(p: Primitives) -> tuple[list[Path], list[str]]:
    found: list[Path] = []
    notes: list[str] = []
    for relative in p.frontend_targets:
        path = project_root() / relative
        if not path.is_file():
            notes.append(f"{relative}: 文件不存在")
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            notes.append(f"{relative}: 读取失败 {type(exc).__name__}")
            continue
        if p.frontend_begin in text:
            found.append(path)
            notes.append(f"{relative}: 含注入块")
        else:
            notes.append(f"{relative}: 无注入块")
    return found, notes


def has_key(lines: list[str], key: str) -> bool:
    """键是否**存在**（不论值是否为空）。

    与 ``parse_value`` 的区别至关重要：后者对"键不存在"和"键存在但为空"
    都返回空串。而还原时这两者含义相反 —— 键存在且为空 ⇒ 原值本就是空 ⇒
    必须清掉插件注入的值；键不存在 ⇒ 不知道原值 ⇒ 安全阀保留现值。
    """
    prefix = f"{key}="
    for line in lines:
        stripped = line.strip().lstrip("\ufeff")
        if stripped.startswith("#"):
            continue
        if stripped.startswith(prefix):
            return True
    return False


def env_probe(p: Primitives) -> dict[str, object]:
    lines = read_lines(env_path())
    has_block = any(line.strip().lstrip("\ufeff") == p.env_begin for line in lines)
    backup = env_path().with_name(env_path().name + p.backup_suffix)
    backup_lines = read_lines(backup) if backup.is_file() else []
    prev_recorded: list[str] = []
    keeps: list[str] = []
    for key in p.overridden:
        recorded = parse_value(lines, f"{p.prev_prefix}{key}")
        if recorded:
            prev_recorded.append(key)
            continue
        if parse_value(backup_lines, key):
            continue
        # 记录空 + 快照空 + 键不存在 + 现值非空 ⇒ 会走安全阀保留
        if not has_key(lines, key) and parse_value(lines, key):
            keeps.append(key)
    return {"has_block": has_block, "prev_recorded": prev_recorded, "kept_nonempty": keeps, "lines": lines}


def pycache_dirs() -> list[Path]:
    return sorted(PLUGIN_DIR.rglob("__pycache__"))


def var_dir() -> Path:
    return PLUGIN_DIR / "var"


def shim_pid_file() -> Path:
    return var_dir() / "shim.pid"


def lock_file() -> Path:
    return PLUGIN_DIR.parent / SHARED_FRONTEND_LOCK


def shim_state() -> dict[str, object]:
    """shim 现在是否在跑、是谁在托管（只读）。"""
    pid_file = shim_pid_file()
    pid = None
    if pid_file.is_file():
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = None
    port = 8799
    try:
        lines = read_lines(env_path())
        raw = parse_value(lines, "CLOUD_STACK_SHIM_PORT")
        if raw:
            port = int(raw)
    except ValueError:
        pass
    health: dict[str, object] = {}
    try:
        import json
        import urllib.request

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1.5) as resp:
            health = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - 探活失败就是"没在跑"
        health = {}
    data = health.get("data") if isinstance(health.get("data"), dict) else {}
    return {"pid_file": pid, "port": port, "listening": bool(data), "mode": data.get("mode"), "pid": data.get("pid")}


# --------------------------------------------------------------------------
# 执行动作
# --------------------------------------------------------------------------

def do_remove_pth(target: Path) -> bool:
    try:
        target.unlink()
        return True
    except OSError as exc:
        say(f"  × 删除锚点失败：{type(exc).__name__}: {exc}")
        return False


def do_remove_frontend(p: Primitives, targets: list[Path]) -> list[Path]:
    """摘除前端注入块。优先用 ctl 的实现（与 ocv_watermark 共用同一把锁）。"""
    if p.ctl is not None and hasattr(p.ctl, "remove_frontend"):
        try:
            return list(p.ctl.remove_frontend())
        except Exception as exc:  # noqa: BLE001
            say(f"  ! ctl.remove_frontend 失败，改用内置实现：{type(exc).__name__}: {exc}")

    # ---- 内置回落：原子写 + 与另一插件共用同一把锁 ----
    import contextlib
    import uuid

    lock = lock_file()

    @contextlib.contextmanager
    def _locked(timeout: float = 15.0):
        deadline = time.monotonic() + timeout
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, str(os.getpid()).encode("ascii"))
                finally:
                    os.close(fd)
                break
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime > LOCK_STALE_SECONDS:
                        lock.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    raise TimeoutError(f"抢锁超时：{lock}")
                time.sleep(0.1)
        try:
            yield
        finally:
            try:
                lock.unlink(missing_ok=True)
            except OSError:
                pass

    pattern = re.compile(
        re.escape(p.frontend_begin) + r".*?" + re.escape(p.frontend_end) + r"\s*",
        re.DOTALL,
    )
    changed: list[Path] = []
    with _locked():
        for path in targets:
            try:
                html = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not html.strip():
                continue  # 读到别人的中间态，绝不在残缺内容上做读-改-写
            updated = pattern.sub("", html)
            if updated == html:
                continue
            data = updated.encode("utf-8")
            if os.linesep != "\n":
                data = data.replace(b"\n", os.linesep.encode("ascii"))
            pending = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:10]}.pending")
            try:
                pending.write_bytes(data)
                os.replace(pending, path)
                changed.append(path)
            finally:
                pending.unlink(missing_ok=True)
    return changed


def do_restore_env(p: Primitives, dry_run: bool) -> dict[str, object]:
    """移除插件块并定向还原被顶掉的键。返回变更摘要。"""
    target = env_path()
    if not target.is_file():
        return {"ok": False, "reason": ".env 不存在"}

    original = read_lines(target)
    if not any(line.strip().lstrip("\ufeff") == p.env_begin for line in original):
        return {"ok": True, "changed": False, "reason": "无插件块"}

    backup = target.with_name(target.name + p.backup_suffix)
    stripped = strip_env_block(original, p.env_begin, p.env_end)

    restored: list[tuple[str, str]] = []
    kept: list[str] = []
    for key in p.overridden:
        recorded = parse_value(original, f"{p.prev_prefix}{key}")
        if recorded:
            value = recorded                                    # ① 权威原值
        else:
            value = parse_value(read_lines(backup), key) if backup.is_file() else ""
            if not value:
                # ② 键存在但原值为空 ⇒ 必须清空（否则会把插件注入的死地址留下）
                if has_key(original, key):
                    value = ""
                else:
                    # ③ 键不存在且无记录 ⇒ 安全阀：保留现值，绝不静默清空
                    if parse_value(original, key):
                        kept.append(key)
                        continue
                    continue
        # 替换或追加，并**清除重复行**。
        # 只替换第一次出现不够：install 把被顶掉的键改写**在插件块之外**，
        # 而插件块里那份 PREV 记录之外可能还留有旧注入值；不去重就会让同一个键
        # 在文件里出现两次，第二个成为陈旧残留（见 DEVLOG F-004）。
        prefix = f"{key}="
        replaced = False
        deduped: list[str] = []
        for line in stripped:
            if line.strip().lstrip("\ufeff").startswith(prefix):
                if not replaced:
                    deduped.append(f"{key}={value}")
                    replaced = True
                continue
            deduped.append(line)
        if not replaced:
            deduped.append(f"{key}={value}")
        stripped = deduped
        restored.append((key, value))

    if dry_run:
        return {"ok": True, "changed": True, "dry_run": True,
                "restored": restored, "kept": kept, "lines": len(stripped)}

    payload = "\n".join(stripped).rstrip("\n") + "\n"
    before_bytes = target.read_bytes()
    temp = target.with_name(f".{target.name}.{os.getpid()}.pending")
    try:
        temp.write_text(payload, encoding="utf-8")
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)

    # 卸载前副本，便于回溯（不覆盖首次安装快照）
    kept_copy = None
    try:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        kept_copy = target.with_name(f"{target.name}.uninstalled_{stamp}")
        kept_copy.write_bytes(before_bytes)
    except OSError:
        kept_copy = None

    return {"ok": True, "changed": True, "restored": restored, "kept": kept, "copy": kept_copy}


def do_stop_shim() -> bool:
    """停掉**独立运行**的 shim。进程内托管的那个随 OCV 重启消失，不在此处理。"""
    if str(PLUGIN_DIR) not in sys.path:
        sys.path.insert(0, str(PLUGIN_DIR))
    try:
        from ocv_cloud_stack import shim_launcher  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - 包不可用时只清 pid 文件
        pid_file = shim_pid_file()
        if pid_file.is_file():
            pid_file.unlink(missing_ok=True)
            return True
        return False
    try:
        return bool(shim_launcher.stop())
    except Exception as exc:  # noqa: BLE001
        say(f"  ! 停止 shim 失败（已忽略）：{type(exc).__name__}: {exc}")
        return False


def do_remove_pycache(dirs: list[Path]) -> int:
    removed = 0
    for path in dirs:
        try:
            shutil.rmtree(path)
            removed += 1
        except OSError:
            pass
    return removed


def do_purge_var() -> bool:
    target = var_dir()
    if not target.is_dir():
        return False
    try:
        shutil.rmtree(target)
        return True
    except OSError as exc:
        say(f"  × 清理 var/ 失败：{type(exc).__name__}: {exc}")
        return False


def do_release_stale_lock() -> bool:
    """共享锁只有在**过期**时才回收 —— 它同时被 ocv_watermark 使用。"""
    lock = lock_file()
    if not lock.is_file():
        return False
    try:
        if time.time() - lock.stat().st_mtime > LOCK_STALE_SECONDS:
            lock.unlink(missing_ok=True)
            return True
    except OSError:
        pass
    return False


def do_disable() -> bool:
    """只停用：把 CLOUD_STACK_ENABLED 置 0，保留全部注入（可随时恢复）。"""
    target = env_path()
    lines = read_lines(target)
    if not target.is_file():
        return False
    changed = False
    for index, line in enumerate(lines):
        if line.strip().startswith("CLOUD_STACK_ENABLED="):
            lines[index] = "CLOUD_STACK_ENABLED=0"
            changed = True
            break
    if not changed:
        # 没有插件块就往末尾补一行（最轻量的停用方式）
        lines.append("CLOUD_STACK_ENABLED=0")
    payload = "\n".join(lines).rstrip("\n") + "\n"
    temp = target.with_name(f".{target.name}.{os.getpid()}.pending")
    try:
        temp.write_text(payload, encoding="utf-8")
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    return True


# --------------------------------------------------------------------------
# 计划 / 执行 / 校验
# --------------------------------------------------------------------------

def build_plan(p: Primitives) -> dict[str, object]:
    pth, pth_note = pth_probe(p)
    frontend, frontend_notes = frontend_probe(p)
    env = env_probe(p)
    pyc = pycache_dirs()
    shim = shim_state()
    lock = lock_file()
    return {
        "pth": pth, "pth_note": pth_note,
        "frontend": frontend, "frontend_notes": frontend_notes,
        "env": env, "pycache": pyc, "shim": shim,
        "lock": lock if lock.is_file() else None,
        "var": var_dir() if var_dir().is_dir() else None,
    }


def print_plan(p: Primitives, plan: dict[str, object]) -> None:
    say("== 现状（探针结果）==")
    say(f"  OCV 根目录        : {project_root()}")
    say(f"  插件目录          : {PLUGIN_DIR}")
    say(f"  注入原语来源      : {'cloud_stack_ctl（推荐）' if p.source == 'ctl' else '内置回落'}")
    if p.source != "ctl":
        say(f"    ! 无法导入 cloud_stack_ctl：{p.reason}")
        say("    ! 已使用内置标记常量；若与 ctl 漂移，注入块可能摘不干净，")
        say("    ! 请卸载后手工确认 frontend/index.html 里没有 cloud_free_stack 字样。")
    say("")
    say(f"  1. .pth 导入锚点  : {plan['pth_note']}")
    say(f"  2. 前端注入块     : {'、'.join(plan['frontend_notes'])}")
    env = plan["env"]
    if env.get("has_block"):
        recorded = env.get("prev_recorded") or []
        say(f"  3. .env 插件块    : 存在；可还原的原生键 {len(recorded)} 个"
            + (f"（{'、'.join(recorded)}）" if recorded else ""))
        if env.get("kept_nonempty"):
            say(f"     ! 这些键的原值记录与快照均为空，将**保留你当前的值**（不清空）："
                f"{'、'.join(env['kept_nonempty'])}")
    else:
        say("  3. .env 插件块    : 无")
    shim = plan["shim"]
    mode = f"，托管方式 {shim['mode']}" if shim.get("mode") else ""
    say(f"  4. 图片 shim      : {'在监听 ' + str(shim['port']) + mode if shim.get('listening') else '未在运行'}"
        + (f"（pid 文件 {shim['pid_file']}）" if shim.get("pid_file") else ""))
    say(f"  5. __pycache__    : {len(plan['pycache'])} 个目录")
    say(f"  6. var/ 运行期数据: {'存在（--purge 时才清）' if plan['var'] else '无'}")
    if plan["lock"]:
        say(f"  ·  共享注入锁      : 存在（与 ocv_watermark 共用，只在过期时回收）")


def verify_clean(p: Primitives) -> list[str]:
    """卸载后再扫一遍，返回仍未清净的项。"""
    problems: list[str] = []
    pth, note = pth_probe(p)
    if pth is not None:
        problems.append(f"锚点仍在：{pth}（{note}）")
    frontend, notes = frontend_probe(p)
    if frontend:
        problems.append("前端注入块仍在：" + "、".join(str(x.name) for x in frontend))
    lines = read_lines(env_path())
    if any(line.strip().lstrip("\ufeff") == p.env_begin for line in lines):
        problems.append(".env 里仍有插件块")
    return problems


def main(argv: list[str] | None = None) -> int:
    harden_console()

    parser = argparse.ArgumentParser(
        prog="uninstall.py",
        description="OCV 全云端免费栈插件 —— 独立卸载脚本（只用标准库，包坏了也能跑）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只报告将要做什么，不修改任何文件")
    parser.add_argument("--yes", "-y", action="store_true", help="不再交互确认（供 .bat 调用）")
    parser.add_argument("--purge", action="store_true",
                        help="连 var/ 运行期数据一起清（出图缓存 / 面板预览 / 模型缓存 / 日志 / 面板配置）")
    parser.add_argument("--keep-pycache", action="store_true", help="保留 __pycache__（默认清理）")
    parser.add_argument("--disable", action="store_true",
                        help="只停用（置 CLOUD_STACK_ENABLED=0），保留全部注入，可随时恢复")
    parser.add_argument("--restore-keys-from", default="",
                        help="卸载后再从指定 .env 快照定向恢复若干键，如 .env.endpoint_backup")
    parser.add_argument("--restore-keys", default="",
                        help="配合 --restore-keys-from：逗号分隔的键名；缺省恢复一组常用键")
    args = parser.parse_args(argv)

    p = Primitives()
    plan = build_plan(p)

    say("=" * 62)
    say(" OCV 全云端免费栈插件 -- 卸载")
    say("=" * 62)
    say("")

    if args.disable:
        say("== 模式：只停用（--disable）==")
        say("  将把 CLOUD_STACK_ENABLED 置 0：插件对 OCV 变为透明，")
        say("  注入与配置全部保留，改回 1 即可恢复。")
        say("")
        if args.dry_run:
            say("  [dry-run] 未做任何修改。")
            return 0
        if not do_disable():
            say("  × 找不到 .env，无法停用")
            return 1
        say("  √ 已停用。恢复方式：把 CLOUD_STACK_ENABLED 改回 1（或重跑 cloud_stack_ctl.py install）")
        return 0

    print_plan(p, plan)
    say("")

    if args.dry_run:
        say("== [dry-run] 以上是现状；未修改任何文件 ==")
        say("  去掉 --dry-run 即真正执行。")
        return 0

    if not args.yes:
        try:
            answer = input("确认卸载？输入 y 回车继续，其它任意键取消：").strip().lower()
        except (EOFError, KeyboardInterrupt):
            say("\n已取消。")
            return 130
        if answer not in {"y", "yes"}:
            say("已取消，未做任何修改。")
            return 0
        say("")

    say("== 执行卸载 ==")
    actions: list[str] = []

    # 1) shim（先停，避免它继续写 var/）
    if plan["shim"].get("listening"):
        mode = plan["shim"].get("mode")
        if mode == "inproc":
            say(f"  · 图片 shim 由 OCV 进程内托管（pid {plan['shim'].get('pid')}）——")
            say("    它随 OCV 下次重启消失，这里只清 pid 文件。")
            shim_pid_file().unlink(missing_ok=True)
        elif do_stop_shim():
            say("  √ 已停止独立运行的图片 shim")
            actions.append("停止 shim")
        else:
            say("  · shim 无需停止")
    elif shim_pid_file().is_file():
        shim_pid_file().unlink(missing_ok=True)
        say("  · 清理了过期的 shim.pid")

    # 2) .pth 锚点
    if plan["pth"] is not None:
        if do_remove_pth(plan["pth"]):  # type: ignore[arg-type]
            say(f"  √ 已删除导入锚点 {SITECUSTOMIZE_PTH_NAME}")
            actions.append("删除 .pth 锚点")
    else:
        say(f"  · 导入锚点：{plan['pth_note']}")

    # 3) 前端注入块
    if plan["frontend"]:
        changed = do_remove_frontend(p, plan["frontend"])  # type: ignore[arg-type]
        if changed:
            say("  √ 已从 " + "、".join(str(x.relative_to(project_root())) for x in changed) + " 移除面板按钮")
            actions.append("移除前端注入")
        else:
            say("  ! 前端注入块未能移除，请手工检查 frontend/index.html")
    else:
        say("  · 前端无注入块")

    # 4) .env 定向还原
    result = do_restore_env(p, dry_run=False)
    if result.get("changed"):
        restored = result.get("restored") or []
        say(f"  √ 已移除 .env 插件块，还原 {len(restored)} 个原生键")
        for key, value in restored:  # type: ignore[misc]
            shown = value if len(str(value)) <= 24 else f"{str(value)[:6]}…{str(value)[-4:]}"
            say(f"      {key} = {shown or '（空）'}")
        for key in (result.get("kept") or []):  # type: ignore[union-attr]
            say(f"      ! {key} 原值记录为空，已保留你当前设置（未清空）")
        if result.get("copy"):
            say(f"      · 卸载前副本：{Path(str(result['copy'])).name}")
        actions.append("还原 .env")
    else:
        say(f"  · .env：{result.get('reason')}")

    # 5) __pycache__
    if not args.keep_pycache and plan["pycache"]:
        count = do_remove_pycache(plan["pycache"])  # type: ignore[arg-type]
        say(f"  √ 已清理 {count} 个 __pycache__ 目录")
        actions.append("清理 __pycache__")

    # 6) var/（仅 --purge）
    if args.purge:
        if do_purge_var():
            say("  √ 已清理 var/（出图缓存 / 面板预览 / 模型缓存 / 日志 / 面板配置）")
            actions.append("清理 var/")
        else:
            say("  · var/ 无需清理")
    elif plan["var"]:
        say("  · 保留 var/（面板配置与缓存）。要一并清理请加 --purge")

    # 7) 过期共享锁
    if do_release_stale_lock():
        say("  √ 回收了过期的共享注入锁")

    # 8) 可选的按键恢复
    if args.restore_keys_from:
        say("")
        say(f"== 从 {args.restore_keys_from} 定向恢复键 ==")
        keys = [k.strip() for k in args.restore_keys.split(",") if k.strip()] or \
               list(FALLBACK_OVERRIDDEN) + ["ASR_DEVICE", "ASR_MODEL", "ASR_LANGUAGE",
                                            "ASR_RUNTIME_CHECK_TIMEOUT_SECONDS"]
        done = _restore_keys(args.restore_keys_from, keys)
        if done:
            actions.append(f"恢复 {len(done)} 个键")
        else:
            say("  · 无需恢复")

    # 9) 卸载后校验
    say("")
    say("== 卸载后校验 ==")
    problems = verify_clean(p)
    if problems:
        for item in problems:
            say(f"  × {item}")
        say("")
        say("  ⚠ 有残留，请按上面提示手工处理（或把本报告反馈给维护者）。")
        return 1
    say("  √ 锚点、前端注入块、.env 插件块均已清除")

    say("")
    say("== 结果 ==")
    if actions:
        for item in actions:
            say(f"  · {item}")
    else:
        say("  · 没有需要清理的东西（插件本来就未安装或不完整）")
    say("")
    say("后续：")
    say("  1. 重启 OCV 一次，让进程内托管的 shim 与运行时补丁彻底卸载。")
    say("  2. 插件目录 plugins\\cloud_free_stack\\ 未被删除（本脚本自身在里面）。")
    say("     确认无残留后，可以整个删掉这个目录。")
    if not args.purge:
        say("  3. var\\ 仍在（含面板配置与出图缓存）。要清干净请重跑并加 --purge。")
    say("")
    say("如需恢复安装：cloud_stack_ctl.py install")
    return 0


def _restore_keys(source_name: str, keys: list[str]) -> list[tuple[str, str, str]]:
    """从快照按键恢复（复用 ctl 的原语，避免两套实现）。"""
    source = project_root() / source_name
    if not source.is_file():
        say(f"  × 快照不存在：{source}")
        return []
    if str(PLUGIN_DIR) not in sys.path:
        sys.path.insert(0, str(PLUGIN_DIR))
    try:
        import cloud_stack_ctl as ctl  # noqa: PLC0415

        changes = ctl.restore_keys_from_snapshot(source, keys)
    except Exception as exc:  # noqa: BLE001 - 回落：内置实现
        say(f"  ! 复用 ctl 失败（{type(exc).__name__}），改用内置实现")
        changes = _restore_keys_builtin(source, keys)
    for key, old, new in changes:
        shown_old = old if len(old) <= 24 else f"{old[:6]}…{old[-4:]}"
        shown_new = new if len(new) <= 24 else f"{new[:6]}…{new[-4:]}"
        say(f"  √ {key}: {shown_old or '（空）'}  ->  {shown_new or '（空）'}")
    if not changes:
        say("  · 当前值与快照一致，无需恢复")
    return changes


def _restore_keys_builtin(source: Path, keys: list[str]) -> list[tuple[str, str, str]]:
    target = env_path()
    snap_lines = read_lines(source)
    lines = read_lines(target)
    changes: list[tuple[str, str, str]] = []
    for key in keys:
        value = parse_value(snap_lines, key)
        old = parse_value(lines, key)
        if old == value:
            continue
        prefix = f"{key}="
        replaced = False
        for index, line in enumerate(lines):
            if line.strip().startswith(prefix):
                lines[index] = f"{key}={value}"
                replaced = True
                break
        if not replaced:
            lines.append(f"{key}={value}")
        changes.append((key, old, value))
    if changes:
        payload = "\n".join(lines).rstrip("\n") + "\n"
        temp = target.with_name(f".{target.name}.{os.getpid()}.pending")
        try:
            temp.write_text(payload, encoding="utf-8")
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
    return changes


if __name__ == "__main__":
    raise SystemExit(main())
