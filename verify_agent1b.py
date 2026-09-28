"""Agent 1B 边界细化：原生失败 vs 插件健壮性补丁的**对照复现**脚本。

## 为什么需要这个脚本

2026-09-17 用户报错「Agent 1B 语言模型规划失败 … 原始错误：边界验收失败: empty」。
根因是**校验规则的表达力盲区**，不是输出截断（详见 ``ocv_cloud_stack/agent1b_resilience.py``
的模块文档）。这个脚本把「原生必炸 / 打补丁后必过」做成**可重复执行的证据**，
避免以后靠回忆或推测判断这条链路。

## 用法

    # 用真实数据集跑（默认 output/7179永恒，107 个 slide）
    runtime\\python\\python.exe plugins\\cloud_free_stack\\verify_agent1b.py

    # 只看原生对照（重新加载 story_agents 拿未包装的原生函数）
    ... verify_agent1b.py --native

    # 跳过 Agent 1 的真调模型步骤，用合成的父单元划分
    ... verify_agent1b.py --no-agent1

    # 指定别的项目
    ... verify_agent1b.py --project output/我的项目

    # 指定 scene 快照
    ... verify_agent1b.py --scenes path/to/画面时间线.json

## 退出码

    0  验收通过（覆盖完整）
    1  验收失败（覆盖断裂/重复/或抛出了本不该抛的致命错）
    2  前置条件不满足（数据集缺失等）
    3  调用方要求原生模式，且原生**确实**按预期失败了（对照组成功）

## 注意

* 本脚本**会真调语言模型**（Agent 1 与 Agent 1B），消耗 API 配额。
  只想快速验证补丁逻辑请加 ``--no-agent1``，它仍会真调 Agent 1B。
* 必须用插件内嵌解释器运行：
  ``E:/1B1BLaoYang/runtime/python/python.exe``。
  用系统 Python 会因为找不到 ``ocv_cloud_stack`` 而失败。
* 中文 Windows 上建议剥掉 UTF-8 环境变量，以覆盖 GBK 控制台场景：
  ``env -u PYTHONUTF8 -u PYTHONIOENCODING -u LC_ALL -u LANG <python> ...``
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径准备：本脚本位于 plugins/cloud_free_stack/ 下，需要把仓库根加进 sys.path，
# 才能 import story_agents / backend.app.config。
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent  # plugins/cloud_free_stack -> plugins -> 仓库根
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _fail(message: str, code: int = 2) -> "None":
    print(f"!! {message}")
    sys.exit(code)


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        _fail(f"读取失败 {path}: {exc}")
    except json.JSONDecodeError as exc:
        _fail(f"JSON 解析失败 {path}: {exc}")
    return None


def _resolve_scenes(project: str, explicit: str | None) -> tuple[list[dict], Path]:
    """找到画面时间线。优先用 --scenes，否则在项目目录下探测常见文件名。"""
    if explicit:
        path = Path(explicit)
        if not path.is_absolute():
            path = _ROOT / path
        if not path.is_file():
            _fail(f"指定的 scene 快照不存在：{path}")
        data = _load_json(path)
        if isinstance(data, dict):
            for key in ("scenes", "semantic_scenes", "timeline", "slide_timeline"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break
        if not isinstance(data, list) or not data:
            _fail(f"{path} 里没有可用的 scene 列表")
        return data, path

    proj = Path(project)
    if not proj.is_absolute():
        proj = _ROOT / proj
    if not proj.is_dir():
        _fail(f"项目目录不存在：{proj}（用 --project 指定，或先跑一次 OCV 生成数据）")
    for candidate in ("other/画面时间线.json", "other/模块2.5_校对后字幕场景.json"):
        path = proj / candidate
        if path.is_file():
            data = _load_json(path)
            if isinstance(data, list) and data:
                return data, path
    _fail(
        f"{proj} 下找不到画面时间线。已尝试 other/画面时间线.json、"
        "other/模块2.5_校对后字幕场景.json"
    )
    return [], proj  # 不可达，仅为类型检查


def _synthesize_units(scenes: list[dict], small: int = 5, large: int = 9,
                      small_span: int = 15) -> list[dict]:
    """合成一份父单元划分（复现 Agent 1 常见的分组形态）。

    刻意让前 ``small_span`` 个 slide 走小分组（5 个一单元），这样会出现
    「单 slide 父单元」与「跨时间阶段的长单元」两类边界情况 ——
    前者是原始报错的直接触发者。
    """
    units: list[dict] = []
    index = 0
    while index < len(scenes):
        size = small if index < small_span else large
        chunk = scenes[index:index + size]
        if not chunk:
            break
        units.append({
            "unit_id": f"unit_{len(units) + 1:02d}",
            "start_slide_id": str(chunk[0].get("slide_id") or ""),
            "end_slide_id": str(chunk[-1].get("slide_id") or ""),
            "boundary_after": "hard",
            "text": " ".join(str(s.get("text_content") or "") for s in chunk),
        })
        index += size
    return units


def _positions(scenes: list[dict]) -> dict[str, int]:
    return {
        str(scene.get("slide_id") or ""): index
        for index, scene in enumerate(scenes)
        if str(scene.get("slide_id") or "")
    }


def _audit_coverage(units: list[dict], scenes: list[dict],
                    positions: dict[str, int], label: str) -> tuple[bool, int]:
    """检查 units 是否**严格连续、无重复、无缺口**地覆盖全部 scene。"""
    total = len(scenes)
    previous = -1
    breaks: list[int] = []
    seen: dict[int, int] = {}
    for index, unit in enumerate(units, 1):
        start = positions.get(str(unit.get("start_slide_id") or ""), -1)
        end = positions.get(str(unit.get("end_slide_id") or ""), -1)
        if start < 0 or end < start:
            breaks.append(index)
            continue
        if start != previous + 1:
            breaks.append(index)
            print(
                f"   断裂 @ #{index} {unit.get('unit_id')} "
                f"{unit.get('start_slide_id')}->{unit.get('end_slide_id')} "
                f"(期望起点 idx {previous + 1}，实际 {start})"
            )
        for slide in range(start, end + 1):
            seen[slide] = seen.get(slide, 0) + 1
        previous = end

    duplicated = sorted(slide for slide, count in seen.items() if count > 1)
    covered = previous + 1
    ok = not breaks and covered == total and not duplicated
    print(f"  [{label}] 输出 {len(units)} 个单元，覆盖 {covered}/{total} 个 slide，"
          f"断裂点 {len(breaks)}，重复 slide {len(duplicated)}")
    if duplicated:
        sample = [scenes[i].get("slide_id") for i in duplicated[:5]]
        print(f"    重复示例: {sample}")
    if ok:
        print(f"  [{label}] 完整覆盖: True ({covered}/{total})")
    return ok, covered


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Agent 1B 边界细化：原生失败 vs 插件补丁的对照复现",
    )
    parser.add_argument("--project", default="output/7179永恒",
                        help="OCV 输出项目目录（默认 output/7179永恒）")
    parser.add_argument("--scenes", default=None,
                        help="直接指定 scene 快照 JSON，绕过项目目录探测")
    parser.add_argument("--no-agent1", action="store_true",
                        help="不调 Agent 1，用合成的父单元划分")
    parser.add_argument("--native", action="store_true",
                        help="重新加载 story_agents，跑未包装的原生函数（对照组）")
    parser.add_argument("--content-mode", default="documentary",
                        help="内容模式（默认 documentary）")
    parser.add_argument("--verbose", action="store_true",
                        help="打印输出的前若干个单元区间")
    args = parser.parse_args()

    from backend.app.config import load_project_env

    load_project_env()

    # 这两个 import 必须在 load_project_env() 之后 —— 插件靠 .env / 面板层
    # 注入 provider，先 import 会把「未配置」状态固化下来。
    import story_agents  # noqa: E402
    from ocv_cloud_stack import agent1b_resilience as resilience  # noqa: E402

    scenes, scenes_path = _resolve_scenes(args.project, args.scenes)
    positions = _positions(scenes)
    total = len(scenes)
    print(f"数据集: {scenes_path}")
    print(f"共 {total} 个 slide")

    if args.native:
        resilience.reload_and_uninstall("story_agents")
        wrapped = getattr(
            story_agents.refine_risky_semantic_units, "_cloud_stack_wrapped", False
        )
        print(f"模式: 原生（重新加载 story_agents 后 wrapped={wrapped}）")
    else:
        resilience.install(story_agents)
        print(
            f"模式: 插件补丁  installed={resilience.installed()} "
            f"degrade={resilience.enabled()} skip={resilience.skip_inseparable()}"
        )

    # ---------------- 取得父单元 ----------------
    if args.no_agent1:
        units = _synthesize_units(scenes)
        print(f"[--no-agent1] 合成 {len(units)} 个父单元")
    else:
        print("调用 Agent 1（真调语言模型，走 load_or_create_story_plan）...")
        started = time.time()
        plan = story_agents.load_or_create_story_plan(
            scenes,
            resume=True,
            content_mode=args.content_mode,
            require_ai_success=True,
        )
        print(f"Agent 1 完成，耗时 {time.time() - started:.1f}s")
        source = plan.get("generation_source") if isinstance(plan, dict) else None
        print(f"generation_source={source}")
        units = plan.get("semantic_units") if isinstance(plan, dict) else None
        if not units:
            nested = plan.get("story_plan") if isinstance(plan, dict) else None
            if isinstance(nested, dict):
                units = nested.get("semantic_units")
        if not units:
            _fail("Agent 1 未返回 semantic_units")

    print(f"父单元数: {len(units)}")
    single = [u for u in units if resilience._slide_count(u, scenes) == 1]
    print(f"其中单 slide 单元: {len(single)}")

    ok_in, _ = _audit_coverage(units, scenes, positions, "输入")
    if not ok_in:
        print("  注意：输入本身覆盖不完整，后续验收以输出为准")

    # ---------------- 主调用 ----------------
    print()
    print("=== refine_risky_semantic_units(require_ai_success=True) ===")
    started = time.time()
    try:
        refined, diagnostics = story_agents.refine_risky_semantic_units(
            units, scenes, {"semantic_units": units}, args.content_mode,
            require_ai_success=True,
        )
    except Exception as exc:  # noqa: BLE001
        elapsed = time.time() - started
        print(f"抛出异常: {type(exc).__name__}: {str(exc)[:300]}")
        print(f"耗时 {elapsed:.1f}s")
        if args.native:
            print()
            print("=== 对照组结论 ===")
            print("原生按预期终止 —— 这正是用户报错的那条路径。")
            print("去掉 --native 再跑一次，插件补丁应当让它通过。")
            return 3
        print()
        print("=== 验收结论 ===")
        print("FAIL（插件补丁模式下不应抛出致命错）")
        return 1

    print(f"耗时 {time.time() - started:.1f}s")
    print(f"输入单元数: {len(units)} -> 输出单元数: {len(refined)}")
    stats = diagnostics.get("resilience") if isinstance(diagnostics, dict) else None
    if isinstance(stats, dict):
        print("resilience:", json.dumps(stats, ensure_ascii=False))
    for key in ("triggered_units", "accepted_units", "failed_units"):
        rows = diagnostics.get(key) if isinstance(diagnostics, dict) else None
        print(f"{key}: {len(rows or [])}")
    for item in (diagnostics.get("failed_units") or []) if isinstance(diagnostics, dict) else []:
        print("   failed:", json.dumps(item, ensure_ascii=False)[:200])
    for item in (diagnostics.get("skipped_units") or []) if isinstance(diagnostics, dict) else []:
        print("   skipped:", json.dumps(item, ensure_ascii=False)[:200])

    # ---------------- 覆盖验收 ----------------
    print()
    print("=== 覆盖校验 ===")
    ok, covered = _audit_coverage(refined, scenes, positions, "输出")

    if args.verbose:
        print()
        print("=== 输出前 15 个单元的区间 ===")
        for index, unit in enumerate(refined[:15], 1):
            start = positions.get(str(unit.get("start_slide_id") or ""), -1)
            end = positions.get(str(unit.get("end_slide_id") or ""), -1)
            print(f"  {index:3d} {unit.get('unit_id'):<10} "
                  f"{unit.get('start_slide_id')}->{unit.get('end_slide_id')} "
                  f"(idx {start}..{end}, n={end - start + 1})")

    print()
    print("=== 验收结论 ===")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
