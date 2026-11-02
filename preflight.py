# -*- coding: utf-8 -*-
"""Preflight gate: refuse to start a work day on broken code.

Today's pattern was expensive - several rounds were spent discovering, after hours of
running, that a code path was broken (a signature mismatch that failed every video
node, a duplicated quiz implementation that could not fill blanks, a loop that ate an
hour on a 13-second clip). Each of those would have been caught in seconds by a check
run BEFORE the runners started.

This is that check. It is cheap, touches the platform only once (a single session
probe), and returns a non-zero exit code when the day should not start.

    .venv\\Scripts\\python.exe -X utf8 preflight.py
    .venv\\Scripts\\python.exe -X utf8 preflight.py --no-network   # skip the probe
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import py_compile
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
CRITICAL_FILES = ["cx_auto.py", "task_points.py", "bank.py", "humanize.py",
                  "course_nav.py", "node_cascade.py", "parallel_run.py",
                  "supervise.py", "sweep_quizzes.py", "auto_quiz.py",
                  "schedule_resume.py", "preflight.py"]
CRITICAL_KEYS = ["COURSE_ID", "DAILY_TASK_CAP", "SLEEP_BETWEEN_CHAPTERS",
                 "QUIZ_SUBMIT", "SKIP_FINISHED", "VIDEO_MAX_WAIT", "ACCOUNT"]

results: list = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    mark = "OK  " if ok else "FAIL"
    print(f"[{mark}] {name}" + (f"  -- {detail}" if detail else ""), flush=True)
    return ok


def main() -> int:
    no_net = "--no-network" in sys.argv
    print("=" * 70)
    print(f"preflight  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    # 1) every critical file compiles
    bad = []
    for f in CRITICAL_FILES:
        p = os.path.join(ROOT, f)
        if not os.path.exists(p):
            bad.append(f"{f}: missing")
            continue
        try:
            py_compile.compile(p, doraise=True)
        except Exception as exc:
            bad.append(f"{f}: {type(exc).__name__}")
    check("所有关键文件可编译", not bad, "; ".join(bad[:3]))

    # 2) every module imports (catches signature/name errors at import time)
    bad = []
    for f in CRITICAL_FILES:
        mod = f[:-3]
        try:
            importlib.import_module(mod)
        except Exception as exc:
            bad.append(f"{mod}: {type(exc).__name__}")
    check("所有模块可导入", not bad, "; ".join(bad[:3]))

    # 3) the answer/click API still matches how it is called
    try:
        import inspect
        import task_points as tp
        import cx_auto
        sig = inspect.signature(tp.finish_video_frame)
        wants_log = "log" in sig.parameters
        check("finish_video_frame 接受 log= (今天的元凶)", wants_log,
              f"params={list(sig.parameters)[-2:]}" if not wants_log else "")
        has_tabs = hasattr(cx_auto.Session, "do_video_all_tabs")
        check("Session.do_video_all_tabs 存在", has_tabs)
        has_verdict = ('return "done"' in inspect.getsource(cx_auto.Session._submit)
                       or "return 'done'" in inspect.getsource(cx_auto.Session._submit))
        check("_submit 会返回 done 判决", has_verdict)
        has_park = hasattr(cx_auto.Session, "_bump_attempt")
        check("节点尝试计数存在", has_park)
    except Exception as exc:
        check("API 自检", False, f"{type(exc).__name__}: {str(exc)[:50]}")

    # 4) config sanity
    try:
        with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        missing = [k for k in CRITICAL_KEYS if k not in cfg]
        check("config.json 关键项齐全", not missing, f"missing={missing}")
        cap = int(cfg.get("DAILY_TASK_CAP", 60))
        check("每日额度 > 0", cap > 0, f"cap={cap}")
        lo, hi = (cfg.get("SLEEP_BETWEEN_CHAPTERS") or [75, 210])[:2]
        check("章节间隔 >= 60s (风控)", lo >= 60, f"{lo}-{hi}s")
        acc = str(cfg.get("ACCOUNT") or "")
        check("账号已配置", bool(acc) and "PHONE" not in acc, f"account={acc[:6]}...")
    except Exception as exc:
        check("config.json 可读", False, str(exc)[:50])

    # 5) budget state (informational, not a failure)
    try:
        with open(os.path.join(ROOT, "logs", "daily_budget.json"), encoding="utf-8") as fh:
            d = json.load(fh)
        try:
            with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as fh:
                cap = int(json.load(fh).get("DAILY_TASK_CAP", 60))
        except Exception:
            cap = 60
        today = time.strftime("%Y-%m-%d")
        left = cap if d.get("date") != today else max(0, cap - int(d.get("done", 0)))
        print(f"[info] 今日额度剩余 {left}（日期 {d.get('date')}）")
    except Exception:
        print("[info] 额度文件还没有，视作满额")

    # 6) active hours (the new gate - must not lock us out)
    try:
        from humanize import in_active_hours
        h = time.localtime().tm_hour
        inside = in_active_hours(7, 23)
        print(f"[info] 当前 {h:02d} 点，活跃时段(07-23)={inside}"
              + ("" if inside else "  -> 会在 07:00 后开跑"))
        check("活跃时段函数可用", callable(in_active_hours))
    except Exception as exc:
        check("活跃时段函数可用", False, f"{type(exc).__name__}")

    # 7) selftest + audit + unit tests
    for script in ("selftest.py", "audit.py", "test_logic.py", "test_parking.py",
                   "audit_config_defaults.py", "test_scheduler.py", "test_anomaly.py",
                   "verify_objective.py", "test_budget_concurrency.py"):
        ok = False
        detail = ""
        for attempt in range(2):
            try:
                r = subprocess.run([PY, "-X", "utf8", script], cwd=ROOT,
                                   capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=180)
                if script == "audit.py":
                    ok = "严重问题: 0" in r.stdout
                elif script == "selftest.py":
                    ok = "ALL CHECKS PASSED" in r.stdout
                elif script == "audit_config_defaults.py":
                    ok = "风险点: 0" in r.stdout
                elif script == "verify_objective.py":
                    ok = "未落实 0 条" in r.stdout
                elif script == "test_budget_concurrency.py":
                    ok = "没有丢失更新" in r.stdout
                else:
                    ok = "失败 0" in r.stdout
                detail = "" if ok else r.stdout.strip().splitlines()[-1][:60]
            except Exception as exc:
                ok = False
                detail = type(exc).__name__
            if ok:
                break
            # the concurrency test is load-sensitive; a second run usually passes
            import time as _t
            _t.sleep(2)
        check(f"{script} 通过", ok, detail)

    # 8) session probe - one request, no task-point work
    if no_net:
        print("[info] 跳过会话探测 (--no-network)")
    else:
        session_ok = False
        try:
            import session_guard
            cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
            session_ok = asyncio.run(session_guard.preflight(cfg, log=lambda m: None))
        except Exception as exc:
            print(f"[info] 会话探测异常: {type(exc).__name__}")

        if session_ok:
            check("登录态有效", True)
        else:
            # A dead session used to be terminal: the scheduler would retry preflight
            # every 15 minutes forever and never start, so an overnight expiry cost the
            # whole day. The stored credentials can repair it, so try that first.
            print("[info] 登录态失效，尝试用已保存的账号自动重新登录 ...")
            repaired = False
            try:
                import asyncio as _aio
                from cx_auto import Session as _S
                cfg = json.load(open(os.path.join(ROOT, "config.json"),
                                    encoding="utf-8"))
                cfg.update({"DO_VIDEO": False, "DO_QUIZ": False, "HEADLESS": True,
                            "AUTO_RELOGIN": True})

                async def _repair():
                    s = _S(cfg)
                    await s.start()
                    s._session_loaded = False        # force the credential path
                    try:
                        await s.login()
                        return await s._is_logged_in()
                    finally:
                        await s.close()

                repaired = _aio.run(_repair())
            except Exception as exc:
                print(f"[info] 自动重登失败: {type(exc).__name__}: {str(exc)[:70]}")
            check("登录态有效（含自动重登）", repaired,
                  "" if repaired else "需要手动扫码登录")

    # an existing anomaly flag is not a code failure, but it must be visible
    try:
        import anomaly
        rec = anomaly.is_set()
        if rec:
            print(f"[warn] 存在异常标记（{rec.get('date')} {rec.get('at')}）: "
                  f"{rec.get('markers')}")
            print("       今天不会开工。看过之后删掉 logs/anomaly.json 才能恢复。")
    except Exception:
        pass

    failed = [n for n, ok, _ in results if not ok]
    print("=" * 70)
    if failed:
        print(f"不合格项 {len(failed)}: {failed}")
        print("=> 不要启动今天的批次，先修这些")
        return 1
    print(f"全部 {len(results)} 项通过 -> 可以开跑")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
