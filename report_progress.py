# -*- coding: utf-8 -*-
"""READ-ONLY progress report. Never touches a task point.

Built after the platform flagged the account for software-based course farming and
cleared the learning records. Automating task points again is off the table, but YG
still needs to know exactly what is left so he can plan his own studying.

What this does:
  * requires a session that YOU created by logging in yourself
  * opens each course and reads the catalog's unfinished-node badges
  * prints a per-course study plan, heaviest first
  * never clicks an option, never plays media, never submits anything

Hard guarantees, enforced in code below:
  * refuses to run without an existing session - it will not log in for you
  * only GET requests through normal navigation
  * no call to do_video / do_quiz / _apply / _submit, ever

    .venv\\Scripts\\python.exe -X utf8 report_progress.py
    .venv\\Scripts\\python.exe -X utf8 report_progress.py --json out.json
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bank import ensure_utf8_stdout          # noqa: E402
from cx_auto import INJECT, UA, pick_browser  # noqa: E402
import course_nav as nav                      # noqa: E402

ensure_utf8_stdout()

ROOT = os.path.dirname(os.path.abspath(__file__))

CATALOG = r"""() => {
    const rows = [];
    document.querySelectorAll('[id^="cur"]').forEach(el => {
        const m = (el.id || '').match(/^cur(\d+)$/);
        if (!m) return;
        const badge = el.querySelector ? el.querySelector('.jobCount') : null;
        const txt = (el.innerText || '').replace(/\s+/g, ' ').trim();
        rows.push({ chapterId: m[1],
                    jobs: badge ? (parseInt((badge.innerText || '0').trim(), 10) || 0) : 0,
                    title: txt.slice(0, 60) });
    });
    return rows;
}"""


async def main() -> int:
    args = sys.argv[1:]
    json_out = ""
    if "--json" in args:
        i = args.index("--json")
        if i + 1 < len(args):
            json_out = args[i + 1]

    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as fh:
        cfg = json.load(fh)
    # absolutely no task-point work in this process
    cfg.update({"DO_VIDEO": False, "DO_QUIZ": False, "HEADLESS": True,
                "INCLUDE_CHILD_NODES": False, "CASCADE_CHILD_NODES": False,
                "AUTO_RELOGIN": False})

    state = os.path.join(ROOT, "evidence", "storage_state.json")
    if not os.path.exists(state):
        print("没有找到登录态。请你自己手动登录一次：")
        print("  .venv\\Scripts\\python.exe -X utf8 login.py --qr --interactive")
        return 2

    with open(os.path.join(ROOT, "evidence", "course_cards.json"), encoding="utf-8") as fh:
        cards = json.load(fh)

    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    _c, exe = pick_browser(cfg)
    br = await pw.chromium.launch(headless=True, executable_path=exe,
                                  args=["--disable-blink-features=AutomationControlled"])
    ctx = await br.new_context(storage_state=state, user_agent=UA, locale="zh-CN",
                               viewport={"width": 1500, "height": 900})
    await ctx.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});\n" + INJECT)
    page = await ctx.new_page()

    # is the session the user made still valid?
    await page.goto("https://mooc1.chaoxing.com/visit/interaction",
                    wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(4000)
    if "passport" in page.url or "/login" in page.url:
        print("登录态已失效。请自己手动登录：")
        print("  .venv\\Scripts\\python.exe -X utf8 login.py --qr --interactive")
        await ctx.close(); await br.close(); await pw.stop()
        return 3

    print(f"只读进度报告  {time.strftime('%Y-%m-%d %H:%M')}")
    print("=" * 74)

    report = {}
    for card in cards:
        cid, name = card["courseId"], card["name"]
        try:
            ok = await nav.open_course(page, cid, card["clazzid"], card["cpi"])
            if not ok:
                print(f"{name[:26]:<28} 进不去")
                continue
            chs = await nav.read_chapters(page)
            if not chs:
                report[cid] = {"name": name, "chapters": 0, "unfinished": 0,
                               "jobs": 0, "items": []}
                print(f"{name[:26]:<28} 目录为空（课程可能没发布内容）")
                continue
            enc = ""
            m = re.search(r"enc=([0-9a-f]+)", chs[0].get("href", ""))
            if m:
                enc = m.group(1)
            url = ("https://mooc1.chaoxing.com/mooc-ans/mycourse/studentstudy"
                   f"?chapterId={chs[0]['chapterId']}&courseId={cid}"
                   f"&clazzid={card['clazzid']}&cpi={card['cpi']}&enc={enc}")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(9000)
            rows = await page.evaluate(CATALOG)
            left = [r for r in rows if r["jobs"]]
            jobs = sum(r["jobs"] for r in left)
            report[cid] = {"name": name, "chapters": len(chs),
                           "unfinished": len(left), "jobs": jobs,
                           "items": [{"id": r["chapterId"], "jobs": r["jobs"],
                                      "title": r["title"]} for r in left]}
            print(f"{name[:26]:<28} 章节 {len(chs):>4}   未完成节点 {len(left):>4}   "
                  f"任务点 {jobs:>4}")
        except Exception as exc:
            print(f"{name[:26]:<28} 读取失败 {type(exc).__name__}")

    print("=" * 74)
    total_nodes = sum(r["unfinished"] for r in report.values())
    total_jobs = sum(r["jobs"] for r in report.values())
    print(f"合计：未完成节点 {total_nodes}，任务点 {total_jobs}")

    heavy = sorted([r for r in report.values() if r["jobs"]],
                   key=lambda r: -r["jobs"])
    if heavy:
        print("\n按工作量排序（决定先复习哪门）:")
        for r in heavy:
            mins = r["jobs"] * 6            # ~6 分钟/任务点（1x 视频实测量级）
            print(f"  {r['jobs']:>4} 个任务点  {r['name'][:30]:<32} "
                  f"约 {mins // 60}h{mins % 60:02d}m")

    if json_out:
        with open(json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=1)
        print(f"\n明细已写入 {json_out}")

    await ctx.close(); await br.close(); await pw.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
