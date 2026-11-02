# -*- coding: utf-8 -*-
"""Task-point completion checker - reads real progress, never writes.

Two independent readings:
  1. the course list shows "任务点进度: done/total pct%" per course
  2. each chapter shows how many of its task points are still unfinished

    .venv\\Scripts\\python.exe -X utf8 check_progress.py 237168226
    .venv\\Scripts\\python.exe -X utf8 check_progress.py 237168226 784508420
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bank import ensure_utf8_stdout             # noqa: E402
from cx_auto import INJECT, UA, pick_browser     # noqa: E402
import course_nav as nav                         # noqa: E402

ensure_utf8_stdout()

ROOT = os.path.dirname(os.path.abspath(__file__))


async def list_progress(page) -> dict:
    await page.goto("https://mooc2-ans.chaoxing.com/mooc2-ans/visit/interaction",
                    wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(6000)
    for _ in range(3):
        await page.mouse.wheel(0, 1000)
        await page.wait_for_timeout(700)
    return await page.evaluate(r"""() => {
        const out = {};
        const links = document.querySelectorAll('a[href*="courseId="], a[href*="courseid="]');
        links.forEach(el => {
            const m = (el.getAttribute('href') || '').match(/[cC]ourse[iI]d=(\d+)/);
            if (!m) return;
            let box = el.closest('div,li,dl');
            for (let i = 0; i < 4 && box; i++) {
                const t = (box.innerText || '');
                const p = t.match(/任务点进度[:：]?\s*(\d+)\s*\/\s*(\d+)\s*([\d.]+)?%?/);
                if (p) {
                    const name = (t.match(/[\u4e00-\u9fffA-Za-z0-9（）()·\-—\s]{3,40}/) || [''])[0];
                    out[m[1]] = { done: +p[1], total: +p[2], pct: p[3] || '',
                                  raw: t.replace(/\s+/g, ' ').slice(0, 80) };
                    return;
                }
                box = box.parentElement;
            }
        });
        return out;
    }""")


async def chapter_unfinished(page, cid: str, clz: str, cpi: str, ch: str) -> dict:
    """Open one chapter and read its own task-point counters."""
    href = nav.STUDY.format(ch=ch, cid=cid, clz=clz, cpi=cpi, enc="")
    href = re.sub(r"&enc=$", "", href)
    await page.goto(href, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(8000)
    return await page.evaluate(r"""() => {
        const el = document.querySelector('#jobUnfinishCount, .jobUnfinishCount');
        const txt = (document.body.innerText || '');
        const ratios = txt.match(/\d+\s*\/\s*\d+/g) || [];
        const done = (txt.match(/已完成/g) || []).length;
        return { url: location.href.slice(0, 90), unfinishCount: el ? el.innerText.trim() : null,
                 ratios: ratios.slice(0, 8), doneMarkers: done };
    }""")


async def main() -> int:
    cid = sys.argv[1] if len(sys.argv) > 1 else "237168226"
    ch = sys.argv[2] if len(sys.argv) > 2 else ""
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as fh:
        cfg = json.load(fh)

    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    _c, exe = pick_browser(cfg)
    br = await pw.chromium.launch(headless=True, executable_path=exe,
                                  args=["--disable-blink-features=AutomationControlled"])
    ctx = await br.new_context(
        storage_state=os.path.join(ROOT, "evidence", "storage_state.json"),
        user_agent=UA, locale="zh-CN", viewport={"width": 1600, "height": 950})
    await ctx.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});\n" + INJECT)
    page = await ctx.new_page()

    print("=== course list progress")
    prog = await list_progress(page)
    cards = await nav.fetch_course_cards(page)
    names = {c["courseId"]: c["name"] for c in cards}
    for cid_, p in sorted(prog.items(), key=lambda kv: names.get(kv[0], "")):
        print(f"  {cid_:>10}  {p['done']:>4}/{p['total']:<4} {p['pct']:>6}%  "
              f"{names.get(cid_, '')[:26]}")
    if not prog:
        print("  (no progress markers matched; page layout may differ)")

    if cid in prog:
        print(f"\nTARGET course {cid} ({names.get(cid,'')}): "
              f"{prog[cid]['done']}/{prog[cid]['total']} = {prog[cid]['pct']}%")

    if ch:
        card = next((c for c in cards if c["courseId"] == cid), None)
        if card:
            print(f"\n=== chapter {ch} detail")
            info = await chapter_unfinished(page, cid, card["clazzid"], card["cpi"], ch)
            print(f"  {json.dumps(info, ensure_ascii=False)}")
            await page.screenshot(path=os.path.join(ROOT, "logs", "check_progress.png"))

    await ctx.close(); await br.close(); await pw.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
