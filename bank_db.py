# -*- coding: utf-8 -*-
"""Local question bank on SQLite - tikuAdapter-compatible schema.

Table `tiku` mirrors the tikuAdapter cloud schema so an existing `tiku.db` can be
dropped in, and so third-party tooling can read this file directly:

    id | question | question_text | type | options(json) | answer(json)
       | plat | course | question_hash | hash | source | extra

`answer` stores a list of answer variants (tikuAdapter convention:
[["B"], ["资源共享"]]); the first usable variant wins on read.

Access is serialized behind a lock because the bank is written from the search
loop while the HTTP server reads it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS `tiku` (
  `id` integer PRIMARY KEY AUTOINCREMENT,
  `question` text,
  `question_text` text,          -- only han/alnum, for fuzzy match
  `type` integer,                -- 0单选 1多选 2填空 3判断 4问答
  `options` text,                -- json list
  `answer` text,                 -- json list of variants
  `plat` integer DEFAULT 0,
  `course` text DEFAULT '',
  `question_hash` text,
  `hash` text UNIQUE,
  `source` text DEFAULT '',
  `extra` text DEFAULT '',
  `created_at` integer,
  `updated_at` integer
);
CREATE INDEX IF NOT EXISTS idx_question_hash ON `tiku`(`question_hash`);
CREATE INDEX IF NOT EXISTS idx_question_text ON `tiku`(`question_text`);
"""

TYPE_INT = {
    "单选题": 0, "单选": 0, "选择题": 0, "其他": 0,
    "多选题": 1, "多选": 1,
    "填空题": 2, "填空": 2,
    "判断题": 3, "判断": 3,
    "简答题": 4, "简答": 4, "论述题": 4, "名词解释": 4, "问答": 4,
}
TYPE_NAME = {0: "单选题", 1: "多选题", 2: "填空题", 3: "判断题", 4: "简答题"}

_PUNCT = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)


def text_only(s: str) -> str:
    """Keep only CJK + alnum - the tikuAdapter fuzzy-match normal form."""
    return _PUNCT.sub("", (s or "").lower())


def md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


class TikuDB:
    def __init__(self, path: str, log=print):
        self.path = path
        self.log = log
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.RLock()
        self._init()
        self.stats: Dict[str, int] = {"insert": 0, "update": 0, "skip": 0}

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init(self) -> None:
        with self._lock, self._conn() as conn:
            conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ keys
    @staticmethod
    def make_hash(question: str, options: Sequence[str], qtype, plat: int = 0) -> str:
        opts = json.dumps(sorted(options or []), ensure_ascii=False)
        return md5(text_only(question) + opts + str(qtype) + str(plat))

    # ------------------------------------------------------------------ write
    def put(self, question: str, answers: Iterable[str], qtype: str = "单选题",
            options: Optional[Sequence[str]] = None, source: str = "harvest",
            course: str = "", extra: str = "", plat: int = 0) -> str:
        """Insert or merge one question. Returns 'insert' | 'update' | 'skip'."""
        question = (question or "").strip()
        variants = [a.strip() for a in (answers or []) if str(a).strip()]
        if not question or not variants:
            return "skip"
        t = TYPE_INT.get(qtype, 0)
        opts = list(options or [])
        h = self.make_hash(question, opts, t, plat)
        qh = md5(text_only(question))
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT id, answer FROM tiku WHERE hash=?", (h,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO tiku (question, question_text, type, options, answer,"
                    " plat, course, question_hash, hash, source, extra, created_at,"
                    " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (question, text_only(question), t,
                     json.dumps(opts, ensure_ascii=False),
                     json.dumps([variants], ensure_ascii=False),
                     plat, course, qh, h, source, extra,
                     int(time.time()), int(time.time())))
                self.stats["insert"] += 1
                return "insert"
            try:
                existing = json.loads(row["answer"] or "[]")
            except Exception:
                existing = []
            flat = {json.dumps(v, ensure_ascii=False, sort_keys=True) for v in existing}
            new = json.dumps(variants, ensure_ascii=False, sort_keys=True)
            if new in flat:
                self.stats["skip"] += 1
                return "skip"
            existing.append(variants)
            conn.execute("UPDATE tiku SET answer=?, source=?, updated_at=? WHERE id=?",
                         (json.dumps(existing, ensure_ascii=False), source,
                          int(time.time()), row["id"]))
            self.stats["update"] += 1
            return "update"

    def put_many(self, rows: Iterable[Dict[str, Any]]) -> Dict[str, int]:
        out = {"insert": 0, "update": 0, "skip": 0}
        for r in rows:
            out[self.put(r.get("question", ""), r.get("answers") or [],
                         r.get("type", "单选题"), r.get("options"),
                         r.get("source", "bulk"), r.get("course", ""),
                         r.get("extra", ""), r.get("plat", 0))] += 1
        return out

    # ------------------------------------------------------------------- read
    def search(self, question: str, qtype: str = "", options: Optional[Sequence[str]] = None,
               limit: int = 5) -> Optional[Tuple[List[str], str]]:
        """Exact hash first, then normalized-text match. Returns (answers, how)."""
        question = (question or "").strip()
        if not question:
            return None
        t = TYPE_INT.get(qtype, None)
        opts = list(options or [])

        with self._lock, self._conn() as conn:
            # 1) exact hash over every type (question text + options identify it)
            for ty in ([t] if t is not None else []) + [0, 1, 2, 3, 4]:
                if ty is None:
                    continue
                h = self.make_hash(question, opts, ty)
                row = conn.execute(
                    "SELECT answer, type FROM tiku WHERE hash=? LIMIT 1", (h,)).fetchone()
                if row:
                    ans = self._first_variant(row["answer"])
                    if ans:
                        return ans, "hash"

            # 2) normalized question text (ignores option-order and type drift)
            qh = md5(text_only(question))
            row = conn.execute(
                "SELECT answer FROM tiku WHERE question_hash=? LIMIT 1", (qh,)).fetchone()
            if row:
                ans = self._first_variant(row["answer"])
                if ans:
                    return ans, "text"

            # 3) containment: stored stem is a prefix/substring of the asked one
            norm = text_only(question)
            if len(norm) >= 8:
                rows = conn.execute(
                    "SELECT question_text, answer FROM tiku"
                    " WHERE length(question_text) >= 6 LIMIT 20000").fetchall()
                for r in rows:
                    a = r["question_text"] or ""
                    if a and (a in norm or norm in a):
                        ans = self._first_variant(r["answer"])
                        if ans:
                            return ans, "fuzzy"
        return None

    @staticmethod
    def _first_variant(raw: str) -> List[str]:
        try:
            variants = json.loads(raw or "[]")
        except Exception:
            return []
        if isinstance(variants, str):
            return [variants]
        for v in variants:
            if isinstance(v, list) and v:
                return [str(x) for x in v]
            if isinstance(v, str) and v:
                return [v]
        return []

    def count(self) -> int:
        with self._lock, self._conn() as conn:
            return conn.execute("SELECT COUNT(*) AS c FROM tiku").fetchone()["c"]

    def by_source(self) -> List[Tuple[str, int]]:
        with self._lock, self._conn() as conn:
            return [(r["source"], r["c"]) for r in conn.execute(
                "SELECT source, COUNT(*) AS c FROM tiku GROUP BY source ORDER BY c DESC")]

    def export_rows(self) -> List[Dict[str, Any]]:
        with self._lock, self._conn() as conn:
            out = []
            for r in conn.execute("SELECT * FROM tiku"):
                try:
                    opts = json.loads(r["options"] or "[]")
                except Exception:
                    opts = []
                ans = self._first_variant(r["answer"])
                out.append({"question": r["question"], "type": TYPE_NAME.get(r["type"], ""),
                            "options": opts, "answer": ans, "source": r["source"],
                            "course": r["course"], "extra": r["extra"]})
            return out
