"""图片 shim 的离线协议自检 —— 不需要 API Key，不消耗任何额度。

做法：把 ``sense_image.generate`` 换成一个返回假 JPEG 的桩函数，
然后在进程内起一个 shim，完整走一遍 OCV 会走的调用序列：

    提交 text-to-image → 轮询 query → 下载结果图
    提交 image-to-image（带 Data-URL 参考图）
    查询 accountStatus

用法（在 OCV 项目根目录下）::

    runtime\\python\\python.exe plugins\\cloud_free_stack\\shim_selftest.py
"""

from __future__ import annotations

import base64
import io
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ocv_cloud_stack import config, image_shim, sense_image  # noqa: E402

FAILURES: list[str] = []
PORT = 8877


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {label}{'  ' + detail if detail else ''}")
    if not ok:
        FAILURES.append(label)


def _fake_jpeg() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), (210, 40, 40)).save(buffer, "JPEG")
    return buffer.getvalue()


def _post(path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    print("== 0) size 映射表 ==")
    cases = [
        ("16:9", "2k", "2720x1536"),
        ("9:16", "2k", "1536x2720"),
        ("1:1", "2k", "2720x2720"),
        ("16:9", "1k", "1280x736"),
    ]
    for ratio, resolution, expected in cases:
        actual = sense_image.size_for(ratio, resolution)
        check(f"{ratio} / {resolution} -> {expected}", actual == expected, f"实际 {actual}")

    print("\n== 1) 参考图形态归一化 ==")
    blob = _fake_jpeg()
    data_uri = f"data:image/jpeg;base64,{base64.b64encode(blob).decode('ascii')}"
    normalized = sense_image._normalize_references([data_uri] * 5)
    check("Data-URL 原样通过", normalized and normalized[0]["image_url"] == data_uri)
    check("参考图上限截断为 3", len(normalized) == config.image_max_reference(), f"{len(normalized)} 张")
    check("纯 base64（无前缀）被丢弃", sense_image._normalize_references([base64.b64encode(blob).decode()]) == [])

    # 用桩函数替换真实商汤调用
    captured: dict = {}

    def fake_generate(*, prompt, ratio="16:9", resolution="1k", reference_images=None, retries=2):
        captured["prompt"] = prompt
        captured["ratio"] = ratio
        captured["references"] = reference_images
        return blob, sense_image.size_for(ratio, resolution)

    sense_image.generate = fake_generate
    # 让 shim 生成的结果地址指向测试端口
    config.shim_base_url = lambda: f"http://127.0.0.1:{PORT}"  # type: ignore[assignment]

    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), image_shim._Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"\n== 2) shim 已在 127.0.0.1:{PORT} 启动 ==")

    try:
        print("\n== 3) 账号状态端点 ==")
        status = _post("/uc/openapi/accountStatus", {"apikey": "dummy"})
        check("返回 code=0", status.get("code") == 0, json.dumps(status, ensure_ascii=False))
        check(
            "含 data.currentTaskCounts",
            isinstance((status.get("data") or {}).get("currentTaskCounts"), int),
        )

        print("\n== 4) 文生图：提交 → 轮询 → 下载 ==")
        submitted = _post(
            "/openapi/v2/rhart-image-g-2/text-to-image",
            {"prompt": "一只猫", "aspectRatio": "16:9", "resolution": "1k", "model": "rhart-image-g-2"},
        )
        task_id = (submitted.get("data") or {}).get("taskId")
        check("显式返回 data.taskId", bool(task_id), str(task_id))

        image_url = ""
        for _ in range(40):
            result = _post("/openapi/v2/query", {"taskId": task_id})
            data = result.get("data") or {}
            if data.get("status") == "SUCCESS":
                image_url = (data.get("results") or [{}])[0].get("url") or ""
                break
            time.sleep(0.2)
        check("轮询返回 SUCCESS", bool(image_url), image_url)
        check("结果地址是 http(s) URL（_find_image_url 只认这个）", image_url.startswith("http"))

        fetched = urllib.request.urlopen(image_url, timeout=10).read()
        # shim 会用 Pillow 统一转成 JPEG，所以字节不会和输入完全一致
        check(
            "结果图可下载且是合法 JPEG",
            len(fetched) > 128 and fetched[:3] == b"\xff\xd8\xff",
            f"{len(fetched)} 字节",
        )

        print("\n== 5) 幂等键 ==")
        payload = {"prompt": "重复提交", "aspectRatio": "16:9", "resolution": "1k", "clientJobId": "ocv-test-fixed"}
        first = ( _post("/openapi/v2/rhart-image-g-2/text-to-image", payload).get("data") or {}).get("taskId")
        second = (_post("/openapi/v2/rhart-image-g-2/text-to-image", payload).get("data") or {}).get("taskId")
        check("相同 clientJobId 复用同一任务", first == second, f"{first} / {second}")

        print("\n== 6) 图生图：参考图传递 ==")
        submitted = _post(
            "/openapi/v2/rhart-image-g-2/image-to-image",
            {"prompt": "改成雪山", "aspectRatio": "2:1", "resolution": "1k", "imageUrls": [data_uri, data_uri]},
        )
        task_id = (submitted.get("data") or {}).get("taskId")
        for _ in range(40):
            if (_post("/openapi/v2/query", {"taskId": task_id}).get("data") or {}).get("status") == "SUCCESS":
                break
            time.sleep(0.2)
        check("参考图已透传给商汤客户端", len(captured.get("references") or []) == 2, str(len(captured.get("references") or [])))
        check("2:1 比例换算成功", captured.get("ratio") == "2:1", str(captured.get("ratio")))

        print("\n== 7) 未知端点 ==")
        try:
            _post("/openapi/v2/media/upload/binary", {})
            check("未实现端点返回 4xx（触发 OCV 的 Data-URL 回退）", False, "竟然成功了")
        except urllib.error.HTTPError as exc:
            check("未实现端点返回 4xx（触发 OCV 的 Data-URL 回退）", exc.code == 404, f"HTTP {exc.code}")
    finally:
        httpd.shutdown()
        httpd.server_close()

    print()
    if FAILURES:
        print(f"✗ {len(FAILURES)} 项未通过：" + "；".join(FAILURES))
        return 1
    print("✓ shim 协议自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
