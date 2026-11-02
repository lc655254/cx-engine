# -*- coding: utf-8 -*-
"""Minimal offline answer-server for the bank cache.

Serves evidence/bank.json over HTTP so cx_auto.py has a zero-cost, zero-network
answer backend with exact matching. The API shape mirrors the remote banks:

    GET /api/answer?q=<stem>&type=<qtype>     -> {"code":0,"data":{"answer":"B"}}
    GET /api/search?title=<stem>&type=<qtype> -> {"code":0,"data":{"answer":"B"}}
    GET /api/health                           -> counts
    POST /api/remember  {"question":..,"answer":..,"type":..}

Run:
    .venv\\Scripts\\python.exe bank_server.py --port 8000
The default config already points ANSWER_APIS at http://localhost:8000/api/answer.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from bank import (clean_stem, ensure_utf8_stdout, key_of,  # noqa: E402
                  letter_indexes, normalize_answer, text_indexes)

ensure_utf8_stdout()

CACHE = os.path.join(ROOT, "evidence", "bank.json")
_DB = None
_DB_PATH = None


def db_path() -> str:
    # env override lets tests (and alternate profiles) point at another bank
    env = os.environ.get("CX_LOCAL_DB")
    if env:
        return env if os.path.isabs(env) else os.path.join(ROOT, env)
    try:
        with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as fh:
            rel = (json.load(fh).get("LOCAL_DB") or "evidence/tiku.db")
    except Exception:
        rel = "evidence/tiku.db"
    return rel if os.path.isabs(rel) else os.path.join(ROOT, rel)


def get_db():
    """Lazy local sqlite bank; None when it has not been created yet."""
    global _DB, _DB_PATH
    path = db_path()
    if _DB is not None and _DB_PATH == path:
        return _DB
    if not os.path.exists(path):
        return None
    try:
        from bank_db import TikuDB
        _DB = TikuDB(path, log=lambda m: None)
        _DB_PATH = path
    except Exception:
        return None
    return _DB


def load() -> dict:
    if os.path.exists(CACHE):
        try:
            with open(CACHE, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}
    return {}


class Handler(BaseHTTPRequestHandler):
    server_version = "cx-bank/1.0"

    def _json(self, obj, code=200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):        # quieter default logging
        return

    def do_GET(self) -> None:
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        if u.path == "/api/health":
            bank = load()
            db = get_db()
            self._json({"code": 0, "data": {
                "json_cache": len(bank), "json_path": CACHE,
                "local_db": db.count() if db else 0,
                "local_db_path": db_path() if db else None}})
            return
        if u.path in ("/api/answer", "/api/search"):
            stem = (qs.get("q") or qs.get("title") or qs.get("question") or [""])[0]
            qtype = (qs.get("type") or [""])[0]
            if not stem:
                self._json({"code": 1, "msg": "no query"}, 400)
                return
            bank = load()
            k = key_of(stem, qtype)
            hit = bank.get(k)
            if hit is None:                     # type-agnostic fallback
                for cand in bank.values():
                    if clean_stem(cand.get("q", "")) == clean_stem(stem):
                        hit = cand
                        break
            if hit:
                ans = normalize_answer(hit.get("a", ""), qtype)
                print(f"  HIT  {stem[:38]}  -> {ans[:24]}", flush=True)
                self._json({"code": 0, "data": {"answer": ans,
                                                "source": hit.get("source", "local")}})
            else:
                print(f"  miss {stem[:38]}", flush=True)
                self._json({"code": 1, "msg": "not found", "data": {"answer": ""}})
            return
        self._json({"code": 1, "msg": "unknown path"}, 404)

    def do_POST(self) -> None:
        u = urllib.parse.urlparse(self.path)
        if u.path == "/adapter-service/search":
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode("utf-8", "replace")
            try:
                body = json.loads(raw or "{}")
            except Exception:
                self._json({"code": 1, "msg": "bad json"}, 400)
                return
            question = body.get("question") or ""
            options = body.get("options") or []
            qtype = body.get("type", 0)
            # tikuAdapter type codes: 0单选 1多选 2填空 3判断 4问答
            type_name = {0: "单选题", 1: "多选题", 2: "填空题",
                         3: "判断题", 4: "简答题"}.get(qtype, "")
            db = get_db()
            hit = db.search(question, type_name, options) if db else None
            if not hit:
                self._json({"plat": 0, "question": question, "options": options,
                            "type": qtype, "answer": None, "msg": "not found"})
                return
            answers, how = hit
            keys, answer_indexes, texts = [], [], []
            for a in answers:
                s = str(a)
                letters = letter_indexes(s)
                if letters:
                    keys += [chr(65 + i) for i in letters]
                    # letter answers must also carry their option indexes, the
                    # same way text answers do, so callers can use either field
                    answer_indexes += letters
                    continue
                idxs = text_indexes(s, options) if options else []
                if not idxs:
                    texts.append(s)
                answer_indexes += idxs
            self._json({
                "plat": 0, "question": question, "options": options, "type": qtype,
                "answer": {
                    "answerKey": sorted(set(keys)),
                    "answerKeyText": "".join(sorted(set(keys))),
                    "answerIndex": sorted(set(answer_indexes)),
                    "answerText": "#".join(texts) or ",".join(answers),
                    "bestAnswer": answers,
                    "allAnswer": [answers],
                },
                "source": f"local-db:{how}",
            })
            return

        if u.path != "/api/remember":
            self._json({"code": 1, "msg": "unknown path"}, 404)
            return
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace")
        try:
            row = json.loads(raw or "{}")
        except Exception:
            row = dict(urllib.parse.parse_qsl(raw))
        stem = row.get("question") or row.get("q") or row.get("title") or ""
        ans = row.get("answer") or row.get("a") or ""
        qtype = row.get("type") or ""
        if not stem or not ans:
            self._json({"code": 1, "msg": "need question+answer"}, 400)
            return

        # write through to the local sqlite bank when it exists, else the json cache
        db = get_db()
        if db is not None:
            action = db.put(stem, [ans] if isinstance(ans, str) else list(ans),
                            qtype or "简答题", row.get("options") or [],
                            source="server", course=row.get("course") or "")
            self._json({"code": 0, "data": {"entries": db.count(), "action": action}})
            return

        bank = load()
        bank[key_of(stem, qtype)] = {"q": stem, "type": qtype, "a": ans,
                                     "source": "server", "t": 0}
        with open(CACHE, "w", encoding="utf-8") as fh:
            json.dump(bank, fh, ensure_ascii=False, indent=1)
        self._json({"code": 0, "data": {"entries": len(bank)}})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    if not os.path.exists(CACHE):
        with open(CACHE, "w", encoding="utf-8") as fh:
            json.dump({}, fh)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"cx bank server on http://{args.host}:{args.port}  ({len(load())} entries)")
    print("  GET  /api/answer?q=&type=")
    print("  GET  /api/health")
    print("  POST /api/remember")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
