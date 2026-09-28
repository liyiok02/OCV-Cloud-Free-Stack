"""视频 shim —— 把 OCV 的 RunningHub 异步协议翻译成 Agnes 的异步视频协议。

## 为什么需要这一层（与图片 shim 同一个理由）

OCV 的视频层把整套「付费任务身份」的持久化建立在**一种**协议上：
``POST <submit_path>`` 拿 ``taskId`` → ``POST <query_path>`` 带 ``{taskId}`` 轮询
→ 从响应里递归找 http(s) URL。它的断点续跑、防重复扣费、状态机全部围着
这个形状转（见 ``module6_dynamic_video.RunningHubVideoProvider``）。

Agnes 的视频协议在七个环节上都不同（创建路径、请求体、任务标识、查询方式、
状态取值、结果地址、时长/分辨率格式）。**改 OCV 违背零改动原则、且会被软件
更新覆盖**，所以这里对 OCV 复刻 RunningHub 协议、对上游讲 Agnes 协议。

这与图片层（``image_shim`` 把 RunningHub 的异步出图翻译成商汤的同步接口）
是同一套思路，区别只有一个：**Agnes 也是异步的**，所以这里必须自己维护
「OCV 的 taskId ↔ Agnes 的 video_id」映射，并用后台线程按 OCV 的节奏轮询。

## 任务生命周期

```
OCV  POST /openapi/v2/video/<model>/multimodal-video  {prompt, duration, imageUrls…}
  │
  ├─ 翻译成 Agnes: POST {base}/videos  {model, prompt, seconds:"5", mode:"keyframe", …}
  │     拿到 video_id
  ├─ 生成一个本地 taskId（uuid），建立 taskId -> video_id 映射
  └─ 立刻返回 {code:0, data:{taskId}}        ← OCV 认为"已提交"

OCV  POST /openapi/v2/query  {taskId}
  └─ 从映射取 video_id，GET {base}/agnesapi?video_id=…&model_name=…
       把 completed/failed 映射成 SUCCESS/FAILED，把顶层 url 包成 results[].url
```

**为什么提交后是"立刻返回 taskId"而不是等上游返回**：OCV 的提交调用有
120 秒超时，且它把"拿到 taskId"当作**付费已发生**的证据（之后只查询、
绝不重复提交）。Agnes 的创建调用是同步返回 video_id 的（不需要等待生成），
所以这里可以诚实地等到 video_id 到手再返回 taskId —— **不要**先返回一个
假 taskId 再后台提交，那会在提交失败时留下一个永远查不到的任务身份。
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from . import agnes_video, config


class _VideoTask:
    """一个视频任务在 shim 内的身份与状态。"""

    __slots__ = ("task_id", "video_id", "model", "payload", "status", "url",
                 "error", "created_at", "finished_at", "notes",
                 "awaiting_upstream", "submit_attempts", "next_submit_at",
                 "last_submit_error", "retry_deadline", "submitting")

    def __init__(self, task_id: str, video_id: str, model: str, payload: dict[str, Any]) -> None:
        self.task_id = task_id
        self.video_id = video_id
        self.model = model
        self.payload = payload
        # 初始状态用 OCV 认识的大写集合（QUEUED 属于它的 RUNNING 集）。
        self.status = "QUEUED"
        self.url = ""
        self.error = ""
        self.created_at = time.time()
        self.finished_at: float | None = None
        self.notes: list[str] = []

        # ---- 延后提交（F-013）----
        # 上游暂时性拒绝（503 队列满 / 429）时，**立刻**给 OCV 一个任务身份，
        # 把"每 30 秒重试、最多 15 次"交给后台轮询线程做。
        #
        # 为什么不能在 ``submit()`` 里同步重试：OCV 的提交 POST
        # 带 ``timeout=120``（``module6_dynamic_video`` 第 246 行），
        # 而 15 × 30s = 7.5 分钟远超它。同步重试会被 OCV 先超时掐断，
        # 反而变成"结果无法确认"。返回身份后 OCV 会持续查询 30 分钟，
        # 足够后台把重试做完。
        self.awaiting_upstream = False
        self.submit_attempts = 0
        self.next_submit_at = 0.0
        self.last_submit_error = ""
        # 重试窗口的**硬截止时间**。见 ``_retry_deadline``：绝不能晚于
        # OCV 自己的轮询窗口（1800s），否则会出现"OCV 已放弃、后台却
        # 又创建了一个没人跟踪的付费任务"—— 那是最坏的静默坏结果。
        self.retry_deadline = 0.0
        # 「正在向上游提交」的占用标记。见 :func:`_retry_deferred_submit`：
        # 后台轮询线程与任何手工/测试调用都可能同时进来，没有它就可能
        # **重复提交同一镜头**（对按秒计费的能力来说是真花钱的事）。
        self.submitting = False

    def snapshot(self) -> dict[str, Any]:
        return {
            "taskId": self.task_id,
            "videoId": self.video_id,
            "model": self.model,
            "status": self.status,
            "url": self.url,
            "error": self.error,
            "mode": self.payload.get("mode"),
            "size": self.payload.get("size"),
            "seconds": self.payload.get("seconds"),
            "createdAt": self.created_at,
            # 便于面板/日志看清"还在等上游收下"这一中间态。
            "awaitingUpstream": self.awaiting_upstream,
            "submitAttempts": self.submit_attempts,
        }


_LOCK = threading.Lock()
_TASKS: dict[str, _VideoTask] = {}
# OCV 会用 clientJobId 做幂等（同一次点击重复提交时只应产生一个付费任务）。
_CLIENT_JOBS: dict[str, str] = {}
_POLLER: threading.Thread | None = None
_POLLER_STOP = threading.Event()

_TASK_TTL_SECONDS = 6 * 3600.0

# OCV 提交后自己的轮询预算（``module6_dynamic_video.run`` 的
# ``timeout_seconds`` 默认值，单位秒）。后台重试窗口绝不能超过它 ——
# 否则会出现"OCV 已放弃、后台却又创建了没人跟踪的付费任务"。
# 自检有断言守着它与 OCV 源码一致。
OCV_POLL_BUDGET_SECONDS = 1800.0


def _log(message: str) -> None:
    """复用图片 shim 的日志（同一个 var/shim.log），失败不抛。"""
    try:
        from . import image_shim

        image_shim._log(message)  # noqa: SLF001 - 同一份日志，刻意共用
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# 任务登记
# --------------------------------------------------------------------------

def submit(payload: dict[str, Any]) -> dict[str, Any]:
    """把 OCV 的提交体翻译成 Agnes 任务，返回 ``{taskId, ...}``。

    抛出 :class:`agnes_video.AgnesVideoError` 时，调用方应把它映射成 4xx/5xx ——
    关键是**不要**返回 taskId，否则 OCV 会认为付费已发生而永远只查询。

    ## 上游暂时性拒绝（503 队列满 / 429）⇒ **延后提交**，不是当场失败

    实测（2026-09-24 日志）：95 次受理 / 3 次被拒，**3 次全是同一个**
    ``503 video queue is full, please retry later``。上游自己写着
    "please retry later"，而 OCV 把提交失败当**终态**、一次都不重试
    ⇒ 用户连续几个镜头都失败。

    所以遇到这类**确定未受理**的拒绝时，本函数**立刻**给 OCV 一个任务身份，
    把"每 30 秒重试、最多 15 次"交给后台轮询线程
    （见 :func:`_retry_deferred_submit`）。

    **为什么不能在 ``submit()`` 里同步重试**：OCV 的提交 POST 带
    ``timeout=120``（``module6_dynamic_video`` 第 246 行），而
    15 × 30s = 7.5 分钟远超它 —— 同步重试会被 OCV 先超时掐断，
    反而退化成"结果无法确认"。返回身份后 OCV 会持续查询 30 分钟，
    足够后台把重试做完。

    **为什么重试不会重复扣费**：只有 ``AgnesVideoError.retryable``
    （上游明确未受理，``definite=True``）才延后重试；"状态不明"
    （网络中断 / 200 却没给视频身份）一律**当场报错**，交给 OCV 冻结镜头。
    """
    if not config.video_enabled():
        raise agnes_video.AgnesVideoError("插件未启用云端视频接管", definite=True)

    api_key = config.video_api_key()
    if not api_key:
        raise agnes_video.AgnesVideoError("未配置 AGNES_API_KEY，无法提交视频任务", definite=True)

    # OCV 的字段名 -> 本模块的语义名
    prompt = str(payload.get("prompt") or "")
    resolution = str(payload.get("resolution") or "720p")
    ratio = str(payload.get("ratio") or "16:9")
    try:
        duration = int(payload.get("duration") or 5)
    except (TypeError, ValueError):
        duration = 5
    try:
        seed = int(payload.get("seed"))
    except (TypeError, ValueError):
        seed = -1

    raw_images = payload.get("imageUrls")
    images = [str(x) for x in raw_images] if isinstance(raw_images, list) else []

    model = config.video_model()
    built, notes = agnes_video.build_payload(
        prompt=prompt, ratio=ratio, resolution=resolution, duration=duration,
        seed=seed, reference_images=images, model=model,
    )

    client_job_id = str(payload.get("clientJobId") or "").strip()
    if client_job_id:
        with _LOCK:
            existing = _CLIENT_JOBS.get(client_job_id)
            if existing and existing in _TASKS:
                _log(f"命中幂等键 {client_job_id} -> 复用视频任务 {existing}")
                return _TASKS[existing].snapshot()

    task_id = uuid.uuid4().hex
    try:
        created = agnes_video.create_task(built, api_key=api_key)
    except agnes_video.AgnesVideoError as exc:
        if not exc.retryable:
            raise  # 参数/凭据错误，或"状态不明" ⇒ 当场交出去
        # 上游暂时性拒绝且**确定未受理** ⇒ 建一个"待提交"任务，后台重试。
        # 注意 create_task 返回 timeout=120 内，所以这一次尝试是快的。
        task = _VideoTask(task_id, "", model, built)
        task.notes = list(notes)
        task.awaiting_upstream = True
        task.submit_attempts = 1
        # 首次提交（第 1 次尝试）刚失败 ⇒ 下一次是「第 1 次重试」。
        interval = config.video_submit_retry_delay(1)
        task.next_submit_at = time.time() + interval
        task.retry_deadline = _retry_deadline(time.time())
        task.error = ""
        task.last_submit_error = str(exc)
        with _LOCK:
            _TASKS[task_id] = task
            if client_job_id:
                _CLIENT_JOBS[client_job_id] = task_id
        _log(
            f"视频提交遇上游暂时性拒绝，已转入后台重试：{exc}"
            f"（任务 {task_id}；最多重试 {config.video_submit_retries()} 次，"
            f"阶梯退避：{config.video_submit_retry_schedule()}）"
        )
        _ensure_poller()
        return task.snapshot()

    task = _VideoTask(task_id, created["video_id"], model, built)
    task.notes = list(notes)
    task.submit_attempts = 1
    # 上游可能已经直接返回终态（极短任务）；以它的 status 为准初始化。
    mapped = agnes_video._STATUS_MAP.get(str(created.get("status") or "").lower())
    if mapped:
        task.status = mapped

    with _LOCK:
        _TASKS[task_id] = task
        if client_job_id:
            _CLIENT_JOBS[client_job_id] = task_id
    for note in notes:
        _log(f"视频任务 {task_id}：{note}")
    _log(
        f"接受视频任务 {task_id} -> Agnes {created['video_id']} "
        f"模型={model} 模式={built.get('mode')} 时长={built.get('seconds')}s "
        f"档位={built.get('size')} 参考图={len(images)}"
    )
    _ensure_poller()
    return task.snapshot()


def get(task_id: str) -> dict[str, Any] | None:
    with _LOCK:
        task = _TASKS.get(str(task_id).strip())
        return task.snapshot() if task else None


# --------------------------------------------------------------------------
# 轮询
# --------------------------------------------------------------------------

def _refresh(task: _VideoTask) -> None:
    """查一次上游并更新任务状态。异常不抛出（留给下一次轮询重试）。"""
    try:
        result = agnes_video.fetch_task(task.video_id, api_key=config.video_api_key(),
                                        model=task.model)
    except Exception as exc:  # noqa: BLE001 - 单次查询失败不能让任务崩掉
        task.error = f"{type(exc).__name__}: {exc}"[:500]
        _log(f"视频任务 {task.task_id} 查询失败（将重试）：{task.error}")
        return

    task.status = result["status"]
    if result.get("url"):
        task.url = result["url"]
    if result.get("error"):
        task.error = str(result["error"])[:500]
    if task.status in {"SUCCESS", "FAILED", "CANCELLED"}:
        task.finished_at = time.time()
        _log(
            f"视频任务 {task.task_id} 结束：{task.status}"
            + (f" url={task.url}" if task.url else "")
            + (f" 错误={task.error}" if task.error else "")
        )


def _retry_deadline(now: float) -> float:
    """算出后台重试的**硬截止时间**，绝不越过 OCV 自己的轮询窗口。

    ## 为什么必须有这个上限（本模块最要紧的安全约束之一）

    OCV 提交后会轮询 ``timeout_seconds``（``module6_dynamic_video.run``
    默认 **1800 秒**）就放弃，并把镜头记为 UNKNOWN。如果后台重试窗口比它更长，
    就可能出现：

        OCV 已经放弃并停止查询  →  后台重试**成功了**（上游创建了付费任务）
        →  这个任务没有任何人跟踪、结果永远不会被下载

    那是**付了钱却拿不到片子**的静默坏结果。所以重试窗口一律夹在
    OCV 的轮询预算之内，并且留出余量（默认取 80%，把 20% 留给
    提交成功后的正常生成与轮询）。

    ## 默认值够不够

    默认 30 次**阶梯退避**（1-3 次每 1s、4-8 次每 5s、9-12 次每 10s、
    13-15 次每 15s、16-20 次每 30s、21-25 次每 45s、26-30 次每 60s）
    合计 **788 秒**（约 13 分钟），小于 1800×0.8 = 1440 秒
    ⇒ 默认配置不受这个上限约束。它只在用户把上限调得更大时兜底。
    """
    budget = OCV_POLL_BUDGET_SECONDS * 0.8
    span = config.video_submit_retry_window()
    return now + min(span, budget)


def _retry_deferred_submit(task: _VideoTask) -> None:
    """对"待提交"任务再试一次上游（F-013）。由轮询线程调用。

    ## 状态机

    ``awaiting_upstream=True`` 期间任务**没有** ``video_id``，对 OCV 呈现
    ``QUEUED``（属于它的 RUNNING 集）—— 也就是说 OCV 看到的是"已提交、排队中"，
    它会继续查询（最长 30 分钟），完全不知道我们还在重试提交。

    * 重试**成功** ⇒ 拿到 ``video_id``，清 ``awaiting_upstream``，转正常轮询；
    * 重试**又遇暂时性拒绝** ⇒ 计数 +1，``next_submit_at`` 按**阶梯退避**
      推后（见 :func:`config.video_submit_retry_delay`），继续等；
    * 重试遇**非暂时性**失败（4xx 参数错 / 状态不明）⇒ 判 ``FAILED``，
      把真实原因写进 ``error`` 给 OCV 看到；
    * 达到 ``CLOUD_STACK_VIDEO_SUBMIT_RETRIES`` 次仍不成功 ⇒ 判 ``FAILED``；
    * 越过 ``retry_deadline``（**OCV 的轮询窗口**）⇒ 立刻判 ``FAILED``，
      **绝不再提交**。理由见 :func:`_retry_deadline`：OCV 已放弃查询时
      再创建任务，就会产生一个没人跟踪的付费任务。

    **安全性**：只在"上游确定未受理"时才走到这里，所以重试不会重复扣费。
    一旦某次重试拿到 ``video_id``，后续就只查询、绝不再提交。

    ## ⚠ 同一任务串行化（``submitting`` 占用标记）

    本函数会被**后台轮询线程**调用，也可能被其它路径触发（面板、测试、探针）。
    若两次调用交叠，就可能**对同一个镜头重复提交** —— 对按秒计费的视频接口
    来说那是真花钱的事故。

    所以整个"检查 → 提交 → 记账"过程用 ``task.submitting`` 串起来：
    已在提交中就直接返回，绝不并发发起第二次。
    （实测发现：一个手工探针与轮询线程并发驱动时，日志出现「第 2/30」
    先于「第 1/30」打印 —— 正是这种交叠。）
    """
    # 占用标记：交叠调用直接让位，不做第二次提交。
    with _LOCK:
        if task.submitting:
            return
        task.submitting = True
    try:
        _retry_deferred_submit_locked(task)
    finally:
        with _LOCK:
            task.submitting = False


def _retry_deferred_submit_locked(task: _VideoTask) -> None:
    """:func:`_retry_deferred_submit` 的实际逻辑（已持有 ``submitting`` 占用）。"""
    task.submit_attempts += 1
    attempt = task.submit_attempts
    limit = config.video_submit_retries()
    # ⚠ 三个序号必须分清（差一位就差一整档，实测踩过两次）：
    #
    #   ``attempt``        = **累计尝试次数**，含 submit() 里那次首次提交
    #   ``retry_ordinal``  = **这是第几次重试** = attempt - 1
    #   ``limit``          = 用户配置的**重试次数上限**（不含首次提交）
    #
    # 本次调用**本身就是第 retry_ordinal 次重试**；若它失败，下一次将是
    # 第 retry_ordinal + 1 次。因此：
    #   * 还能不能再等一轮 → ``retry_ordinal < limit``
    #   * 该等多久         → ``delay(retry_ordinal + 1)``，
    #     即"第 N 次重试之前等 delay(N) 秒"，与
    #     ``config.video_submit_retry_window`` 的求和口径完全一致。
    retry_ordinal = attempt - 1
    next_retry_ordinal = retry_ordinal + 1
    interval = config.video_submit_retry_delay(next_retry_ordinal)

    # ⚠ 窗口检查必须放在**发起请求之前**（见 docstring 的最后一条）。
    if task.retry_deadline and time.time() >= task.retry_deadline:
        task.status = "FAILED"
        task.finished_at = time.time()
        task.awaiting_upstream = False
        task.error = (
            f"上游持续拒绝提交（已重试 {retry_ordinal - 1} 次）；"
            f"为避免在 OCV 停止查询后创建无人跟踪的付费任务，已提前停止重试。"
            f"最后一次上游回应：{task.last_submit_error or '（无）'}"
        )[:500]
        _log(f"视频任务 {task.task_id} 因超出重试窗口而停止：{task.error}")
        return

    try:
        created = agnes_video.create_task(task.payload, api_key=config.video_api_key())
    except agnes_video.AgnesVideoError as exc:
        # ``retry_ordinal < limit``：本次是第 retry_ordinal 次重试，
        # 若它失败，下一次会是第 retry_ordinal + 1 次 —— 只有那个序号仍在
        # 上限内才值得再等一轮。用 ``<=`` 会多试一次（实测差一）。
        if exc.retryable and retry_ordinal < limit:
            task.next_submit_at = time.time() + interval
            task.last_submit_error = str(exc)
            _log(
                f"视频任务 {task.task_id} 第 {retry_ordinal}/{limit} 次重试仍被上游暂时拒绝：{exc}"
                f"；{interval:.0f}s 后重试"
            )
            return
        # 不可重试，或已达上限 ⇒ 明确失败，把真实原因交给 OCV。
        task.status = "FAILED"
        task.finished_at = time.time()
        reason = str(exc) if not exc.retryable else (
            f"上游持续拒绝提交（已重试 {retry_ordinal} 次；退避节奏："
            f"{config.video_submit_retry_schedule()}）：{exc}"
        )
        task.error = reason[:500]
        task.awaiting_upstream = False
        _log(f"视频任务 {task.task_id} 提交最终失败：{task.error}")
        return
    except Exception as exc:  # noqa: BLE001 - 轮询线程绝不能死
        task.next_submit_at = time.time() + interval
        task.last_submit_error = f"{type(exc).__name__}: {exc}"
        _log(f"视频任务 {task.task_id} 第 {retry_ordinal}/{limit} 次重试异常（将重试）：{task.last_submit_error}")
        return

    # 成功：接上正常轮询。
    task.video_id = created["video_id"]
    task.awaiting_upstream = False
    task.last_submit_error = ""
    mapped = agnes_video._STATUS_MAP.get(str(created.get("status") or "").lower())
    if mapped:
        task.status = mapped
    _log(
        f"视频任务 {task.task_id} 第 {attempt}/{limit} 次提交成功 -> Agnes {created['video_id']}"
        f"（上游此前 {attempt - 1} 次暂时性拒绝）"
    )


def _sweep() -> None:
    now = time.time()
    with _LOCK:
        expired = [
            key for key, task in _TASKS.items()
            if task.finished_at and now - task.finished_at > _TASK_TTL_SECONDS
        ]
        for key in expired:
            _TASKS.pop(key, None)
        for key, task_id in list(_CLIENT_JOBS.items()):
            if task_id not in _TASKS:
                _CLIENT_JOBS.pop(key, None)


def _poll_loop() -> None:
    """后台线程：① 推进"待提交"任务的重试；② 刷新所有已提交任务。

    官方建议 1–2 秒轮询一次。这里**只查询已提交的任务**，
    绝不重新提交 —— 与 OCV 的「防重复扣费」原则一致。
    唯一的"提交"动作是 :func:`_retry_deferred_submit`，而它只在
    **上游确定未受理**时才被调用（见那里的文档）。
    """
    while not _POLLER_STOP.is_set():
        try:
            now = time.time()
            with _LOCK:
                # ① 到点该重试提交的（F-013）
                deferred = [
                    task for task in _TASKS.values()
                    if task.awaiting_upstream and now >= task.next_submit_at
                ]
                # ② 已提交、还在跑的
                pending = [
                    task for task in _TASKS.values()
                    if not task.awaiting_upstream
                    and task.status not in {"SUCCESS", "FAILED", "CANCELLED"}
                ]
            for task in deferred:
                _retry_deferred_submit(task)
            for task in pending:
                _refresh(task)
            _sweep()
        except Exception as exc:  # noqa: BLE001 - 轮询线程绝不能死
            _log(f"视频轮询线程异常（继续运行）：{type(exc).__name__}: {exc}")
        # 轮询间隔不能超过"下次重试时刻"，否则 30s 的重试节奏会被拖长。
        interval = config.video_poll_seconds()
        with _LOCK:
            upcoming = [t.next_submit_at - time.time() for t in _TASKS.values()
                        if t.awaiting_upstream]
        if upcoming:
            interval = min(interval, max(0.2, min(upcoming)))
        _POLLER_STOP.wait(interval)


def _ensure_poller() -> None:
    global _POLLER
    with _LOCK:
        if _POLLER is not None and _POLLER.is_alive():
            return
        _POLLER_STOP.clear()
        _POLLER = threading.Thread(target=_poll_loop, name="ocv-cloud-video-poller", daemon=True)
        _POLLER.start()


# --------------------------------------------------------------------------
# OCV 协议响应构造
# --------------------------------------------------------------------------

def query_response(task_id: str) -> dict[str, Any]:
    """把任务状态包成 OCV **视频 provider** 要的响应形态。

    ## 关键：视频层读的是**顶层**字段，不是 `data` 里的

    OCV 内部两套客户端读法**不一样**，这一点极易踩错（实测踩过）：

    * 图片层 ``module4_video_render._submit_poster_request``：
      先读 ``submitted["taskId"]``，**再回退** ``submitted["data"]["taskId"]``；
      查询读 ``data.status`` / ``data.results``。
    * 视频层 ``module6_dynamic_video.RunningHubVideoProvider``：
      只读**顶层** ``body["taskId"]``；查询只读**顶层** ``body["status"]``
      与 ``body["results"]``。**没有任何 `data` 回退**。

    所以视频路由必须返回顶层字段。首版照抄了图片层的 ``{"code":0,"data":{…}}``
    形状，结果 OCV 在提交成功后仍报「未返回任务身份」——
    而 shim 日志里明明写着任务已接受。``verify_agnes_video_e2e.py`` 抓到了它。

    这里**同时**带顶层与 ``data`` 里的字段：顶层喂给视频 provider，
    ``data`` 便于人工排查/面板展示，两边都不吃亏。
    """
    with _LOCK:
        task = _TASKS.get(str(task_id).strip())
        if task is None:
            # 任务不存在时也给出顶层 status，让 OCV 立刻判失败而不是空转轮询。
            return {"taskId": task_id, "status": "FAILED",
                    "errorMessage": "任务不存在", "code": 404,
                    "data": {"taskId": task_id, "status": "FAILED"}}
        snapshot = task.snapshot()

    status = snapshot["status"]
    # 「待提交」任务（上游暂时性拒绝，后台正在按 30s 节奏重试）对 OCV 呈现
    # RUNNING 语义：**绝不能**报 FAILED，否则 OCV 会把镜头判成终态失败、
    # 用户在后台其实还在重试时就被引导去"重新付费生成"（F-013）。
    if snapshot.get("awaitingUpstream") and status not in {"SUCCESS", "FAILED", "CANCELLED"}:
        status = "QUEUED"
    payload: dict[str, Any] = {"taskId": snapshot["taskId"], "status": status, "code": 0}
    if status == "SUCCESS":
        results = [{"url": snapshot["url"]}]
        payload["results"] = results
        payload["data"] = {"taskId": snapshot["taskId"], "status": "SUCCESS", "results": results}
    elif status in {"FAILED", "CANCELLED"}:
        message = snapshot["error"] or "Agnes 视频生成失败"
        payload["errorMessage"] = message
        payload["data"] = {"taskId": snapshot["taskId"], "status": status,
                           "errorMessage": message}
    else:
        payload["data"] = {"taskId": snapshot["taskId"], "status": status or "RUNNING"}
    return payload


def status_snapshot() -> dict[str, Any]:
    with _LOCK:
        tasks = [task.snapshot() for task in _TASKS.values()]
    return {
        "enabled": config.video_enabled(),
        "model": config.video_model(),
        "base": config.video_base_url(),
        "size": config.video_size(),
        "mode": config.video_mode(),
        "inline_media": config.video_inline_media(),
        "has_api_key": bool(config.video_api_key()),
        "tasks": len(tasks),
        "running": sum(1 for t in tasks if t["status"] not in {"SUCCESS", "FAILED", "CANCELLED"}),
    }
