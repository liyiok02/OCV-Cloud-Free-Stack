# SPDX-License-Identifier: AGPL-3.0-only
"""构建干净的发行 zip（Release 附件用）。

用法：
    python tools/build_release_zip.py            # 出包 -> dist/OCV-Cloud-Free-Stack-v<版本>.zip
    python tools/build_release_zip.py --check    # 只做凭据扫描，不出包

设计：
- 只打包源码；运行期数据（var/、state/）、缓存、日志、归档一律排除。
- 出包前对所有文本文件做凭据特征扫描，命中即拒绝打包（vendor/ 内上游库的
  命中只警告不拦截）。
- zip 条目使用 UTF-8 文件名（中文 .bat 不再乱码），顶层目录为
  `cloud_free_stack/`，解压即落入 OCV 的 plugins/ 目录可直接使用。
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DIST = REPO_ROOT / "dist"
TOP = "cloud_free_stack"

EXCLUDE_DIRS = {
    ".git", ".github", "var", "state", "__pycache__",
    ".pytest_cache", "dist", ".idea", ".vscode",
}
EXCLUDE_GLOBS = (
    "*.log", "*.zip", "*.pyc", "*.pyo", "*.tmp",
    "*.user-backup", ".DS_Store", "Thumbs.db",
)
# 内部开发文档永不随包发布（维护者本地保留；即使被拷回仓库目录也不打包）
EXCLUDE_FILES = {"docs/DEVLOG.md", "docs/JETHUB-移植侦察报告.md"}

# 凭据特征：真实 Key 都是 sk- + 连续字母数字（测试夹具用的带连字符假 Key 不匹配）
SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{24,}"),
    re.compile(r"eyJhbGciOi[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."),  # JWT
    re.compile(r"\b1[3-9]\d{9}\b"),  # 手机号（11 位）
    re.compile(r"(?:access_token|refresh_token)\"?\s*[:=]\s*\"[A-Za-z0-9._-]{20,}\""),
)
TEXT_SUFFIXES = {
    ".py", ".mjs", ".js", ".cjs", ".json", ".md", ".txt", ".bat", ".cmd",
    ".html", ".css", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".sh", ".ps1",
}


def excluded(rel: Path) -> bool:
    if any(part in EXCLUDE_DIRS for part in rel.parts):
        return True
    if rel.as_posix() in EXCLUDE_FILES:
        return True
    return any(fnmatch.fnmatch(rel.name, pat) for pat in EXCLUDE_GLOBS)


def iter_files():
    for path in sorted(REPO_ROOT.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(REPO_ROOT)
        if excluded(rel):
            continue
        yield path, rel


def scan(path: Path) -> list[str]:
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    hits = []
    for pat in SECRET_PATTERNS:
        for m in pat.finditer(text):
            hits.append(f"{pat.pattern[:40]} -> {m.group(0)[:12]}...")
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description="构建干净的发行 zip")
    ap.add_argument("--check", action="store_true", help="只扫描凭据，不打包")
    args = ap.parse_args()

    plugin_json = json.loads((REPO_ROOT / "plugin.json").read_text(encoding="utf-8"))
    version = plugin_json.get("version", "0.0.0")

    files = list(iter_files())
    blocking: dict[str, list[str]] = {}
    warnings: dict[str, list[str]] = {}
    for path, rel in files:
        hits = scan(path)
        if not hits:
            continue
        key = rel.as_posix()
        if key.startswith("vendor/"):
            warnings[key] = hits
        else:
            blocking[key] = hits

    for rel, hits in warnings.items():
        print(f"⚠ vendor 内命中（上游库文本，请人工确认）: {rel}")
        for h in hits[:3]:
            print(f"    {h}")

    if blocking:
        print("✗ 发现凭据特征，拒绝打包：", file=sys.stderr)
        for rel, hits in blocking.items():
            print(f"  {rel}", file=sys.stderr)
            for h in hits[:5]:
                print(f"    {h}", file=sys.stderr)
        return 1

    print(f"✓ 凭据扫描通过（{len(files)} 个文件）")
    if args.check:
        return 0

    DIST.mkdir(exist_ok=True)
    out = DIST / f"OCV-Cloud-Free-Stack-v{version}.zip"
    if out.exists():
        out.unlink()
    raw = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, rel in files:
            zf.write(path, f"{TOP}/{rel.as_posix()}")
            raw += path.stat().st_size
    print(
        f"✓ {out.name}: {out.stat().st_size / 1e6:.2f} MB（压缩后），"
        f"{len(files)} 个文件，{raw / 1e6:.2f} MB（原始）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
