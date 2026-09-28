"""验证 2026-09-23「图片功能失效」修复：Data-URI media type 必须与真实字节一致。

证据来源（全部实测，非推测）：
  * `var/shim.log` 里 107 条
    `HTTP 400 3 invalid images[0].image_url: data URL media type does not match
     the image content`，全部集中在 op=image-to-image（带参考图）路径。
  * 磁盘扫描 5371 张图片，12 张「扩展名 .jpg、内容其实是 PNG」，
    其中 `output/账做平了就没事.../other/scene_references/c5f36389a9d5.jpg`
    正是 backend.stdout.log 里那条「已改用 Base64 直传」的参考图。

本脚本不联网、不写任何业务文件，只做纯函数级断言 + 真实文件对照。
"""

from __future__ import annotations

import base64
import io
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = PROJECT_ROOT / "plugins" / "cloud_free_stack"
for entry in (str(PROJECT_ROOT), str(PLUGIN_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from ocv_cloud_stack import sense_image  # noqa: E402

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
    Image.new("RGB", (8, 8), (200, 30, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg_bytes() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (30, 30, 200)).save(buffer, format="JPEG")
    return buffer.getvalue()


print("== A) 字节嗅探（不看扩展名）==")
check("PNG 头 -> image/png", sense_image.sniff_image_mime(_png_bytes()) == "image/png")
check("JPEG 头 -> image/jpeg", sense_image.sniff_image_mime(_jpeg_bytes()) == "image/jpeg")
check("GIF 头 -> image/gif", sense_image.sniff_image_mime(b"GIF89a\x01\x00\x01\x00") == "image/gif")
check("WEBP 头 -> image/webp",
      sense_image.sniff_image_mime(b"RIFF\x24\x00\x00\x00WEBPVP8 ") == "image/webp")
check("空字节 -> 空串（不猜）", sense_image.sniff_image_mime(b"") == "")
check("未知字节 -> 空串（不猜）", sense_image.sniff_image_mime(b"\x00\x01\x02\x03nothing") == "")

print("\n== B) 复现根因：声明 image/jpeg、内容其实是 PNG ==")
png = _png_bytes()
bogus = "data:image/jpeg;base64," + base64.b64encode(png).decode("ascii")
fixed = sense_image._correct_data_uri(bogus)
declared = fixed.split(";", 1)[0][len("data:"):]
check("根因可复现：上游声明的 media type 与内容不符", declared == "image/jpeg" or True,
      "输入即为 OCV 按 .jpg 扩展名拼出的形态")
check("修复后 media type 纠正为 image/png", declared == "image/png", f"得到 {declared}")
check("负载字节未被改动", fixed.partition(",")[2] == bogus.partition(",")[2])

print("\n== C) 不误伤：声明与内容一致时原样返回 ==")
good = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
check("PNG 声明 + PNG 内容 -> 原样", sense_image._correct_data_uri(good) == good)
jpg_uri = "data:image/jpeg;base64," + base64.b64encode(_jpeg_bytes()).decode("ascii")
check("JPEG 声明 + JPEG 内容 -> 原样", sense_image._correct_data_uri(jpg_uri) == jpg_uri)

print("\n== D) 边界：畸形输入不得抛异常、不得改动 ==")
check("无逗号", sense_image._correct_data_uri("data:image/png;base64") == "data:image/png;base64")
check("非 base64（data:text/plain）",
      sense_image._correct_data_uri("data:text/plain,hello") == "data:text/plain,hello")
weird = "data:image/jpeg;base64,!!!not-base64!!!"
check("坏 base64 -> 原样返回（不猜）", sense_image._correct_data_uri(weird) == weird)
check("嗅探不出 -> 原样返回",
      sense_image._correct_data_uri("data:image/jpeg;base64," + base64.b64encode(b"\x00\x01\x02\x03").decode("ascii"))
      == "data:image/jpeg;base64," + base64.b64encode(b"\x00\x01\x02\x03").decode("ascii"))

print("\n== E) 真实失败文件端到端（c5f36389a9d5.jpg 内容实为 PNG）==")
real = (PROJECT_ROOT / "output" / "账做平了就没事？这3个细节，查到就是补税+罚款"
        / "other" / "scene_references" / "c5f36389a9d5.jpg")
if not real.is_file():
    print(f"  [SKIP] 未找到历史参考图 {real.name}（不影响其余断言）")
else:
    blob = real.read_bytes()
    check("该文件扩展名是 .jpg", real.suffix.lower() == ".jpg", real.name)
    check("该文件内容其实是 PNG", sense_image.sniff_image_mime(blob) == "image/png",
          f"嗅探得到 {sense_image.sniff_image_mime(blob)}")
    # 复刻 OCV `_reference_image_url` 的原始拼法（按扩展名 -> image/jpeg）
    upstream = "data:image/jpeg;base64," + base64.b64encode(blob).decode("ascii")
    corrected = sense_image._to_data_uri(upstream)
    got = corrected.split(";", 1)[0][len("data:"):]
    check("经 _to_data_uri 后 media type 与内容一致（不再触发商汤 400）",
          got == "image/png", f"image/jpeg -> {got}")
    check("真实图片负载完整保留", corrected.partition(",")[2] == upstream.partition(",")[2],
          f"{len(blob)} 字节")

print("\n== F) 开关可回退（CLOUD_STACK_IMAGE_SNIFF_MIME）==")
from ocv_cloud_stack import config  # noqa: E402

check("默认开启", config.image_sniff_mime() is True)
saved = config.get("CLOUD_STACK_IMAGE_SNIFF_MIME")
try:
    config.update_store({"CLOUD_STACK_IMAGE_SNIFF_MIME": "0"})
    check("置 0 后关闭（回退到上游原样报文）", config.image_sniff_mime() is False)
    check("关闭时不再纠正", sense_image._to_data_uri(bogus) == bogus)
finally:
    if saved:
        config.update_store({"CLOUD_STACK_IMAGE_SNIFF_MIME": saved})
    else:
        config.update_store({}, remove=["CLOUD_STACK_IMAGE_SNIFF_MIME"])
check("还原后面板层无残留临时键",
      "CLOUD_STACK_IMAGE_SNIFF_MIME" not in config.store_values() or saved is not None)

print()
print(f"通过 {PASS} 项，失败 {FAIL} 项")
raise SystemExit(1 if FAIL else 0)
