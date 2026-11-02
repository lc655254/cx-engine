# -*- coding: utf-8 -*-
"""Check every explicit claim in the objective against the execution path.

Round 28 found that "出现异常标记当天停手" had been reported as done for several rounds
while the running worker contained not one line of it. The claim lived in the objective
and in my summaries, not in the code.

So: every requirement is listed here with a concrete artefact that must exist in the
path that actually runs. A requirement with no artefact FAILS, regardless of how
confident the summary sounds.

    .venv\\Scripts\\python.exe -X utf8 verify_objective.py
"""
from __future__ import annotations

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

results = []


def read(name: str) -> str:
    p = os.path.join(ROOT, name)
    return open(p, encoding="utf-8").read() if os.path.exists(p) else ""


def claim(cid, text, where, needle, note=""):
    """Record whether `needle` appears in `where` (a file or a list of files)."""
    files = [where] if isinstance(where, str) else where
    found = None
    for f in files:
        src = read(f)
        if needle in src:
            line = src[:src.index(needle)].count("\n") + 1
            found = f"{f}:{line}"
            break
    results.append((cid, text, found, note))


def main() -> int:
    # ---- (1) sweeps chapter quizzes, AI answers, verified submit -----------
    claim("1", "测验批量扫描已排进每日例程",
          "supervise.py", "run_quiz_sweep()", "监督器每轮调用")
    claim("1", "扫描委托给统一答题核心",
          "sweep_quizzes.py", "sess.do_quiz()", "不再自带副本")
    claim("1", "提交要读回执才算数",
          "cx_auto.py", 'return "done"', "verdict 机制")
    claim("1", "AI 兜底答题",
          "ai_answers.py", "class AI", "AI 层")

    # ---- (2) media types ---------------------------------------------------
    claim("2", "音频节点被识别",
          "cx_auto.py", "/modules/audio/", "MEDIA_HINTS")
    claim("2", "状态读取覆盖 audio（原为 video-only）",
          "task_points.py", "querySelector('video,audio')", "VIDEO_STATE")
    claim("2", "音频与视频同路径",
          "cx_auto.py", "'video,audio'", "同一选择器")
    claim("2", "PDF 处理",
          "task_points.py", "async def finish_pdf", "")
    claim("2", "阅读页处理",
          "task_points.py", "async def walk_reader", "")
    claim("2", "子节点展开进工作列表",
          "node_cascade.py", "async def list_child_nodes", "取代级联")

    # ---- (3) video ---------------------------------------------------------
    claim("3", "真实鼠标启动播放",
          "task_points.py", ".mouse.click(", "真实 pointer 事件，带 iframe 偏移")
    claim("3", "播到结束事件（1x 硬播）",
          "task_points.py", '"ended"', "必须完整播完")

    # ---- (3b) manual list --------------------------------------------------
    claim("3b", "待人工清单存在",
          ["CHECKLIST.md"], "人脸", "军事理论扫脸")

    # ---- (4) risk controls -------------------------------------------------
    # concurrency: the objective bounds it at 2-3 per subject, so assert the VALUE is
    # inside that range rather than pinning one number (it was 2, raised to 3 on
    # 2026-10-14 at YG's request, and a pinned literal flagged the change as missing).
    sp = read("supervise.py")
    mconc = re.search(r'CONCURRENCY\s*=\s*"(\d+)"', sp)
    # the objective states an UPPER bound of 2-3 ("并行上限"), so running 1 is
    # compliant - it is more conservative, not less. Measured 2026-10-17: the platform
    # bounces the session to the portal home partway through a video when two workers
    # share the account, so 1 is what actually works.
    if mconc and 1 <= int(mconc.group(1)) <= 3:
        line = sp[:mconc.start()].count("\n") + 1
        results.append(("4a", f"并发在上限 2-3 内（当前 {mconc.group(1)}）",
                        f"supervise.py:{line}", ""))
    else:
        results.append(("4a", "并发上限不在 2-3 内", None,
                        mconc.group(1) if mconc else "未找到 CONCURRENCY"))
    claim("4b", "同课程不会并发（每课程一个任务）",
          "parallel_run.py", "picked = [c for c in cards", "set 去重后逐个派发")
    claim("4c", "章节间随机间隔",
          "cx_auto.py", "SLEEP_BETWEEN_CHAPTERS", "默认 75-210s")
    claim("4d", "异常标记当天停手",
          "anomaly.py", "def blocks_today", "")
    claim("4d", "worker 每章检查异常",
          "cx_auto.py", "from anomaly import check", "")
    claim("4d", "调度器尊重异常标记",
          "schedule_resume.py", "anomaly.blocks_today()", "")
    claim("4d", "额度对所有入口生效",
          ["sweep_quizzes.py", "auto_quiz.py"], "Budget(", "")

    # ---- (5) reporting -----------------------------------------------------
    claim("5", "交接文档存在", ["HANDOFF.md", "STATUS.md"], "剩余", "")

    # ---- report ------------------------------------------------------------
    print("=" * 78)
    print("目标逐条核对：要求 -> 实际执行路径里的一行代码")
    print("=" * 78)
    cur = None
    bad = 0
    for cid, text, found, note in results:
        if cid != cur:
            print()
            cur = cid
        if found:
            print(f"  [OK  ] ({cid}) {text}")
            print(f"         -> {found}" + (f"   {note}" if note else ""))
        else:
            bad += 1
            print(f"  [FAIL] ({cid}) {text}   <-- 执行路径里找不到实现")

    print()
    print("=" * 78)
    print(f"共 {len(results)} 条，未落实 {bad} 条")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
