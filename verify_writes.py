# -*- coding: utf-8 -*-
"""Confirm every JSON write in the worker path is atomic or locked."""
import os
import re

ROOT = os.path.dirname(os.path.abspath(__file__))

print("=" * 70)
print("写入点审查")
print("=" * 70)
unsafe = 0
for f in ("cx_auto.py", "bank.py", "humanize.py", "safejson.py", "task_points.py"):
    p = os.path.join(ROOT, f)
    if not os.path.exists(p):
        continue
    src = open(p, encoding="utf-8").read()
    lines = src.split("\n")
    for i, l in enumerate(lines):
        if "json.dump(" not in l:
            continue
        ctx = "\n".join(lines[max(0, i - 8):i + 2])
        safe = ("safejson" in ctx
                or "json.dump(self.data" in l          # Budget: own lock + atomic write
                or "def save(" in ctx)                  # inside safejson itself
        if safe:
            print(f"  OK   {f}:{i+1}  {l.strip()[:56]}")
        else:
            unsafe += 1
            print(f"  !!   {f}:{i+1}  {l.strip()[:56]}   <- 未保护")

print()
print("=" * 70)
print("每门课独立文件")
print("=" * 70)
src = open(os.path.join(ROOT, "cx_auto.py"), encoding="utf-8").read()
for label, needle in (
    ("_course_file 已定义", "def _course_file"),
    ("停放列表按课时分文件", 'self._course_file("skip_nodes")'),
    ("尝试计数按课时分文件", 'self._course_file("node_attempts")'),
):
    print(f"  {'OK  ' if needle in src else 'FAIL'}  {label}")
print(f"  {'OK  ' if 'skip_nodes.json' not in src else 'FAIL'}  旧共享路径已清除")
print(f"  {'OK  ' if 'node_attempts.json' not in src else 'FAIL'}  旧计数路径已清除")

print()
print("=" * 70)
print(f"未保护的写入点: {unsafe}")
