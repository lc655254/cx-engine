# -*- coding: utf-8 -*-
"""Parallel per-course runner.

Each course is handled by its own cx_auto.py process with its own config and its
own browser context. Isolation is per-process on purpose: a crash or selector
drift in one course cannot corrupt another, and the shared session is only read.

Concurrency is per COURSE, never per chapter - same-course parallel writes are
exactly the pattern the platform flags as abnormal.

    .venv\\Scripts\\python.exe -X utf8 parallel_run.py --courses 267087606,254215593
    .venv\\Scripts\\python.exe -X utf8 parallel_run.py --courses all --concurrency 3
    .venv\\Scripts\\python.exe -X utf8 parallel_run.py --dry
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bank import ensure_utf8_stdout  # noqa: E402

ensure_utf8_stdout()

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
TMP = os.path.join(ROOT, "logs", "parallel")
SKIP = {"237168226"}          # already handled / in use


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_cards() -> list:
    with open(os.path.join(ROOT, "evidence", "course_cards.json"), encoding="utf-8") as fh:
        return json.load(fh)


def make_config(course: dict, base: dict, tag: str) -> str:
    cfg = dict(base)
    cfg["COURSE_ID"] = course["courseId"]
    cfg["CLAZZID"] = course["clazzid"]
    cfg["CPI"] = course["cpi"]
    # per-course browser profile and cache so workers never share browser state
    cfg["STORAGE_STATE"] = os.path.join("evidence", "storage_state.json")
    cfg["CACHE_PATH"] = os.path.join("evidence", f"bank_{tag}.json")
    cfg["SLEEP_BETWEEN_CHAPTERS"] = base.get("SLEEP_BETWEEN_CHAPTERS", [60, 150])
    path = os.path.join(TMP, f"config_{tag}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=1)
    return path


async def run_one(course: dict, base: dict, sem: asyncio.Semaphore,
                  results: list, extra_args: list) -> None:
    tag = course["courseId"]
    async with sem:
        cfg_path = make_config(course, base, tag)
        log_path = os.path.join(TMP, f"run_{tag}.log")
        env = dict(os.environ)
        env["CX_CONFIG"] = cfg_path
        env["CX_PROFILE_TAG"] = tag
        t0 = time.time()
        log(f"START {tag}  {course['name']}")
        # stagger: three browsers initialising in the same second was part of what
        # made the 22:47 launches so fragile
        try:
            import random as _r
            await asyncio.sleep(_r.uniform(2.0, 7.0))
        except Exception:
            pass
        async def spawn() -> int:
            with open(log_path, "w", encoding="utf-8") as lf:
                proc = await asyncio.create_subprocess_exec(
                    PY, "-X", "utf8", os.path.join(ROOT, "cx_auto.py"),
                    *extra_args,
                    cwd=ROOT, env=env, stdout=lf, stderr=asyncio.subprocess.STDOUT)
                return await proc.wait()

        try:
            code = await spawn()
            elapsed = time.time() - t0
            # A worker that died almost immediately never reached the platform, so
            # retrying costs nothing but a few seconds and rescues the course.
            # A worker that ran a while before failing DID consume quota, so it is
            # left alone rather than charged twice.
            if code != 0 and elapsed < 90:
                log(f"RETRY {tag}  exited {code} after {elapsed:.0f}s - one retry")
                try:
                    import subprocess as _sp
                    _sp.run(["taskkill", "/F", "/IM", "chrome.exe"],
                            capture_output=True, timeout=20)
                except Exception:
                    pass
                await asyncio.sleep(6)
                code = await spawn()
                if code == 0:
                    log(f"RETRY {tag}  succeeded on the second attempt")
        except Exception as exc:
            code = -1
            log(f"FAIL  {tag}  {type(exc).__name__}: {exc}")
        dt = time.time() - t0
        summary = parse_summary(log_path)
        results.append({"courseId": tag, "name": course["name"], "exit": code,
                        "sec": round(dt, 1), "summary": summary, "log": log_path})
        log(f"{'OK   ' if code == 0 else 'EXIT ' + str(code)} {tag} "
            f"{course['name']}  {dt:.0f}s  {summary}")


def parse_summary(log_path: str) -> dict:
    """Pull the final counters out of a worker log."""
    out = {}
    if not os.path.exists(log_path):
        return out
    with open(log_path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    for key in ("video_done", "video_fail", "video_none", "quiz_done", "quiz_none",
                "ans_hit", "ans_miss"):
        import re
        m = re.search(rf'"{key}":\s*(\d+)', text)
        if m:
            out[key] = int(m.group(1))
    dones = len(re.findall(r"video: DONE", text))
    nomedia = len(re.findall(r"video: NO_MEDIA", text))
    chapters = len(re.findall(r"=== \[\d+/\d+\]", text))
    out["chapters_seen"] = chapters
    out["chapters_done"] = dones
    out["chapters_no_media"] = nomedia
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--courses", default="",
                    help="comma-separated courseIds, or 'all'")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--chapters", type=int, default=0, help="cap chapters per course")
    ap.add_argument("--quiz", action="store_true", help="also handle quizzes")
    ap.add_argument("--submit", action="store_true", help="submit quizzes")
    ap.add_argument("--dry", action="store_true", help="show the plan only")
    ap.add_argument("--skip-preflight", action="store_true",
                    help="launch even if the saved session looks dead")
    args = ap.parse_args()

    cards = load_cards()
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as fh:
        base = json.load(fh)

    # Refuse to start a batch on a dead session. Tonight proved this is critical:
    # with an expired cookie every worker request hits the login redirect, which
    # is exactly the traffic pattern that gets an account flagged.
    if not args.dry and not args.skip_preflight:
        from session_guard import preflight
        log("preflight: verifying the saved session before launching")
        if not await preflight(base, log=log):
            log("REFUSING TO START: no usable session.")
            log("  re-authenticate:  .venv\\Scripts\\python.exe -X utf8 "
                "login.py --qr --interactive --keep-open --timeout 900")
            return 4

    if args.courses in ("", "all"):
        picked = [c for c in cards if c["courseId"] not in SKIP]
    else:
        want = {x.strip() for x in args.courses.split(",") if x.strip()}
        picked = [c for c in cards if c["courseId"] in want]

    if not picked:
        print("no courses selected")
        return 2

    os.makedirs(TMP, exist_ok=True)
    print(f"plan: {len(picked)} course(s), concurrency {args.concurrency}")
    for c in picked:
        print(f"  {c['courseId']:>10}  {c['name']}")
    if args.dry:
        print("\n(dry run: nothing executed)")
        return 0

    # cx_auto accepts --no-quiz and --quiz-submit (there is no --quiz flag). Build
    # the list once and explicitly: the previous version overwrote extra when
    # --submit was given, which silently discarded the quiz decision and only worked
    # because the worker config happens to default DO_QUIZ to True.
    if args.submit:
        extra = ["--quiz-submit"]
    elif args.quiz:
        extra = ["--quiz"]                # explicit, so a config default cannot veto it
    else:
        extra = ["--no-quiz"]
    if args.chapters:
        extra += ["--chapters", str(args.chapters)]

    sem = asyncio.Semaphore(max(1, args.concurrency))
    results: list = []
    t0 = time.time()
    await asyncio.gather(*[run_one(c, base, sem, results, extra) for c in picked])
    wall = time.time() - t0

    print(f"\n{'=' * 68}")
    for r in sorted(results, key=lambda x: x["courseId"]):
        s = r["summary"]
        print(f"  {r['courseId']:>10}  {r['name'][:22]:<24} exit={r['exit']}  "
              f"{r['sec']:>6.0f}s  chapters {s.get('chapters_done', 0)}/"
              f"{s.get('chapters_seen', 0)} done, {s.get('chapters_no_media', 0)} no-media")
    print(f"\nwall clock: {wall:.0f}s for {len(picked)} course(s) "
          f"@ concurrency {args.concurrency}")
    out = os.path.join(ROOT, "logs", "parallel_report.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"wall": round(wall, 1), "results": results}, fh,
                  ensure_ascii=False, indent=1)
    print(f"report -> {os.path.relpath(out, ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
