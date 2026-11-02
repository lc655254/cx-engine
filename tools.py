# -*- coding: utf-8 -*-
"""Offline helpers for the answer bank.

    python tools.py stats
    python tools.py export evidence/bank_export.jsonl
    python tools.py import some_bank.json          # {stem: answer} or list of rows
    python tools.py import dump.csv --csv
    python tools.py merge bank_a.json bank_b.json
    python tools.py dbstats                        # local sqlite bank overview
    python tools.py dbsearch "题干" [单选题]
    python tools.py dbexport evidence/export.jsonl # sqlite -> jsonl
"""
from __future__ import annotations

import csv
import json
import os
import sys
from typing import Any, Dict, Iterable, List

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from bank import clean_stem, ensure_utf8_stdout, key_of  # noqa: E402

ensure_utf8_stdout()

CACHE = os.path.join(ROOT, "evidence", "bank.json")


def load_cache() -> Dict[str, Any]:
    if os.path.exists(CACHE):
        with open(CACHE, encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def save_cache(data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)


def rows_of(path: str) -> List[Dict[str, str]]:
    """Accept json dict, json list, jsonl, or csv."""
    out: List[Dict[str, str]] = []
    if path.lower().endswith(".csv"):
        with open(path, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                out.append({k: (v or "") for k, v in row.items()})
        return out

    with open(path, encoding="utf-8") as fh:
        head = fh.read(2048)
        fh.seek(0)
        text = fh.read()

    if text.lstrip().startswith("["):
        data = json.loads(text)
        for row in data:
            if isinstance(row, dict):
                out.append({str(k): str(v) for k, v in row.items()})
        return out

    if text.lstrip().startswith("{"):
        data = json.loads(text)
        for k, v in data.items():
            if isinstance(v, dict):
                stem = v.get("q") or k
                out.append({"question": stem, "answer": v.get("a", ""),
                            "type": v.get("type", "")})
            else:
                out.append({"question": k, "answer": str(v), "type": ""})
        return out

    for line in text.splitlines():                      # jsonl
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            out.append({str(k): str(v) for k, v in obj.items()})
    return out


def normalize_row(row: Dict[str, str]) -> Dict[str, str]:
    stem = (row.get("question") or row.get("title") or row.get("q")
            or row.get("stem") or "").strip()
    ans = (row.get("answer") or row.get("a") or row.get("value")
           or row.get("content") or "").strip()
    typ = (row.get("type") or row.get("qtype") or "").strip()
    return {"question": stem, "answer": ans, "type": typ}


def cmd_import(path: str) -> None:
    rows = [normalize_row(r) for r in rows_of(path)]
    rows = [r for r in rows if r["question"] and r["answer"]]
    cache = load_cache()
    added = upd = 0
    for r in rows:
        k = key_of(r["question"], r["type"])
        if k in cache and cache[k].get("a"):
            if cache[k]["a"] != r["answer"]:
                cache[k] = {"q": r["question"], "type": r["type"], "a": r["answer"],
                            "source": f"import:{os.path.basename(path)}", "t": 0}
                upd += 1
            continue
        cache[k] = {"q": r["question"], "type": r["type"], "a": r["answer"],
                    "source": f"import:{os.path.basename(path)}", "t": 0}
        added += 1
    save_cache(cache)
    print(f"import {path}: parsed {len(rows)}, added {added}, updated {upd}, "
          f"total {len(cache)}")


def cmd_export(path: str) -> None:
    cache = load_cache()
    with open(path, "w", encoding="utf-8") as fh:
        for v in cache.values():
            fh.write(json.dumps({"question": v.get("q"), "type": v.get("type"),
                                 "answer": v.get("a"), "source": v.get("source")},
                                ensure_ascii=False) + "\n")
    print(f"export {len(cache)} rows -> {path}")


def cmd_stats() -> None:
    cache = load_cache()
    by_type: Dict[str, int] = {}
    by_src: Dict[str, int] = {}
    for v in cache.values():
        by_type[v.get("type") or "?"] = by_type.get(v.get("type") or "?", 0) + 1
        by_src[v.get("source") or "?"] = by_src.get(v.get("source") or "?", 0) + 1
    import glob as _g
    homeworks = sorted(_g.glob(os.path.join(ROOT, "logs", "last_run_*.json"))) \
        or [os.path.join(ROOT, "logs", "last_run.json")]
    print(f"cache: {cache and len(cache) or 0} entries  ({CACHE})")
    print("by type:", json.dumps(by_type, ensure_ascii=False))
    print("by src :", json.dumps(by_src, ensure_ascii=False))
    for p in homeworks:
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                print("last run:", fh.read().strip()[:400])
                break


def cmd_merge(paths: Iterable[str]) -> None:
    for p in paths:
        cmd_import(p)


# --------------------------------------------------------------- sqlite bank
def _db_path() -> str:
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as fh:
        cfg = json.load(fh)
    rel = cfg.get("LOCAL_DB") or os.path.join("evidence", "tiku.db")
    return rel if os.path.isabs(rel) else os.path.join(ROOT, rel)


def _open_db():
    from bank_db import TikuDB
    return TikuDB(_db_path())


def cmd_dbstats() -> None:
    path = _db_path()
    if not os.path.exists(path):
        print(f"local db not created yet: {path}")
        print("harvest first: python harvest.py --ask '题干'")
        return
    db = _open_db()
    print(f"local db: {path}")
    print(f"entries : {db.count()}")
    print("by source:")
    for src, c in db.by_source():
        print(f"  {src:18s} {c}")
    types: Dict[str, int] = {}
    for row in db.export_rows():
        types[row["type"] or "?"] = types.get(row["type"] or "?", 0) + 1
    print("by type :", json.dumps(types, ensure_ascii=False))


def cmd_dbsearch(rest: List[str]) -> None:
    if not rest:
        print('usage: tools.py dbsearch "题干" [题型]')
        return
    stem = rest[0]
    qtype = rest[1] if len(rest) > 1 else ""
    path = _db_path()
    if not os.path.exists(path):
        print(f"local db not created yet: {path}")
        return
    db = _open_db()
    hit = db.search(stem, qtype, [])
    if not hit:
        print("miss")
        return
    answers, how = hit
    print(f"hit via {how}: {answers}")


def cmd_dbexport(rest: List[str]) -> None:
    path = _db_path()
    if not os.path.exists(path):
        print(f"local db not created yet: {path}")
        return
    out = rest[0] if rest else os.path.join(ROOT, "logs", "tiku_export.jsonl")
    db = _open_db()
    rows = db.export_rows()
    with open(out, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"exported {len(rows)} rows -> {out}")


if __name__ == "__main__":
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        raise SystemExit(0)
    cmd, rest = argv[0], argv[1:]
    if cmd == "stats":
        cmd_stats()
    elif cmd == "export":
        cmd_export(rest[0] if rest else os.path.join(ROOT, "logs", "bank_export.jsonl"))
    elif cmd in ("import", "merge"):
        for p in rest:
            cmd_import(p)
    elif cmd == "dedupe":
        cache = load_cache()
        save_cache(cache)
        print(f"deduped -> {len(cache)} entries")
    elif cmd == "dbstats":
        cmd_dbstats()
    elif cmd == "dbsearch":
        cmd_dbsearch(rest)
    elif cmd == "dbexport":
        cmd_dbexport(rest)
    else:
        print(__doc__)
