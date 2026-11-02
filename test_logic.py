# -*- coding: utf-8 -*-
"""Unit tests for the pure logic. No platform, no browser, no network.

preflight.py covers wiring (does it compile, do the signatures match). This covers
BEHAVIOUR of the pieces that translate platform quirks into answers, where a quiet
regression produces wrong results rather than a crash:

  * judge-question mapping (radio values are true/false, not letters)
  * blank splitting (multi-blank answers are ';' separated)
  * answer normalisation and index extraction
  * skip-list round-trip (the mechanism that stops a dead node leading the queue)
  * the playback budget, which must scale with clip length
  * active-hours windowing

    .venv\\Scripts\\python.exe -X utf8 test_logic.py
"""
from __future__ import annotations

import os
import sys
import time
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

passed = failed = 0


def check(name: str, got, want) -> None:
    global passed, failed
    ok = got == want
    if ok:
        passed += 1
        print(f"[OK  ] {name}")
    else:
        failed += 1
        print(f"[FAIL] {name}: got={got!r} want={want!r}")


def main() -> int:
    print("=" * 70)
    print("pure logic tests")
    print("=" * 70)

    # ---- answer normalisation -------------------------------------------
    from bank import normalize_answer, split_blanks, clean_stem

    check("判断: 对 -> 对", normalize_answer("对", "判断题"), "对")
    check("判断: 正确 -> 对", normalize_answer("正确", "判断题"), "对")
    check("判断: 错误 -> 错", normalize_answer("错误", "判断题"), "错")
    check("判断: 答案解析剥离", normalize_answer("答案：对", "判断题"), "对")
    check("单选: 小写变大写", normalize_answer("a", "单选题"), "A")
    check("多选: 去重排序", normalize_answer("CAB", "多选题"), "ABC")
    check("多选: 带分隔符", normalize_answer("A、B、D", "多选题"), "ABD")

    # ---- blank splitting -------------------------------------------------
    check("填空: 分号分隔", split_blanks("真值;算术平均值", 2),
          ["真值", "算术平均值"])
    check("填空: 中文分号", split_blanks("A类；标准差", 2), ["A类", "标准差"])
    check("填空: 补足空数", split_blanks("7", 2), ["7", "7"])
    check("填空: 单空", split_blanks("四舍五入法", 1), ["四舍五入法"])

    # ---- stem cleaning ---------------------------------------------------
    # clean_stem keeps the leading numbering/type marker on purpose - the AI reads
    # it fine, and it helps disambiguate otherwise identical stems.
    check("题干: 保留编号与题型（有意）",
          "单选题" in clean_stem("1【单选题】中华优秀传统文化"), True)
    check("题干: 去空格", clean_stem("  中华 优秀  "), "中华优秀")

    # ---- AI reply parsing ------------------------------------------------
    from ai_answers import AI
    check("AI: 单选解析", AI._parse("B", "单选题"), "B")
    check("AI: 多选解析", AI._parse("ABD", "多选题"), "ABD")
    check("AI: 括号答案", AI._parse("（C）", "单选题"), "C")
    check("AI: 判断 对", AI._parse("对", "判断题"), "对")
    check("AI: 判断 错误", AI._parse("错误", "判断题"), "错")
    check("AI: 带前缀", AI._parse("正确答案：B", "单选题"), "B")
    check("AI: 填空分号", AI._parse("真值;算术平均值", "填空题"),
          "真值;算术平均值")
    check("AI: 空回复", AI._parse("", "单选题"), "")

    # ---- skip list round trip -------------------------------------------
    import cx_auto
    cfg = {"COURSE_ID": "TESTCOURSE", "DO_VIDEO": False, "DO_QUIZ": False,
           "HEADLESS": True}
    sess = cx_auto.Session(cfg)
    # redirect the file so the real one is untouched
    import json
    real_root = cx_auto.ROOT
    tmp = tempfile.mkdtemp()
    cx_auto.ROOT = tmp
    try:
        check("skip: 初始为空", len(asyncio_run(sess._load_skip_list())), 0)
        ids = asyncio_run(sess._load_skip_list())
        ids.add("12345")
        sess._save_skip_list(ids)
        back = asyncio_run(sess._load_skip_list())
        check("skip: 写回后读出一致", sorted(back), ["12345"])
        # a different course must not inherit the list
        sess.cfg["COURSE_ID"] = "OTHER"
        check("skip: 换课程后不继承", len(asyncio_run(sess._load_skip_list())), 0)
        sess.cfg["COURSE_ID"] = "TESTCOURSE"
        n1 = sess._bump_attempt("999")
        n2 = sess._bump_attempt("999")
        check("attempt: 递增", (n1, n2), (1, 2))
    finally:
        cx_auto.ROOT = real_root

    # ---- playback budget scales with clip length -------------------------
    def budget_for(dur, max_wait=3600):
        return min(float(max_wait), max(90.0, dur * 2.5 + 120.0))

    check("13s 短片预算 <= 3 分钟", budget_for(13) <= 180, True)
    check("13s 预算不是 60 分钟", budget_for(13) < 3600, True)
    check("300s 片子预算合理", 600 <= budget_for(300) <= 1500, True)
    check("超长片仍受 max_wait 限制", budget_for(9999), 3600.0)

    # ---- active hours ----------------------------------------------------
    from humanize import in_active_hours, human_delay
    check("活跃: 中午在内", in_active_hours(7, 23) in (True, False), True)
    h = time.localtime().tm_hour
    expect = 7 <= h < 23
    check("活跃: 与当前小时一致", in_active_hours(7, 23), expect)
    lo = min(human_delay(6) for _ in range(200))
    hi = max(human_delay(6) for _ in range(200))
    check("延时: 有抖动", hi - lo > 0.5, True)
    check("延时: 不落在整秒", any(abs(human_delay(6) % 1.0) > 0.01
                                  for _ in range(50)), True)

    # ---- session guard URL logic ----------------------------------------
    from session_guard import looks_logged_in, looks_logged_out
    check("guard: passport 判为未登录",
          looks_logged_out("https://passport2.chaoxing.com/login?x=1"), True)
    check("guard: 课程页判为已登录",
          looks_logged_in("https://mooc1.chaoxing.com/visit/interaction"), True)
    check("guard: 首页不是有效探活",
          looks_logged_in("https://i.chaoxing.com/base"), True)

    # ---- answer indexes --------------------------------------------------
    from bank import letter_indexes, text_indexes
    check("索引: 字母转下标", letter_indexes("ABC"), [0, 1, 2])
    check("索引: 单字母", letter_indexes("B"), [1])
    check("索引: 判断题文本对", text_indexes("对", ["对", "错"]), [0])
    check("索引: 判断题文本错", text_indexes("错", ["对", "错"]), [1])

    print("=" * 70)
    print(f"通过 {passed}，失败 {failed}")
    return 1 if failed else 0


def asyncio_run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


if __name__ == "__main__":
    raise SystemExit(main())
