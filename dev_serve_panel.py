# SPDX-License-Identifier: AGPL-3.0-only
"""开发期辅助：起一个只用于渲染验证的面板 shim（指定端口）。

```bat
runtime\\python\\python.exe plugins\\cloud_free_stack\\dev_serve_panel.py 8962
```
按 Ctrl+C 停止。**不要用于正式使用** —— 正式的 shim 由 OCV 后端托管
（`patches._patch_main` → `shim_launcher.ensure_in_process`）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent
OCV_ROOT = PLUGIN_ROOT.parents[1]
if str(OCV_ROOT) not in sys.path:
    sys.path.insert(0, str(OCV_ROOT))

from plugins.cloud_free_stack.ocv_cloud_stack import image_shim  # noqa: E402


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8962
    server = image_shim.serve_in_thread(port)
    if server is None:
        print(f"端口 {port} 起不来（可能被占用）", flush=True)
        return 1
    print(f"面板已在 http://127.0.0.1:{port}/panel 就绪（Ctrl+C 停止）", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("停止", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
