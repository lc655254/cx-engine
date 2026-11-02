# -*- coding: utf-8 -*-
"""Stop the day when the platform shows an anomaly warning.

Objective item (4): "出现异常标记当天停手". That was never implemented - the workers
had no idea whether the platform was displaying a warning, so they would keep running
straight through one.

This module gives the runners a shared check plus a persistent one-day stop flag:

    if await anomaly.check(page):     # page shows a warning -> raise the flag
        return                        # caller should stop for the day

    anomaly.is_set()                  # scheduler refuses to start while set
    anomaly.clear()                   # the human clears it after dealing with it

The flag is keyed by date, so it lapses automatically the next day rather than needing
manual cleanup - but the scheduler will not start a new day's work until the previous
day's warning has been seen.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import List, Optional

ROOT = os.path.dirname(os.path.abspath(__file__))
FLAG = os.path.join(ROOT, "logs", "anomaly.json")

# Phrases that mean the platform has noticed something. Kept narrow on purpose: a
# false positive stops a whole day's work, so only unambiguous wording is matched.
MARKERS = [
    "软件刷课",          # the notice seen on 2026-10-14
    "不良记录",
    "学习记录已全部清空",
    "学习异常",
    "已将学习记录清空",
    "以作舞弊论处",
    "刷课行为",
    "系统检测到异常",
    "账号异常",
    "禁止使用第三方",
    "请勿使用脚本",
    "违规学习",
]

# Checking every page for a dozen strings is cheap, but compiling once is cheaper.
_PAT = re.compile("|".join(re.escape(m) for m in MARKERS))


def scan(text: str) -> List[str]:
    """Return the marker phrases present in the given page text."""
    if not text:
        return []
    return sorted({m for m in MARKERS if m in text})


def is_set() -> Optional[dict]:
    """Return the stop record if the flag is raised, else None."""
    try:
        with open(FLAG, encoding="utf-8") as fh:
            d = json.load(fh) or {}
        return d if d.get("date") else None
    except Exception:
        return None


def raise_flag(markers: List[str], where: str = "", note: str = "") -> dict:
    """Raise the stop flag for the current day."""
    rec = {"date": time.strftime("%Y-%m-%d"),
           "at": time.strftime("%H:%M:%S"),
           "markers": markers,
           "where": where[:200],
           "note": note[:200]}
    try:
        os.makedirs(os.path.dirname(FLAG), exist_ok=True)
        with open(FLAG, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False, indent=1)
    except Exception:
        pass
    return rec


def clear() -> bool:
    try:
        os.remove(FLAG)
        return True
    except Exception:
        return False


def blocks_today() -> bool:
    """True when work should not start today."""
    rec = is_set()
    if not rec:
        return False
    return rec.get("date") == time.strftime("%Y-%m-%d")


async def check(page, log=print, where: str = "") -> List[str]:
    """Look at the current page for anomaly markers; raise the flag if found.

    Returns the markers found (empty list = nothing wrong).
    """
    try:
        text = await page.evaluate(
            '() => (document.body.innerText || "").slice(0, 4000)')
    except Exception:
        return []
    found = scan(text or "")
    if found:
        rec = raise_flag(found, where=where or (page.url or "")[:160])
        log("=" * 66)
        log("ANOMALY DETECTED - stopping for the day")
        log(f"  markers : {found}")
        log(f"  where   : {rec['where'][:90]}")
        log(f"  flag    : logs/anomaly.json  (delete it once you have looked)")
        log("=" * 66)
    return found


if __name__ == "__main__":
    import sys
    if "--clear" in sys.argv:
        print("cleared" if clear() else "nothing to clear")
    else:
        rec = is_set()
        if rec:
            print(json.dumps(rec, ensure_ascii=False, indent=1))
            print(f"blocks today: {blocks_today()}")
        else:
            print("no anomaly flag set")
            print(f"markers watched: {len(MARKERS)}")
