# -*- coding: utf-8 -*-
"""Dump the audio player's real controls, so start_video can be taught to click one.

start_video tries three selectors:

    button.vjs-big-play-button   video only
    .vjs-poster                  video only
    button.vjs-play-control      neither (checked on a live page)

On /modules/audio/ frames all three report 0, so an audio task point can never be
started - and 大学英语B1 is an audio course. This finds what IS clickable there.

Read-only.

    .venv\\Scripts\\python.exe -X utf8 probe_audio_controls.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from bank import ensure_utf8_stdout          # noqa: E402
from cx_auto import Session                  # noqa: E402

ensure_utf8_stdout()

CID = sys.argv[1] if len(sys.argv) > 1 else "237168226"   # 大学英语B1

JS = r"""() => {
    const out = {url: location.href.slice(0, 90), buttons: [], clickable: [], html: ''};
    const els = document.querySelectorAll('button, a, div[role=button], i, span');
    els.forEach(el => {
        const cls = (el.className && el.className.toString()) || '';
        const id  = el.id || '';
        const txt = (el.innerText || '').trim().slice(0, 14);
        const r = el.getBoundingClientRect();
        const big = r.width > 12 && r.height > 12;
        if (!big) return;
        const tag = el.tagName.toLowerCase();
        if (tag === 'button' || el.getAttribute('role') === 'button' ||
            /play|pause|btn|control|vjs|prism|icon/i.test(cls + ' ' + id)) {
            out.buttons.push({tag, cls: cls.slice(0, 70), id: id.slice(0, 24),
                              txt, w: Math.round(r.width), h: Math.round(r.height)});
        }
    });
    out.buttons = out.buttons.slice(0, 18);
    const v = document.querySelector('video,audio');
    if (v) { out.mediaTag = v.tagName.toLowerCase();
             out.mediaCls = (v.className || '').toString().slice(0, 70); }
    return out;
}"""


async def main() -> int:
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg.update({"COURSE_ID": CID, "DO_VIDEO": False, "DO_QUIZ": False,
                "HEADLESS": True, "MAX_CHAPTERS": 3})
    sess = Session(cfg)
    await sess.start()
    try:
        await sess.login()
        chapters = await sess.chapters()
        for i, url in enumerate(chapters[:3], 1):
            await sess.open_chapter(url, count_attempt=False)
            print(f"\n=== [{i}] {url[:100]}")
            for fr in sess.page.frames:
                u = fr.url or ""
                if "/modules/audio/" not in u:
                    continue
                try:
                    info = await fr.evaluate(JS)
                except Exception as exc:
                    print(f"    evaluate failed: {type(exc).__name__}")
                    continue
                print(f"    frame: {info['url']}")
                print(f"    media: {info.get('mediaTag')}  class={info.get('mediaCls')!r}")
                print(f"    候选控件 {len(info['buttons'])} 个:")
                for b in info["buttons"]:
                    print(f"      <{b['tag']}> {b['w']}x{b['h']} txt={b['txt']!r} "
                          f"id={b['id']!r} class={b['cls']!r}")
    finally:
        await sess.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
