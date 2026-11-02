# -*- coding: utf-8 -*-
"""Static audit of the runner: find the bug classes that wasted today.

Today's most expensive defect was

    TypeError: finish_video_frame() got an unexpected keyword argument 'log'

i.e. a call site and a signature drifting apart. It broke EVERY video node while
showing up only as "FAIL 0/1". This audit looks for that whole family of problems
before they cost another day:

  1. calls to our own functions whose arguments do not match the signature
  2. `except: pass` blocks that could swallow a real failure
  3. config keys read by the code that do not exist in config.json
  4. modules referenced but not importable

    .venv\\Scripts\\python.exe -X utf8 audit.py
"""
from __future__ import annotations

import ast
import importlib
import inspect
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

FILES = ["cx_auto.py", "task_points.py", "bank.py", "humanize.py",
         "session_guard.py", "node_cascade.py", "course_nav.py",
         "parallel_run.py", "supervise.py", "ai_answers.py", "bank_db.py",
         "auto_quiz.py", "sweep_quizzes.py", "preflight.py"]


def load_signatures() -> dict:
    """Map function name -> accepted parameter names, for our own modules."""
    sigs: dict = {}
    for f in FILES:
        path = os.path.join(ROOT, f)
        if not os.path.exists(path):
            continue
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except Exception:
            continue
        mod = f[:-3]
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = [a.arg for a in node.args.args]
                if node.args.vararg:
                    args.append("*" + node.args.vararg.arg)
                args += [a.arg for a in node.args.kwonlyargs]
                if node.args.kwarg:
                    args.append("**" + node.args.kwarg.arg)
                sigs[f"{mod}.{node.name}"] = (args, node.lineno, f)
    return sigs


def find_bad_calls(sigs: dict) -> list:
    """Calls with a keyword our own function does not accept."""
    problems = []
    known = {}
    for key, (args, line, f) in sigs.items():
        known.setdefault(key.split(".", 1)[1], []).append((args, line, f))

    for f in FILES:
        path = os.path.join(ROOT, f)
        if not os.path.exists(path):
            continue
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = None
            if isinstance(fn, ast.Name):
                name = fn.id
            elif isinstance(fn, ast.Attribute):
                # only self.<method>(...) refers to our own code; subprocess.run,
                # json.load etc. are library calls that must not be matched
                if not (isinstance(fn.value, ast.Name) and fn.value.id == "self"):
                    continue
                name = fn.attr
            if not name or name not in known:
                continue
            kws = [k.arg for k in node.keywords if k.arg]
            if not kws:
                continue
            for args, def_line, def_file in known[name]:
                if any(a.startswith("**") for a in args):
                    continue
                if "*" not in args and len(node.args) > len(args):
                    continue
                bad = [k for k in kws if k not in args]
                if bad and all(k not in args for k in kws):
                    problems.append(
                        f"{f}:{node.lineno} calls {name}(...) with {bad} "
                        f"but {def_file}:{def_line} accepts {args}")
    return problems


def find_silent_excepts() -> list:
    """`except ...: pass` with no logging - the class that hid real failures."""
    hits = []
    for f in FILES:
        path = os.path.join(ROOT, f)
        if not os.path.exists(path):
            continue
        lines = open(path, encoding="utf-8").read().splitlines()
        for i, ln in enumerate(lines):
            s = ln.strip()
            if not s.startswith("except"):
                continue
            # look at what follows
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if nxt == "pass":
                hits.append(f"{f}:{i + 1} silent except (pass)")
    return hits


def config_audit() -> list:
    """Config keys the code reads that config.json does not define."""
    try:
        cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    except Exception as exc:
        return [f"config.json unreadable: {exc}"]
    keys = set()
    import re
    for f in FILES:
        path = os.path.join(ROOT, f)
        if not os.path.exists(path):
            continue
        src = open(path, encoding="utf-8").read()
        keys |= set(re.findall(r'(?:self\.)?cfg(?:\.get\(|\.)["\']([A-Z0-9_]{2,})["\']', src))
        keys |= set(re.findall(r'self\.cfg\.get\(["\']([A-Z0-9_]{2,})["\']', src))
    missing = sorted(k for k in keys if k not in cfg)
    return missing


def import_audit() -> list:
    out = []
    for f in FILES:
        mod = f[:-3]
        try:
            importlib.import_module(mod)
        except Exception as exc:
            out.append(f"{mod}: {type(exc).__name__}: {str(exc)[:70]}")
    return out


def find_duplicate_defs() -> list:
    """Top-level definitions that appear more than once in a file.

    Python keeps the LAST one, silently discarding everything before it. That is how
    task_points.py ended up running a stale finish_video_frame for several rounds
    while every fix went into the copy that was thrown away - no error, no warning,
    just fixes that appeared not to work.
    """
    import ast as _ast
    out = []
    for f in FILES:
        p = os.path.join(ROOT, f)
        if not os.path.exists(p):
            continue
        try:
            tree = _ast.parse(open(p, encoding="utf-8").read())
        except Exception:
            continue
        seen = {}
        for node in tree.body:
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef,
                                 _ast.ClassDef)):
                seen.setdefault(node.name, []).append(node.lineno)
        for name, lns in seen.items():
            if len(lns) > 1:
                out.append(f"{f}: {name} defined {len(lns)}x at lines {lns}")
    return out


def find_undefined_names() -> list:
    """Names used but never bound - the class that produced log(f"{no}").

    Catches typos in f-strings and half-applied patches, which otherwise only surface
    when that exact line finally runs (sometimes hours into a batch).
    """
    import ast as _ast
    import builtins as _bi
    problems = []
    for f in FILES:
        path = os.path.join(ROOT, f)
        if not os.path.exists(path):
            continue
        try:
            tree = _ast.parse(open(path, encoding="utf-8").read())
        except Exception:
            continue
        mod_level = set()
        for n in tree.body:
            if isinstance(n, (_ast.FunctionDef, _ast.ClassDef)):
                mod_level.add(n.name)
            elif isinstance(n, _ast.Assign):
                for t in n.targets:
                    if isinstance(t, _ast.Name):
                        mod_level.add(t.id)
            elif isinstance(n, _ast.Import):
                for a in n.names:
                    mod_level.add((a.asname or a.name).split(".")[0])
            elif isinstance(n, _ast.ImportFrom):
                for a in n.names:
                    mod_level.add(a.asname or a.name)
        for node in _ast.walk(tree):
            if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                continue
            local = {n.id for n in _ast.walk(node)
                     if isinstance(n, _ast.Name) and isinstance(n.ctx, _ast.Store)}
            local |= {a.arg for a in node.args.args}
            local |= {a.arg for a in node.args.kwonlyargs}
            if node.args.vararg:
                local.add(node.args.vararg.arg)
            if node.args.kwarg:
                local.add(node.args.kwarg.arg)
            local |= {h.name for h in _ast.walk(node)
                      if isinstance(h, _ast.ExceptHandler) and h.name}
            # names bound by comprehensions / with-as inside the function
            local |= {n.id for n in _ast.walk(node) if isinstance(n, _ast.Name)}
            used = {n.id for n in _ast.walk(node)
                    if isinstance(n, _ast.Name) and isinstance(n.ctx, _ast.Load)}
            undef = used - local - set(dir(_bi)) - mod_level
            if undef:
                problems.append(f"{f}:{node.lineno} {node.name}() -> {sorted(undef)[:4]}")
    return problems


def main() -> int:
    print("=" * 74)
    print("1) 调用与签名不匹配（今天的元凶类）")
    sigs = load_signatures()
    bad = find_bad_calls(sigs)
    if bad:
        for b in bad:
            print(f"   ✗ {b}")
    else:
        print("   ✓ 没有发现")

    print("\n2) 静默 except（会藏住真实失败）")
    silent = find_silent_excepts()
    print(f"   共 {len(silent)} 处")
    for s in silent[:12]:
        print(f"   · {s}")
    if len(silent) > 12:
        print(f"   ... 还有 {len(silent) - 12} 处")

    print("\n3) 代码读取但 config.json 里没有的键")
    missing = config_audit()
    if missing:
        for m in missing[:20]:
            print(f"   ? {m}")
        print(f"   （共 {len(missing)} 个；这些会走 .get 的默认值，通常无害，"
              f"但值得确认默认值是你想要的）")
    else:
        print("   ✓ 全部存在")

    print("\n4a) 重复的顶层定义（后者会静默覆盖前者）")
    dups = find_duplicate_defs()
    if dups:
        for d in dups:
            print(f"   !! {d}")
    else:
        print("   ✓ 没有发现")

    print("\n4) 用到但从未绑定的变量（f-string 笔误、半途补丁）")
    undef = find_undefined_names()
    if undef:
        for u in undef[:12]:
            print(f"   ? {u}")
    else:
        print("   ✓ 没有发现")

    print("\n5) 模块导入")
    imp = import_audit()
    if imp:
        for i in imp:
            print(f"   ✗ {i}")
    else:
        print("   ✓ 全部可导入")

    print("=" * 74)
    total = len(bad) + len(imp)
    print(f"严重问题: {total}   静默 except: {len(silent)}   未定义配置键: {len(missing)}")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
