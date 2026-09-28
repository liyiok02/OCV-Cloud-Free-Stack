"""验证 F-011：面板配好 Agnes 后，OCV 原生视频门禁必须放行。

用户症状：
    一键制作已暂停：请先配置视频 API。
（但用户已在云端免费栈面板填了 Agnes API Key）

## 根因

前端放行条件是（``frontend/src/components/VideoStudio.vue``）：

    videoConfigured = videoModel.source === 'dedicated' && videoModel.has_api_key

``videoModel`` 来自 OCV 自己的 ``GET /api/video-model`` →
``video_model_config.load_config()``，而它只读 ``VIDEO_API_*``
（``.env`` / ``os.environ``），**完全不认识**插件的 ``AGNES_API_KEY``。
两者之间的桥原本只有手工执行、且需要重启的 ``video-install``。

## 分节

  A. 负向对照：关掉补丁，逐字复现"门禁判定未配置"
  B. 正向：开着补丁，门禁放行，且指向本地 shim
  C. 两个开关都要满足（**绝不在用户没同意时改他的视频通道**）
  D. 桥接结果必须与 shim 的实际路由一致（否则门禁过了、提交打偏）
  E. 免重启：面板改了立刻生效（wrapper 每次实时读）
  F. 不泄露真 Key；不写 .env
"""
from __future__ import annotations

import copy
import json
import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = PROJECT_ROOT / "plugins" / "cloud_free_stack"
for entry in (str(PROJECT_ROOT), str(PLUGIN_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

try:
    from ocv_cloud_stack import console as _console

    _console.harden()
except Exception:  # noqa: BLE001
    pass

from ocv_cloud_stack import config, video_gate_bridge  # noqa: E402

# ⚠ **绝不要 `from backend.app.video_model_config import load_config`！**
#   `uninstall()` 只改模块属性，改不到已被 from-import 绑进本模块命名空间的
#   那个对象 —— 于是"关掉补丁"的对照组会拿到**仍然是桥接版**的函数。
#   实测踩到：A 节负向对照因此误判（铁律 29 的绑定陷阱，测试自己掉进去了）。
#   一律通过模块属性调用。
import backend.app.video_model_config as vmc  # noqa: E402

PASS = 0
FAIL = 0


def load_config():
    return vmc.load_config()


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {label}" + (f"  {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  {detail}" if detail else ""))


def gate_open(cfg: dict) -> bool:
    """前端那条放行条件，逐字复刻。"""
    return cfg.get("source") == "dedicated" and bool(cfg.get("has_api_key"))


# 保存并统一设定测试前提（测试必须封闭，不能依赖用户当前配置）
#
# ⚠ 必须**同时**快照面板层与 os.environ：`config.get` 的分层是
#   `panel > environ > .env > default`，只删面板键会**回落到环境变量**
#   （实测踩到：删掉面板的 AGNES_API_KEY 后判定仍为 enabled，
#    因为环境里还留着 bootstrap 物化过去的那一份）。
saved = config.store_values()
ENV_KEYS = ("VIDEO_API_KEY", "VIDEO_API_KEYS", "AGNES_API_KEY",
            "CLOUD_STACK_VIDEO_ENABLED", "AGNES_VIDEO_MODEL")
env_saved = {k: os.environ.get(k) for k in ENV_KEYS}


def set_env(**kv: str) -> None:
    """显式设定环境层，避免用例之间互相残留。"""
    for key, value in kv.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

print("== A) 负向对照：关掉补丁，复现「未配置」 ==")
video_gate_bridge.uninstall()
# 模拟用户现状：面板有 Agnes Key、接管开关已开；.env 无 VIDEO_API_*
# force=True：要把用户的真 Key 换成测试 Key（属被保护的写入）。
config.update_store({"AGNES_API_KEY": "sk-test-agnes-key-0123456789",
                     "CLOUD_STACK_VIDEO_ENABLED": "1",
                     "AGNES_VIDEO_MODEL": "agnes-video-2.5-flash"}, force=True)
set_env(VIDEO_API_KEY=None, VIDEO_API_KEYS=None,
        AGNES_API_KEY="sk-test-agnes-key-0123456789",
        CLOUD_STACK_VIDEO_ENABLED="1")
native = load_config()
check("原生门禁判定为未配置（复现用户症状）", not gate_open(native),
      f"source={native.get('source')!r} has_api_key={native.get('has_api_key')!r}")

print("\n== B) 正向：开着补丁，门禁放行 ==")
video_gate_bridge.install()
bridged = load_config()
check("门禁放行（source=dedicated 且有 Key）", gate_open(bridged),
      f"source={bridged.get('source')!r} has_api_key={bridged.get('has_api_key')!r}")
check("base_url 指向本地 shim",
      str(bridged.get("base_url")).rstrip("/") == config.shim_base_url().rstrip("/"),
      str(bridged.get("base_url")))
check("提交路径是插件约定的 shim 路由",
      bridged.get("submit_path") == video_gate_bridge.SHIM_SUBMIT_PATH,
      str(bridged.get("submit_path")))
check("查询路径是 shim 路由",
      bridged.get("query_path") == video_gate_bridge.SHIM_QUERY_PATH,
      str(bridged.get("query_path")))
check("分辨率在 OCV 允许的集合内",
      bridged.get("resolution") in {"480p", "720p"}, str(bridged.get("resolution")))
check("凭据数量与并发字段齐全（video_generation 会直接消费）",
      isinstance(bridged.get("api_keys"), list) and bridged.get("api_keys")
      and isinstance(bridged.get("effective_concurrency"), int)
      and bridged["effective_concurrency"] >= 1,
      f"keys={len(bridged.get('api_keys') or [])} eff={bridged.get('effective_concurrency')}")

print("\n== C) 两个开关都要满足（不在用户没同意时改他的通道）==")
config.update_store({"CLOUD_STACK_VIDEO_ENABLED": "0"})
set_env(CLOUD_STACK_VIDEO_ENABLED=None)
try:
    off = load_config()
    check("接管开关=0 时原样返回原生配置（不插手）", not gate_open(off),
          f"source={off.get('source')!r}")
finally:
    config.update_store({"CLOUD_STACK_VIDEO_ENABLED": "1"})

# 面板键与环境键都要清掉，否则 config.get 会从环境层回落到旧值
config.update_store({}, remove=["AGNES_API_KEY"])
set_env(AGNES_API_KEY=None)
try:
    no_key = load_config()
    check("没有 Agnes Key 时原样返回（接管无意义）", not gate_open(no_key),
          f"source={no_key.get('source')!r}")
finally:
    config.update_store({"AGNES_API_KEY": "sk-test-agnes-key-0123456789"}, force=True)

print("\n== D) 桥接值与 shim 实际路由一致（否则门禁过了、提交打偏）==")
ctl = (PLUGIN_DIR / "cloud_stack_ctl.py").read_text(encoding="utf-8", errors="replace")
checks = [
    ("提交路径", "VIDEO_SUBMIT_PATH_VALUE", video_gate_bridge.SHIM_SUBMIT_PATH),
    ("查询路径", "VIDEO_QUERY_PATH_VALUE", video_gate_bridge.SHIM_QUERY_PATH),
    ("上传路径", "VIDEO_UPLOAD_PATH_VALUE", video_gate_bridge.SHIM_UPLOAD_PATH),
    ("占位 Key", "_VIDEO_PLACEHOLDER_KEY", video_gate_bridge.PLACEHOLDER_KEY),
]
for label, name, ours in checks:
    m = re.search(rf'^{name}\s*=\s*"([^"]+)"', ctl, re.M)
    check(f"{label}与 cloud_stack_ctl.py 一致", bool(m) and m.group(1) == ours,
          f"ctl={m.group(1) if m else '?'} vs 桥接={ours}")

# shim 侧必须真的认这个提交路径
shim = (PLUGIN_DIR / "ocv_cloud_stack" / "image_shim.py").read_text(encoding="utf-8", errors="replace")
check("shim 的正则能匹配桥接的提交路径",
      bool(re.search(r"_VIDEO_SUBMIT_RE\s*=\s*re\.compile\(", shim))
      and video_gate_bridge.SHIM_SUBMIT_PATH.startswith("/openapi/v2/video/"),
      video_gate_bridge.SHIM_SUBMIT_PATH)
check("shim 实现了 /api/video/query",
      '"/api/video/query"' in shim, "等待轮询端点")

print("\n== E) 免重启：面板改完立刻生效（wrapper 每次实时读）==")
config.update_store({"CLOUD_STACK_VIDEO_ENABLED": "0"})
set_env(CLOUD_STACK_VIDEO_ENABLED=None)
try:
    check("改 0 → 立刻关闭", not gate_open(load_config()))
finally:
    config.update_store({"CLOUD_STACK_VIDEO_ENABLED": "1"})
check("改回 1 → 立刻放行", gate_open(load_config()))

print("\n== F) 不泄露真 Key；不写 .env ==")
real_key = config.video_api_key()
blob = json.dumps(bridged, ensure_ascii=False)
check("返回值里没有真 Key", bool(real_key) and real_key not in blob)
check("返回值里没有真 Key 的后四位",
      not (len(real_key) >= 4 and real_key[-4:] in blob))
check("key_hints 为空（连末四位都不给）", bridged.get("key_hints") == [],
      str(bridged.get("key_hints")))
check("凭据是那个显眼的占位串",
      bridged.get("api_key") == video_gate_bridge.PLACEHOLDER_KEY)
env_path = PROJECT_ROOT / ".env"
env_text = env_path.read_text(encoding="utf-8-sig", errors="replace")
check("桥接没有往 .env 写 VIDEO_API_KEY",
      not any(l.startswith("VIDEO_API_KEY=") for l in env_text.splitlines()),
      "（.env 只在 video-install 时才由用户显式改）")

print("\n== G) 集成：video_generation 的 from-import 绑定也被换掉 ==")
import backend.app.video_generation as vg  # noqa: E402

check("video_generation.load_config 是桥接版（否则提交路径仍用旧配置）",
      getattr(vg.load_config, "_cloud_stack_wrapped", False),
      type(vg.load_config).__name__)
check("video_model_config.load_config 也是桥接版",
      getattr(vmc.load_config, "_cloud_stack_wrapped", False),
      type(vmc.load_config).__name__)

# ---- 收尾：完整还原测试前提（面板层 + 环境层）----
# force=True：还原时要把用户的长真 Key 写回（属被保护的写入）。
config.update_store({k: v for k, v in saved.items()},
                    remove=[k for k in config.store_values() if k not in saved], force=True)
for k, v in env_saved.items():
    if v is None:
        os.environ.pop(k, None)
    else:
        os.environ[k] = v
check("测试前提已完整还原（面板层）", config.store_values() == saved,
      f"{len(config.store_values())} vs 原 {len(saved)}")
check("测试前提已完整还原（环境层）",
      all(os.environ.get(k) == v for k, v in env_saved.items()))

print()
print(f"通过 {PASS} 项，失败 {FAIL} 项")
sys.exit(1 if FAIL else 0)
