# -*- coding: utf-8 -*-
"""Read-only status check: log in with the stored credentials and report progress.

Nothing here touches a task point. It exists so the account's real state can be seen
after the platform's "software farming" notice, which said learning records had been
cleared.

    .venv\\Scripts\\python.exe -X utf8 status_check.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bank import ensure_utf8_stdout   # noqa: E402
from cx_auto import Session           # noqa: E402

ensure_utf8_stdout()

ROOT = os.path.dirname(os.path.abspath(__file__))


async def main() -> int:
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg.update({"DO_VIDEO": False, "DO_QUIZ": False, "HEADLESS": True})

    sess = Session(cfg)
    await sess.start()
    sess._session_loaded = False          # force the credential path
    try:
        await sess.login()
    except SystemExit as exc:
        print(f"LOGIN_FAIL: {exc}")
        await sess.close()
        return 2

    ok = await sess._is_logged_in()
    print(f"LOGIN_OK: {ok}")
    if ok:
        # dump the notice area of the course list, in case the platform is showing one
        try:
            page = sess.page
            await page.goto("https://mooc1.chaoxing.com/visit/interaction",
                            wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(5000)
            txt = await page.evaluate(
                '() => (document.body.innerText || "").replace(/\\s+/g, " ")')
            for kw in ("刷课", "不良记录", "清空", "不及格", "重修", "舞弊"):
                if kw in txt:
                    i = txt.find(kw)
                    print(f"NOTICE[{kw}]: ...{txt[max(0, i - 60):i + 160]}...")
                    break
        except Exception as exc:
            print(f"notice scan failed: {type(exc).__name__}")

    await sess.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
