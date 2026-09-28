"""端到端验证：经 shim 的真实 HTTP 提交，发给商汤的 image_url 声明必须与内容一致。

与 `verify_image_mime.py` 的分工：
  * `verify_image_mime.py` 做纯函数级断言（快、无副作用）。
  * 本脚本走**真实链路**：起 shim HTTP 服务 -> 提交 image-to-image -> 拦下
    `requests.post` 捕获实际上游请求体 -> 断言 images[0].image_url 的 media type。

两个必须记住的探针陷阱（首版都踩到了）：
  1. **同一进程内替换 `requests.post` 会把自己的 shim 往返也吞掉。**
     首版因此拿到假响应（`.get("data")` 得到 list）。解法：本地 shim 调用一律走
     `urllib.request`，与 `requests` 彻底分离 —— 而不是按 URL 分流后在
     `finally` 里恢复，因为…
  2. **恢复太早会漏放真实请求。** shim 的 `_run_task` 在 ThreadPoolExecutor
     线程里异步执行，`finally` 把 `requests.post` 还原回真实现后，工作线程才
     调用上游 —— 那就打到真实商汤去了。所以探针必须**覆盖到任务结束**。
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
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = PROJECT_ROOT / "plugins" / "cloud_free_stack"
for entry in (str(PROJECT_ROOT), str(PLUGIN_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import requests  # noqa: E402

from ocv_cloud_stack import config, image_shim  # noqa: E402

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


def _png_bytes() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), (10, 120, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


def _local_post(url: str, payload: dict, timeout: float = 15.0) -> dict:
    """访问本地 shim 专用：走 urllib，绕开被替换的 requests.post。"""
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _local_get(url: str, timeout: float = 10.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


PORT = 8931
CAPTURED: list[dict] = []


class _FakeUpstreamResponse:
    """伪造成商汤同步返回，避免真的花钱调上游。"""

    status_code = 200
    ok = True

    def __init__(self, png: bytes) -> None:
        self._payload = {"data": [{"b64_json": base64.b64encode(png).decode("ascii")}]}

    def json(self):  # noqa: ANN201
        return self._payload

    def raise_for_status(self):  # noqa: ANN201
        return None


def main() -> int:
    png = _png_bytes()
    print("== 启动 shim（仅本机，端口 %d）==" % PORT)
    config.update_store({"CLOUD_STACK_SHIM_PORT": str(PORT)})
    server = image_shim.serve_in_thread(port=PORT)
    check("shim 已在进程内托管", server is not None, f"http://127.0.0.1:{PORT}")
    shim_base = f"http://127.0.0.1:{PORT}"

    original_post = requests.post

    def _capture(url, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        CAPTURED.append({"url": str(url), "json": kwargs.get("json")})
        return _FakeUpstreamResponse(png)

    try:
        # ---- 探针从此刻起覆盖整个任务生命周期（含 shim 工作线程的上游调用）----
        requests.post = _capture  # type: ignore[assignment]

        health = _local_get(f"{shim_base}/health")
        check("/health 可用", (health.get("data") or {}).get("status") == "ok",
              json.dumps(health.get("data"), ensure_ascii=False))

        # 复刻 OCV 上游形态：扩展名 .jpg -> 声明 image/jpeg，但内容其实是 PNG
        bogus_uri = "data:image/jpeg;base64," + base64.b64encode(png).decode("ascii")

        submit = _local_post(
            f"{shim_base}/openapi/v2/sensenova-u1.5-lite/image-to-image",
            {
                "prompt": "端到端验证：参考图 media type 纠正",
                "aspectRatio": "1:1",
                "resolution": "1k",
                "imageUrls": [bogus_uri],
            },
        )
        task_id = (submit.get("data") or {}).get("taskId")
        check("提交成功并返回 data.taskId", bool(task_id), str(task_id))

        deadline = time.monotonic() + 30
        status = ""
        while time.monotonic() < deadline:
            info = _local_post(f"{shim_base}/openapi/v2/query", {"taskId": task_id})
            status = str((info.get("data") or {}).get("status") or "")
            if status in {"SUCCESS", "FAILED"}:
                break
            time.sleep(0.3)

        check("任务最终成功（未因 media type 被上游拒绝）", status == "SUCCESS", f"status={status}")

        edit_calls = [c for c in CAPTURED if str(c["url"]).endswith("/images/edits")]
        check("实际上游调用的是 /images/edits（图生图）", len(edit_calls) >= 1,
              f"捕获 {len(CAPTURED)} 次上游请求，其中 edits {len(edit_calls)} 次")
        if edit_calls:
            payload = edit_calls[0]["json"] or {}
            images = payload.get("images") or []
            check("payload 带 images[0].image_url", bool(images), json.dumps(payload)[:200])
            if images:
                uri = str(images[0].get("image_url") or "")
                got = uri.split(";", 1)[0][len("data:"):]
                check("发给商汤的 media type 已是 image/png（与内容一致）",
                      got == "image/png", f"声明的 image/jpeg -> {got}")
                check("图片负载未被改动",
                      uri.partition(",")[2] == base64.b64encode(png).decode("ascii"))

        # ---- 文生图回归：不应带 images，且走 /images/generations ----
        CAPTURED.clear()
        submit2 = _local_post(
            f"{shim_base}/openapi/v2/sensenova-u1.5-lite/text-to-image",
            {"prompt": "文生图回归", "aspectRatio": "16:9", "resolution": "1k"},
        )
        deadline = time.monotonic() + 30
        status2 = ""
        while time.monotonic() < deadline:
            info = _local_post(f"{shim_base}/openapi/v2/query",
                               {"taskId": (submit2.get("data") or {}).get("taskId")})
            status2 = str((info.get("data") or {}).get("status") or "")
            if status2 in {"SUCCESS", "FAILED"}:
                break
            time.sleep(0.3)
        gen_calls = [c for c in CAPTURED if str(c["url"]).endswith("/images/generations")]
        check("文生图走 /images/generations 且不带 images 字段",
              len(gen_calls) == 1 and "images" not in (gen_calls[0]["json"] or {}),
              f"status={status2}, 捕获 {len(gen_calls)} 次 generations")

        # ---- 未实现端点仍应 404（OCV 依赖它触发 Data-URL 回退）----
        try:
            _local_post(f"{shim_base}/openapi/v2/media/upload/binary", {})
            check("upload/binary 返回 404", False, "居然没有报错")
        except urllib.error.HTTPError as exc:
            check("upload/binary 仍返回 404（保留 OCV 的 Data-URL 回退路径）",
                  exc.code == 404, f"HTTP {exc.code}")
    finally:
        requests.post = original_post  # type: ignore[assignment]
        config.update_store({}, remove=["CLOUD_STACK_SHIM_PORT"])
        if server is not None:
            threading.Thread(target=server.shutdown, daemon=True).start()
            time.sleep(0.4)

    print()
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
