"""控制台输出加固 —— 让插件在任何 locale 下都不因编码问题崩溃或丢日志。

**这一组问题都出在「Windows 中文环境的 locale 是 GBK」上**，与 OCV 逻辑无关。

1. ``print("✓ ...")`` 会抛 ``UnicodeEncodeError`` **直接终止进程**。
   ``✓``(U+2713)、``✗``(U+2717)、``⚠``(U+26A0) 都不在 GBK 里，
   而中文 Windows 上 ``sys.stdout.encoding`` 就是 ``cp936``。
   本模块注册一个 codec error handler，把这些字符降级成 GBK 里存在的等价符号
   （``✓``→``√``、``✗``→``×``、``⚠``→``!``），而不是让整条命令挂掉。
   在 UTF-8 控制台（``PYTHONUTF8=1`` / VSCode / Git Bash）下不受影响，符号原样输出。

2. 同一个坑的另一面：**CPython 用 locale 编码读 ``.pth``**（不是 UTF-8），
   所以注入锚点必须是纯 ASCII。那件事由
   :func:`cloud_stack_ctl.install_injection` 负责，本模块不碰；这里只记一笔，
   因为排查时两者会一起出现（都表现为「中文 Windows 下环境检查失败」）。

调用点：:func:`ocv_cloud_stack.bootstrap.install`（每个用便携解释器的进程都会走到）
以及 :func:`cloud_stack_ctl.main`（插件被禁用时的兜底）。
"""

from __future__ import annotations

import codecs
import sys

_HANDLER_NAME = "ocv_cloud_stack_glyph"

# 降级表：目标符号必须是 GBK 里存在的，否则等于没换。
#   √ U+221A / × U+00D7 / → U+2192 都在 GBK 中（A1CC / A1C1 / A1FA）
_FALLBACKS: dict[str, str] = {
    "\u2713": "\u221a",  # ✓ -> √
    "\u2714": "\u221a",
    "\u2717": "\u00d7",  # ✗ -> ×
    "\u2718": "\u00d7",
    "\u26a0": "!",       # ⚠ -> !
    "\u2192": "\u2192",  # → 保留（GBK 有）
    "\u2026": "...",     # … -> ...
    "\u00b7": "-",       # · -> -
}

_REGISTERED = False


def _handler(error: BaseException) -> tuple[str, int]:
    """codecs 错误处理器：把编不出来的字符换成 locale 里有的等价符号。"""
    replaced_from = getattr(error, "object", "")[getattr(error, "start", 0):getattr(error, "end", 0)]
    if not isinstance(replaced_from, str):
        replaced_from = ""
    replacement = "".join(_FALLBACKS.get(char, "?") for char in replaced_from)
    return (replacement, getattr(error, "end", 0))


def harden() -> None:
    """把 stdout / stderr 的编码错误处理换成上面那个 handler。

    永不抛异常：拿不到流、流已被替换、``reconfigure`` 不存在（<3.7）都直接跳过。
    """
    global _REGISTERED
    try:
        if not _REGISTERED:
            codecs.register_error(_HANDLER_NAME, _handler)
            _REGISTERED = True
    except Exception:
        return

    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors=_HANDLER_NAME)
        except Exception:
            pass


def supports(char: str) -> bool:
    """当前 stdout 编得出这个字符吗？交互式输出想挑符号时可以用。"""
    encoding = getattr(getattr(sys, "stdout", None), "encoding", None) or "ascii"
    try:
        char.encode(encoding)
        return True
    except (UnicodeEncodeError, LookupError):
        return False
