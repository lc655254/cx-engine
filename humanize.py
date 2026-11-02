# -*- coding: utf-8 -*-
"""Make the automation behave less like a robot.

The platform's notice complained about "软件刷课" (software course-farming). What gives
that away is not one single request but the SHAPE of the traffic: actions landing on
exact second boundaries, a browser that is perfectly active for hours with no idle,
a mouse that never moves, and a fixed number of requests per minute.

This module supplies the countermeasures, all of them cheap:

  pause()        waits with jitter, never a constant interval
  idle()         nudges the pointer and scrolls a little, the way a person does
  take_break()   stops for minutes at a time, because nobody studies non-stop
  budget         caps how many task points are completed per day
  active_hours() keeps work inside plausible waking hours

    from humanize import pause, idle, take_break, Budget

    await pause(page, 6, 3)          # 4.5-7.5s
    await idle(page)                 # a couple of pointer nudges
    await take_break(page, log)      # occasionally a few minutes off
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import time
from typing import Any, Dict, Optional

ROOT = os.path.dirname(os.path.abspath(__file__))
BUDGET_FILE = os.path.join(ROOT, "logs", "daily_budget.json")


# --------------------------------------------------------------------- timing
async def pause(page, base: float, jitter: float = 0.0, minimum: float = 0.4) -> float:
    """Sleep for base +/- jitter seconds. Returns what was actually slept."""
    secs = max(minimum, base + random.uniform(-jitter, jitter))
    # a little sub-second noise so nothing lands on a round number
    secs += random.uniform(0.0, 0.35)
    await page.wait_for_timeout(int(secs * 1000))
    return secs


def human_delay(base: float, spread: float = 0.35) -> float:
    """A delay drawn from a slightly skewed distribution (people are not gaussians)."""
    lo, hi = base * (1 - spread), base * (1 + spread)
    d = random.triangular(lo, hi, lo + (hi - lo) * random.random())
    return max(0.2, d)


async def idle(page, moves: int = 0) -> None:
    """A couple of harmless pointer nudges and a small scroll.

    During video playback the page would otherwise be frozen for minutes with zero
    input, which is exactly what a headless farmer looks like.
    """
    if moves <= 0:
        moves = random.randint(1, 3)
    try:
        vp = page.viewport_size or {"width": 1400, "height": 900}
    except Exception:
        vp = {"width": 1400, "height": 900}
    for _ in range(moves):
        try:
            x = random.randint(int(vp["width"] * 0.25), int(vp["width"] * 0.75))
            y = random.randint(int(vp["height"] * 0.25), int(vp["height"] * 0.75))
            # walk there in a few steps rather than teleporting
            for step in range(random.randint(2, 4)):
                await page.mouse.move(
                    x + random.randint(-25, 25), y + random.randint(-20, 20),
                    steps=random.randint(3, 9))
                await page.wait_for_timeout(random.randint(80, 260))
            if random.random() < 0.35:
                await page.mouse.wheel(0, random.choice([-260, -140, 140, 260]))
                await page.wait_for_timeout(random.randint(300, 900))
        except Exception:
            continue


async def take_break(page, log=print, base_min: float = 3.0,
                     spread_min: float = 4.0) -> float:
    """Step away for a few minutes. Returns the minutes actually taken."""
    mins = max(1.0, base_min + random.random() * spread_min)
    log(f"humanize: break {mins:.1f} min (a student does not study non-stop)")
    # during the break, prod the page occasionally so the session stays warm but the
    # request rate collapses
    end = time.time() + mins * 60
    while time.time() < end:
        chunk = min(random.uniform(45, 120), end - time.time())
        if chunk > 0:
            await page.wait_for_timeout(int(chunk * 1000))
        try:
            await idle(page, moves=1)
        except Exception:
            pass
    return mins


# --------------------------------------------------------------------- budget
class _BudgetLock:
    """Cross-process mutual exclusion for the budget file.

    msvcrt.locking blocks and retries on Windows, which is where this runs. The
    O_EXCL fallback covers other platforms; a lock older than 30s is treated as
    stale (a crashed holder) and broken, so a dead worker cannot wedge the day.
    """

    def __init__(self, path: str):
        self.path = path + ".lock"
        self.fh = None

    def __enter__(self):
        try:
            import msvcrt
            # "a+" positions at end-of-file; the lock covers nbytes from THERE, so
            # without this seek two processes can lock different regions and the
            # exclusion quietly stops working.
            self.fh = open(self.path, "a+")
            try:
                self.fh.seek(0)
            except Exception:
                pass
            for _ in range(200):                      # ~20s at 0.1s
                try:
                    self.fh.seek(0)
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_LOCK, 1)
                    return self
                except OSError:
                    time.sleep(0.1)
            raise TimeoutError("budget lock not acquired")
        except ImportError:
            # POSIX: atomic create, with staleness breaking
            start = time.time()
            while True:
                try:
                    fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                    os.close(fd)
                    return self
                except FileExistsError:
                    try:
                        if time.time() - os.path.getmtime(self.path) > 30:
                            os.remove(self.path)
                            continue
                    except Exception:
                        pass
                    if time.time() - start > 20:
                        return self                   # proceed rather than stall
                    time.sleep(0.1)

    def __exit__(self, *exc):
        try:
            import msvcrt
            if self.fh:
                try:
                    self.fh.seek(0)
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
                except Exception:
                    pass
                self.fh.close()
        except ImportError:
            try:
                os.remove(self.path)
            except Exception:
                pass
        return False


class Budget:
    """Per-day cap on completed task points, so no single day looks superhuman."""

    def __init__(self, daily_cap: int = 60, log=print):
        self.cap = max(1, daily_cap)
        self.log = log
        self.data: Dict[str, Any] = {}
        self._load()

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d")

    def _load(self) -> None:
        try:
            with open(BUDGET_FILE, encoding="utf-8") as fh:
                self.data = json.load(fh) or {}
        except Exception:
            self.data = {}
        if self.data.get("date") != self._today():
            self.data = {"date": self._today(), "done": 0, "sessions": 0}

    def _save(self) -> None:
        """Atomic write: a concurrent reader never sees a half-written file."""
        try:
            os.makedirs(os.path.dirname(BUDGET_FILE), exist_ok=True)
            tmp = BUDGET_FILE + f".{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, BUDGET_FILE)     # atomic on Windows and POSIX
        except Exception:
            pass

    def _reread(self) -> dict:
        """Current on-disk record, or {} when unreadable/corrupt."""
        try:
            with open(BUDGET_FILE, encoding="utf-8") as fh:
                d = json.load(fh)
            return d if isinstance(d, dict) and d.get("date") == self._today() else {}
        except Exception:
            return {}

    def new_session(self) -> None:
        self.data["sessions"] = self.data.get("sessions", 0) + 1
        self._save()

    @property
    def done(self) -> int:
        return int(self.data.get("done", 0))

    @property
    def left(self) -> int:
        return max(0, self.cap - self.done)

    def allow(self) -> bool:
        return self.left > 0

    def record(self, n: int = 1) -> None:
        """Add progress without clobbering a concurrent writer.

        Re-read the file first: three workers each hold a private copy, and writing
        back a stale `done` would erase whatever the others just recorded.
        """
        with _BudgetLock(BUDGET_FILE):
            fresh = self._reread()
            base = max(int(fresh.get("done", 0) or 0),
                       int(self.data.get("done", 0) or 0))
            self.data["done"] = base + n
            self.data["date"] = self._today()
            self.data["sessions"] = max(
                int(fresh.get("sessions", 0) or 0),
                int(self.data.get("sessions", 0) or 0))
            self._save()

    def summary(self) -> str:
        return (f"今日已完成 {self.done}/{self.cap} 个任务点，"
                f"剩余额度 {self.left}，会话数 {self.data.get('sessions', 0)}")


# ----------------------------------------------------------------- active hours
def in_active_hours(start_hour: int = 8, end_hour: int = 23) -> bool:
    """True when the local clock is inside the study window.

    The end hour is EXCLUSIVE: `<= end_hour` made 23:00-23:59 count as waking time, so
    the configured 07:00-23:00 window silently ran until midnight. schedule_resume's
    decide() already used `<`, so the two disagreed.

    An end_hour above 23 expresses an overnight window (07:00-05:00 -> end=29):
    hours before start_hour are shifted by +24 so 00:xx-04:xx counts as 24-28.
    2026-10-19: user asked to let the batch continue past 23:00 for the night.
    """
    h = time.localtime().tm_hour
    if h < start_hour:
        h += 24
    return start_hour <= h < end_hour


def seconds_until_active(start_hour: int = 8) -> int:
    now = time.localtime()
    if now.tm_hour < start_hour:
        return (start_hour - now.tm_hour) * 3600 - now.tm_min * 60
    return 0


if __name__ == "__main__":
    b = Budget(daily_cap=60)
    print(b.summary())
    print(f"当前是否活跃时段: {in_active_hours()}")
    print(f"pause(3,1) 会睡 {human_delay(3):.2f}s 左右")
