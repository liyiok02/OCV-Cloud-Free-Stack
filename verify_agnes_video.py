"""端到端验证：OCV 协议 → shim → Agnes 协议，用**忠实模拟的 Agnes 服务**校验。

## 为什么这样验证

无法用真实 Agnes Key（用户未提供），但"没测过"不等于"不用测"。这里按官方文档
**逐字段**实现一个假 Agnes 服务，它像真上游一样**严格校验**：

* ``POST /v1/videos``：缺 ``model``/``prompt``/``mode`` 直接 400；
  ``seconds`` 不是 "4".."12" 的**字符串**直接 400；
  ``size`` 不在允许集合直接 400；Flash 的 ``images`` 超过 5 张直接 400；
  ``mode=text`` 却带媒体字段、``mode=reference`` 却不带媒体 → 400。
* ``GET /agnesapi``：``video_id`` 不存在 → 404；
  未传 ``model_name`` 且原始任务非 text 模式 → 404（**文档明说的行为**）。

于是"我们的翻译对不对"就变成可判定的：只要假服务不返回 400/404，就说明
请求体与查询串都符合官方契约。

覆盖：
  A. 纯文生视频（0 张图 → mode=text）
  B. 单图（→ keyframe + first_frame；对 OCV「核心分镜动起来」的主场景）
  C. 双图（→ keyframe + first_frame + last_frame）
  D. 多图（→ reference + images，Flash 截断到 5 张）
  E. 时长夹紧（OCV 允许 4–15，Agnes 只收 4–12）
  F. 分辨率映射（480p 在 Agnes 不存在 → 按面板档位）
  G. 状态映射（queued/in_progress/completed → OCV 大写集合）
  H. 轮询链路：submit → query 直到 SUCCESS，并带 results[].url
  I. 失败任务：status=failed → OCV 的 FAILURE + errorMessage
  J. 提交被上游拒绝时**绝不返回 taskId**（否则 OCV 会永久卡死等待）
"""
from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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

from ocv_cloud_stack import agnes_video, config, video_shim  # noqa: E402

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


# --------------------------------------------------------------------------
# 忠实的假 Agnes 服务
# --------------------------------------------------------------------------

CREATED: dict[str, dict] = {}
SEEN: list[dict] = []
MODES = {"text", "keyframe", "reference"}
SIZES = {"720P", "1080P", "1K", "2K"}
FLASH_SIZES = {"720P"}
RATIOS = {"21:9", "16:9", "4:3", "1:1", "3:4", "9:16"}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # noqa: ANN002 - 静音
        pass

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            self._json({"error": {"message": "invalid json"}}, 400)
            return
        SEEN.append({"path": self.path, "body": data})

        # ---- 严格复刻官方契约校验 ----
        if self.path.rstrip("/").endswith("/v1/videos") or self.path.rstrip("/").endswith("/videos"):
            model = str(data.get("model") or "")
            problems = []
            if not model:
                problems.append("model is required")
            if not str(data.get("prompt") or "").strip():
                problems.append("prompt is required")
            mode = data.get("mode")
            if mode not in MODES:
                problems.append(f"mode must be one of {sorted(MODES)}")
            seconds = data.get("seconds")
            if not isinstance(seconds, str) or not seconds.isdigit() or not 4 <= int(seconds) <= 12:
                problems.append("seconds must be a string between 4 and 12")
            size = data.get("size")
            if size not in SIZES:
                problems.append(f"size must be one of {sorted(SIZES)}")
            if "flash" in model and size not in FLASH_SIZES:
                problems.append("size must be 720P")
            if data.get("n") not in (None, 1):
                problems.append("n must be 1")
            if "aspect_ratio" in data and data["aspect_ratio"] not in RATIOS:
                problems.append("aspect_ratio unsupported")
            # 模式与媒体字段的互斥规则
            media = {"first_frame", "last_frame", "images", "audios", "videos"}
            present = {k for k in media if data.get(k)}
            if mode == "text" and present:
                problems.append("mode=text must not carry media fields")
            if mode == "keyframe" and not ({"first_frame", "last_frame"} & present):
                problems.append("keyframe requires first_frame or last_frame")
            if mode == "keyframe" and ({"images", "audios", "videos"} & present):
                problems.append("keyframe must not carry images/audios/videos")
            if mode == "reference" and not ({"images", "audios", "videos"} & present):
                problems.append("reference requires media")
            if mode == "reference" and ({"first_frame", "last_frame"} & present):
                problems.append("reference must not carry first/last_frame")
            if problems:
                self._json({"error": {"message": "; ".join(problems)}}, 400)
                return

            video_id = f"video_{len(CREATED) + 1}"
            CREATED[video_id] = {"model": model, "mode": mode, "status": "queued",
                                 "queries": 0, "body": data}
            self._json({
                "id": f"task_{video_id}", "task_id": f"task_{video_id}",
                "video_id": video_id, "object": "video", "model": model,
                "status": "queued", "progress": 0, "created_at": int(time.time()),
                "seconds": seconds, "size": size,
            })
            return

        self._json({"error": {"message": "not found"}}, 404)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        SEEN.append({"path": self.path, "query": {k: v[0] for k, v in query.items()}})
        if not parsed.path.rstrip("/").endswith("/agnesapi"):
            self._json({"error": {"message": "not found"}}, 404)
            return
        video_id = (query.get("video_id") or [""])[0]
        task = CREATED.get(video_id)
        if task is None:
            self._json({"error": {"message": "video not found"}}, 404)
            return
        # 文档明说：不带 model_name 只能查 text 模式的任务。
        if not (query.get("model_name") or [None])[0] and task["mode"] != "text":
            self._json({"error": {"message": "model_name required for non-text mode"}}, 404)
            return
        task["queries"] += 1
        # 前两次排队，之后完成（或按场景失败）。
        fail_mode = FAIL_SCENARIO.get("value")
        if task["queries"] < 2:
            task["status"] = "in_progress"
            self._json({"id": f"task_{video_id}", "object": "video", "status": "in_progress",
                        "progress": 50, "url": None, "error": None})
            return
        if fail_mode:
            task["status"] = "failed"
            self._json({"id": f"task_{video_id}", "object": "video", "status": "failed",
                        "progress": 100, "url": None,
                        "error": {"message": "Invalid reference media"}})
            return
        task["status"] = "completed"
        self._json({"id": f"task_{video_id}", "object": "video", "status": "completed",
                    "progress": 100, "quality": "standard",
                    "url": f"http://127.0.0.1:{PORT}/generated/{video_id}.mp4",
                    "error": None})


PORT = 8951
FAIL_SCENARIO: dict[str, object] = {"value": ""}


def _reset() -> None:
    CREATED.clear()
    SEEN.clear()
    FAIL_SCENARIO["value"] = ""


# --------------------------------------------------------------------------
# 驱动
# --------------------------------------------------------------------------

def main() -> int:
    global PORT
    server = ThreadingHTTPServer(("127.0.0.1", PORT), _Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="fake-agnes", daemon=True).start()

    # 让插件把"上游"指向假服务，并开启视频接管。
    #
    # ⚠ 保存并**完整还原**面板层：早先版本的测试只删了自己加的键，
    #   却把 `AGNES_API_BASE` 之类留在了面板层 —— 那会污染用户配置，
    #   也会让后面跑的自检读到非预期的值。这里快照整个 store，
    #   finally 里按键还原（删掉自己加的、恢复原有值）。
    _saved_store = config.store_values()
    # ``force=True``：本测试要把用户的真 AGNES_API_KEY 换成假 Key，
    # 而 ``update_store`` 默认会拒绝这种"长凭据 → 短值"写入
    # （见 ``config.CredentialClobberError``）。快照 + finally 完整还原已保证安全。
    config.update_store({
        "AGNES_API_BASE": f"http://127.0.0.1:{PORT}/v1",
        "AGNES_API_KEY": "sk-fake-key-for-local-test",
        "AGNES_VIDEO_MODEL": "agnes-video-2.5-flash",
        "CLOUD_STACK_VIDEO_ENABLED": "1",
        "CLOUD_STACK_VIDEO_SIZE": "720P",
        # ⚠ 必须显式设 auto：面板层优先级最高，用户若把它钉死成 text，
        #   带图镜头会走 text 分支、本脚本的模式断言全部失败（本轮实测踩到）。
        "CLOUD_STACK_VIDEO_MODE": "auto",
        "CLOUD_STACK_VIDEO_INLINE_MEDIA": "1",
        "CLOUD_STACK_VIDEO_POLL_SECONDS": "1",
    }, force=True)
    try:
        print("== A) 纯文生视频：0 张图 -> mode=text ==")
        _reset()
        payload, notes = agnes_video.build_payload(prompt="一只猫", duration=5, reference_images=[])
        check("推导出 text 模式", payload["mode"] == "text", str(payload["mode"]))
        check("不带媒体字段", not any(k in payload for k in ("images", "first_frame")))
        created = agnes_video.create_task(payload, api_key=config.video_api_key())
        check("假服务接受（未 400）", bool(created.get("video_id")), str(created.get("video_id")))
        check("返回 video_id 而非 task_id 优先", str(created["video_id"]).startswith("video_"))

        print("\n== B) 单图 -> keyframe + first_frame（核心场景）==")
        _reset()
        payload, _ = agnes_video.build_payload(
            prompt="让分镜动起来", duration=5, reference_images=["https://example.com/core.png"])
        check("推导出 keyframe", payload["mode"] == "keyframe", str(payload["mode"]))
        check("核心分镜作为 first_frame",
              payload.get("first_frame") == "https://example.com/core.png")
        check("不带 last_frame", "last_frame" not in payload)
        created = agnes_video.create_task(payload, api_key=config.video_api_key())
        check("假服务接受", bool(created.get("video_id")))

        print("\n== C) 双图 -> keyframe + 首尾帧 ==")
        _reset()
        payload, _ = agnes_video.build_payload(
            prompt="过渡", duration=5,
            reference_images=["https://example.com/a.png", "https://example.com/b.png"])
        check("keyframe 模式", payload["mode"] == "keyframe")
        check("首帧正确", payload.get("first_frame") == "https://example.com/a.png")
        check("尾帧正确", payload.get("last_frame") == "https://example.com/b.png")
        created = agnes_video.create_task(payload, api_key=config.video_api_key())
        check("假服务接受", bool(created.get("video_id")))

        print("\n== D) 多图 -> reference + images（Flash 截断到 5）==")
        _reset()
        many = [f"https://example.com/p{i}.png" for i in range(1, 9)]
        payload, notes = agnes_video.build_payload(
            prompt="以 <Picture 1> 为参考", duration=5, reference_images=many)
        check("reference 模式", payload["mode"] == "reference", str(payload["mode"]))
        check("Flash 截断到 5 张", len(payload.get("images") or []) == 5,
              f"{len(payload.get('images') or [])} 张")
        created = agnes_video.create_task(payload, api_key=config.video_api_key())
        check("假服务接受（未因超限 400）", bool(created.get("video_id")))

        print("\n== E) 时长夹紧：OCV 允许 4–15，Agnes 只收 4–12 ==")
        _reset()
        payload, notes = agnes_video.build_payload(prompt="x", duration=15, reference_images=[])
        check("15s 被夹紧成字符串 '12'", payload["seconds"] == "12", repr(payload["seconds"]))
        check("夹紧有提示信息（不静默）", any("夹紧" in n for n in notes), str(notes))
        check("是字符串而非整数（Agnes 要字符串）", isinstance(payload["seconds"], str))
        created = agnes_video.create_task(payload, api_key=config.video_api_key())
        check("假服务接受", bool(created.get("video_id")))

        print("\n== F) 分辨率：Agnes 无 480p ==")
        _reset()
        payload, notes = agnes_video.build_payload(
            prompt="x", duration=5, resolution="480p", reference_images=[])
        check("480p 映射到 720P", payload["size"] == "720P", payload["size"])
        check("映射有提示（如实告知，不假装 480p 生效）",
              any("480p" in n for n in notes), str(notes))
        created = agnes_video.create_task(payload, api_key=config.video_api_key())
        check("假服务接受", bool(created.get("video_id")))

        print("\n== G) 状态映射 ==")
        for raw, expect in (("queued", "QUEUED"), ("in_progress", "RUNNING"),
                            ("completed", "SUCCESS"), ("failed", "FAILED")):
            check(f"{raw} -> {expect}", agnes_video._STATUS_MAP.get(raw) == expect,
                  str(agnes_video._STATUS_MAP.get(raw)))

        print("\n== H) shim 轮询链路：submit -> query 直到 SUCCESS ==")
        _reset()
        snapshot = video_shim.submit({
            "prompt": "让分镜动起来", "duration": 5, "ratio": "16:9", "resolution": "720p",
            "imageUrls": ["https://example.com/core.png"],
        })
        task_id = snapshot["taskId"]
        check("shim 返回 taskId", bool(task_id), task_id)

        deadline = time.monotonic() + 30
        final = {}
        while time.monotonic() < deadline:
            final = video_shim.query_response(task_id)
            if (final.get("data") or {}).get("status") in {"SUCCESS", "FAILED"}:
                break
            time.sleep(0.3)
        data = final.get("data") or {}
        check("最终状态是 OCV 认的 SUCCESS", data.get("status") == "SUCCESS", str(data.get("status")))
        results = data.get("results") or []
        check("带 results[].url（_find_url 认的形态）",
              bool(results) and str(results[0].get("url", "")).startswith("http"))
        # OCV 的 _find_url 真能吃下去吗？用它的实现验证
        sys.path.insert(0, str(PROJECT_ROOT))
        from module6_dynamic_video import _find_url  # noqa: PLC0415

        check("OCV 的 _find_url 能从该响应取出 url",
              _find_url(final.get("data")) == results[0]["url"] if results else False)

        print("\n== H2) 查询串必须带 model_name（非 text 模式）==")
        get_seen = [s for s in SEEN if "query" in s]
        check("确实发起了 GET /agnesapi", bool(get_seen), f"{len(get_seen)} 次")
        if get_seen:
            check("每次都带 video_id", all("video_id" in s["query"] for s in get_seen))
            check("每次都带 model_name（否则 keyframe/reference 会 404）",
                  all(s["query"].get("model_name") for s in get_seen),
                  str(get_seen[0]["query"]))

        print("\n== I) 失败任务 -> OCV 的 FAILURE + errorMessage ==")
        _reset()
        FAIL_SCENARIO["value"] = "fail"
        snapshot = video_shim.submit({
            "prompt": "会失败的任务", "duration": 5, "imageUrls": ["https://example.com/x.png"],
        })
        task_id = snapshot["taskId"]
        deadline = time.monotonic() + 30
        final = {}
        while time.monotonic() < deadline:
            final = video_shim.query_response(task_id)
            if (final.get("data") or {}).get("status") in {"SUCCESS", "FAILED"}:
                break
            time.sleep(0.3)
        data = final.get("data") or {}
        check("失败状态传给 OCV", data.get("status") == "FAILED", str(data.get("status")))
        check("错误信息来自上游",
              "Invalid reference media" in str(data.get("errorMessage") or ""),
              str(data.get("errorMessage")))
        from module6_dynamic_video import FAILURE as OCV_FAILURE  # noqa: PLC0415

        check("该状态在 OCV 的 FAILURE 集合里", data.get("status") in OCV_FAILURE)

        print("\n== J) 提交被上游拒绝时绝不返回 taskId ==")
        _reset()
        FAIL_SCENARIO["value"] = ""
        # 构造一个必然被 400 的请求：mode=reference 但不带任何媒体
        bad = {"model": "agnes-video-2.5-flash", "prompt": "x", "seconds": "5",
               "mode": "reference", "size": "720P", "n": 1}
        raised = False
        try:
            agnes_video.create_task(bad, api_key=config.video_api_key())
        except agnes_video.AgnesVideoError as exc:
            raised = True
            check("上游 400 被如实转成异常", "400" in str(exc), str(exc)[:90])
        check("确实抛出了异常（没有伪造成功）", raised)

        print("\n== K) 内置默认关闭（付费能力必须 opt-in）==")
        # ⚠ 不能断言"当前生效值" —— 用户可能在面板里打开过视频接管，
        #   而面板层优先级最高（> os.environ > .env > 默认），
        #   此时 config.video_enabled() 为 True，断言会恒假。
        #   要验的不变量是「**没人配置时**默认关闭」⇒ 直接验内置默认值。
        import inspect

        src = inspect.getsource(config.video_enabled)
        check("内置默认关闭", 'get_bool("CLOUD_STACK_VIDEO_ENABLED", False)' in src)
        print(f"    当前生效值 = {config.video_enabled()}"
              f"（来源层：{config.layer_of('CLOUD_STACK_VIDEO_ENABLED')}）")

        print("\n== L) 幂等：同一 clientJobId 只产生一个付费任务 ==")
        _reset()
        job = {"prompt": "幂等测试", "duration": 5, "clientJobId": "fixed-job-1",
               "imageUrls": ["https://example.com/a.png"]}
        first = video_shim.submit(dict(job))
        second = video_shim.submit(dict(job))
        check("两次提交返回同一 taskId", first["taskId"] == second["taskId"],
              f"{first['taskId'][:8]} vs {second['taskId'][:8]}")
        check("上游只收到一次创建请求", len(CREATED) == 1, f"{len(CREATED)} 个上游任务")
    finally:
        # 完整还原面板层：恢复原值、删掉本测试新增的键。
        # force=True：还原时要把用户的长真 Key 写回，同属被保护的写入。
        _mine = set(config.store_values()) - set(_saved_store)
        config.update_store(_saved_store, force=True)
        config.update_store({}, remove=_mine, force=True)
        server.shutdown()

    print()
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
