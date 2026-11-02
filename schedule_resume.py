# -*- coding: utf-8 -*-
"""Wait for the daily task budget to reset, then start the supervisor.

The budget is capped at 60 task points per calendar day, so from the moment it is
exhausted until midnight the runners deliberately do nothing. Rather than leave that
gap unused, this waits for the date to roll over and then launches supervise.py.

    .venv\\Scripts\\python.exe -X utf8 schedule_resume.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
BUDGET = os.path.join(ROOT, "logs", "daily_budget.json")


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def budget_state() -> tuple:
    """(date, done, cap). cap comes from config so this agrees with the runners."""
    cap = 60
    try:
        with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as fh:
            cap = int(json.load(fh).get("DAILY_TASK_CAP", 60))
    except Exception:
        pass
    try:
        with open(BUDGET, encoding="utf-8") as fh:
            d = json.load(fh)
        return d.get("date", ""), int(d.get("done", 0)), cap
    except Exception:
        return "", 0, cap


def in_active_hours(start_hour: int = 0, end_hour: int = 24) -> bool:
    """Is the wall clock inside plausible study hours?

    The budget resets at midnight, so without this the runners would resume at 00:00
    and grind through the small hours. Work is confined to a window; an end_hour
    above 23 expresses an overnight window (07:00-05:00 -> end=29) because the user
    asked on 2026-10-19 to run continuously with no window; (0, 24) means always active.
    """
    h = time.localtime().tm_hour
    if h < start_hour:
        h += 24
    return start_hour <= h < end_hour


def decide(stored_date: str, done: int, cap: int, hour: int,
           today: str, start_hour: int = 0, end_hour: int = 24) -> str:
    """Return 'run' or 'wait' for the scheduler.

    Pure so it can be tested: this is the gate that decides whether a whole day of
    work happens, and a mistake here fails silently (the process just sleeps).
    """
    budget_ok = (stored_date != today) or (done < cap)
    hh = hour + 24 if hour < start_hour else hour
    hours_ok = start_hour <= hh < end_hour
    if not (budget_ok and hours_ok):
        return "wait"
    # An anomaly warning stops the day. Checked here as well as in the worker so a
    # flagged account is not restarted every 5 minutes by the supervisor loop.
    try:
        import anomaly
        if anomaly.blocks_today():
            return "wait"
    except Exception:
        pass
    return "run"


def main() -> int:
    log("resume scheduler started")
    while True:
        date, done, cap = budget_state()
        today = time.strftime("%Y-%m-%d")
        hour = time.localtime().tm_hour
        verdict = decide(date, done, cap, hour, today)
        budget_ok = (date != today) or (done < cap)
        hours_ok = in_active_hours()

        if budget_ok and hours_ok:
            # Gate the day on a preflight check. Several rounds were spent today
            # discovering code paths that were broken (a signature mismatch that
            # failed every video node, a duplicated quiz implementation that could
            # not fill blanks). Each would have been caught in seconds before the
            # runners started, so nothing launches unless the check passes.
            log("running preflight before launching")
            try:
                pf = subprocess.run([PY, "-X", "utf8", "preflight.py"], cwd=ROOT,
                                    capture_output=True, text=True, timeout=900)
                if pf.returncode != 0:
                    tail = (pf.stdout or "").strip().splitlines()[-4:]
                    log("PREFLIGHT FAILED - not starting. Last lines:")
                    for line in tail:
                        log(f"    {line}")
                    log("retrying in 15 minutes")
                    time.sleep(900)
                    continue
                log("preflight passed")
            except Exception as exc:
                log(f"preflight could not run ({type(exc).__name__}); "
                    f"proceeding anyway")

            log(f"budget available (stored date={date} done={done}/{cap}) and it is "
                f"a plausible hour; launching the supervisor")
            try:
                subprocess.run([PY, "-X", "utf8", "supervise.py"], cwd=ROOT)
            except Exception as exc:
                log(f"supervise.py failed: {type(exc).__name__}")
            log("supervisor returned; re-checking in 60s")
            time.sleep(60)
            continue

        try:
            import anomaly as _a
            if _a.blocks_today():
                rec = _a.is_set() or {}
                log(f"ANOMALY FLAG for today ({rec.get('markers')}) - not starting. "
                    f"Look at logs/anomaly.json, then delete it to resume.")
                time.sleep(300)
                continue
        except Exception:
            pass
        if not budget_ok:
            log(f"capped for today ({done}/{cap}) - sleeping 300s")
        else:
            h = time.localtime().tm_hour
            # Outside the window with budget to spare means the day is over: finish,
            # so the scheduled task instance ends and tomorrow's starts cleanly. The
            # old code slept forever, so an instance started at 16:00 was still alive
            # at 07:00 and blocked the next day's run (MultipleInstances=IgnoreNew).
            log(f"budget is available but it is {h:02d}:xx, outside the active "
                f"window - finishing for the day")
            return 0
        time.sleep(300)


if __name__ == "__main__":
    raise SystemExit(main())
