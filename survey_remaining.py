# -*- coding: utf-8 -*-
"""Sample the remaining work to estimate how long it will really take.

The "~8 days at 60/day" figure came from counted task points. But the work is not
uniform: a video node costs its full runtime at 1x (the site disables speed-up), while
a quiz or a reading node costs a minute. So the timeline depends on the MIX, and the mix
was never measured.

This walks a sample of each course's first unfinished nodes with --probe (read-only, no
writes, no budget) and records what each one actually contains.

    .venv\\Scripts\\python.exe -X utf8 survey_remaining.py [--per-course 8]
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")

COURSES = {
    "254215593": "大学物理",
    "267087606": "思想道德与法治",
    "88819267": "信息技术基础",
    "203417266": "数学",
    "237168226": "大学英语B1",
    "267261427": "大学生心理健康教育",
}


def main() -> int:
    per = 8
    if "--per-course" in sys.argv:
        per = int(sys.argv[sys.argv.index("--per-course") + 1])

    out_path = os.path.join(ROOT, "logs", "survey.json")
    survey = {}

    for cid, name in COURSES.items():
        cfg = os.path.join(ROOT, "logs", "parallel", f"config_{cid}.json")
        if not os.path.exists(cfg):
            print(f"  {name}: 无配置，跳过")
            continue
        print(f"  探测 {name} ...", flush=True)
        t0 = time.time()
        try:
            r = subprocess.run([PY, "-X", "utf8", "cx_auto.py", "--probe",
                                "--chapters", str(per), "--config", cfg],
                               cwd=ROOT, capture_output=True, text=True, timeout=1800)
            out = (r.stdout or "") + (r.stderr or "")
        except Exception as exc:
            print(f"    {name}: 失败 {type(exc).__name__}")
            continue

        # The log lines carry a "[HH:MM:SS]" prefix, so an anchored ^\s+ match never
        # fires - anchor on the known kind names followed by a URL instead.
        KIND = re.compile(r"((?:\b(?:video|audio|quiz|pdf)x\d+\s*)+)\s+(https?://\S+)")
        nodes = []
        cur = None
        for line in out.splitlines():
            m = re.search(r"probe: title=.*?media=(\d+)(.*)$", line)
            if m:
                cur = {"media": int(m.group(1)),
                       "bad_enc": "enc校验失败" in m.group(2),
                       "kinds": {}}
                nodes.append(cur)
                continue
            m2 = KIND.search(line)
            if m2 and cur is not None:
                for km in re.finditer(r"(video|audio|quiz|pdf)x(\d+)", m2.group(1)):
                    k, n = km.group(1), int(km.group(2))
                    cur["kinds"][k] = cur["kinds"].get(k, 0) + n

        survey[cid] = {"name": name, "sampled": len(nodes), "nodes": nodes}
        videos = sum(n["kinds"].get("video", 0) for n in nodes)
        quizzes = sum(1 for n in nodes if n["kinds"].get("quiz"))
        empty = sum(1 for n in nodes if n["media"] == 0)
        print(f"    {len(nodes)} 个: 视频帧 {videos}, 含测验 {quizzes}, 空节点 {empty}, "
              f"耗时 {time.time() - t0:.0f}s")

    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(survey, fh, ensure_ascii=False, indent=1)

    # ---- aggregate ---------------------------------------------------------
    print()
    print("=" * 70)
    print("剩余节点类型分布（抽样）")
    print("=" * 70)
    tot_v = tot_q = tot_e = tot_n = 0
    for cid, d in survey.items():
        n = d["sampled"]
        v = sum(x["kinds"].get("video", 0) for x in d["nodes"])
        a = sum(x["kinds"].get("audio", 0) for x in d["nodes"])
        q = sum(1 for x in d["nodes"] if x["kinds"].get("quiz"))
        p = sum(1 for x in d["nodes"] if x["kinds"].get("pdf"))
        e = sum(1 for x in d["nodes"] if x["media"] == 0)
        tot_v += v; tot_q += q; tot_e += e; tot_n += n
        print(f"  {d['name']:<22} 抽 {n:>2} 个 | 视频帧 {v:>3} | 音频 {a:>2} | "
              f"测验 {q:>2} | PDF {p:>2} | 空 {e:>2}")
    print("-" * 70)
    if tot_n:
        print(f"  合计: {tot_n} 个节点, 视频帧 {tot_v}, 含测验 {tot_q}, 空节点 {tot_e}")
        print(f"  空节点占比: {tot_e / tot_n * 100:.0f}%")
    print(f"-> {os.path.relpath(out_path, ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
