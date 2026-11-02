# -*- coding: utf-8 -*-
"""Check that every script named in the docs actually exists.

Stale documentation is the same failure mode as the dead JS copies: it looks
authoritative while describing something that is no longer true. README previously
told the reader that videos are finished by jumping to the end - the exact technique
that was proven not to register.
"""
import os
import re

ROOT = os.path.dirname(os.path.abspath(__file__))
DOCS = ["README.md", "STATUS.md", "CHECKLIST.md", "HANDOFF.md"]
PAT = re.compile(r"`([a-zA-Z_][a-zA-Z0-9_]*\.py)`")

missing = []
total = 0
for doc in DOCS:
    path = os.path.join(ROOT, doc)
    if not os.path.exists(path):
        continue
    src = open(path, encoding="utf-8").read()
    names = sorted(set(PAT.findall(src)))
    total += len(names)
    for name in names:
        if not os.path.exists(os.path.join(ROOT, name)):
            missing.append(f"{doc}: {name}")

print(f"文档中提到的脚本引用: {total} 处")
if missing:
    print("以下引用指向不存在的文件:")
    for m in missing:
        print(f"   · {m}")
else:
    print("✓ 全部存在")

# 反过来：有没有重要脚本完全没被文档提到
key = ["cx_auto.py", "task_points.py", "bank.py", "humanize.py", "supervise.py",
       "schedule_resume.py", "preflight.py", "report_progress.py",
       "sweep_quizzes.py", "parallel_run.py"]
allsrc = ""
for doc in DOCS:
    p = os.path.join(ROOT, doc)
    if os.path.exists(p):
        allsrc += open(p, encoding="utf-8").read()
undocumented = [k for k in key if k not in allsrc]
if undocumented:
    print("未被文档提及的关键脚本:", undocumented)
else:
    print("✓ 关键脚本都有文档覆盖")
