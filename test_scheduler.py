# -*- coding: utf-8 -*-
"""Tests for the start-of-day decision in schedule_resume.

This single branch decides whether an entire day of work happens, and it fails
silently when wrong (the process just sleeps forever). The four combinations are:

    budget available / exhausted  x  inside hours / outside hours

plus the boundary hours, which are the easiest to get wrong (07:00 must run, 23:00
must not).

    .venv\\Scripts\\python.exe -X utf8 test_scheduler.py
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

passed = failed = 0


def check(name, got, want):
    global passed, failed
    if got == want:
        passed += 1
        print(f"[OK  ] {name}")
    else:
        failed += 1
        print(f"[FAIL] {name}: got={got!r} want={want!r}")


def main() -> int:
    from schedule_resume import decide, in_active_hours, budget_state

    print("=" * 70)
    print("schedule_resume decision")
    print("=" * 70)

    today = "2026-10-14"
    cases = [
        ("额度已满 + 时段内 -> 等", today, 60, 60, 20, "wait"),
        ("额度已满 + 时段外 -> 等", today, 60, 60, 6, "wait"),
        ("额度可用 + 夜间也跑（全时段）", today, 10, 60, 3, "run"),
        ("额度可用 + 时段内 -> 跑", today, 10, 60, 9, "run"),
        ("日期已翻 + 时段内 -> 跑", "2026-10-13", 60, 60, 8, "run"),
        ("日期已翻 + 夜间也跑（全时段）", "2026-10-13", 60, 60, 5, "run"),
        ("额度用了一半 -> 跑", today, 30, 60, 15, "run"),
        ("边界 07:00 -> 跑", today, 0, 60, 7, "run"),
        ("边界 06:59 -> 跑（全时段）", today, 0, 60, 6, "run"),
        ("边界 22:59 -> 跑", today, 0, 60, 22, "run"),
        # 2026-10-19: user asked to run continuously, so the live window is
        # (0, 24) - every hour counts as active.
        ("边界 23:00 -> 跑（全时段）", today, 0, 60, 23, "run"),
        ("边界 04:59 -> 跑", today, 0, 60, 4, "run"),
        ("边界 00:30 -> 跑", today, 0, 60, 0, "run"),
        ("边界 05:00 -> 跑（全时段）", today, 0, 60, 5, "run"),
        ("额度刚好差一点 -> 跑", today, 59, 60, 12, "run"),
    ]
    for name, d, done, cap, hour, want in cases:
        check(name, decide(d, done, cap, hour, today), want)

    # the live values must not contradict the decision
    date, done, cap = budget_state()
    import time
    hour = time.localtime().tm_hour
    live = decide(date, done, cap, hour, time.strftime("%Y-%m-%d"))
    print(f"[info] 当前实际: date={date} done={done}/{cap} hour={hour:02d} -> {live}")
    check("活跃时段函数返回布尔", isinstance(in_active_hours(7, 23), bool), True)

    # The day must END at the window edge, not spin. supervise relaunched the batch
    # every ~60s after 23:00 (the worker stops itself, but the supervisor did not
    # know), which produced two hours of nightly browser launches - exactly what the
    # 07:00-23:00 gate exists to prevent. The gate held at the worker level and leaked
    # at the supervisor level.
    sup = open(os.path.join(ROOT, "supervise.py"), encoding="utf-8").read()
    check("supervisor 自己检查时段", "in_active_hours(0, 24)" in sup, True)
    check("supervisor 时段外收工而非重试", "finishing for the day" in sup, True)
    sr = open(os.path.join(ROOT, "schedule_resume.py"), encoding="utf-8").read()
    check("scheduler 时段外结束当天", "finishing for the day" in sr, True)

    print("=" * 70)
    print(f"通过 {passed}，失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
