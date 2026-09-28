"""真实端到端：连线上商汤 /models，确认面板下拉拿到正确的图片模型集合。

与 verify_model_catalog.py 的分工：
  * verify_model_catalog.py 用**构造数据**做纯逻辑断言（可离线跑）。
  * 本脚本**真连上游**，验证"实况清单 + 校正"后的最终结果。

只读：不写配置、不落盘模型图。
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = PROJECT_ROOT / "plugins" / "cloud_free_stack"
for entry in (str(PROJECT_ROOT), str(PLUGIN_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from ocv_cloud_stack import config, models_catalog  # noqa: E402

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


print("=== 凭据 ===")
print(f"  图片 base   = {config.sensenova_image_base_url()}")
print(f"  图片 Key    = {config.sensenova_image_api_key()[:6]}… "
      f"(长度 {len(config.sensenova_image_api_key())})")
print(f"  语言 base   = {config.sensenova_base_url()}")

print("\n=== 1) 原始上游清单（force 拉取，绕过缓存）===")
raw = models_catalog.list_models("image", force=True)
check("拉取成功", bool(raw.get("ok")), str(raw.get("message")))
upstream_ids = [row["value"] for row in (raw.get("models") or [])]
print(f"    上游返回的图片类模型（校正后）：{upstream_ids}")
check("校正后的文生图清单含 u1.5-lite", "sensenova-u1.5-lite" in upstream_ids)
check("校正后的文生图清单含 u1.5-fast（/models 原本漏报）",
      "sensenova-u1.5-fast" in upstream_ids, "这是本次修复的核心")

print("\n=== 2) 图生图清单（应剔除 u1-fast）===")
edit = models_catalog.list_models("image_edit", force=True)
check("拉取成功", bool(edit.get("ok")), str(edit.get("message")))
edit_ids = [row["value"] for row in (edit.get("models") or [])]
print(f"    图生图可选：{edit_ids}")
check("不含 sensenova-u1-fast（实测不支持 edits）", "sensenova-u1-fast" not in edit_ids)
check("含 sensenova-u1.5-lite", "sensenova-u1.5-lite" in edit_ids)
check("含 sensenova-u1.5-fast", "sensenova-u1.5-fast" in edit_ids)

print("\n=== 3) 面板下拉（走磁盘缓存，不发网络）===")
img_opts = [o["value"] for o in models_catalog.panel_options("image")]
edit_opts = [o["value"] for o in models_catalog.panel_options("image_edit")]
print(f"    文生图下拉：{img_opts}")
print(f"    图生图下拉：{edit_opts}")
check("文生图下拉含 u1.5-fast", "sensenova-u1.5-fast" in img_opts)
check("图生图下拉含 u1.5-fast", "sensenova-u1.5-fast" in edit_opts)
check("图生图下拉不含 u1-fast", "sensenova-u1-fast" not in edit_opts)

print("\n=== 4) 面板层当前值是否仍在候选集内 ===")
current_img = config.get("SENSENOVA_IMAGE_MODEL")
current_edit = config.get("SENSENOVA_IMAGE_EDIT_MODEL")
print(f"    SENSENOVA_IMAGE_MODEL      = {current_img}")
print(f"    SENSENOVA_IMAGE_EDIT_MODEL = {current_edit}")
check("当前文生图模型在候选集内", current_img in img_opts, current_img)
check("当前图生图模型在候选集内", current_edit in edit_opts, current_edit)

print()
print(f"通过 {PASS} 项，失败 {FAIL} 项")
raise SystemExit(1 if FAIL else 0)
