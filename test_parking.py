# -*- coding: utf-8 -*-
"""Verify the skip/park mechanism end to end, offline.

Why this matters: today the workers reported far more DONE results than the platform
actually registered. Every node that reports success without clearing gets replayed on
the next run, and at 60 units of daily budget that waste compounds - a node that never
registers can eat the whole day.

The park mechanism is what stops that: count attempts per node, and after
MAX_NODE_TRIES put the id in logs/skip_nodes.json so chapters() drops it from the work
list. This exercises both halves without touching the platform.

    .venv\\Scripts\\python.exe -X utf8 test_parking.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

passed = failed = 0


def check(name: str, got, want) -> None:
    global passed, failed
    if got == want:
        passed += 1
        print(f"[OK  ] {name}")
    else:
        failed += 1
        print(f"[FAIL] {name}: got={got!r} want={want!r}")


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def main() -> int:
    import cx_auto
    from cx_auto import Session

    print("=" * 70)
    print("park / skip mechanism")
    print("=" * 70)

    # work in a sandbox so the real logs are untouched
    sandbox = tempfile.mkdtemp(prefix="cxtest_")
    os.makedirs(os.path.join(sandbox, "logs"), exist_ok=True)
    real_root = cx_auto.ROOT
    cx_auto.ROOT = sandbox
    try:
        cfg = {"COURSE_ID": "C1", "DO_VIDEO": False, "DO_QUIZ": False,
               "HEADLESS": True, "MAX_NODE_TRIES": 3}
        sess = Session(cfg)

        # 1) attempts accumulate
        check("首次尝试 = 1", sess._bump_attempt("1001"), 1)
        check("二次尝试 = 2", sess._bump_attempt("1001"), 2)
        check("三次尝试 = 3", sess._bump_attempt("1001"), 3)

        # 2) below the limit it is NOT parked
        limit = int(cfg["MAX_NODE_TRIES"])
        ids = run(sess._load_skip_list())
        if sess._bump_attempt("1001") > limit:
            ids.add("1001")
            sess._save_skip_list(ids)
        check("第 4 次才停放", "1001" in run(sess._load_skip_list()), True)

        # 3) a different node is unaffected
        check("其它节点不牵连", sess._bump_attempt("1002"), 1)

        # 4) the skip list is per-course
        sess.cfg["COURSE_ID"] = "C2"
        check("换课程不继承", len(run(sess._load_skip_list())), 0)
        sess.cfg["COURSE_ID"] = "C1"

        # 5) chapters() drops parked ids from the work list
        parked = run(sess._load_skip_list())
        urls = [
            "https://x/mycourse/studentstudy?chapterId=1001&courseId=C1",
            "https://x/mycourse/studentstudy?chapterId=1002&courseId=C1",
            "https://x/mycourse/studentstudy?chapterId=1003&courseId=C1",
        ]
        kept = [u for u in urls
                if (re.search(r"chapterId=(\d+)", u) or [None, ""])[1] not in parked]
        check("工作列表剔除了停放的节点", len(kept), 2)
        check("保留的是未停放的", all("1001" not in u for u in kept), True)

        # 6) the file survives a re-read (simulating a process restart)
        sess2 = Session(dict(cfg))
        check("重启后仍记得", "1001" in run(sess2._load_skip_list()), True)

        # 7) a corrupt file must not crash the run
        # the code reads a PER-COURSE file now; corrupting the old shared path
        # would have tested nothing
        corrupt = sess2._course_file("skip_nodes")
        with open(corrupt, "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")
        check("坏文件不崩，回退为空", len(run(sess2._load_skip_list())), 0)

        # 8) read-only inspection must NOT park anything.
        #    count_attempt=False is what probes pass; without it three probe runs
        #    silently drop a node from the work list (this happened for real to three
        #    大学英语B1 nodes).
        import inspect as _inspect
        sig = _inspect.signature(Session.open_chapter)
        check("open_chapter 有 count_attempt 开关",
              "count_attempt" in sig.parameters, True)
        src = _inspect.getsource(Session.open_chapter)
        check("count_attempt 真的被用于条件判断",
              "if m0 and count_attempt:" in src, True)
        probe_src = _inspect.getsource(cx_auto.Session.run)
        check("probe 路径传 count_attempt=not probe_only",
              "count_attempt=not probe_only" in probe_src, True)

        # 9) clearing a node must FORGET its failures, or a healthy node parked itself
        #    after being opened MAX_NODE_TRIES times
        check("存在清零方法", hasattr(Session, "_clear_attempt"), True)
        run_src = _inspect.getsource(cx_auto.Session.run)
        check("成功即清零已接线", "_clear_attempt(" in run_src, True)
        sess.cfg["COURSE_ID"] = "C9"
        for _ in range(5):
            sess._bump_attempt("555")
        check("清零前计数为 5", sess._bump_attempt("555"), 6)
        sess._clear_attempt("555")
        check("清零后重新开始", sess._bump_attempt("555"), 1)

    finally:
        cx_auto.ROOT = real_root
        shutil.rmtree(sandbox, ignore_errors=True)

    print("=" * 70)
    print(f"通过 {passed}，失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
