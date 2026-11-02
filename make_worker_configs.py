# -*- coding: utf-8 -*-
"""Rebuild the per-worker config files from config.json, safely.

PowerShell's ConvertTo-Json round-trip corrupted these (BOM + escaping), which made
cx_auto crash with "Expecting value: line 1 column 1" before doing any work. Build
them in Python instead, which also keeps every tuned key in sync.

    .venv\\Scripts\\python.exe -X utf8 make_worker_configs.py 267087606 254215593 ...
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bank import ensure_utf8_stdout  # noqa: E402

ensure_utf8_stdout()

ROOT = os.path.dirname(os.path.abspath(__file__))
PAR = os.path.join(ROOT, "logs", "parallel")

# keys a worker overrides on top of the shared config
OVERRIDES = {
    "DO_VIDEO": True,
    "DO_QUIZ": True,
    "QUIZ_SUBMIT": True,
    "HEADLESS": True,
    "MAX_CHAPTERS": 0,
    "INCLUDE_CHILD_NODES": True,
    "CASCADE_CHILD_NODES": True,
    "CASCADE_LIMIT": 40,
    "CASCADE_QUIZ": True,
    "REUSE_SESSION": True,
    "VIDEO_RATE": 2.0,
    "VIDEO_MAX_WAIT": 3600,
}


def main() -> int:
    ids = [a for a in sys.argv[1:] if a.isdigit()]
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as fh:
        base = json.load(fh)
    with open(os.path.join(ROOT, "evidence", "course_cards.json"), encoding="utf-8") as fh:
        cards = {c["courseId"]: c for c in json.load(fh)}
    if not ids:
        ids = ["267087606", "254215593", "203417266", "88819267",
               "237168226", "267261427", "267511667"]

    os.makedirs(PAR, exist_ok=True)
    for cid in ids:
        card = cards.get(cid, {})
        cfg = dict(base)
        cfg.update(OVERRIDES)
        cfg["COURSE_ID"] = cid
        if card.get("clazzid"):
            cfg["CLAZZID"] = card["clazzid"]
        if card.get("cpi"):
            cfg["CPI"] = card["cpi"]
        path = os.path.join(PAR, f"config_{cid}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=1)
        # validate immediately
        with open(path, encoding="utf-8") as fh:
            back = json.load(fh)
        print(f"  {cid}  {card.get('name', '?')[:24]:<26} ok  "
              f"CASCADE_LIMIT={back.get('CASCADE_LIMIT')} "
              f"QUIZ_SUBMIT={back.get('QUIZ_SUBMIT')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
