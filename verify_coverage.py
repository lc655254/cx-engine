# -*- coding: utf-8 -*-
"""Does the work list really cover every unfinished node, for EVERY course?

CASCADE_CHILD_NODES was turned off on the finding that the work list (after child
expansion + the finished-node filter) already contains all unfinished nodes - true for
思想道德 (131 = 131) and 大学物理 (57 vs 56). If that holds for every course, the
cascade is genuinely redundant and disabling it costs nothing. If even one course has
unfinished nodes that never enter the work list, those nodes are orphaned and would
never be touched at all.

Read-only: it builds the work list and reads the catalog, and never answers or plays
anything, so it costs no daily budget.

    .venv\\Scripts\\python.exe -X utf8 verify_coverage.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from bank import ensure_utf8_stdout              # noqa: E402
from cx_auto import Session                      # noqa: E402
from node_cascade import list_child_nodes        # noqa: E402

ensure_utf8_stdout()

COURSES = ["254215593",  # 大学物理
           "267087606",  # 思想道德与法治
           "88819267",   # 信息技术基础
           "203417266",  # 数学
           "237168226",  # 大学英语B1
           "267261427"]  # 大学生心理健康教育


async def main() -> int:
    only = [a for a in sys.argv[1:] if a.isdigit()]
    ids = only or COURSES
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as fh:
        cfg = json.load(fh)
    with open(os.path.join(ROOT, "evidence", "course_cards.json"), encoding="utf-8") as fh:
        cards = {c["courseId"]: c for c in json.load(fh)}

    print("课程                        工作列表  目录未完成   缺口")
    print("-" * 60)
    problems = []
    total_gap = 0

    for cid in ids:
        card = cards.get(cid)
        if not card:
            continue
        name = card["name"][:22]
        work_cfg = dict(cfg)
        work_cfg.update({"COURSE_ID": cid, "DO_VIDEO": False, "DO_QUIZ": False,
                         "HEADLESS": True})
        sess = Session(work_cfg)
        try:
            await sess.start()
            await sess.login()
            work = await sess.chapters()          # expansion + finished filter applied
            page = sess.page
            if not work:
                print(f"{name:<26} {0:>8}  {'目录为空':>10}")
                continue
            # read the catalog's unfinished ids
            await page.goto(work[0], wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(9000)
            rows = await page.evaluate("""() => {
                const out = [];
                document.querySelectorAll('[id^="cur"]').forEach(el => {
                    const m = (el.id || '').match(/^cur(\\d+)$/);
                    if (!m) return;
                    const b = el.querySelector ? el.querySelector('.jobCount') : null;
                    if (b && (parseInt((b.innerText || '0').trim(), 10) || 0) > 0)
                        out.push(m[1]);
                });
                return out;
            }""")
            work_ids = {re.search(r"chapterId=(\d+)", u).group(1)
                        for u in work if re.search(r"chapterId=(\d+)", u)}
            uncovered = [i for i in rows if i not in work_ids]
            gap = len(uncovered)
            total_gap += gap
            flag = "OK" if gap == 0 else f"!! {gap} 个漏网"
            print(f"{name:<26} {len(work_ids):>8}  {len(rows):>10}   {flag}")
            if gap:
                problems.append((cid, name, uncovered[:6]))
        except Exception as exc:
            print(f"{name:<26} 读取失败 {type(exc).__name__}")
        finally:
            try:
                await sess.close()
            except Exception:
                pass

    print("-" * 60)
    if total_gap == 0:
        print("结论: 每门课的未完成节点都进了工作列表，级联确实是冗余的")
    else:
        print(f"结论: 有 {total_gap} 个节点不在任何工作列表里 -> 需要恢复级联")
        for cid, name, sample in problems:
            print(f"   {name}: {sample}")
    out = os.path.join(ROOT, "logs", "coverage.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"gap": total_gap, "problems": problems}, fh,
                  ensure_ascii=False, indent=1)
    print(f"-> {os.path.relpath(out, ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
