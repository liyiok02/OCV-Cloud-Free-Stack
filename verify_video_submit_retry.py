"""验证 F-013：上游 503 队列满时的**阶梯退避**重试（30 次）。

用户报错：
    从第三个动态视频开始提示视频提交被明确拒绝（HTTP 400）：未返回任务身份

## 日志给出的决定性证据

    shim.log: 95 次"接受视频任务" / 3 次"视频提交被拒绝"
    3 次拒绝原因**全部相同**：
        视频提交被拒绝：AgnesVideoError: Agnes 视频提交失败：
            HTTP 503 video queue is full, please retry later

## 两个叠加缺陷

**① 响应字段名不匹配 ⇒ 真实原因被丢弃**
OCV（``module6_dynamic_video`` 第 260 行）读：
    body["errorMessage"] or body["message"] or "未返回任务身份"
而 shim 首版只给 ``{"code":400, "msg":...}`` ⇒ 两者都不命中
⇒ 用户看到的永远是兜底文案「未返回任务身份」，
上游真实原因（"503 队列满，请稍后重试"）被彻底丢掉，只能翻日志。

**② 上游明确说"稍后重试"，OCV 却一次都不重试**
上游 503 写着 ``please retry later``，而 OCV 把提交失败当**终态**。
shim 又把它硬编码成自己的 400 ⇒ 判成"明确拒绝" ⇒ 用户连续几个镜头失败。

## 本脚本验证的修复

  A. 真实原因必须传到 OCV（字段名 + HTTP 语义）
  B. 503/429（确定未受理）⇒ **延后重试**，默认 30 次、**阶梯退避**
  C. "状态不明"（网络中断 / 200 无身份）⇒ **绝不重试**，冻结镜头
  D. 重试期间对 OCV 呈现"排队中"，**不是**失败（否则会被引导重新付费）
  E. 30 次耗尽后必须给出可读原因
  F. 重试成功即接上正常轮询，且**不再重复提交**
  G. 退避档位逐项与用户指定一致，且总窗口不超过 OCV 的轮询预算
"""
from __future__ import annotations

import json
import sys
import time
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


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body, ensure_ascii=False)

    def json(self):
        return self._body


class FakeSession:
    """按脚本回放响应；记录调用次数。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.payloads = []

    def post(self, url, **kwargs):
        self.calls += 1
        self.payloads.append(kwargs.get("json"))
        item = self.script.pop(0) if self.script else self.script_default()
        return item

    def script_default(self):
        return FakeResponse(200, {"video_id": "task_default"})

    def get(self, url, **kwargs):
        return FakeResponse(200, {"status": "completed", "url": "https://x/v.mp4"})


# 测试前提（封闭：显式设定面板层，finally 还原）
#
# ⚠ 这里**不要**写 CLOUD_STACK_VIDEO_SUBMIT_RETRIES —— 本脚本要断言的正是
#   **内置默认值**（30 次 / 788s 窗口）。往面板层写一个值会盖住默认值，
#   让"默认是多少"的断言变成在断言测试自己设的数（实测踩到过一次）。
saved = config.store_values()
config.update_store({
    "AGNES_API_KEY": "sk-test-f013",
    "AGNES_VIDEO_MODEL": "agnes-video-2.5-flash",
    "CLOUD_STACK_VIDEO_ENABLED": "1",
}, force=True)

QUEUE_FULL = FakeResponse(503, {"message": "video queue is full, please retry later"})

print("== A) 错误分类：503/429 可重试，状态不明绝不重试 ==")
for status in (408, 425, 429, 500, 502, 503, 504):
    session = FakeSession([FakeResponse(status, {"message": "x"})])
    try:
        agnes_video.create_task({"prompt": "p"}, api_key="k", session=session)
        exc = None
    except agnes_video.AgnesVideoError as e:
        exc = e
    check(f"HTTP {status} 标记为可重试且确定未创建",
          exc is not None and exc.retryable and exc.definite,
          f"retryable={getattr(exc, 'retryable', None)} definite={getattr(exc, 'definite', None)}")

for status in (400, 401, 403, 404, 422):
    session = FakeSession([FakeResponse(status, {"message": "bad"})])
    try:
        agnes_video.create_task({"prompt": "p"}, api_key="k", session=session)
        exc = None
    except agnes_video.AgnesVideoError as e:
        exc = e
    check(f"HTTP {status} 不可重试但确定未创建",
          exc is not None and not exc.retryable and exc.definite,
          f"retryable={getattr(exc, 'retryable', None)} definite={getattr(exc, 'definite', None)}")

# 200 但没有身份 -> 状态不明，绝不能重试
session = FakeSession([FakeResponse(200, {"status": "queued"})])
try:
    agnes_video.create_task({"prompt": "p"}, api_key="k", session=session)
    exc = None
except agnes_video.AgnesVideoError as e:
    exc = e
check("HTTP 200 但无 video_id ⇒ 状态不明（不可重试、不确定）",
      exc is not None and not exc.retryable and not exc.definite,
      f"retryable={getattr(exc, 'retryable', None)} definite={getattr(exc, 'definite', None)}")

import requests as _requests  # noqa: E402


class NetFailSession(FakeSession):
    def post(self, url, **kwargs):
        self.calls += 1
        raise _requests.ConnectionError("connection reset")


session = NetFailSession([])
try:
    agnes_video.create_task({"prompt": "p"}, api_key="k", session=session)
    exc = None
except agnes_video.AgnesVideoError as e:
    exc = e
check("网络异常 ⇒ 状态不明（请求可能已到达，绝不自动重发）",
      exc is not None and not exc.retryable and not exc.definite,
      f"retryable={getattr(exc, 'retryable', None)} definite={getattr(exc, 'definite', None)}")

print("\n== B) 503 ⇒ 延后重试（不是当场失败）==")
original_create = agnes_video.create_task
_orig_delay_b = config.video_submit_retry_delay
_orig_window_b = config.video_submit_retry_window

calls = {"n": 0, "payloads": []}


def scripted_create(payload, *, api_key, session=None, timeout=None):
    """前 3 次 503，第 4 次成功。"""
    calls["n"] += 1
    calls["payloads"].append(payload)
    if calls["n"] <= 3:
        raise agnes_video.AgnesVideoError(
            "Agnes 视频提交失败：HTTP 503 video queue is full, please retry later",
            status_code=503, retryable=True, definite=True)
    return {"video_id": "task_ok", "task_id": "task_ok", "status": "queued", "raw": {}}


agnes_video.create_task = scripted_create
# 测速：档位延迟压到 50ms，只验证状态机不等真实时间。
config.video_submit_retry_delay = lambda ordinal: 0.05
config.video_submit_retry_window = lambda retries=None: 600.0
video_shim._TASKS.clear()
video_shim._CLIENT_JOBS.clear()
try:
    snap = video_shim.submit({"prompt": "p", "duration": "5", "imageUrls": [],
                              "clientJobId": "job-f013"})
    check("503 后仍返回任务身份（不抛错）", bool(snap.get("taskId")), str(snap.get("taskId"))[:12])
    check("标记为 waitingUpstream", snap.get("awaitingUpstream") is True,
          str(snap.get("awaitingUpstream")))
    check("首次尝试已计数", snap.get("submitAttempts") == 1, str(snap.get("submitAttempts")))

    # 对 OCV 呈现"排队中"，绝不是 FAILED
    q = video_shim.query_response(snap["taskId"])
    check("重试期间对 OCV 呈现 QUEUED（不是 FAILED）", q.get("status") == "QUEUED",
          str(q.get("status")))
    check("响应仍是顶层 taskId（视频层读法）", "taskId" in q)

    # 驱动后台重试直到成功
    task = video_shim._TASKS[snap["taskId"]]
    for _ in range(40):
        if not task.awaiting_upstream:
            break
        task.next_submit_at = 0.0
        video_shim._retry_deferred_submit(task)
    check("重试后最终拿到上游身份", task.video_id == "task_ok", task.video_id)
    check("重试期间未改变 prompt（同一次提交）",
          all(p.get("prompt") == "p" for p in calls["payloads"]),
          f"{len(calls['payloads'])} 次")
    check("共尝试 4 次（3 次被拒 + 1 次成功）", calls["n"] == 4, str(calls["n"]))
    check("成功后不再标记 waitingUpstream", task.awaiting_upstream is False)
finally:
    agnes_video.create_task = original_create
    config.video_submit_retry_delay = _orig_delay_b
    config.video_submit_retry_window = _orig_window_b

print("\n== C) 重试 30 次耗尽后给出可读原因（默认上限）==")
check("默认重试次数为 30", config.video_submit_retries() == 30,
      str(config.video_submit_retries()))

print("\n== C2) 阶梯退避序列必须与用户指定逐项一致 ==")
# 用户 2026-09-24 指定：1-3@1s、4-8@5s、9-12@10s、13-15@15s、
# 16-20@30s、21-25@45s、26-30@60s。
# 原文里 8 与 12 各出现在两个档位中，这里按「先出现的档位优先」取。
EXPECTED = (
    [1.0] * 3 + [5.0] * 5 + [10.0] * 4 + [15.0] * 3
    + [30.0] * 5 + [45.0] * 5 + [60.0] * 5
)
check("档位表共 30 项", len(EXPECTED) == 30, str(len(EXPECTED)))
actual = [config.video_submit_retry_delay(i) for i in range(1, 31)]
mismatch = [(i + 1, a, e) for i, (a, e) in enumerate(zip(actual, EXPECTED)) if a != e]
check("逐项与用户指定的档位一致", not mismatch,
      f"不一致 {mismatch[:4]}" if mismatch else "30 项全对")
check("第 1-3 次为 1 秒", actual[0:3] == [1.0] * 3, str(actual[0:3]))
check("第 4-8 次为 5 秒", actual[3:8] == [5.0] * 5, str(actual[3:8]))
check("第 9-12 次为 10 秒", actual[8:12] == [10.0] * 4, str(actual[8:12]))
check("第 13-15 次为 15 秒", actual[12:15] == [15.0] * 3, str(actual[12:15]))
check("第 16-20 次为 30 秒", actual[15:20] == [30.0] * 5, str(actual[15:20]))
check("第 21-25 次为 45 秒", actual[20:25] == [45.0] * 5, str(actual[20:25]))
check("第 26-30 次为 60 秒", actual[25:30] == [60.0] * 5, str(actual[25:30]))
check("序列单调不减（阶梯式退避的本质）",
      all(b >= a for a, b in zip(actual, actual[1:])), "单调不减")
check("超出一档后沿用最后一档（不会退化成 0 等待）",
      config.video_submit_retry_delay(99) == 60.0,
      str(config.video_submit_retry_delay(99)))
check("总窗口 = 788 秒（约 13.1 分钟）",
      abs(config.video_submit_retry_window() - 788.0) < 0.5,
      f"{config.video_submit_retry_window():.0f}s")

always_fail = {"n": 0}


def always_503(payload, *, api_key, session=None, timeout=None):
    always_fail["n"] += 1
    raise agnes_video.AgnesVideoError(
        "Agnes 视频提交失败：HTTP 503 video queue is full, please retry later",
        status_code=503, retryable=True, definite=True)


agnes_video.create_task = always_503
_orig_delay_c = config.video_submit_retry_delay
_orig_window_c = config.video_submit_retry_window
config.video_submit_retry_delay = lambda ordinal: 0.001
config.video_submit_retry_window = lambda retries=None: 600.0
video_shim._TASKS.clear()
video_shim._CLIENT_JOBS.clear()
try:
    snap = video_shim.submit({"prompt": "p", "duration": "5", "imageUrls": []})
    task = video_shim._TASKS[snap["taskId"]]
    for _ in range(60):
        if task.status == "FAILED":
            break
        task.next_submit_at = 0.0
        video_shim._retry_deferred_submit(task)
    check("耗尽 30 次后判 FAILED", task.status == "FAILED", task.status)
    check("总尝试次数正好 31（首次 + 30 次重试）", always_fail["n"] == 31,
          str(always_fail["n"]))
    check("失败原因可读且含关键信息",
          "503" in task.error or "退避" in task.error or "重试" in task.error,
          task.error[:110])
    q = video_shim.query_response(snap["taskId"])
    check("最终态对 OCV 报 FAILED", q.get("status") == "FAILED", str(q.get("status")))
    check("FAILED 时带上 errorMessage（OCV 会显示它）",
          bool(q.get("errorMessage")), str(q.get("errorMessage"))[:90])
finally:
    agnes_video.create_task = original_create
    config.video_submit_retry_delay = _orig_delay_c
    config.video_submit_retry_window = _orig_window_c

print("\n== D) shim 交给 OCV 的错误响应必须能被读懂（F-013 核心）==")
from ocv_cloud_stack import image_shim  # noqa: E402

# 复刻 OCV 的解析（module6_dynamic_video 第 253-268 行）
def ocv_reads(body: dict) -> str:
    return str(body.get("errorMessage") or body.get("message") or "未返回任务身份")


definite_exc = agnes_video.AgnesVideoError("上游参数错误：HTTP 400 bad", status_code=400,
                                           retryable=False, definite=True)
status, code = image_shim._video_error_status(definite_exc)
check("确定未创建 ⇒ HTTP 400（OCV 给一键重试）", status == 400 and code == 400,
      f"{status}/{code}")

unknown_exc = agnes_video.AgnesVideoError("网络中断，状态不明", retryable=False, definite=False)
status2, code2 = image_shim._video_error_status(unknown_exc)
check("状态不明 ⇒ HTTP 503（OCV 冻结镜头，防重复扣费）", status2 == 503 and code2 == 503,
      f"{status2}/{code2}")

# 关键：真实原因必须能被 OCV 读到，而不是兜底文案
real = "Agnes 视频提交失败：HTTP 503 video queue is full, please retry later"
resp = {"errorMessage": real, "message": real, "msg": real, "code": 503}
check("OCV 能读到真实原因（不再是「未返回任务身份」）",
      ocv_reads(resp) == real, ocv_reads(resp)[:80])

print("\n== E) 上游 503 不再被压成 400 ==")
src = (PLUGIN_DIR / "ocv_cloud_stack" / "image_shim.py").read_text(encoding="utf-8")
check("提交失败分支使用 _video_error_status 分流",
      "_video_error_status(exc)" in src)
# 精确定位**视频提交**那一段，而不是全文搜 status=400
# （面板处理器 `_panel_call` 里有一个合法的 status=400，见 L380）
video_branch = src.split("video_match = _VIDEO_SUBMIT_RE.match(path)")[1].split(
    'if path == "/api/video/status"')[0]
check("视频提交失败不再硬编码 status=400",
      "status=400" not in video_branch,
      "视频分支无硬编码 400" if "status=400" not in video_branch else "仍有 status=400")
check("视频提交失败按分流结果设置 HTTP 状态", "status=status" in video_branch)
check("响应同时给出 errorMessage 与 message",
      '"errorMessage"' in video_branch and '"message"' in video_branch)
check("响应带 retryable 供排查", '"retryable"' in video_branch)

print("\n== F) 延后重试的关键约束：不能超过 OCV 的提交超时 ==")
ocv_src = (PROJECT_ROOT / "module6_dynamic_video.py").read_text(encoding="utf-8")
check("OCV 提交 POST 确实是 timeout=120",
      "self.url(self.submit_path), headers=self.headers, json=payload, timeout=120" in ocv_src)
shim_src = (PLUGIN_DIR / "ocv_cloud_stack" / "video_shim.py").read_text(encoding="utf-8")
check("因此重试放在后台（submit 不在循环里 sleep）",
      "awaiting_upstream = True" in shim_src and "_retry_deferred_submit" in shim_src)
check("submit() 内不出现 time.sleep（否则会被 OCV 120s 超时掐断）",
      "time.sleep" not in shim_src.split("def submit(")[1].split("def get(")[0],
      "submit 内无 sleep")

print("\n== G) 重试窗口绝不能超过 OCV 的轮询窗口（防'没人跟踪的付费任务'）==")
# OCV 的轮询预算（run 的 timeout_seconds 默认值）
m = __import__("re").search(r"timeout_seconds: float = (\d+)", ocv_src)
ocv_budget = float(m.group(1)) if m else None
check("OCV 轮询预算是 1800 秒", ocv_budget == 1800.0, str(ocv_budget))
check("shim 的预算常量与 OCV 源码一致",
      video_shim.OCV_POLL_BUDGET_SECONDS == ocv_budget,
      f"shim={video_shim.OCV_POLL_BUDGET_SECONDS} ocv={ocv_budget}")

# 默认 30 次阶梯退避：合计 788 秒，必须落在 OCV 的轮询预算内
now = time.time()
deadline = video_shim._retry_deadline(now)
span = deadline - now
check("默认配置的重试窗口 = 788s（30 次阶梯退避）",
      abs(span - 788.0) < 1.0, f"{span:.0f}s")
check("默认窗口在 OCV 轮询预算之内", span < ocv_budget, f"{span:.0f}s")

# 极端配置必须被夹住
saved_retries = config.video_submit_retries
saved_window = config.video_submit_retry_window
config.video_submit_retries = lambda: 60
# 60 次会一路沿用最后一档 60s ⇒ 60×60 = 3600s，远超预算
config.video_submit_retry_window = lambda retries=None: 3600.0
try:
    now2 = time.time()
    span2 = video_shim._retry_deadline(now2) - now2
    check("60 次 × 60s（3600s）被夹到 OCV 预算的 80%（1440s）",
          abs(span2 - 1440.0) < 1.0, f"{span2:.0f}s")
    check("极端配置下仍不越过 OCV 轮询窗口", span2 < ocv_budget, f"{span2:.0f}s")
finally:
    config.video_submit_retries = saved_retries
    config.video_submit_retry_window = saved_window

# 越窗后必须停止重试（绝不再创建任务）
video_shim._TASKS.clear()
video_shim._CLIENT_JOBS.clear()
guard_calls = {"n": 0}


def must_not_be_called(payload, *, api_key, session=None, timeout=None):
    guard_calls["n"] += 1
    return {"video_id": "SHOULD_NOT_HAPPEN", "task_id": "x", "status": "queued", "raw": {}}


agnes_video.create_task = must_not_be_called
try:
    snap = video_shim.submit({"prompt": "p", "duration": "5", "imageUrls": []}) \
        if False else None
    # 直接构造一个"已过截止"的任务
    t = video_shim._VideoTask("tk", "", "m", {"prompt": "p"})
    t.awaiting_upstream = True
    t.submit_attempts = 3
    t.retry_deadline = time.time() - 1
    t.last_submit_error = "HTTP 503 queue full"
    video_shim._retry_deferred_submit(t)
    check("越过窗口后判 FAILED", t.status == "FAILED", t.status)
    check("越过窗口后**绝不再调用上游**（防无人跟踪的付费任务）",
          guard_calls["n"] == 0, f"调用 {guard_calls['n']} 次")
    check("失败原因说明了为什么停止", "停止重试" in t.error or "窗口" in t.error or "OCV" in t.error,
          t.error[:100])
finally:
    agnes_video.create_task = original_create

# ---- 还原测试前提 ----
config.update_store({k: v for k, v in saved.items()},
                    remove=[k for k in config.store_values() if k not in saved], force=True)
check("测试前提已完整还原", config.store_values() == saved,
      f"{len(config.store_values())} vs {len(saved)}")

print()
print(f"通过 {PASS} 项，失败 {FAIL} 项")
sys.exit(1 if FAIL else 0)
