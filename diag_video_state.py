# -*- coding: utf-8 -*-
"""Watch one video node for two minutes and print the state every 5 seconds.

The single-node check passed (dur=314 cur=314 -> ok) but the same code fails inside the
batch (dur=1527 cur=71 -> no_state), so the difference is the environment, not the
logic. This records what actually happens to the element over time: does playback
advance and then stop, does the element disappear, or does the evaluate start throwing?

Read-only beyond starting the clip.

    .venv\\Scripts\\python.exe -X utf8 diag_video_state.py <courseId> [seconds]
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from bank import ensure_utf8_stdout          # noqa: E402
from cx_auto import Session                  # noqa: E402
import task_points as tp                     # noqa: E402

ensure_utf8_stdout()

CID = sys.argv[1] if len(sys.argv) > 1 else "267511667"
SECONDS = int(sys.argv[2]) if len(sys.argv) > 2 else 120

STATE = tp.VIDEO_STATE


async def main() -> int:
    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    cfg.update({"COURSE_ID": CID, "DO_VIDEO": False, "DO_QUIZ": False,
                "HEADLESS": True, "MAX_NODE_TRIES": 99})

    sess = Session(cfg)
    await sess.start()
    try:
        await sess.login()
        chapters = await sess.chapters()
        url = chapters[0]
        print(f"  节点: {url[:100]}")
        await sess.open_chapter(url, count_attempt=False)

        # locate the media frame the way production does, then start playback
        media = [f for f in sess.page.frames
                 if any(h in (f.url or "") for h in
                        ("/modules/video/", "/modules/audio/", "/ananas/modules/"))]
        print(f"  媒体 frame 数: {len(media)}")
        if not media:
            print("  没有媒体 frame")
            return 1
        frame = media[0]
        print(f"  frame url: {frame.url[:90]}")

        started = await tp.start_video(frame, sess.page)
        print(f"  启动: ok={started.get('ok')} dur={started.get('dur')} "
              f"via={started.get('via')}")
        if not started.get("ok"):
            print("  启动失败")
            return 1

        dur = started["dur"]
        print()
        print(f"  {'时间':>6}  {'cur':>7} {'dur':>7} {'ready':>6} {'paused':>7} "
              f"{'frame?':>7}  说明")
        t0 = time.time()
        last = -1
        while time.time() - t0 < SECONDS:
            await sess.page.wait_for_timeout(5000)
            el = int(time.time() - t0)
            # a: the captured handle
            a, aerr = None, ""
            try:
                a = await frame.evaluate(STATE)
            except Exception as exc:
                aerr = type(exc).__name__
            # b: re-resolved
            b = None
            for fr in sess.page.frames:
                if any(h in (fr.url or "") for h in
                       ("/modules/video/", "/modules/audio/", "/ananas/modules/")):
                    try:
                        b = await fr.evaluate(STATE)
                        if b:
                            break
                    except Exception:
                        continue
            show = b or a
            delta = ""
            if show:
                cur = show.get("cur") or 0
                delta = f"+{cur - last:.0f}s" if last >= 0 else ""
                last = cur
            print(f"  {el:>5}s  "
                  f"{(a or {}).get('cur', '-'):>7} {(show or {}).get('dur', '-'):>7} "
                  f"{(show or {}).get('ready', '-'):>6} "
                  f"{str((show or {}).get('paused')):>7} "
                  f"{('yes' if b else 'no'):>7}  "
                  f"handle={'err:' + aerr if aerr else ('ok' if a else 'null')} {delta}")
            if show and show.get("ended"):
                print("  -> 播放到结尾")
                break
        return 0
    finally:
        await sess.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
