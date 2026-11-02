# -*- coding: utf-8 -*-
"""Prove the media path works end to end, on one real node.

Tonight's batch died before reaching a single media node, so the path that produces the
bulk of the remaining points (start_video -> hold_playback -> progress reporting) has
never been exercised since the audio-selector, hold_playback and browser-retry changes.

This drives ONE node through the same code the worker uses and reports what the server
says afterwards. It deliberately does not go through run(), whose 07:00-23:00 gate would
stop it - one node is a few minutes of activity, not a night of it.

    .venv\\Scripts\\python.exe -X utf8 verify_media_once.py <courseId> [index]
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from bank import ensure_utf8_stdout              # noqa: E402
from cx_auto import Session                      # noqa: E402

ensure_utf8_stdout()

CID = sys.argv[1] if len(sys.argv) > 1 else "254215593"
IDX = int(sys.argv[2]) if len(sys.argv) > 2 else 0


async def main() -> int:
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg.update({"COURSE_ID": CID, "DO_VIDEO": True, "DO_QUIZ": True,
                "QUIZ_SUBMIT": True, "HEADLESS": True,
                "MAX_NODE_TRIES": 99})   # do not park during a diagnostic

    sess = Session(cfg)
    await sess.start()
    try:
        await sess.login()
        chapters = await sess.chapters()
        print(f"  {CID}: {len(chapters)} 个未完成节点")
        if not chapters:
            print("  没有未完成节点")
            return 0

        url = chapters[min(IDX, len(chapters) - 1)]
        print(f"  目标节点: {url[:110]}")
        print()
        await sess.open_chapter(url, count_attempt=False)
        print("  --- do_video_all_tabs() ---")
        res = await sess.do_video_all_tabs()
        print(f"  结果: {res}")
        print()
        print(f"  统计: {sess.stats}")
        return 0
    finally:
        await sess.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
