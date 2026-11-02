# -*- coding: utf-8 -*-
"""Find out WHERE the media frame goes when it disappears mid-playback.

diag_video_state showed playback running at a clean 1x (+5s per 5s, ready=4,
paused=false) and then, at ~50s, both the captured handle AND a fresh re-resolution
failing - the frame is genuinely gone, not merely stale.

That is a different problem from the one fixed in round 42, so this records the page
after the loss: the top URL, every remaining frame URL, and the visible text, which
together say whether the page navigated, the player was torn down, or the platform
replaced the document.

    .venv\\Scripts\\python.exe -X utf8 diag_frame_loss.py <courseId>
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

CID = sys.argv[1] if len(sys.argv) > 1 else "267087606"
MEDIA_HINTS = ("/modules/video/", "/modules/audio/", "/ananas/modules/")


def media_frames(page):
    return [f for f in page.frames if any(h in (f.url or "") for h in MEDIA_HINTS)]


async def snapshot(sess, label: str, before_urls: set) -> None:
    page = sess.page
    print(f"\n  ===== {label} =====")
    try:
        print(f'  顶层 URL 完整:')
        print('   ', page.url)
    except Exception as exc:
        print(f"  顶层 URL 读取失败: {type(exc).__name__}")
    try:
        frames = page.frames
        print(f"  frame 总数: {len(frames)}")
        for fr in frames:
            u = fr.url or "(about:blank)"
            mark = "  <-- 媒体" if any(h in u for h in MEDIA_HINTS) else ""
            print(f"    {u[:95]}{mark}")
    except Exception as exc:
        print(f"  frame 列举失败: {type(exc).__name__}")
    try:
        txt = await page.evaluate(
            '() => (document.body.innerText || "").replace(/\\s+/g," ").slice(0,300)')
        print(f"  页面文字: {txt[:220]}")
    except Exception as exc:
        print(f"  页面文字读取失败: {type(exc).__name__}")


async def main() -> int:
    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    cfg.update({"COURSE_ID": CID, "DO_VIDEO": False, "DO_QUIZ": False,
                "HEADLESS": True, "MAX_NODE_TRIES": 99})

    sess = Session(cfg)
    await sess.start()
    try:
        await sess.login()
        chapters = await sess.chapters()
        await sess.open_chapter(chapters[0], count_attempt=False)
        frames = media_frames(sess.page)
        if not frames:
            print("  没有媒体 frame")
            return 1
        frame = frames[0]
        before = {f.url for f in sess.page.frames}
        print(f"  媒体 frame: {frame.url[:90]}")

        started = await tp.start_video(frame, sess.page)
        print(f"  启动 ok={started.get('ok')} dur={started.get('dur')}")
        if not started.get("ok"):
            return 1

        lost_at = None
        t0 = time.time()
        while time.time() - t0 < 150:
            await sess.page.wait_for_timeout(5000)
            el = int(time.time() - t0)
            live = media_frames(sess.page)
            cur = None
            if live:
                try:
                    st = await live[0].evaluate(tp.VIDEO_STATE)
                    cur = (st or {}).get("cur")
                except Exception:
                    cur = "err"
            if not live and lost_at is None:
                lost_at = el
                print(f"\n  *** {el}s: 媒体 frame 消失了 ***")
                await snapshot(sess, f"{el}s 状态", before)
                break
        if lost_at is None:
            print(f"\n  {int(time.time()-t0)}s 内 frame 未消失（末次 cur={cur}）")
        return 0
    finally:
        await sess.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
