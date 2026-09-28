"""协议差异对照表（Agnes Video vs OCV 期望的 RunningHub 异步协议）。

来源：https://wiki.agnes-ai.com/zh-Hans/docs/agnes-video-25.md （2026-09-24 实取）
      以及 OCV 侧 module6_dynamic_video.py::RunningHubVideoProvider 的实际实现。

本文件是**取证记录**，不是运行代码；写它是为了让"为什么需要 shim"有据可查。
"""
PROTOCOL_GAP = [
    # (环节, OCV 期望, Agnes 实际, 是否必须翻译)
    ("创建任务路径", "POST <submit_path>（可配，默认 /openapi/v2/.../multimodal-video）",
     "POST /v1/videos", "是"),
    ("创建任务请求体", "{prompt, resolution, duration, imageUrls[], videoUrls[], audioUrls[], "
                  "generateAudio, ratio, realPersonMode, returnLastFrame, seed}",
     "{model, prompt, seconds, mode, size, aspect_ratio, seed?, "
     "first_frame|last_frame|images[]|audios[]|videos[]}", "是"),
    ("任务标识", "响应顶层 taskId",
     "响应有 id / task_id / video_id 三个；**查询必须用 video_id**", "是"),
    ("查询方式", "POST <query_path>，请求体 {taskId}",
     "GET /agnesapi?video_id=<ID>&model_name=<MODEL>", "是"),
    ("状态取值", "大写 SUCCESS / FAILED / RUNNING 集合",
     "小写 queued / in_progress / completed / failed", "是"),
    ("结果地址", "results[].url（递归 _find_url）",
     "顶层 url（仅 status=completed 时有效）", "是"),
    ("时长参数", "duration，int，4–15",
     "seconds，**字符串** \"4\"–\"12\"", "是（且区间更窄）"),
    ("分辨率", "480p / 720p",
     "720P / 1080P / 1K / 2K（无 480p）", "是（480p 无处可映射）"),
    ("画幅", "ratio: 16:9 / 9:16 / 4:3 / 3:4 / 1:1 / 21:9",
     "aspect_ratio: 完全相同的 6 个值", "否（同名直通，仅改名）"),
    ("参考图", "imageUrls[]（data URI 可用，实测商汤接受）",
     "images[]（文档要求**公网可访问 URL**；data URI 未在文档中确认）", "**待实测**"),
    ("模式", "无该概念（一律多模态）",
     "mode 必填：text / keyframe / reference；三者互斥、媒体字段受限", "是"),
    ("音频", "generateAudio 布尔 + audioUrls[]",
     "audios[] 作为参考音频；无 generate_audio 开关", "部分（丢弃 generateAudio）"),
]

# 必须由 shim 承担的翻译工作
SHIM_RESPONSIBILITY = [
    "对外复刻 OCV 要的 RunningHub 异步协议（提交拿 taskId → POST 轮询 → 取 results[].url）",
    "对内把请求翻译成 Agnes 的 POST /v1/videos —— 含 mode 推导、seconds 字符串化与夹紧、"
    "size 档位映射、媒体字段按模式分流",
    "维护 taskId → video_id 映射（OCV 只知道 taskId，Agnes 查询只认 video_id）",
    "把 Agnes 的小写状态映射回 OCV 的大写集合",
    "把顶层 url 包装成 _find_url 认得的形态",
]

# 已知不确定项（必须向用户说明，不能假装已解决）
OPEN_QUESTIONS = [
    "Agnes 视频的 images/first_frame 是否接受 data URI？"
    "其图像 API 明确接受（extra_body.image），但视频文档只写了 '公网可访问 URL'，"
    "且明确要求避免 '带本地网络地址'。**无 API Key 无法实测**，故先按 data URI 实现并留可切换。",
    "agnes-video-2.5-flash 的 size 仅支持 \"720P\"；agnes-video-2.5 支持 720P/1080P/1K/2K。"
    "OCV 侧分辨率枚举只有 480p/720p，故 480p 需上映射到 720P（并如实告知用户）。",
    "agnes-video-v2.0 将于 2026-09-25 23:59:59 (UTC+8) 下线，默认模型应选 2.5 系列。",
]

if __name__ == "__main__":
    print("== 协议差异 ==")
    for row in PROTOCOL_GAP:
        need = "【须翻译】" if row[3].startswith("是") else f"[{row[3]}]"
        print(f"\n{need} {row[0]}")
        print(f"    OCV : {row[1]}")
        print(f"    Agnes: {row[2]}")
    print("\n== shim 必须做的事 ==")
    for item in SHIM_RESPONSIBILITY:
        print(f"  - {item}")
    print("\n== 待实测/待确认 ==")
    for item in OPEN_QUESTIONS:
        print(f"  ! {item}")
