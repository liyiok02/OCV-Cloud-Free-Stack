"""本地 RunningHub 协议 shim —— 把商汤同步接口伪装成 OCV 期待的异步任务接口。

OCV 的生图层（``module4_video_render.py``）把整个并发体系都建立在
「提交拿 taskId → 轮询 /query」之上：账号池、``clientJobId`` 幂等、
421 排队、审核错误码映射、重试代际，全部围绕这个模型。

商汤的图片接口是同步返回的。与其去重构 OCV 的协议层（改动面极大），
这里做一个小型本地 HTTP 服务，对外精确复刻 RunningHub 的四个端点，
对内把请求翻译成商汤的 ``/v1/images/generations`` 与 ``/v1/images/edits``。

OCV 侧只改一个配置：``IMAGE_API_BASE_URL=http://127.0.0.1:8799``。

端点清单（OCV 实际会打过来的）：

===============================  ==========================================
``POST /openapi/v2/<model>/text-to-image``    提交文生图任务
``POST /openapi/v2/<model>/image-to-image``   提交图生图（参考图）任务
``POST /openapi/v2/query``                    查询任务状态与结果
``POST /uc/openapi/accountStatus``            查询活跃任务数
===============================  ==========================================

图片上传端点 ``/openapi/v2/media/upload/binary`` **故意不实现**：
OCV 有内置回退，上传失败时会把参考图转成 ``data:image/*;base64,``
直传（见 ``_reference_image_url``），而商汤正好接受 Data-URL。
少实现一个端点，就少一次无意义的本地往返。

除了伪造 RunningHub 协议，这台服务还顺带托管**配置面板**：

===============================  ==========================================
``GET  /panel``                    配置面板页面
``GET  /panel.js``                 注入到 OCV 前端的入口脚本
``GET  /api/panel/state``          读取当前配置
``GET  /api/panel/models``         拉取服务商在线模型清单（kind=llm/image/tts）
``POST /api/panel/save``           写回配置
``POST /api/panel/reset``          清空面板层
``POST /api/panel/test/tts``       试听 MiMo 配音
``POST /api/panel/voice/upload``   上传克隆参考音频（data URI，≤8MB）
``POST /api/panel/voice/delete``   删除克隆音色
``POST /api/panel/test/llm``       测试商汤 JSON 输出
``POST /api/panel/test/image``     测试商汤出图
===============================  ==========================================
"""

from __future__ import annotations

import json
import re
import signal
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import config, panel, sense_image

_SUBMIT_RE = re.compile(r"^/openapi/v2/(?P<model>[^/]+)/(?P<op>text-to-image|image-to-image)$")

# 视频提交路由。OCV 的 video provider 把 URL 拼成 ``base_url + submit_path``，
# 而 submit_path 来自 ``VIDEO_SUBMIT_PATH``（由 install 写成下面这个形状）。
# 用 ``[^/]+`` 匹配模型段，是为了让用户改 ``AGNES_VIDEO_MODEL`` 时不必同步改路径。
_VIDEO_SUBMIT_RE = re.compile(r"^/openapi/v2/video/(?P<model>[^/]+)/multimodal-video$")


def _video_error_status(exc: Exception) -> tuple[int, int]:
    """把提交失败的异常映射成 ``(HTTP 状态, 给 OCV 的错误码)``（F-013）。

    ## 只有"确实失败"才会走到这里

    上游**暂时性拒绝**（503 队列满 / 429）不会走到这里 —— ``video_shim.submit``
    已经把它转成"待提交任务"并返回了任务身份，由后台每 30 秒重试、最多 15 次。
    所以下面只处理两类**真失败**：

    ## 为什么要按"确定没创建"分流，而不是一律 400

    OCV 自己的判定逻辑（``module6_dynamic_video`` 的 ``submit``）是：

    * **HTTP < 500 且有错误码** ⇒ ``DynamicVideoTaskFailed``
      ⇒ 写 ``terminal=True`` ⇒ 界面给出「重新付费生成本镜」**一键重试**入口；
    * **HTTP ≥ 500** ⇒ ``DynamicVideoTaskUnknown``
      ⇒ 镜头被**冻结**，要求用户先向服务商核实（防重复扣费）。

    所以这两档对应两种真实语义，必须按事实选：

    | 事实 | HTTP | OCV 行为 | 为什么对 |
    |---|---|---|---|
    | 上游**确实没收下**（4xx） | 400 | 终态 + 可一键重试 | 我们**确定**没创建任务，重试绝不会重复扣费 |
    | **状态不明**（网络中断 / 200 但没给身份） | 503 | 冻结镜头 | **可能已创建**，绝不能让用户一键重发 |

    ⚠ 首版一律返回 400，把「状态不明」也当成了"确定拒绝" —— 那会让用户在
    "可能已经扣过费"的情况下被鼓励再发一次。**不确定时应该冻结，
    而不是给一键重试。**
    """
    if bool(getattr(exc, "definite", False)):
        # 确定没创建 ⇒ 终态 + 一键重试是安全的。
        return 400, 400
    # 状态不明 ⇒ 用 ≥500 让 OCV 冻结镜头（它的语义正是"结果无法确认"）。
    return 503, 503

# 兼容别名：整合包 .env 自带 RUNNINGHUB_ENDPOINT=/v1/images/generations
# （OpenAI 风格值），OCV 会给它套上 /openapi/v2/ 前缀后打到 shim。
# 没有这个别名时直接 404「未实现的端点」，模块 4 永远提交不出图。
_SUBMIT_ALIAS_RE = re.compile(r"^/openapi/v2/(?:[^/]+/)?images/generations$")

_TASKS: dict[str, dict[str, Any]] = {}
_CLIENT_JOBS: dict[str, str] = {}
_TASKS_LOCK = threading.Lock()
_EXECUTOR: ThreadPoolExecutor | None = None

_TASK_TTL_SECONDS = 3600.0
_FILES_DIR_NAME = "images"

# "inproc" = 由某个 OCV 进程内托管（默认）；"detached" = 独立进程前台运行
_MODE = "detached"

_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, api-key",
    "Access-Control-Max-Age": "600",
}


def _panel_static_dir() -> Path:
    return config.plugin_root() / "panel"


def _send_named_image(handler: BaseHTTPRequestHandler, directory: Path, relative: str) -> None:
    """按文件名安全地回传一张图。只取 basename，杜绝路径穿越。"""
    name = Path(relative).name
    target = (directory / name).resolve()
    if not name or not target.is_file() or directory.resolve() not in target.parents:
        handler._send_json({"code": 404, "msg": "文件不存在"}, status=404)  # type: ignore[attr-defined]
        return
    blob = target.read_bytes()
    content_type = "image/png" if target.suffix.lower() == ".png" else "image/jpeg"
    handler._send_bytes(blob, content_type)  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------

_LOG_LOCK = threading.Lock()


def _log(message: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {message}"
    with _LOG_LOCK:
        try:
            with (config.plugin_var_dir() / "shim.log").open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass
    try:
        print(line, flush=True)
    except Exception:
        pass


# --------------------------------------------------------------------------
# 任务执行
# --------------------------------------------------------------------------

def _files_dir() -> Path:
    path = config.plugin_var_dir() / _FILES_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sweep_tasks() -> None:
    now = time.time()
    with _TASKS_LOCK:
        expired = [
            key for key, item in _TASKS.items()
            if item.get("finished_at") and now - float(item["finished_at"]) > _TASK_TTL_SECONDS
        ]
        for key in expired:
            _TASKS.pop(key, None)
        for key, task_id in list(_CLIENT_JOBS.items()):
            if task_id not in _TASKS:
                _CLIENT_JOBS.pop(key, None)


def _run_task(task_id: str, payload: dict[str, Any], is_edit: bool) -> None:
    started = time.time()
    try:
        references = payload.get("imageUrls") if is_edit else None
        if not isinstance(references, list):
            references = None
        blob, size = sense_image.generate(
            prompt=str(payload.get("prompt") or ""),
            ratio=str(payload.get("aspectRatio") or "16:9"),
            resolution=str(payload.get("resolution") or "1k"),
            reference_images=references,
        )
        filename = f"{task_id}.jpg"
        sense_image.save_image(blob, _files_dir() / filename)
        url = f"{config.shim_base_url()}/files/{filename}"
        with _TASKS_LOCK:
            _TASKS[task_id].update(
                {
                    "status": "SUCCESS",
                    "url": url,
                    "size": size,
                    "elapsed": round(time.time() - started, 1),
                    "finished_at": time.time(),
                }
            )
        _log(
            f"任务完成 {task_id} size={size} 参考图={len(references or [])} "
            f"耗时={time.time() - started:.1f}s"
        )
    except Exception as exc:  # noqa: BLE001 - 任何异常都要落到任务状态里
        detail = f"{type(exc).__name__}: {exc}"
        with _TASKS_LOCK:
            _TASKS[task_id].update(
                {
                    "status": "FAILED",
                    "error": detail[:800],
                    "finished_at": time.time(),
                }
            )
        _log(f"任务失败 {task_id} {detail}")
        if config.debug():
            _log(traceback.format_exc())


def _submit(payload: dict[str, Any], is_edit: bool) -> str:
    global _EXECUTOR
    _sweep_tasks()

    client_job_id = str(payload.get("clientJobId") or "").strip()
    if client_job_id:
        with _TASKS_LOCK:
            existing = _CLIENT_JOBS.get(client_job_id)
            if existing and existing in _TASKS:
                _log(f"命中幂等键 {client_job_id} -> 复用任务 {existing}")
                return existing

    task_id = uuid.uuid4().hex
    with _TASKS_LOCK:
        _TASKS[task_id] = {
            "status": "RUNNING",
            "created_at": time.time(),
            "finished_at": None,
        }
        if client_job_id:
            _CLIENT_JOBS[client_job_id] = task_id

    if _EXECUTOR is None:
        _EXECUTOR = ThreadPoolExecutor(
            max_workers=config.image_workers(), thread_name_prefix="sense-image"
        )
    _EXECUTOR.submit(_run_task, task_id, payload, is_edit)
    _log(
        f"接受任务 {task_id} op={'image-to-image' if is_edit else 'text-to-image'} "
        f"比率={payload.get('aspectRatio')} 分辨率={payload.get('resolution')} "
        f"参考图={len(payload.get('imageUrls') or []) if is_edit else 0}"
    )
    return task_id


# --------------------------------------------------------------------------
# HTTP 处理
# --------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    server_version = "OCVCloudStackShim/1.0"
    protocol_version = "HTTP/1.1"

    # ---- 工具 ----
    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        # 面板页面的脚本是跨端口调过来的（5173 -> 8799），必须放开 CORS。
        # 服务只绑定 127.0.0.1，因此这里是本机内的访问，不涉及对外暴露。
        for name, value in _CORS_HEADERS.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, blob: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for name, value in _CORS_HEADERS.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def _send_static(self, path: Path, content_type: str) -> None:
        try:
            blob = path.read_bytes()
        except OSError:
            self._send_json({"ok": False, "message": f"文件不存在: {path.name}"}, status=404)
            return
        self._send_bytes(blob, content_type)

    def _send_file(self, relative: str) -> None:
        _send_named_image(self, _files_dir(), relative)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # 静音默认的 stderr 访问日志，改由 _log 统一输出
        return

    # ---- 路由 ----
    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        for name, value in _CORS_HEADERS.items():
            self.send_header(name, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/health":
            self._send_json(
                {
                    "code": 0,
                    "data": {
                        "status": "ok",
                        "tasks": len(_TASKS),
                        # 便于排查：pid 与托管方式（inproc = 由某个 OCV 进程内托管）
                        "pid": _this_pid(),
                        "mode": _MODE,
                    },
                }
            )
            return
        if path in {"/panel", "/panel/", "/panel/index.html"}:
            self._send_static(_panel_static_dir() / "index.html", "text/html; charset=utf-8")
            return
        if path == "/panel.js":
            self._send_static(_panel_static_dir() / "panel.js", "application/javascript; charset=utf-8")
            return
        if path.startswith("/panel-files/"):
            _send_named_image(self, config.plugin_var_dir() / "panel", path[len("/panel-files/"):])
            return
        if path == "/api/panel/state":
            self._panel_call(panel.snapshot)
            return
        if path == "/api/panel/models":
            query = parse_qs(urlparse(self.path).query)
            kind = (query.get("kind") or ["llm"])[0]
            force = (query.get("force") or ["0"])[0] in {"1", "true", "yes"}
            self._panel_call(panel.list_models, {"kind": kind, "force": force})
            return
        if path.startswith("/files/"):
            self._send_file(path[len("/files/"):])
            return
        self._send_json({"code": 404, "msg": f"未实现的端点: {path}"}, status=404)

    def _panel_call(self, handler: Any, payload: dict[str, Any] | None = None) -> None:
        """统一包住面板调用：任何异常都要变成可读消息，不能让 shim 500 掉。"""
        try:
            result = handler(payload) if payload is not None else handler()
        except ValueError as exc:
            self._send_json({"ok": False, "message": str(exc)}, status=400)
            return
        except Exception as exc:  # noqa: BLE001
            _log(f"面板调用失败: {type(exc).__name__}: {exc}")
            if config.debug():
                _log(traceback.format_exc())
            self._send_json({"ok": False, "message": f"{type(exc).__name__}: {exc}"}, status=500)
            return
        if isinstance(result, dict) and "ok" in result:
            self._send_json(result)
        else:
            self._send_json({"ok": True, "data": result})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path

        if path == "/openapi/v2/query":
            payload = self._read_json()
            task_id = str(payload.get("taskId") or "").strip()
            with _TASKS_LOCK:
                task = dict(_TASKS.get(task_id) or {})
            if not task:
                self._send_json({"code": 404, "msg": "任务不存在", "data": {"status": "FAILED"}}, status=404)
                return
            status = str(task.get("status") or "RUNNING")
            if status == "SUCCESS":
                self._send_json(
                    {
                        "code": 0,
                        "data": {
                            "taskId": task_id,
                            "status": "SUCCESS",
                            # _find_image_url 只认 http(s) URL，因此这里给本机静态地址
                            "results": [{"url": task.get("url")}],
                        },
                    }
                )
            elif status == "FAILED":
                self._send_json(
                    {
                        "code": 0,
                        "data": {
                            "taskId": task_id,
                            "status": "FAILED",
                            "errorMessage": task.get("error") or "商汤图片生成失败",
                        },
                    }
                )
            else:
                self._send_json({"code": 0, "data": {"taskId": task_id, "status": "RUNNING"}})
            return

        if path == "/uc/openapi/accountStatus":
            self._read_json()
            with _TASKS_LOCK:
                active = sum(1 for item in _TASKS.values() if item.get("status") == "RUNNING")
            self._send_json({"code": 0, "data": {"currentTaskCounts": active}})
            return

        matched = _SUBMIT_RE.match(path)
        alias_matched = not matched and bool(_SUBMIT_ALIAS_RE.match(path))
        if matched or alias_matched:
            payload = self._read_json()
            if alias_matched:
                # 别名路径里不含操作名；image_studio 的参考图场景靠把
                # /text-to-image 替换成 /image-to-image 实现，别名替换不到，
                # 所以只能按提交体判定：带了参考图就是图生图。
                is_edit = bool(payload.get("imageUrls"))
            else:
                is_edit = matched.group("op") == "image-to-image"
            task_id = _submit(payload, is_edit)
            # 必须显式给出 data.taskId：OCV 的兜底解析
            # _find_first_key({"taskId","taskID","id"}) 会认裸 id，
            # 若响应里恰好存在别的 id 字段会被误当任务号。
            self._send_json({"code": 0, "data": {"taskId": task_id}})
            return

        # ---- 视频（Agnes）----
        # OCV 的视频 provider 与图片层共用这套「提交→轮询」的服务，
        # 但两者走完全不同的上游协议（见 video_shim 模块文档）。
        video_match = _VIDEO_SUBMIT_RE.match(path)
        if video_match:
            from . import video_shim  # 延迟导入：不开视频时零成本

            payload = self._read_json()
            try:
                snapshot = video_shim.submit(payload)
            except Exception as exc:  # noqa: BLE001 - 上游拒绝要如实回传
                _log(f"视频提交被拒绝：{type(exc).__name__}: {exc}")
                # 提交失败**绝不能**返回 taskId —— OCV 把"拿到 taskId"当作
                # 付费已发生的证据，之后只查询、不再提交；给一个查不到的
                # 假身份会让这镜头永久卡死。
                #
                # ⚠ 响应字段名必须被 OCV 读懂（F-013，实测踩过）：
                #   ``module6_dynamic_video`` 读的是
                #       body["errorMessage"] or body["message"] or "未返回任务身份"
                #   首版只给了 ``msg`` ⇒ 两者都不命中 ⇒ 用户看到的是
                #   **兜底文案「未返回任务身份」**，而上游的真实原因
                #   （如 "503 video queue is full, please retry later"）
                #   被彻底丢掉，排查时只能去翻 shim 日志。
                #   这里三个字段都给：errorMessage / message 喂 OCV，msg 兼容面板与旧脚本。
                #
                #   HTTP 状态也必须如实转达上游的"暂时性"语义：上游 503/429
                #   表示"未受理、可重试"，若统一压成 400，OCV 会判成
                #   "明确拒绝"（终态）而一次都不重试。
                status, upstream = _video_error_status(exc)
                self._send_json({
                    "errorMessage": str(exc)[:800],
                    "message": str(exc)[:800],
                    "msg": str(exc)[:800],
                    "code": upstream or 400,
                    "errorCode": upstream or 400,
                    "retryable": bool(getattr(exc, "retryable", False)),
                }, status=status)
                return
            self._send_json({
                "code": 0,
                # ⚠ 视频 provider 只读**顶层** taskId，图片层才读 data.taskId。
                # 两边都给，见 video_shim.query_response 的文档。
                "taskId": snapshot["taskId"],
                "status": "QUEUED",
                "data": {"taskId": snapshot["taskId"], "status": "QUEUED"},
            })
            return

        if path == "/api/video/status":
            from . import video_shim  # noqa: PLC0415

            self._send_json({"code": 0, "data": video_shim.status_snapshot()})
            return

        if path == "/api/video/query":
            from . import video_shim  # noqa: PLC0415

            payload = self._read_json()
            self._send_json(video_shim.query_response(str(payload.get("taskId") or "")))
            return

        # ---- 配置面板 ----
        if path == "/api/panel/save":
            self._panel_call(panel.apply, self._read_json())
            return
        if path == "/api/panel/reset":
            self._read_json()
            self._panel_call(panel.reset)
            return
        if path == "/api/panel/models":
            self._panel_call(panel.list_models, self._read_json())
            return
        if path == "/api/panel/test/tts":
            self._panel_call(panel.test_tts, self._read_json())
            return
        if path == "/api/panel/voice/upload":
            # 克隆参考音频经 base64 内联在 JSON 里，可能有好几 MB
            self._panel_call(panel.upload_clone_voice, self._read_json())
            return
        if path == "/api/panel/voice/delete":
            self._panel_call(panel.delete_clone_voice, self._read_json())
            return
        if path == "/api/panel/test/llm":
            self._read_json()
            self._panel_call(panel.test_llm)
            return
        if path == "/api/panel/test/image":
            self._panel_call(panel.test_image, self._read_json())
            return

        # ---- 云端 OAuth（账号 / 登录 / 模型开关 / 积分签到 / 备份恢复） ----
        # 每个 handler 都返回含 `ok` 的 dict，因此会被 _panel_call 原样下发。
        if path == "/api/panel/jethub/status":
            self._panel_call(panel.jethub_status, self._read_json())
            return
        if path == "/api/panel/jethub/ensure":
            self._panel_call(panel.jethub_ensure, self._read_json())
            return
        if path == "/api/panel/jethub/stop":
            self._read_json()
            self._panel_call(panel.jethub_stop)
            return
        if path == "/api/panel/jethub/login/start":
            self._panel_call(panel.jethub_login_start, self._read_json())
            return
        if path == "/api/panel/jethub/login/poll":
            self._panel_call(panel.jethub_login_poll, self._read_json())
            return
        if path == "/api/panel/jethub/account":
            self._panel_call(panel.jethub_account_action, self._read_json())
            return
        if path == "/api/panel/jethub/model/toggle":
            self._panel_call(panel.jethub_model_toggle, self._read_json())
            return
        # 批量开关（「打开全部 / 关闭全部」）—— 前端调的是这个路径。
        # 首版漏了它，导致两个按钮必然 404（见 panel.jethub_models_all 的说明）。
        if path == "/api/panel/jethub/models/all":
            self._panel_call(panel.jethub_models_all, self._read_json())
            return
        if path == "/api/panel/jethub/test":
            self._panel_call(panel.jethub_test, self._read_json())
            return
        if path == "/api/panel/jethub/credits":
            self._panel_call(panel.jethub_credits, self._read_json())
            return
        if path == "/api/panel/jethub/backup":
            self._panel_call(panel.jethub_backup_action, self._read_json())
            return

        _log(f"未实现的端点被调用: POST {path}")
        self._send_json({"code": 404, "msg": f"未实现的端点: {path}"}, status=404)


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------

def _build_server(bind_host: str, bind_port: int) -> ThreadingHTTPServer:
    """创建并绑定监听套接字（不开始服务）。"""
    httpd = ThreadingHTTPServer((bind_host, bind_port), _Handler)
    httpd.daemon_threads = True
    return httpd


def _probe(bind_host: str, bind_port: int) -> bool:
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.4)
            return sock.connect_ex((bind_host, bind_port)) == 0
    except OSError:
        return False


def serve_in_thread(port: int | None = None, host: str | None = None) -> ThreadingHTTPServer | None:
    """在**当前进程**里以守护线程托管 shim。

    这是默认路径：由 OCV 的后端进程托管，shim 的生命周期就与 OCV 窗口一致，
    既不会变成孤儿，也不受 Windows job object / 分离进程权限的影响。
    单独跑一个分离进程反而很脆 —— 沙箱或某些启动方式下
    ``CREATE_BREAKAWAY_FROM_JOB`` 会被拒绝（WinError 5），shim 就被父进程
    连带杀掉了。

    端口已被占用时返回 ``None``（说明已经有实例，不需要再起一个）。
    """
    global _MODE
    bind_host = host or config.shim_host()
    bind_port = int(port or config.shim_port())

    if _probe(bind_host, bind_port):
        return None

    try:
        httpd = _build_server(bind_host, bind_port)
    except OSError as exc:
        _log(f"端口 {bind_host}:{bind_port} 绑定失败（可能已有实例在运行）: {exc}")
        return None

    _MODE = "inproc"
    thread = threading.Thread(
        target=httpd.serve_forever,
        name="ocv-cloud-shim",
        daemon=True,
    )
    thread.start()
    _log(
        f"shim 已由当前进程托管（pid {_this_pid()}）: http://{bind_host}:{bind_port} "
        f"（商汤模型 {config.sensenova_image_model()}）"
    )
    return httpd


def serve_forever(port: int | None = None, host: str | None = None) -> int:
    """前台运行 shim（独立进程模式，供 cloud_stack_ctl.py 使用）。"""
    bind_host = host or config.shim_host()
    bind_port = int(port or config.shim_port())

    if _probe(bind_host, bind_port):
        _log(f"端口 {bind_host}:{bind_port} 已有实例在监听，本次启动放弃")
        return 0

    try:
        httpd = _build_server(bind_host, bind_port)
    except OSError as exc:
        _log(f"端口 {bind_host}:{bind_port} 绑定失败（可能已有实例在运行）: {exc}")
        return 2

    pid_path = config.plugin_var_dir() / "shim.pid"
    try:
        pid_path.write_text(str(_this_pid()), encoding="utf-8")
    except OSError:
        pass
    _log(f"shim 已启动: http://{bind_host}:{bind_port} （商汤模型 {config.sensenova_image_model()}）")

    def _shutdown(_signum: int, _frame: Any) -> None:
        _log("收到退出信号，正在关闭 shim")
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    for name in ("SIGINT", "SIGTERM"):
        handler = getattr(signal, name, None)
        if handler is not None:
            try:
                signal.signal(handler, _shutdown)
            except (ValueError, OSError):
                pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        try:
            pid_path.unlink(missing_ok=True)
        except OSError:
            pass
        _log("shim 已停止")
    return 0


def _this_pid() -> int:
    import os

    return os.getpid()


def main() -> int:
    port = None
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            port = None
    return serve_forever(port=port)


if __name__ == "__main__":
    raise SystemExit(main())
