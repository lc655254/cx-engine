# -*- coding: utf-8 -*-
"""Verify course progress independently, bypassing the list-page progress widget.

Suspicion: 思想道德, 2024级Python, 2025计算机与网络技术类 and 形势与政策 have all
reported EXACTLY the same numbers (4/173 -> 8/173 -> 30/173) even though only
思想道德 is being driven. Either those courses genuinely share content, or the
list-page "任务点进度" element belongs to a different card and my reading is wrong.

This opens each course directly, counts its own task points from the chapter
catalog (jobCount badges) and compares with what the list page claims.

    .venv\\Scripts\\python.exe -X utf8 verify_progress.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bank import ensure_utf8_stdout              # noqa: E402
from cx_auto import INJECT, UA, pick_browser      # noqa: E402
import course_nav as nav                          # noqa: E402

ensure_utf8_stdout()

ROOT = os.path.dirname(os.path.abspath(__file__))

CATALOG = r"""() => {
    const rows = [];
    document.querySelectorAll('h5[id^="cur"], [id^="cur"]').forEach(el => {
        const m = (el.id || '').match(/^cur(\d+)$/);
        if (!m) return;
        const b = el.querySelector ? el.querySelector('.jobCount') : null;
        rows.push({ id: m[1],
                    jobs: b ? (parseInt((b.innerText || '0').trim(), 10) || 0) : 0,
                    done: !b,
                    text: (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 40) });
    });
    return rows;
}"""

LIST_PROGRESS = r"""() => {
    const out = [];
    document.querySelectorAll('a[href*="courseId="], a[href*="courseid="]').forEach(el => {
        const href = el.getAttribute('href') || '';
        const m = href.match(/[cC]ourse[iI]d=(\d+)/);
        if (!m) return;
        let box = el.closest('div,li,dl');
        for (let i = 0; i < 5 && box; i++) {
            const t = (box.innerText || '');
            const p = t.match(/任务点进度[:：]?\s*(\d+)\s*\/\s*(\d+)/);
            if (p) {
                if (!out.some(o => o.cid === m[1]))
                    out.push({ cid: m[1], done: +p[1], total: +p[2],
                               raw: t.replace(/\s+/g, ' ').slice(0, 60) });
                return;
            }
            box = box.parentElement;
        }
    });
    return out;
}"""


async def main() -> int:
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as fh:
        cfg = json.load(fh)
    with open(os.path.join(ROOT, "evidence", "course_cards.json"), encoding="utf-8") as fh:
        cards = json.load(fh)
    by_id = {c["courseId"]: c for c in cards}

    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    _c, exe = pick_browser(cfg)
    br = await pw.chromium.launch(headless=True, executable_path=exe,
                                  args=["--disable-blink-features=AutomationControlled"])
    ctx = await br.new_context(
        storage_state=os.path.join(ROOT, "evidence", "storage_state.json"),
        user_agent=UA, locale="zh-CN", viewport={"width": 1500, "height": 900})
    await ctx.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});\n" + INJECT)
    page = await ctx.new_page()

    print("=== 列表页声称的进度 ===")
    await page.goto("https://mooc2-ans.chaoxing.com/mooc2-ans/visit/interaction",
                    wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(6000)
    claimed = {}
    for attempt in range(3):
        rows = await page.evaluate(LIST_PROGRESS)
        for r in rows:
            claimed.setdefault(r["cid"], r)
        if claimed:
            break
        await page.wait_for_timeout(3000)
    for cid, r in sorted(claimed.items()):
        name = by_id.get(cid, {}).get("name", "?")[:22]
        print(f"  {cid:>10} {name:<24} {r['done']}/{r['total']}")
    if not claimed:
        print("  (列表页没解析出进度)")

    print("\n=== 逐门直连课程，数自己的任务点 ===")
    focus = ["267087606", "247361843", "257386446", "266816164",
             "203417266", "88819267", "237168226"]
    found = {}
    for cid in focus:
        card = by_id.get(cid)
        if not card:
            continue
        ok = await nav.open_course(page, cid, card["clazzid"], card["cpi"])
        if not ok:
            print(f"  {cid:>10} {card['name'][:22]:<24} 进不去（会话/入口问题）")
            continue
        chs = await nav.read_chapters(page)
        total_jobs = done_jobs = 0
        rows = []
        if chs:
            enc = ""
            m = re.search(r"enc=([0-9a-f]+)", chs[0].get("href", ""))
            if m:
                enc = m.group(1)
            url = ("https://mooc1.chaoxing.com/mooc-ans/mycourse/studentstudy"
                   f"?chapterId={chs[0]['chapterId']}&courseId={cid}"
                   f"&clazzid={card['clazzid']}&cpi={card['cpi']}&enc={enc}")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(9000)
                for _ in range(3):
                    await page.mouse.wheel(0, 1200)
                    await page.wait_for_timeout(600)
                rows = await page.evaluate(CATALOG)
            except Exception as exc:
                print(f"  catalog error {type(exc).__name__}")
        total_nodes = len(rows)
        left_nodes = [r for r in rows if r["jobs"]]
        total_jobs = sum(r["jobs"] for r in left_nodes)
        print(f"  {cid:>10} {card['name'][:22]:<24} 章节={len(chs):>3}  "
              f"目录节点={total_nodes:>3}  未完成节点={len(left_nodes):>3}  "
              f"未完成任务点={total_jobs:>3}")
        found[cid] = {"name": card["name"], "chapters": len(chs),
                      "nodes": total_nodes, "unfinished": len(left_nodes),
                      "jobs_left": total_jobs, "claimed": claimed.get(cid)}

    out = os.path.join(ROOT, "logs", "progress_verify.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"claimed": claimed, "measured": found}, fh,
                  ensure_ascii=False, indent=1)
    print(f"\nreport -> {os.path.relpath(out, ROOT)}")
    await ctx.close(); await br.close(); await pw.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
