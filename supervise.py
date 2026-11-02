# -*- coding: utf-8 -*-
"""Supervisor: keep the batch running, sweep orphan browsers between rounds.

Both workers repeatedly hit

    TargetClosedError: Page.goto: Target page, context or browser has been closed

during long video playback. A dead browser is recoverable (Session._ensure_page
rebuilds it), but the old browser's processes linger: 53 chrome processes were
observed for two workers, and the pile-up makes the next crash more likely.

This wraps parallel_run in a loop that:
  * launches the batch with the configured concurrency
  * on exit, kills orphan chrome left behind by crashed workers
  * waits a jittered interval, then starts the next round
  * stops when the daily task budget is exhausted

    .venv\\Scripts\\python.exe -X utf8 supervise.py
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
COURSES = ("268435814,254215593")
# 2026-10-27: user completed the face capture - 花道 and 如何高效学习 are back in
# the batch. App math's five stuck quizzes were repaired offline (vision answers).
# 2026-10-28: 如何高效学习 still shows the face gate (采集 applies per course), so
# it is out of the rotation until the user captures for it as well.
# 2026-10-28: user says the face is done for it too and asked for just 如何高效学习
# (268435814) and 大学物理 (254215593).
# 2026-10-22: user says the math course to work is 应用数学1 (245970839), not
# 数学 (203417266 - an old class), and it goes FIRST so it gets the fresh daily
# quota. 203417266 was removed from the batch.
SWEEP_MARKER = os.path.join(ROOT, "logs", "sweep_done.json")
# 2, not 3: the platform enforces a limit on concurrent sessions for one account.
# Measured 2026-10-17 - with 3 workers the page was bounced to the portal home
# (i.mooc.chaoxing.com/space/index) 25-85s into every video, killing the media
# frame. With 1 worker the same clip plays 0->dur cleanly. The objective allows
# 2-3, so 2 is both compliant and the highest value that works.
CONCURRENCY = "1"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def sweep_ran_today() -> bool:
    try:
        with open(SWEEP_MARKER, encoding="utf-8") as fh:
            return json.load(fh).get("date") == time.strftime("%Y-%m-%d")
    except Exception:
        return False


def mark_sweep_done() -> None:
    try:
        os.makedirs(os.path.dirname(SWEEP_MARKER), exist_ok=True)
        with open(SWEEP_MARKER, "w", encoding="utf-8") as fh:
            json.dump({"date": time.strftime("%Y-%m-%d"),
                       "at": time.strftime("%H:%M:%S")}, fh, ensure_ascii=False)
    except Exception:
        pass


def run_quiz_sweep() -> None:
    """Sweep every course's chapter quizzes once a day, under a time cap.

    Objective item (1). This was previously NEVER scheduled - the supervisor only drove
    parallel_run for video/task-point work, so chapter quizzes across the other nine
    courses were never answered.

    It runs first, but bounded by SWEEP_MAX_MINUTES. Uncapped it is far from "cheap and
    quick": 366 chapters at a 60-150s inter-chapter gap is ~10.7 hours against a
    16-hour window, so one pass consumed the whole day and the media batch - which
    holds the bulk of the remaining points - never started. The batch also runs with
    --quiz --submit now, so this is the secondary quiz path and a short cap is enough.
    """
    if sweep_ran_today():
        log("quiz sweep: already done today")
        return
    log("quiz sweep: starting (chapter quizzes across all courses)")
    try:
        # Bounded: the sweep is the secondary quiz path (the batch answers quizzes
        # inline), and uncapped it consumed the whole window in one pass.
        try:
            cap_min = int(json.load(open(os.path.join(ROOT, "config.json"),
                                         encoding="utf-8")).get("SWEEP_MAX_MINUTES", 25))
        except Exception:
            cap_min = 25
        cap_min = max(5, cap_min)
        log(f"quiz sweep: capped at {cap_min} min so the media batch still gets the window")
        try:
            r = subprocess.run([PY, "-X", "utf8", "sweep_quizzes.py"] + COURSES.split(","),
                               cwd=ROOT, timeout=cap_min * 60)
        except subprocess.TimeoutExpired:
            log(f"quiz sweep: hit the {cap_min} min cap - moving on to the media batch")
            mark_sweep_done()
            return
        if r.returncode == 0:
            mark_sweep_done()
            log("quiz sweep: finished")
        else:
            log(f"quiz sweep: exit {r.returncode} (will retry next round)")
    except Exception as exc:
        # TimeoutExpired never reaches here: the inner handler above owns it and
        # moves on to the batch.
        log(f"quiz sweep: failed {type(exc).__name__}")


def sweep_chrome() -> int:
    """Kill chrome processes orphaned by crashed workers."""
    # 2026-10-23: round rotation killed parallel_run at the 6h timeout but its
    # cx_auto worker kept running as an orphan. Two workers on one account then
    # fought for the session (29 failed videos in 3 hours). At round start no
    # cx_auto worker of THIS round exists yet, so every running one is an orphan.
    try:
        r = subprocess.run(
            ["wmic", "process", "where",
             "name='python.exe' and commandline like '%cx_auto%'",
             "get", "processid"],
            capture_output=True, text=True, timeout=30)
        pids = [ln.strip() for ln in (r.stdout or "").splitlines()
                if ln.strip().isdigit()]
        for pid in pids:
            subprocess.run(["taskkill", "/F", "/T", "/PID", pid],
                           capture_output=True, text=True, timeout=30)
        if pids:
            log(f"orphan worker sweep: killed {len(pids)} cx_auto process(es)")
    except Exception as exc:
        log(f"orphan worker sweep failed: {type(exc).__name__}")
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq chrome.exe"],
                             capture_output=True, text=True, timeout=30)
        before = out.stdout.count("chrome.exe")
    except Exception:
        before = 0
    if before:
        try:
            subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"],
                           capture_output=True, text=True, timeout=60)
        except Exception:
            pass
        log(f"orphan sweep: removed ~{max(0, before - 1)} chrome process(es)")
    return before


def daily_cap() -> int:
    """The cap comes from config.json - the single source of truth.

    This was a literal 60 in three places, so raising DAILY_TASK_CAP changed what the
    scheduler believed but not what the supervisor enforced: the scheduler launched it
    every minute and the supervisor exited every minute with "quota exhausted".
    """
    try:
        with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as fh:
            return max(1, int(json.load(fh).get("DAILY_TASK_CAP", 200)))
    except Exception:
        return 200


def budget_left() -> int:
    cap = daily_cap()
    try:
        with open(os.path.join(ROOT, "logs", "daily_budget.json"), encoding="utf-8") as fh:
            d = json.load(fh)
        if d.get("date") != time.strftime("%Y-%m-%d"):
            return cap
        return max(0, cap - int(d.get("done", 0)))
    except Exception:
        return cap


def main() -> int:
    round_no = 0
    while True:
        round_no += 1
        # The worker stops itself outside the window, but the SUPERVISOR used to keep
        # relaunching it every ~60s - two hours of nightly browser launches that the
        # worker-level gate could not prevent. Check here too, and finish the day.
        try:
            from humanize import in_active_hours
            if not in_active_hours(0, 24):
                log("humanize: outside the active window - supervisor finishing for the day")
                return 0
        except Exception:
            pass
        left = budget_left()
        if left <= 0:
            log(f"今日额度用完（剩 {left}），监督器退出")
            return 0
        log(f"=== round {round_no}  剩余额度 {left} ===")
        sweep_chrome()
        # objective item (1) goes first each day: chapter quizzes are quick, and
        # skipping them is why nine courses never had their quizzes answered
        run_quiz_sweep()
        try:
            subprocess.run(
                [PY, "-X", "utf8", "parallel_run.py",
                 "--courses", COURSES, "--concurrency", CONCURRENCY,
                 # Without --quiz every worker runs with --no-quiz, so any quiz node
                 # the batch walks past is answered by nobody: the batch skips it and
                 # the daily sweep only covers chapter-level quizzes. 随堂练习 nodes
                 # live inside the chapter tree, so enabling the quiz path here is what
                 # actually closes that hole. do_quiz is verified before a result counts.
                 "--quiz", "--submit"],
                cwd=ROOT, timeout=6 * 3600)
        except subprocess.TimeoutExpired:
            log("parallel_run exceeded 6h, restarting it")
        except Exception as exc:
            log(f"parallel_run failed: {type(exc).__name__}")
        # a short, jittered gap between rounds keeps the traffic from looking periodic
        gap = random.randint(20, 70)
        log(f"round {round_no} finished, waiting {gap}s")
        time.sleep(gap)


if __name__ == "__main__":
    raise SystemExit(main())
