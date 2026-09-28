# SPDX-License-Identifier: AGPL-3.0-only
"""把 dsh-codearts-auth 及其依赖闭包「带货」到插件 vendor/ 目录（可重复运行）。

## 为什么需要这个脚本

插件要能在**没装 DSH 的机器**上独立运行，所以第三方依赖不能指望 `~/.dsh`
或全局 npm。我们把运行时依赖闭包整体复制进 `vendor/`，随插件一起分发。

本脚本是**开发机侧**的工具：只在开发/升级时运行，运行结果提交进插件目录。
目标机上不需要它，也不需要 npm/网络。

## 用法

```bat
runtime\\python\\python.exe plugins\\cloud_free_stack\\vendor_tool.py --scan     :: 只探测，不写
runtime\\python\\python.exe plugins\\cloud_free_stack\\vendor_tool.py            :: 复制/更新 vendor
runtime\\python\\python.exe plugins\\cloud_free_stack\\vendor_tool.py --verify   :: 校验 vendor 自洽
```

`--scan` 会解析符号链接（pnpm 布局）、递归求裸包 import 闭包，并报告每个包的
来源与体积。新增 provider（例如以后要接 qoder）时先跑 `--scan` 看缺哪些包。

## 设计约束

* **只复制运行必需品**：`.js` / `.cjs` / `.mjs` / `.wasm` / `package.json` /
  `LICENSE`。剔除 `src/`、`*.d.ts`、`*.map`、测试与文档（实测 8.8MB → 4.4MB）。
* **必须解析符号链接**：pnpm 的 `node_modules` 全是 symlink，直接 `Copy-Item`
  会拷到链接本身（0 文件）。本脚本用 `robocopy` 跟随。
* **绝不触碰插件目录外**：只读源、只写 `vendor/`。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent
VENDOR = PLUGIN_ROOT / "vendor"
PKG_NAME = "dsh-codearts-auth"

# 直接依赖（其余由闭包扫描补全）
DIRECT_DEPS = ("jose",)

# 搜索源：先包内 node_modules（pnpm），再 DSH 主包
SOURCE_ROOTS = (
    Path.home() / ".dsh" / "profiles" / "web" / "node_modules" / PKG_NAME / "node_modules",
    Path.home() / ".dsh" / "profiles" / "web" / "node_modules",
    Path.home() / ".dsh" / "profiles" / "web" / "node_modules" / ".pnpm",
)
PKG_SOURCE = Path.home() / ".dsh" / "profiles" / "web" / "node_modules" / PKG_NAME

# 分发包里只需要这些
KEEP_SUFFIXES = {".js", ".cjs", ".mjs", ".wasm", ".json", ".node"}
KEEP_FILES = {"LICENSE", "LICENSE.md", "LICENSE.txt", "NOTICE", "package.json"}
DROP_DIRS = {"src", "test", "tests", "__tests__", "docs", "doc", "example", "examples", "coverage"}

# 这些不是包名，是 jose 等包内部的相对子路径误报
IGNORE_BARE = {"pkcs8", "spki", "types", "src"}

_IMPORT_RE = re.compile(
    r"""(?:from|import)\s*\(?\s*['"]([^'"]+)['"]|require\(\s*['"]([^'"]+)['"]\s*\)"""
)


def log(msg: str) -> None:
    print(msg, flush=True)


def package_name_of(spec: str) -> str | None:
    """把 import 说明符归约成包名；相对/内置/忽略项返回 None。"""
    spec = (spec or "").strip()
    if not spec or spec.startswith((".", "/", "node:", "data:", "http:", "file:")):
        return None
    parts = spec.split("/")
    name = "/".join(parts[:2]) if spec.startswith("@") else parts[0]
    if name in IGNORE_BARE:
        return None
    return name


def bare_imports(pkg_dir: Path) -> set[str]:
    found: set[str] = set()
    for path in pkg_dir.rglob("*"):
        if not path.is_file() or path.suffix not in {".js", ".cjs", ".mjs"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in _IMPORT_RE.finditer(text):
            spec = match.group(1) or match.group(2)
            name = package_name_of(spec)
            if name:
                found.add(name)
    return found


def resolve_package(name: str) -> Path | None:
    """在搜索源里定位一个包的真实目录（跟随符号链接）。"""
    rel = Path(*name.split("/"))
    for root in SOURCE_ROOTS:
        candidate = root / rel
        if candidate.exists():
            try:
                return candidate.resolve()
            except OSError:
                return candidate
    # pnpm 深层布局兜底
    pnpm = Path.home() / ".dsh" / "profiles" / "web" / "node_modules" / ".pnpm"
    if pnpm.is_dir():
        for hit in pnpm.glob(f"*/node_modules/{rel}"):
            if hit.exists():
                return hit.resolve()
    return None


def copy_package(src: Path, dest: Path) -> tuple[int, int]:
    """复制包内容到 dest，剔除开发用文件。返回 (文件数, 字节数)。"""
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)

    count = 0
    total = 0
    for path in src.rglob("*"):
        rel = path.relative_to(src)
        if any(part in DROP_DIRS for part in rel.parts[:-1]) or (
            rel.parts and rel.parts[0] in DROP_DIRS
        ):
            continue
        if not path.is_file():
            continue
        name = path.name
        if name not in KEEP_FILES:
            if path.suffix not in KEEP_SUFFIXES:
                continue
            # favicon/测试夹具之类没有后缀特征，但 KEEP_SUFFIXES 已覆盖
            if name.endswith((".d.ts", ".d.cts", ".d.mts", ".map")):
                continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(path, target)
            count += 1
            total += target.stat().st_size
        except OSError:
            continue
    return count, total


def build(verify_only: bool = False) -> int:
    if not PKG_SOURCE.is_dir():
        log(f"✗ 找不到源包：{PKG_SOURCE}")
        log("  请确认开发机装过 dsh-codearts-auth（pnpm profile 目录）。")
        return 1

    log(f"源包 : {PKG_SOURCE}")
    log(f"目标 : {VENDOR}")

    # 1) 求依赖闭包
    queue: list[str] = [PKG_NAME, *DIRECT_DEPS]
    seen: dict[str, Path] = {}
    missing: set[str] = set()

    # 主包自身的依赖
    pkg_json = json.loads((PKG_SOURCE / "package.json").read_text(encoding="utf-8"))
    for dep in (pkg_json.get("dependencies") or {}):
        queue.append(dep)
    for dep in (pkg_json.get("peerDependencies") or {}):
        queue.append(dep)

    while queue:
        name = queue.pop(0)
        if name in seen or name in missing:
            continue
        src = resolve_package(name)
        if src is None:
            missing.add(name)
            continue
        seen[name] = src
        for child in bare_imports(src):
            if child not in seen and child not in missing:
                queue.append(child)

    log(f"\n依赖闭包：{len(seen)} 个包" + (f"（{len(missing)} 个未找到）" if missing else ""))
    for name in sorted(seen):
        log(f"  · {name}")
    if missing:
        log("\n⚠ 未找到以下包（若为 jose 内部相对路径可忽略）：")
        for name in sorted(missing):
            log(f"  ! {name}")

    if verify_only:
        return verify()

    # 2) 复制
    log("\n开始复制……")
    if VENDOR.exists():
        for child in VENDOR.iterdir():
            if child.name != PKG_NAME:
                shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink()
    (VENDOR / "node_modules").mkdir(parents=True, exist_ok=True)
    (VENDOR / PKG_NAME / "lib").mkdir(parents=True, exist_ok=True)

    # 主包：只带 lib + package.json + LICENSE
    lib_src = PKG_SOURCE / "lib"
    if not lib_src.is_dir():
        log(f"✗ 源包缺少 lib/：{lib_src}")
        return 1
    for path in lib_src.rglob("*"):
        if not path.is_file():
            continue
        if path.name.endswith((".d.ts", ".d.cts", ".d.mts", ".map")):
            continue
        target = VENDOR / PKG_NAME / "lib" / path.relative_to(lib_src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    for extra in ("package.json", "LICENSE", "LICENSE.md"):
        if (PKG_SOURCE / extra).is_file():
            shutil.copy2(PKG_SOURCE / extra, VENDOR / PKG_NAME / extra)

    grand_files = grand_bytes = 0
    for name, src in sorted(seen.items()):
        if name == PKG_NAME:
            continue
        dest = VENDOR / "node_modules" / Path(*name.split("/"))
        files, size = copy_package(src, dest)
        grand_files += files
        grand_bytes += size
        log(f"  {name:<40} {files:>4}f {size / 1e6:>7.2f}MB")

    total = sum(p.stat().st_size for p in VENDOR.rglob("*") if p.is_file())
    log(f"\n✓ vendor 完成：{total / 1e6:.2f} MB")
    log("  下一步：vendor_tool.py --verify 或直接跑 self_test.py")
    return 0


def verify() -> int:
    if not VENDOR.is_dir():
        log("✗ vendor 不存在")
        return 1

    problems: list[str] = []
    if not (VENDOR / PKG_NAME / "lib" / "index.js").is_file():
        problems.append(f"缺少 {PKG_NAME}/lib/index.js")

    nm = VENDOR / "node_modules"
    if not nm.is_dir():
        problems.append("缺少 vendor/node_modules")
    else:
        declared = {PKG_NAME}
        pkg_json = VENDOR / PKG_NAME / "package.json"
        if pkg_json.is_file():
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
            declared |= set(data.get("dependencies") or {})
            declared |= set(data.get("peerDependencies") or {})
        for name in sorted(declared):
            if name == PKG_NAME:
                continue
            if not (nm / Path(*name.split("/")) / "package.json").is_file():
                problems.append(f"缺少依赖 {name}")

    present = sorted(
        str(p.parent.relative_to(nm)).replace("\\", "/")
        for p in nm.rglob("package.json")
        if p.parent != nm
    )
    total = sum(p.stat().st_size for p in VENDOR.rglob("*") if p.is_file())

    log(f"vendor 体积 ：{total / 1e6:.2f} MB")
    log(f"vendor 包数 ：{len(present)}")
    for name in present:
        log(f"  · {name}")
    if problems:
        log("\n✗ 校验未通过：")
        for item in problems:
            log(f"  ! {item}")
        return 1
    log("\n✓ vendor 自洽")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把 dsh-codearts-auth 依赖闭包带入 vendor/")
    parser.add_argument("--scan", action="store_true", help="只探测闭包，不写文件")
    parser.add_argument("--verify", action="store_true", help="只校验现有 vendor")
    args = parser.parse_args(argv)

    if args.verify:
        return verify()
    if args.scan:
        return build(verify_only=True)
    return build()


if __name__ == "__main__":
    raise SystemExit(main())
