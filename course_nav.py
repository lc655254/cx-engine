# -*- coding: utf-8 -*-
"""Course navigator: the entry points that actually work for this account.

Hard-won facts (2026-10-13, verified against the live account):

  * /mycourse/studentcourse?courseId=X      -> dead legacy redirect. DO NOT USE.
  * mooc-ans/visit/stucoursemiddle?...&ismooc2=1&v=2 -> mooc2-ans shell, 0 chapters.
  * mooc-ans/visit/stucoursemiddle?courseid=X&clazzid=Y&cpi=Z  -> WORKS, real
    chapter list. `cpi` must come from the course card, and `ismooc2` must be absent.
  * chapter links look like
    /mooc-ans/mycourse/studentstudy?chapterId=..&courseId=..&clazzid=..&cpi=..

Everything here reads; it does not submit progress.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.abspath(__file__))
LIST_URL = "https://mooc1.chaoxing.com/visit/interaction"
MIDDLE = ("https://mooc1.chaoxing.com/mooc-ans/visit/stucoursemiddle"
          "?courseid={cid}&clazzid={clz}&vc=1&cpi={cpi}")
STUDY = ("https://mooc1.chaoxing.com/mooc-ans/mycourse/studentstudy"
         "?chapterId={ch}&courseId={cid}&clazzid={clz}&cpi={cpi}&enc={enc}")
# NOTE: do not append mooc2=1 here - the live hrefs do not carry it and adding it
# bounces the navigation to the empty mooc2 shell.


def load_courses() -> List[Dict[str, Any]]:
    path = os.path.join(ROOT, "evidence", "courses.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return []


async def fetch_course_cards(page) -> List[Dict[str, str]]:
    """Read courseid/clazzid/cpi straight off the live course cards."""
    await page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(4000)
    for _ in range(3):
        await page.mouse.wheel(0, 1200)
        await page.wait_for_timeout(800)
    return await page.evaluate(r"""() => {
        const out = [];
        document.querySelectorAll('li.course, .course').forEach(el => {
            const cid = el.getAttribute('courseid');
            const clz = el.getAttribute('clazzid');
            const h3 = el.querySelector('.course-name, h3');
            const a = el.querySelector('a[href*="stucoursemiddle"]');
            const href = a ? a.getAttribute('href') : '';
            const m = href.match(/cpi=(\d+)/);
            out.push({
                courseId: cid || '', clazzid: clz || '',
                cpi: m ? m[1] : '', name: h3 ? (h3.innerText || '').trim() : '',
                href: href,
            });
        });
        const seen = new Set(); const uniq = [];
        for (const o of out) {
            if (!o.courseId || seen.has(o.courseId)) continue;
            seen.add(o.courseId); uniq.push(o);
        }
        return uniq;
    }""")


async def open_course(page, cid: str, clz: str, cpi: str) -> bool:
    """Warm up through the list, then enter the course. True on success."""
    try:
        await page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(2500)
    except Exception:
        pass
    await page.goto(MIDDLE.format(cid=cid, clz=clz, cpi=cpi),
                    wait_until="domcontentloaded", timeout=60000, referer=LIST_URL)
    await page.wait_for_timeout(7000)
    if "passport" in page.url:
        return False
    return "/studentcourse" in page.url or "/stucoursemiddle" in page.url


async def read_chapters(page) -> List[Dict[str, str]]:
    """Chapter nodes from the course home page."""
    return await page.evaluate(r"""() => {
        const out = []; const seen = new Set();
        document.querySelectorAll('a[href*=chapterId], [onclick*=chapterId]').forEach(el => {
            const blob = (el.getAttribute('href') || '') + '|' + (el.getAttribute('onclick') || '');
            const m = blob.match(/chapterId[=\\]*["']?(\d+)/);
            if (!m) return;
            const id = m[1];
            if (seen.has(id)) return; seen.add(id);
            let t = (el.innerText || '').replace(/\s+/g, ' ').trim();
            const li = el.closest('li,div');
            if (!t && li) t = (li.innerText || '').replace(/\s+/g, ' ').trim();
            out.push({ chapterId: id, text: t.slice(0, 80),
                       href: (el.getAttribute('href') || '').slice(0, 200) });
        });
        return out;
    }""")


async def read_study_page(page) -> Dict[str, Any]:
    """Inspect an open chapter page: media, task points, quiz blocks."""
    return await page.evaluate(r"""() => {
        const txt = (document.body.innerText || '');
        const jobs = [];
        document.querySelectorAll('[class*=job], [class*=Job], .icon_Learn, .orange').forEach(el => {
            const t = (el.innerText || el.className || '').toString().replace(/\s+/g, ' ').trim();
            if (t && t.length < 60) jobs.push(t.slice(0, 50));
        });
        return {
            url: location.href,
            title: document.title,
            videos: document.querySelectorAll('video,audio').length,
            iframes: document.querySelectorAll('iframe').length,
            questions: document.querySelectorAll('.TiMu, .questionLi').length,
            jobMarkers: [...new Set(jobs)].slice(0, 8),
            doneMarkers: (txt.match(/已完成|已学完|任务点已完成/g) || []).length,
            progress: (() => { const m = txt.match(/完成度[:：]?\s*([\d.]+)%/); return m ? m[1] : null; })(),
            textHead: txt.replace(/\s+/g, ' ').slice(0, 160),
        };
    }""")
