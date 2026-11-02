# -*- coding: utf-8 -*-
"""Sweep every chapter quiz across every course: scrape -> AI -> fill -> submit.

One session drives all of it, because:
  * the session cookie is reused (no repeated logins)
  * filling and submitting must share a session per quiz
  * chapter navigation is expensive, so the course is walked once

Each quiz is handled the same way that was verified on 心理健康 2.5 and 军事理论
1.1.1: find the work frame, scrape .TiMu items, ask the model, click the options
(multi = hidden field via addcheck, single = the radio), then btnBlueSubmit() +
submitCheckTimes().

    .venv\\Scripts\\python.exe -X utf8 sweep_quizzes.py                 # all courses
    .venv\\Scripts\\python.exe -X utf8 sweep_quizzes.py 267511667       # one course
    .venv\\Scripts\\python.exe -X utf8 sweep_quizzes.py --dry           # fill only
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bank import ensure_utf8_stdout, clean_stem     # noqa: E402
from cx_auto import Session                         # noqa: E402
import course_nav as nav                            # noqa: E402
from ai_answers import AI                           # noqa: E402

ensure_utf8_stdout()

ROOT = os.path.dirname(os.path.abspath(__file__))
QSEL = ".TiMu, .questionLi"
DONE_MARK = re.compile(r"已完成|已提交|批阅|已完成")
QUIZ_HINT = re.compile(r"测验|测试|作业|考试")


async def safe_eval(root, expr, arg=None, default=None):
    """evaluate() that survives a navigation tearing down the execution context."""
    try:
        return await root.evaluate(expr, arg) if arg is not None else await root.evaluate(expr)
    except Exception as exc:
        if "destroyed" in str(exc) or "detached" in str(exc) or "Target closed" in str(exc):
            return default
        return default


async def solve_quiz(stage, page, ai, course_name: str, label: str,
                     sess=None) -> dict:
    """Answer and submit one quiz by delegating to Session.do_quiz.

    do_quiz is the single source of truth: it knows that 判断题 answers live on radios
    valued true/false, that 填空题 blanks are WYSIWYG iframes, and that a submission
    must be read back before it is believed. This function only wraps it for reporting.
    """
    rep = {"label": label, "questions": 0, "filled": 0, "submitted": False,
           "already": False, "verdict": "", "answers": [], "error": ""}
    if sess is None:
        rep["error"] = "no session"
        return rep

    # already finished? the review page carries 已完成
    try:
        body = await stage.evaluate(
            '() => (document.body.innerText || "").slice(0, 400)')
        if re.search(r"已完成|已批阅", body or "") and not re.search(
                r"待完成|未完成", body or ""):
            rep["already"] = True
            rep["verdict"] = "already"
            return rep
    except Exception:
        pass

    try:
        res = await sess.do_quiz()
    except Exception as exc:
        rep["error"] = f"{type(exc).__name__}"
        print(f"    quiz failed: {type(exc).__name__}: {str(exc)[:70]}")
        return rep

    rep["raw"] = res
    m = re.search(r"done=(\d+)", res or "")
    rep["filled"] = int(m.group(1)) if m else 0
    rep["questions"] = rep["filled"]
    vm = re.search(r"verdict=(\w+)", res or "")
    rep["verdict"] = vm.group(1) if vm else "unknown"
    rep["submitted"] = rep["verdict"] == "done"
    return rep


async def main() -> int:
    only = [a for a in sys.argv[1:] if a.isdigit()]
    dry = "--dry" in sys.argv

    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as fh:
        cfg = json.load(fh)
    # do_quiz owns the answering, so it must be enabled here; the sweep no longer
    # fills anything itself. QUIZ_SUBMIT is what makes a result count.
    cfg.update({"DO_VIDEO": False, "DO_QUIZ": True, "QUIZ_SUBMIT": not dry,
                "CASCADE_CHILD_NODES": False, "INCLUDE_CHILD_NODES": False,
                "HEADLESS": True})
    with open(os.path.join(ROOT, "evidence", "course_cards.json"), encoding="utf-8") as fh:
        cards = json.load(fh)
    if only:
        cards = [c for c in cards if c["courseId"] in only]

    ai = AI(log=lambda m: print(m))
    print(f"AI: {'ready' if ai.ready else 'NOT READY'} | {ai.model}")
    print(f"courses: {len(cards)}   dry={dry}")

    # Never sweep with a dead cookie: every request would hit the login redirect.
    if not dry:
        from session_guard import preflight
        print("preflight: checking the saved session")
        if not await preflight(cfg, log=print):
            print("REFUSING TO START: no usable session.")
            print("  re-authenticate:  .venv\\Scripts\\python.exe -X utf8 "
                  "login.py --qr --interactive --keep-open --timeout 900")
            return 4

    # Budget first: no point opening a browser we are not allowed to use.
    try:
        from humanize import Budget as _B
        _pre = _B(daily_cap=int(cfg.get("DAILY_TASK_CAP", 60)), log=lambda m: print(m))
        if not _pre.allow():
            print(f"humanize: {_pre.summary()} - 今日额度用完，不扫描（未启动浏览器）")
            return 0
    except Exception:
        pass

    sess = Session(cfg)
    await sess.start()
    await sess.login()

    # keep checking while the sweep runs, and stop the moment we get kicked
    from session_guard import SessionGuard
    guard = SessionGuard(log=lambda m: print(m, flush=True), interval_s=300)

    # The daily budget governs every entry point. Without this the sweep would answer
    # every quiz in every course in one burst while the video batch sat out on an
    # exhausted allowance - the opposite of what the cap is for.
    try:
        from humanize import Budget
        budget = Budget(daily_cap=int(cfg.get("DAILY_TASK_CAP", 60)), log=print)
        budget.new_session()
        print(f"humanize: {budget.summary()}")
    except Exception as exc:
        print(f"humanize unavailable ({type(exc).__name__}), running unmetered")
        budget = None

    total = {"quizzes": 0, "filled": 0, "submitted": 0, "already": 0}
    report = []

    for card in cards:
        cid = card["courseId"]
        print(f"\n{'=' * 66}\n=== {card['name']} ({cid})")
        cfg["COURSE_ID"] = cid
        sess.cfg["COURSE_ID"] = cid
        try:
            chapters = await sess.chapters()
        except Exception as exc:
            print(f"  chapters failed: {type(exc).__name__}")
            continue
        if not chapters:
            print("  no chapters (course shell may be empty)")
            continue
        print(f"  {len(chapters)} chapters")

        for idx, url in enumerate(chapters, 1):
            if budget is not None and not budget.allow():
                print(f"humanize: {budget.summary()} - 今日额度用完，停止扫描")
                break
            # periodic health check: stop cleanly instead of hammering the
            # platform with a dead cookie
            if not await guard.alive(sess.page):
                print("  session lost mid-sweep - stopping")
                out = os.path.join(ROOT, "logs", "quiz_sweep.json")
                with open(out, "w", encoding="utf-8") as fh:
                    json.dump({"totals": total, "items": report,
                               "ai_calls": ai.calls, "ai_errors": ai.errors,
                               "guard": guard.summary()}, fh,
                              ensure_ascii=False, indent=1)
                print(f"  partial report -> {os.path.relpath(out, ROOT)}")
                await sess.close()
                return 5
            cid_m = re.search(r"chapterId=(\d+)", url)
            ch = cid_m.group(1) if cid_m else str(idx)
            try:
                await sess.open_chapter(url)
            except Exception as exc:
                print(f"  [{idx:>3}] ch={ch} open failed {type(exc).__name__}")
                continue
            # only bother when this chapter offers a quiz
            body = await sess.page.evaluate('() => (document.body.innerText || "").slice(0, 600)')
            if not QUIZ_HINT.search(body):
                continue
            stage = await sess._find_quiz_stage(QSEL)
            if stage is None:
                continue
            total["quizzes"] += 1
            try:
                rep = await solve_quiz(stage, sess.page, ai, card["name"],
                                       f"{cid}:{ch}", sess=sess)
            except Exception as exc:
                print(f"  [{idx:>3}] ch={ch} quiz error {type(exc).__name__}: {str(exc)[:70]}")
                rep = {"label": f"{cid}:{ch}", "questions": 0, "filled": 0,
                       "submitted": False, "already": False, "answers": [],
                       "error": f"{type(exc).__name__}"}
            total["filled"] += rep["filled"]
            total["submitted"] += 1 if rep["submitted"] else 0
            total["already"] += 1 if rep["already"] else 0
            if budget is not None and rep.get("submitted"):
                budget.record(1)
            flag = "already" if rep["already"] else ("submitted" if rep["submitted"] else "no-submit")
            print(f"  [{idx:>3}] ch={ch:<12} q={rep['questions']:>2} filled={rep['filled']:>2} "
                  f"{flag}")
            report.append({**rep, "courseId": cid, "chapterId": ch,
                           "course": card["name"]})

    out = os.path.join(ROOT, "logs", "quiz_sweep.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"totals": total, "items": report, "ai_calls": ai.calls,
                   "ai_errors": ai.errors, "guard": guard.summary()},
                  fh, ensure_ascii=False, indent=1)
    print(f"\n{'=' * 66}")
    print(f"quizzes found={total['quizzes']}  filled={total['filled']}  "
          f"submitted={total['submitted']}  already-done={total['already']}")
    print(f"AI calls={ai.calls} errors={ai.errors}")
    print(f"report -> {os.path.relpath(out, ROOT)}")
    await sess.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
