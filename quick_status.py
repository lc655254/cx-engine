# -*- coding: utf-8 -*-
"""Quick status: skip list state, worker progress, attempts."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import glob as _glob

# these are per-course files now, so collect every instance rather than one path
_pairs = []
for pattern in ("node_attempts_*.json", "skip_nodes_*.json"):
    _pairs.extend((pattern, p) for p in sorted(_glob.glob(os.path.join(ROOT, "logs", pattern))))
# keep the old names for backward compatibility with any pre-split files
for name in ("node_attempts.json", "skip_nodes.json"):
    _p = os.path.join(ROOT, "logs", name)
    if os.path.exists(_p):
        _pairs.append((name, _p))

for name, p in _pairs:
    if not os.path.exists(p):
        print(f"  {name}: 未生成")
        continue
    name = os.path.basename(p)
    try:
        d = json.load(open(p, encoding="utf-8"))
    except Exception as exc:
        print(f"  {name}: 读取失败 {type(exc).__name__}")
        continue
    if name.startswith("node"):
        print(f"  {name}: 记录 {len(d)} 个节点的尝试次数")
        for k, v in list(d.items())[:6]:
            print(f"     {k} = {v} 次")
    else:
        ids = d.get("ids", []) if isinstance(d, dict) else []
        print(f"  {name}: 已停放 {len(ids)} 个顽固节点 {ids[:4]}")

print()
for cid in ("254215593", "267087606"):
    f = os.path.join(ROOT, "logs", "parallel", f"run_{cid}.log")
    if not os.path.exists(f):
        continue
    print(f"--- {cid}")
    lines = open(f, encoding="utf-8", errors="replace").read().splitlines()
    keep = [l for l in lines
            if re.search(r"scan:|parked|=== \[|DONE|tabs:|TargetClosed", l)]
    for l in keep[-4:]:
        print(f"   {l}")
    tc = sum(1 for l in lines if "TargetClosed" in l)
    print(f"   TargetClosed 次数: {tc}")
