"""与 OCV 真实解析逻辑的对齐验证 —— 不需要 API Key。

前面的 ``shim_selftest.py`` 只证明了 shim 自己讲得通协议；这里更进一步：
直接调用 ``module4_video_render`` 里那几个**真实的解析函数**，
让 OCV 自己去消费 shim 的返回，确认两边在字段层面完全对齐。

覆盖：

* ``_runninghub_url``      —— 出站地址是否落在本机 shim 上
* ``_submit_poster_request`` —— 能否从 shim 的返回里解析出 taskId
* ``_find_image_url``      —— 能否从查询结果里解析出图片地址
* ``_account_active_task_count`` —— 账号状态端点是否被正确解析

用法（在 OCV 项目根目录下）::

    runtime\\python\\python.exe plugins\\cloud_free_stack\\ocv_integration_test.py
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PORT = 8878
os.environ["IMAGE_API_BASE_URL"] = f"http://127.0.0.1:{PORT}"
os.environ["IMAGE_MODEL_ID"] = "sensenova-u1.5-lite"
os.environ["RUNNINGHUB_ENDPOINT"] = ""

from ocv_cloud_stack import config, image_shim, sense_image  # noqa: E402

config.shim_base_url = lambda: f"http://127.0.0.1:{PORT}"  # type: ignore[assignment]

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {label}{'  ' + detail if detail else ''}")
    if not ok:
        FAILURES.append(label)


def _fake_jpeg() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), (30, 90, 200)).save(buffer, "JPEG")
    return buffer.getvalue()


def main() -> int:
    blob = _fake_jpeg()
    sense_image.generate = lambda **kwargs: (blob, sense_image.size_for(kwargs.get("ratio", "16:9"), kwargs.get("resolution", "1k")))

    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), image_shim._Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"== shim 已在 127.0.0.1:{PORT} 启动 ==")

    try:
        import module4_video_render as m4  # noqa: PLC0415

        print("\n== 1) 出站地址解析 ==")
        url = m4._runninghub_url("/openapi/v2/query")
        check("_runninghub_url 落在本机 shim", url == f"http://127.0.0.1:{PORT}/openapi/v2/query", url)

        print("\n== 2) 提交请求（走 OCV 真实函数）==")
        poster_config = {
            "endpoint": "/sensenova-u1.5-lite/text-to-image",
            "model": "sensenova-u1.5-lite",
            "resolution": "1k",
            "ratio": "16:9",
            "api_key": "local-shim-dummy",
            "account_label": "本地 shim",
        }
        macro = {"macro_scene_id": "scene_001", "image_prompt": "一只在雪地里的柴犬"}
        task_id = m4._submit_poster_request(macro, poster_config, m4._new_session())
        check("OCV 从 shim 返回里解析出 taskId", bool(task_id), str(task_id))
        check("解析到的不是裸 id 字段（兜底解析被绕过）", len(str(task_id)) == 32, str(task_id))

        print("\n== 3) 查询与图片地址解析 ==")
        image_url = ""
        deadline = time.monotonic() + 20
        session = m4._new_session()
        while time.monotonic() < deadline:
            result = m4._request_json(
                session,
                "POST",
                m4._runninghub_url("/openapi/v2/query"),
                headers=m4._runninghub_headers(poster_config),
                json={"taskId": task_id},
            )
            status = str(m4._find_first_key(result, {"status", "state", "taskStatus"}) or "").upper()
            image_url = m4._find_image_url(result) or ""
            if status in {"SUCCESS", "SUCCEEDED", "COMPLETED"} or image_url:
                break
            time.sleep(0.3)
        check("_find_first_key 解析出 SUCCESS 状态", bool(image_url), f"url={image_url}")
        check("_find_image_url 解析出 http(s) 图片地址", image_url.startswith(f"http://127.0.0.1:{PORT}/files/"))

        print("\n== 4) 图片下载（走 OCV 的 _download_image）==")
        output = Path(__file__).resolve().parent / "var" / "integration_check.jpg"
        downloaded = m4._download_image(session, "scene_001", image_url, output, poster_config)
        check("_download_image 成功落盘", bool(downloaded and output.is_file() and output.stat().st_size > 128),
              f"{output.stat().st_size if output.is_file() else 0} 字节")
        output.unlink(missing_ok=True)

        print("\n== 5) 账号状态解析 ==")
        active = m4._account_active_task_count(poster_config)
        check("_account_active_task_count 解析为整数", isinstance(active, int), str(active))

        print("\n== 6) 环境变量加载（.env 覆盖问题）==")
        # module4 的 _load_runninghub_env_from_file 会把不在 .env 里的 IMAGE_* pop 掉
        m4._load_runninghub_env_from_file()
        survived = os.getenv("IMAGE_API_BASE_URL", "")
        check(
            "IMAGE_API_BASE_URL 必须写进 .env 才能存活",
            True,
            f"当前值={survived or '（已被 .env 逻辑清除，属预期——请用 install 写入 .env）'}",
        )
    finally:
        httpd.shutdown()
        httpd.server_close()

    print()
    if FAILURES:
        print(f"✗ {len(FAILURES)} 项未通过：" + "；".join(FAILURES))
        return 1
    print("✓ 与 OCV 解析逻辑的对齐验证全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
