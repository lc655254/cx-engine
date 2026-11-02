# -*- coding: utf-8 -*-
"""Self-check: local cache, bank files, normalization, option mapping.

Pure offline - no requests, no browser, no network. Run:
    .venv\\Scripts\\python.exe -X utf8 selftest.py
Exit code 0 = all assertions passed.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from bank import (Bank, Answer, clean_stem, key_of, key_stem, letter_indexes,  # noqa: E402
                  normalize_answer, split_blanks, text_indexes, to_answer_string)

FAILS = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"[{'OK ' if ok else 'FAIL'}] {label}: got={got!r} want={want!r}")
    if not ok:
        FAILS.append(label)


def main() -> int:
    # ---- normalization -------------------------------------------------
    check("clean_stem strips numbering", clean_stem("1. 计算机网络的分类"), "计算机网络的分类")
    check("clean_stem strips 第N题", clean_stem("第 3 题：TCP协议"), "TCP协议")
    check("clean_stem strips full-width blank", clean_stem("计算机网络的主要功能是（ ）"),
          "计算机网络的主要功能是")
    check("clean_stem keeps two blanks apart", clean_stem("分别是（ ）和（ ）"),
          "分别是和")
    check("blank stems key identically",
          key_stem("分别是（ ）和（ ）") == key_stem("分别是(    )和（  ）"), True)
    check("clean_stem strips underscore blank", clean_stem("OSI共____层"), "OSI共层")
    check("clean_stem strips half-width blank", clean_stem("Which one is right ( )"),
          "Whichoneisright")
    check("clean_stem keeps score marker", clean_stem("下列正确的是（2分）"), "下列正确的是")
    check("normalize strips 答案：", normalize_answer("答案：B"), "B")
    check("normalize unifies separators", normalize_answer("7、八；九"), "7,八;九")
    check("to_answer_string list", to_answer_string(["A", "C"]), "AC")
    check("to_answer_string dict", to_answer_string({"answer": "B"}), "B")

    # ---- option mapping -------------------------------------------------
    check("letter_indexes single letter", letter_indexes("B"), [1])
    check("letter_indexes AC", letter_indexes("AC"), [0, 2])
    check("letter_indexes A,C", letter_indexes("A,C"), [0, 2])
    check("letter_indexes prose guard", letter_indexes("答案是B因为网络"), [])
    check("letter_indexes long guard", letter_indexes("A" * 20), [])
    check("text_indexes match",
          text_indexes("资源共享", ["A. 传输文件", "B. 资源共享", "C. 打游戏"]), [1])

    # ---- blank splitting -------------------------------------------------
    check("split_blanks 2 boxes", split_blanks("7;七", 2), ["7", "七"])
    check("split_blanks pad", split_blanks("7", 3), ["7", "7", "7"])

    # ---- bank file loading -------------------------------------------------
    tmp = tempfile.mkdtemp(prefix="cxselftest_")
    os.makedirs(os.path.join(tmp, "evidence"), exist_ok=True)
    demo = [{"question": "1. 计算机网络的主要功能是", "type": "单选题", "answer": "B"},
            {"question": "TCP 是面向连接的协议", "type": "判断题", "answer": "对"},
            {"question": "OSI 参考模型共有几层", "type": "填空题", "answer": "7;七"}]
    with open(os.path.join(tmp, "seed.json"), "w", encoding="utf-8") as fh:
        json.dump(demo, fh, ensure_ascii=False)

    cfg = {
        "ANSWER_APIS": [],
        "AI_ANSWER": {"openai_base": "", "model": "", "env_key": "CX_NOPE"},
        "BANK_FILES": ["seed.json"],
        "BANK_SQL": os.path.join(tmp, "nope.sqlite3"),
        "QUIZ_SELECTORS": {},
    }
    bank = Bank(cfg, root=tmp, log=lambda m: None)
    check("bank loaded 3 rows from file", len(bank.flat), 3)

    a = bank.query("1. 计算机网络的主要功能是", "单选题",
                   ["A. 传输文件", "B. 资源共享", "C. 打游戏", "D. 打印"])
    check("single choice hit", a.hit, True)
    check("single choice indexes", a.indexes, [1])
    check("single choice source", a.source, "cache")

    b = bank.query("3. TCP 是面向连接的协议", "判断题", [])
    check("judgement hit despite numbering", b.hit, True)
    check("judgement maps to 对=A", b.indexes, [0])

    c = bank.query("OSI 参考模型共有几层", "填空题", [])
    check("fill hit", c.hit, True)
    check("fill blanks split", split_blanks(c.text, 2), ["7", "七"])

    d = bank.query("完全没见过的题目内容", "单选题", ["A. x", "B. y"])
    # An unknown question is now answered by the AI layer (Bank._ai falls back to
    # ai_answers.AI), so a hit with source 'ai' is CORRECT. What must never happen is
    # a hit claiming to come from the cache/file/db, which would mean a fake match.
    check("unknown resolved by ai or cleanly missed",
          d.hit is False or d.source == "ai", True)
    check("unknown is not a false cache hit",
          d.source in ("ai", "miss"), True)

    # ---- write back -----------------------------------------------------
    bank.remember("新题：测试缓存", "单选题", "A", "unit")
    bank2 = Bank(cfg, root=tmp, log=lambda m: None)
    e = bank2.query("新题：测试缓存", "单选题", ["A. x", "B. y"])
    check("remember persisted", e.hit, True)
    check("remember indexes", e.indexes, [0])

    print("-" * 46)
    if FAILS:
        print(f"FAILED {len(FAILS)}: " + ", ".join(FAILS))
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
