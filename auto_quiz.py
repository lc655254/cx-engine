# -*- coding: utf-8 -*-
"""Answer one chapter quiz by delegating to the shared answer core.

The filling and submitting live in Session.do_quiz, not here: this script only picks
the chapter, finds the quiz frame and reports the verdict. An earlier version carried
its own copy of the click/submit JavaScript, which quietly stopped working the moment
the core was hardened (no 填空题 blanks, no verified submit).

Reuses the mechanics proven on 心理健康 2.5:
  * quizzes live in a work frame (/mooc-ans/work/doHomeWorkNew)
  * multi choice: <li onclick="addcheck(qid)"> writes a hidden input
  * single choice: the radio IS the field (name="answer<qid>")
  * submit is a page function: btnBlueSubmit() then submitCheckTimes()
  * filling and submitting MUST be in the same session or you submit a blank

The AI is a fallback layer, not the first choice: an answer key (psy_answers.py or
the local DB) is used when it has the question, and the model handles the rest.

    .venv\\Scripts\\python.exe -X utf8 auto_quiz.py 267261427 1249011104
    .venv\\Scripts\\python.exe -X utf8 auto_quiz.py 267261427 1249011104 --submit
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bank import ensure_utf8_stdout, clean_stem      # noqa: E402
from cx_auto import Session                          # noqa: E402
from ai_answers import AI                            # noqa: E402

ensure_utf8_stdout()

ROOT = os.path.dirname(os.path.abspath(__file__))
QSEL = ".TiMu, .questionLi"

async def settle_quiz(sess, stage) -> dict:
    """Answer and submit one quiz by delegating to Session.do_quiz.

    do_quiz is the single source of truth: it knows 判断题 answers live on radios
    valued true/false, that 填空题 blanks are WYSIWYG iframes, and that a submission
    must be read back before it is believed. Keeping a second copy here is what let
    this script rot while the core was being hardened.
    """
    rep = {"questions": 0, "filled": 0, "submitted": False, "verdict": "",
           "already": False}
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

    res = await sess.do_quiz()
    rep["raw"] = res
    m = re.search(r"done=(\d+)", res or "")
    rep["filled"] = int(m.group(1)) if m else 0
    rep["questions"] = rep["filled"]
    vm = re.search(r"verdict=(\w+)", res or "")
    rep["verdict"] = vm.group(1) if vm else "unknown"
    rep["submitted"] = rep["verdict"] == "done"
    return rep


async def main() -> int:
    cid = sys.argv[1] if len(sys.argv) > 1 else "267261427"
    ch = sys.argv[2] if len(sys.argv) > 2 else ""
    submit = "--submit" in sys.argv

    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg.update({"COURSE_ID": cid, "DO_VIDEO": False, "DO_QUIZ": True,
                "HEADLESS": False, "QUIZ_SUBMIT": submit,
                "CASCADE_CHILD_NODES": False, "INCLUDE_CHILD_NODES": False})

    ai = AI(log=lambda m: print(m))
    print(f"AI: {'ready' if ai.ready else 'NOT READY'} | {ai.base} | {ai.model}")

    # Check the allowance BEFORE anything starts. Doing this after Session.start()
    # spawned a browser just to close it again, which left chrome processes behind
    # and was pointless traffic.
    try:
        from humanize import Budget
        _b = Budget(daily_cap=int(cfg.get("DAILY_TASK_CAP", 60)),
                    log=lambda m: print(m))
        if not _b.allow():
            print(f"humanize: {_b.summary()} - 今日额度用完，不答题（未启动浏览器）")
            return 0
    except Exception:
        pass

    sess = Session(cfg)
    await sess.start()
    await sess.login()
    chapters = await sess.chapters()
    target = re.sub(r"chapterId=\d+", f"chapterId={ch}", chapters[0]) if ch else chapters[0]
    await sess.open_chapter(target)

    stage = await sess._find_quiz_stage(QSEL)
    if stage is None:
        print("no quiz frame found")
        await sess.close()
        return 2

    rep = await settle_quiz(sess, stage)
    if rep.get("already"):
        print("这个测验之前已完成")
    else:
        print(f"\n答题结果: filled={rep['filled']} verdict={rep['verdict']}")
        if not submit:
            print("（未加 --submit，只填未交）")
        elif rep["submitted"]:
            print("已提交并确认 OK")
        else:
            print(f"未确认提交: verdict={rep['verdict']}")
    print(f"raw: {rep.get('raw', '')}")
    await sess.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
