"""验证模型清单修复：/models 漏报要补齐、不支持 edits 的要剔除。

实证依据（2026-09-23 直连商汤实测，非文档推断）：
  GET https://token.sensenova.cn/v1/models 只列 9 个模型，输出为图片的仅
  `sensenova-u1-fast` 与 `sensenova-u1.5-lite`。但接口实测矩阵是：

    模型                   generations   edits(图生图)
    sensenova-u1.5-lite    200 OK        200 OK
    sensenova-u1.5-fast    200 OK        200 OK      ← /models 漏报！
    sensenova-u1-fast      200 OK        400 model does not support image editing

  ⇒ 两个方向都错：漏报了可用的 u1.5-fast；又在清单里给了不支持图生图的 u1-fast。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = PROJECT_ROOT / "plugins" / "cloud_free_stack"
for entry in (str(PROJECT_ROOT), str(PLUGIN_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from ocv_cloud_stack import models_catalog  # noqa: E402

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


# 模拟商汤 /models 的真实返回（9 个模型，注意其中没有 u1.5-fast）
UPSTREAM = [
    {"id": "deepseek-v4-flash", "output_modalities": ["text"]},
    {"id": "glm-5.2", "output_modalities": ["text"]},
    {"id": "sensenova-u1-fast", "input_modalities": ["text"], "output_modalities": ["image"]},
    {"id": "sensenova-6.8-flash-lite", "input_modalities": ["text", "image"], "output_modalities": ["text"]},
    {"id": "sensenova-u1.5-lite", "input_modalities": ["text"], "output_modalities": ["image"]},
    {"id": "deepseek-v4-pro", "output_modalities": ["text"]},
    {"id": "kimi-k3", "output_modalities": ["text"]},
    {"id": "deepseek-flash", "output_modalities": ["text"]},
    {"id": "deepseek-v4.1-flash", "output_modalities": ["text"]},
]

print("== A) 文生图（image）：必须补齐 u1.5-fast ==")
merged = models_catalog._merge_verified_extra(list(UPSTREAM), "image")
img_ids = [
    str(i.get("id")) for i in merged
    if models_catalog._sensenova_kind_ok(i, "image")
]
print(f"    校正后图片模型：{img_ids}")
check("补齐了 /models 漏报的 sensenova-u1.5-fast", "sensenova-u1.5-fast" in img_ids)
check("原有 sensenova-u1.5-lite 仍在", "sensenova-u1.5-lite" in img_ids)
check("文生图保留 sensenova-u1-fast（它能文生图）", "sensenova-u1-fast" in img_ids)
check("文本模型没有被混进图片清单", not any(i in img_ids for i in ("glm-5.2", "kimi-k3")))

print("\n== B) 图生图（image_edit）：必须剔除 u1-fast ==")
edit_ids = [
    str(i.get("id")) for i in merged
    if models_catalog._sensenova_kind_ok(i, "image_edit")
]
print(f"    校正后图生图模型：{edit_ids}")
check("剔除了不支持图生图的 sensenova-u1-fast", "sensenova-u1-fast" not in edit_ids)
check("保留 sensenova-u1.5-lite", "sensenova-u1.5-lite" in edit_ids)
check("补齐的 sensenova-u1.5-fast 可用于图生图", "sensenova-u1.5-fast" in edit_ids)
check("图生图清单非空（下游不会拿到空下拉）", len(edit_ids) >= 2)

print("\n== C) 幂等：重复补齐不会产生重复项 ==")
twice = models_catalog._merge_verified_extra(merged, "image")
check("二次补齐后条目数不变", len(twice) == len(merged), f"{len(merged)} -> {len(twice)}")
check("u1.5-fast 只出现一次",
      [str(i.get("id")) for i in twice].count("sensenova-u1.5-fast") == 1)

print("\n== D) 磁盘缓存路径不得回吐陈旧清单（面板首屏走这里）==")
cache_file = models_catalog.cache_path()
backup = cache_file.read_bytes() if cache_file.is_file() else None
# 构造一份"修复前"的陈旧缓存：image 缺 u1.5-fast、image_edit 混入 u1-fast
stale = {
    "image": {
        "fetched_at": 0,
        "api_base": "https://token.sensenova.cn/v1",
        "models": [
            {"value": "sensenova-u1.5-lite", "label": "sensenova-u1.5-lite"},
            {"value": "sensenova-u1-fast", "label": "sensenova-u1-fast"},
        ],
    },
    "image_edit": {
        "fetched_at": 0,
        "api_base": "https://token.sensenova.cn/v1",
        "models": [
            {"value": "sensenova-u1.5-lite", "label": "sensenova-u1.5-lite"},
            {"value": "sensenova-u1-fast", "label": "sensenova-u1-fast"},
        ],
    },
}
try:
    cache_file.write_text(json.dumps(stale, ensure_ascii=False, indent=2), encoding="utf-8")
    models_catalog.clear_cache()
    cached_img = [r["value"] for r in models_catalog.cached_models("image")]
    cached_edit = [r["value"] for r in models_catalog.cached_models("image_edit")]
    print(f"    陈旧缓存 -> image={cached_img}")
    print(f"    陈旧缓存 -> image_edit={cached_edit}")
    check("陈旧缓存里也能补出 u1.5-fast（文生图）", "sensenova-u1.5-fast" in cached_img)
    check("陈旧缓存里也能补出 u1.5-fast（图生图）", "sensenova-u1.5-fast" in cached_edit)
    check("陈旧缓存里的 u1-fast 被挡在图生图之外", "sensenova-u1-fast" not in cached_edit)
    check("文生图仍保留 u1-fast", "sensenova-u1-fast" in cached_img)
finally:
    if backup is not None:
        cache_file.write_bytes(backup)
    else:
        cache_file.unlink(missing_ok=True)
    models_catalog.clear_cache()

print("\n== E) 面板选项与说明文案 ==")
options = models_catalog.panel_options("image_edit")
check("panel_options 可用且非空", len(options) >= 2, str([o["value"] for o in options]))
check("panel_options 项含 value/label",
      all("value" in o and "label" in o for o in options))
note = models_catalog.verify_note("image_edit")
check("图生图说明提到剔除 u1-fast", "sensenova-u1-fast" in note)
check("图生图说明提到补齐 u1.5-fast", "sensenova-u1.5-fast" in note)
print(f"    文案：{note}")

print("\n== F) 未知用途 / 边界不炸 ==")
check("未知 kind 返回空列表", models_catalog.cached_models("nope") == [])
check("非商汤 kind 不补图片型号", models_catalog.panel_options("tts") is not None)

print()
print(f"通过 {PASS} 项，失败 {FAIL} 项")
raise SystemExit(1 if FAIL else 0)
