"""决定性集成测试：用 OCV **真实的** 视频 provider 跑通 shim → Agnes。

前一个脚本（`verify_agnes_video.py`）验的是「翻译对不对」；
本脚本验的是「OCV 真的能拿它跑完一整个付费任务」—— 包括：

  * `RunningHubVideoProvider.submit()` 用 OCV 的字段名提交，拿到 taskId
  * `.run()` 里的断点续跑/状态机/防重复提交逻辑
  * 轮询到 SUCCESS 后 `download()` → `validate_video_file()` 的 MP4 容器校验
  * 最终产物落盘且 sha256 记录进状态文件

这一段是真正的风险所在：前一个脚本全绿也不能保证 OCV 的
`_find_url` / `validate_request` / `validate_video_file` 都满意。
所以这里**必须**用 OCV 的真实类，而不是自己模拟。

假 Agnes 服务同时负责：
  * `POST /v1/videos` 创建任务
  * `GET /agnesapi` 查询
  * `GET /generated/<id>.mp4` 提供一个**结构合法的最小 MP4**
    （OCV 的 validate_video_file 会逐 box 校验 ftyp/moov/mdat + vide 轨道）
"""
from __future__ import annotations

import hashlib
import json
import struct
import sys
import tempfile
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

from ocv_cloud_stack import config, video_shim  # noqa: E402
from module6_dynamic_video import (  # noqa: E402
    RunningHubVideoProvider, VideoGenerationRequest, validate_request,
)

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


def minimal_mp4() -> bytes:
    """构造一个 OCV `validate_video_file()` 会接受的最小 MP4。

    它要求：顶层有 ftyp / moov / mdat，且深度 3+ 处有 hdlr 且 handler 为 'vide'。
    结构：
        ftyp
        moov
          trak
            mdia
              hdlr  (handler_type = 'vide')
              mdat? 不需要——mdat 在顶层
        mdat
    """
    def box(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I4s", len(payload) + 8, kind) + payload

    ftyp = box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso2mp41")
    # hdlr: version+flags(4) + predefined(4) + handler_type(4) + reserved(12) + name
    hdlr = box(b"hdlr", b"\x00" * 8 + b"vide" + b"\x00" * 12 + b"VideoHandler\x00")
    mdia = box(b"mdia", hdlr)
    trak = box(b"trak", mdia)
    moov = box(b"moov", trak)
    mdat = box(b"mdat", b"\x00" * 64)
    return ftyp + moov + mdat


VIDEO_BYTES = minimal_mp4()
CREATED: dict[str, dict] = {}
SEEN: list[dict] = []


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # noqa: ANN002
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
        SEEN.append({"path": self.path, "body": data, "auth": self.headers.get("Authorization")})

        if self.path.rstrip("/").endswith("/videos"):
            # 严格校验（同前一个脚本，确保 OCV 发出的体真的合规）
            problems = []
            if not data.get("model"):
                problems.append("model is required")
            if not str(data.get("prompt") or "").strip():
                problems.append("prompt is required")
            if data.get("mode") not in {"text", "keyframe", "reference"}:
                problems.append("mode invalid")
            secs = data.get("seconds")
            if not isinstance(secs, str) or not secs.isdigit() or not 4 <= int(secs) <= 12:
                problems.append("seconds must be string 4-12")
            if data.get("size") not in {"720P", "1080P", "1K", "2K"}:
                problems.append("size invalid")
            mode = data.get("mode")
            if mode == "keyframe" and not (data.get("first_frame") or data.get("last_frame")):
                problems.append("keyframe requires frame")
            if mode == "reference" and not (data.get("images") or data.get("audios") or data.get("videos")):
                problems.append("reference requires media")
            if problems:
                self._json({"error": {"message": "; ".join(problems)}}, 400)
                return
            vid = f"video_{len(CREATED) + 1}"
            CREATED[vid] = {"queries": 0, "body": data}
            self._json({"id": f"task_{vid}", "task_id": f"task_{vid}", "video_id": vid,
                        "object": "video", "model": data["model"], "status": "queued",
                        "progress": 0, "seconds": secs, "size": data["size"]})
            return

        # 上传端点是 shim **故意不实现**的 —— OCV 会回退成 data URI。
        self._json({"error": {"message": "unsupported endpoint"}}, 404)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/generated/"):
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(VIDEO_BYTES)))
            self.end_headers()
            self.wfile.write(VIDEO_BYTES)
            return
        query = parse_qs(parsed.query)
        SEEN.append({"path": self.path, "query": {k: v[0] for k, v in query.items()}})
        if not parsed.path.rstrip("/").endswith("/agnesapi"):
            self._json({"error": {"message": "not found"}}, 404)
            return
        vid = (query.get("video_id") or [""])[0]
        task = CREATED.get(vid)
        if task is None:
            self._json({"error": {"message": "not found"}}, 404)
            return
        if not (query.get("model_name") or [None])[0] and task["body"].get("mode") != "text":
            self._json({"error": {"message": "model_name required"}}, 404)
            return
        task["queries"] += 1
        if task["queries"] < 2:
            self._json({"id": f"task_{vid}", "object": "video", "status": "in_progress",
                        "progress": 40, "url": None})
            return
        self._json({"id": f"task_{vid}", "object": "video", "status": "completed",
                    "progress": 100, "url": f"http://127.0.0.1:{PORT}/generated/{vid}.mp4",
                    "error": None})


PORT = 8952
SHIM_PORT = 8953


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), _Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="fake-agnes-2", daemon=True).start()

    # 关键：**本地 shim 必须真的跑起来** —— OCV 的 provider 把请求打到
    # `shim_base_url`（不是上游），视频路由就挂在 shim 上。首版漏了这一步，
    # provider 打到没人监听的 8799，得到 DynamicVideoTaskUnknown ——
    # 测试如实报错，而这正是"没被验证过的东西不能算完成"的例子。
    from ocv_cloud_stack import image_shim

    # 快照整个面板层，finally 里完整还原（避免污染用户配置）。
    #
    # ⚠ ``force=True`` 是刻意的：本测试要把用户的真 AGNES_API_KEY 换成假 Key，
    #   而 ``update_store`` 默认会**拒绝**这种"长凭据 → 短值"的写入
    #   （见 ``config.CredentialClobberError``）。快照 + finally 完整还原
    #   已经保证了安全性，所以这里显式放行。
    #   实测价值：这条保护上线当天就拦住了本脚本 —— 它原先会静默把用户
    #   51 字符的真 Key 覆盖成 ``sk-fake-key``，而 finally 的还原只在
    #   正常路径生效，一旦中途崩溃就永久丢失。
    _saved_store = config.store_values()
    config.update_store({
        "CLOUD_STACK_SHIM_PORT": str(SHIM_PORT),
        "AGNES_API_BASE": f"http://127.0.0.1:{PORT}/v1",
        "AGNES_API_KEY": "sk-fake-key",
        "AGNES_VIDEO_MODEL": "agnes-video-2.5-flash",
        "CLOUD_STACK_VIDEO_ENABLED": "1",
        "CLOUD_STACK_VIDEO_SIZE": "720P",
        "CLOUD_STACK_VIDEO_MODE": "auto",
        "CLOUD_STACK_VIDEO_INLINE_MEDIA": "1",
        "CLOUD_STACK_VIDEO_POLL_SECONDS": "1",
    }, force=True)
    shim = image_shim.serve_in_thread(port=SHIM_PORT)
    if shim is None:
        print(f"!! 无法在 {SHIM_PORT} 启动 shim，测试无法进行")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="ocv-video-e2e-"))
    try:
        # 造一张真实的参考图（走 data URI 内联路径）
        from PIL import Image

        ref = tmp / "core.png"
        Image.new("RGB", (64, 64), (30, 90, 180)).save(ref, format="PNG")

        output = tmp / "clip.mp4"
        state_path = tmp / "state.json"

        request = VideoGenerationRequest(
            prompt="让分镜中的角色自然转头，镜头缓慢推进",
            image_paths=(ref,),
            output_path=output,
            duration=5,
            ratio="16:9",
            resolution="720p",
            seed=-1,
        )
        validate_request(request)
        print("== 1) OCV 自己的校验通过 ==")
        check("validate_request 接受该请求", True)

        # OCV 会用它自己的 base_url 拼 submit_path —— 那个 base_url 指向 shim。
        provider = RunningHubVideoProvider(
            "managed-by-cloud-free-stack",
            base_url=config.shim_base_url(),
            submit_path="/openapi/v2/video/agnes/multimodal-video",
            query_path="/api/video/query",
            upload_path="/openapi/v2/media/upload/binary",
        )

        print("\n== 2) provider identity ==")
        print(f"    base={provider.base_url} submit={provider.submit_path}")
        print(f"    query={provider.query_path} upload={provider.upload_path}")

        print("\n== 3) 真跑 provider.run()（提交→轮询→下载→MP4 校验）==")
        notes: list[str] = []
        started = time.time()
        try:
            result = provider.run(request, state_path,
                                  progress=lambda m: notes.append(m),
                                  poll_seconds=1, timeout_seconds=60)
            ok = True
            error = ""
        except Exception as exc:  # noqa: BLE001
            ok = False
            error = f"{type(exc).__name__}: {exc}"
            result = None
        elapsed = time.time() - started

        check("provider.run() 成功返回", ok, error or f"耗时 {elapsed:.1f}s")
        for line in notes:
            print(f"      · {line}")

        if ok and result is not None:
            check("产物文件存在", Path(result).is_file(), str(result))
            blob = Path(result).read_bytes()
            check("产物是 MP4（含 ftyp/moov/mdat）",
                  b"ftyp" in blob and b"moov" in blob and b"mdat" in blob,
                  f"{len(blob)} 字节")
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            check("状态文件记录 DOWNLOADED", saved.get("status") == "DOWNLOADED",
                  str(saved.get("status")))
            check("状态文件记录了 sha256",
                  saved.get("sha256") == hashlib.sha256(blob).hexdigest())
            check("状态文件记录了 task_id", bool(saved.get("task_id")),
                  str(saved.get("task_id")))

        print("\n== 4) 上游真的收到了合规的 Agnes 请求 ==")
        creates = [s for s in SEEN if str(s.get("path", "")).rstrip("/").endswith("/videos")]
        check("上游收到 1 次创建请求", len(creates) == 1, f"{len(creates)} 次")
        if creates:
            body = creates[0]["body"]
            print(f"      上游收到的请求体字段：{sorted(body)}")
            check("带 model", body.get("model") == "agnes-video-2.5-flash")
            check("带 mode", body.get("mode") in {"text", "keyframe", "reference"},
                  str(body.get("mode")))
            check("seconds 是字符串 '5'", body.get("seconds") == "5", repr(body.get("seconds")))
            check("size 是 720P", body.get("size") == "720P", str(body.get("size")))
            check("单图 -> keyframe 且首帧是 data URI（内联生效）",
                  body.get("mode") == "keyframe"
                  and str(body.get("first_frame", "")).startswith("data:image/png;base64,"),
                  f"mode={body.get('mode')} first_frame={(str(body.get('first_frame'))[:32])}…")
            check("OCV 发给 shim 的是占位 Key，上游**绝不会**看到它",
                  creates[0].get("auth") != "Bearer managed-by-cloud-free-stack",
                  f"上游收到 {creates[0].get('auth')}")
            check("上游收到的是面板里的真实 Key（shim 负责替换）",
                  creates[0].get("auth") == "Bearer sk-fake-key",
                  f"上游收到 {creates[0].get('auth')}")
        queries = [s for s in SEEN if "query" in s]
        check("查询都带 model_name", all(s["query"].get("model_name") for s in queries),
              f"{len(queries)} 次查询")

        print("\n== 5) 断点续跑：状态文件存在时再次 run 不重新提交 ==")
        before = len(CREATED)
        try:
            provider.run(request, state_path, progress=lambda m: None,
                         poll_seconds=1, timeout_seconds=30)
            resumed_ok = True
            resumed_error = ""
        except Exception as exc:  # noqa: BLE001
            resumed_ok = False
            resumed_error = f"{type(exc).__name__}: {exc}"
        check("续跑成功（复用已下载产物）", resumed_ok, resumed_error)
        check("没有产生新的付费任务", len(CREATED) == before,
              f"{before} -> {len(CREATED)}")
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
        _mine = set(config.store_values()) - set(_saved_store)
        # force=True：还原时要把用户的长真 Key 写回，同样属于"被保护"的写入。
        config.update_store(_saved_store, force=True)
        config.update_store({}, remove=_mine, force=True)
        try:
            shim.shutdown()
        except Exception:  # noqa: BLE001
            pass
        server.shutdown()

    print()
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
