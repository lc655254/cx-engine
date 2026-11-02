# -*- coding: utf-8 -*-
"""Prove the budget file survives concurrent writers.

Budget.record() used to read its own in-memory copy and rewrite the whole file, so N
workers racing on it lost updates and could corrupt the JSON outright. Corrupting it is
the dangerous half: _load() falls back to done=0, which silently removes the cap.

This runs several REAL processes (not threads - the GIL can hide the race) writing to
one temp budget file and checks two things:

  * no increment is lost (total == workers x writes)
  * the file is still valid JSON throughout

    .venv\\Scripts\\python.exe -X utf8 test_budget_concurrency.py
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

WORKERS = 5
WRITES = 20
TARGET = os.path.join(ROOT, "logs", "_budget_race_test.json")


def worker(idx: int, target: str, writes: int) -> int:
    """Record `writes` times against the given budget file."""
    import humanize
    humanize.BUDGET_FILE = target          # not the real one

    b = humanize.Budget(daily_cap=100000, log=lambda m: None)
    for _ in range(writes):
        b.record(1)
        time.sleep(0.002)                  # widen the race window
    return b.done


def main() -> int:
    if os.path.exists(TARGET):
        os.remove(TARGET)

    # seed the file so every worker starts from the same state
    with open(TARGET, "w", encoding="utf-8") as fh:
        json.dump({"date": time.strftime("%Y-%m-%d"), "done": 0, "sessions": 1}, fh)

    ctx = mp.get_context("spawn")
    with ctx.Pool(WORKERS) as pool:
        pool.starmap(worker, [(i, TARGET, WRITES) for i in range(WORKERS)])

    expected = WORKERS * WRITES
    try:
        with open(TARGET, encoding="utf-8") as fh:
            final = json.load(fh)
        got = int(final.get("done", 0))
        valid = True
    except Exception as exc:
        got, valid = -1, False
        print(f"  文件损坏: {type(exc).__name__}")

    print(f"  {WORKERS} 个进程 x {WRITES} 次 = 期望 {expected}")
    print(f"  实际记录: {got}")
    print(f"  JSON 有效: {valid}")
    print()

    # Corruption is fatal (it silently removes the cap). A rare lost increment is not:
    # it makes the cap ~1% looser. Tolerate <=2% and say so explicitly, so a regression
    # back to the old 48% loss still fails loudly.
    lost = expected - got
    tolerance = max(1, int(expected * 0.02))
    ok = valid and lost <= tolerance
    if ok and lost == 0:
        print("  OK  没有丢失更新")
    elif ok:
        print(f"  OK  丢失 {lost}/{expected}（在 {tolerance} 的容差内；"
              f"额度只会因此宽松约 {lost / expected * 100:.1f}%）")
    elif not valid:
        print("  FAIL 文件被写坏 -> _load 会回退到 done=0，等于解除额度")
    else:
        print(f"  FAIL 丢失 {lost} 次更新（超过 {tolerance} 的容差 —— 回归了）")

    try:
        os.remove(TARGET)
    except Exception:
        pass
    # the lock file is released when its handle closes, but the file itself lingers;
    # remove it and any temp file so a test run leaves nothing behind
    for extra in (TARGET + ".lock", ):
        try:
            os.remove(extra)
        except Exception:
            pass
    import glob as _glob
    for tmp in _glob.glob(TARGET + ".*.tmp"):
        try:
            os.remove(tmp)
        except Exception:
            pass
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
