# -*- coding: utf-8 -*-
"""Verify the audio fix: run the REAL VIDEO_STATE snippet inside a live audio frame.

Static inspection says the selector is now 'video,audio'. That is not the same as the
production snippet working on a real audio frame, which is the thing that was broken.
This loads an audio node, grabs VIDEO_STATE straight out of task_points, and runs it in
the /modules/audio/ frame.

Read-only: uses count_attempt=False, so no node is parked, and nothing is played.

    .venv\\Scripts\\python.exe -X utf8 verify_audio_fix.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from bank import ensure_utf8_stdout          # noqa: E402
from cx_auto import Session                  # noqa: E402
import task_points as tp                     # noqa: E402

ensure_utf8_stdout()

CID = sys.argv[1] if len(sys.argv) > 1 else "237168226"   # 大学英语B1

# the exact snippet the production code evaluates
STATE = getattr(tp, "VIDEO_STATE", None)
if STATE is None:
    import re
    src = open(os.path.join(ROOT, "task_points.py"), encoding="utf-8").read()
    STATE = re.search(r'VIDEO_STATE\s*=\s*r"""(.*?)"""', src, re.S).group(1)


async def main() -> int:
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg.update({"COURSE_ID": CID, "DO_VIDEO": False, "DO_QUIZ": False,
                "HEADLESS": True, "MAX_CHAPTERS": 3})

    print("VIDEO_STATE 里是否已包含 audio:",
          "video,audio" in STATE or "audio" in STATE)
    print()

    sess = Session(cfg)
    await sess.start()
    ok_any = False
    try:
        await sess.login()
        chapters = await sess.chapters()
        for i, url in enumerate(chapters[:3], 1):
            await sess.open_chapter(url, count_attempt=False)   # never parks
            print(f"=== [{i}] {url[:88]}")
            for fr in sess.page.frames:
                u = fr.url or ""
                if "/modules/audio/" not in u:
                    continue
                try:
                    st = await fr.evaluate(STATE)
                except Exception as exc:
                    print(f"    evaluate failed: {type(exc).__name__}")
                    continue
                if st is None:
                    print("    VIDEO_STATE -> None   （修复未生效）")
                else:
                    ok_any = True
                    print(f"    VIDEO_STATE -> ready={st.get('ready')} "
                          f"dur={st.get('dur')} cur={st.get('cur')} "
                          f"paused={st.get('paused')} rate={st.get('rate')}"
                          "   （修复生效：音频帧现在能被读到）")
    finally:
        await sess.close()

    print()
    print("结论:", "音频帧可以被状态读取了" if ok_any else "仍读不到音频帧")
    return 0 if ok_any else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
