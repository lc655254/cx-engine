# -*- coding: utf-8 -*-
"""Find config defaults that an explicit null silently defeats.

`cfg.get("KEY", True)` returns True only when KEY is MISSING. If the key exists with
value None (which JSON allows), it returns None - so `if not cfg.get("KEY", True)`
takes the failure branch and the feature turns itself off without a word.

Found on DUMP_COOKIES: the key sits in config.json as null, so session saving was one
`is not None` away from being silently disabled. This scans every such read.
"""
from __future__ import annotations

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
FILES = ["cx_auto.py", "task_points.py", "bank.py", "humanize.py",
         "supervise.py", "parallel_run.py", "sweep_quizzes.py", "auto_quiz.py",
         "session_guard.py", "preflight.py", "schedule_resume.py"]

# cfg.get("KEY", <default>) / self.cfg.get("KEY", <default>)
PAT = re.compile(r'cfg\.get\(\s*["\']([A-Z0-9_]{2,})["\']\s*,\s*([^)]+)\)')


def main() -> int:
    try:
        cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    except Exception as exc:
        print(f"config.json unreadable: {exc}")
        return 2

    risky = []
    null_keys = []
    for f in FILES:
        p = os.path.join(ROOT, f)
        if not os.path.exists(p):
            continue
        src = open(p, encoding="utf-8").read()
        for m in PAT.finditer(src):
            key, default = m.group(1), m.group(2).strip()
            line = src[:m.start()].count("\n") + 1
            if key not in cfg:
                continue                      # missing -> default applies correctly
            if cfg[key] is None:
                null_keys.append((f, line, key))
            # a truthiness test on a key whose default is True is the dangerous shape
            around = src[max(0, m.start() - 40):m.end() + 40]
            if re.search(r"if\s+not\s+self\.cfg\.get|if\s+not\s+cfg\.get"
                         r"|if\s+self\.cfg\.get\(\s*[\"']" + key + r"[\"']\s*\)",
                         around):
                risky.append((f, line, key, default, cfg[key]))

    print("=" * 70)
    print("config 默认值被 null 悄悄击败的风险")
    print("=" * 70)

    if null_keys:
        print("\n配置里存在但值为 null 的键（.get(k, default) 会返回 None 而不是 default）:")
        for f, line, key in null_keys:
            print(f"   · {f}:{line}  {key} = null")
    else:
        print("\n没有 null 值的配置键")

    if risky:
        print("\n风险点（真值判断 + 键存在 + 默认值）:")
        for f, line, key, default, val in risky:
            print(f"   ! {f}:{line}  {key} (default={default}, 实际={val!r})")
    else:
        print("\n未发现风险形态")

    print("\n" + "=" * 70)
    print(f"null 键: {len(null_keys)}   风险点: {len(risky)}")
    print("建议：默认值写成显式比较，例如")
    print('   if self.cfg.get("KEY") is False:   # 只有明确 false 才关闭')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
