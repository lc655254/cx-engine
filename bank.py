# -*- coding: utf-8 -*-
"""Answer lookup engine with multiple pluggable backends.

Lookup order for every question:
  1. local cache      -> evidence/bank.json
  2. local bank files -> config BANK_FILES
  3. local sqlite     -> config BANK_SQL
  4. remote answer APIs (ANSWER_APIS, JSONPath-ish "dot.path")
  5. AI fallback      -> OpenAI-compatible endpoint, only if env key exists

Every backend returns a normalized answer string. Normalization and option
index mapping live here so the browser layer (cx_auto.py) stays dumb.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:                                    # keep the module importable without network deps
    import requests
except Exception:                       # pragma: no cover
    requests = None                     # type: ignore


@dataclass
class Answer:
    text: str = ""
    source: str = "miss"
    confidence: float = 0.0
    raw: Any = None
    # structured extras for multi-blank / multi-choice questions
    blanks: List[str] = field(default_factory=list)
    indexes: List[int] = field(default_factory=list)
    options: List[str] = field(default_factory=list)

    @property
    def hit(self) -> bool:
        return bool(self.text.strip())


def ensure_utf8_stdout() -> None:
    """Windows consoles default to GBK; force UTF-8 so CJK output survives.

    Safe to call from any number of modules: only reconfigures the existing
    stream instead of stacking TextIOWrapper objects (stacking closes the
    underlying buffer when the first wrapper is collected).
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# chaoxing internal type codes used by most answer APIs
TYPE_CODE = {
    "单选题": "0", "单选": "0", "选择题": "0", "其他": "0",
    "多选题": "1", "多选": "1",
    "判断题": "2", "判断": "2",
    "填空题": "3", "填空": "3",
    "简答题": "4", "简答": "4", "论述题": "4", "名词解释": "4",
}

# question stem prefixes that break answer-API fuzzy matching
STEM_NOISE = [
    r"^\s*\d+\s*[\.、\)）]\s*",
    r"^\s*[（(]\s*[^）)]{0,12}[）)]\s*",
    r"^\s*第\s*\d+\s*题\s*[:：]?\s*",
]

# A stem published as "分别是（ ）和（ ）" must key the same as "分别是[__]和[__]",
# so an empty blank becomes a stable marker instead of being deleted (deleting
# would glue the surrounding words together). A private-use char is used so the
# marker can never collide with characters that appear in real text.
BLANK_TOKEN = "\ue000"

# underscore-style blanks: "OSI共____层" -> "OSI共层"
STEM_TAIL = [
    r"\s*[_＿]{2,}\s*",
]


def strip_blank_markers(s: str) -> str:
    """Normalize empty "(  )" answer placeholders into BLANK_TOKEN."""
    out = []
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch in "（(":
            close = "）" if ch == "（" else ")"
            j = i + 1
            while j < n and s[j] != close:
                j += 1
            if j < n and j - i <= 4:
                inner = s[i + 1:j]
                if inner.strip(" \u3000_＿\t") == "":
                    out.append(BLANK_TOKEN)
                    i = j + 1
                    continue
        out.append(ch)
        i += 1
    return "".join(out)


def _base_stem(text: str) -> str:
    """Strip numbering / score noise and normalize blanks (marker kept)."""
    s = (text or "").strip()
    for pat in STEM_NOISE:
        s = re.sub(pat, "", s)
    s = re.sub(r"[\s\u3000]+", "", s)
    s = re.sub(r"[（(]\s*\d+(\.\d+)?\s*分\s*[)）]$", "", s)
    s = strip_blank_markers(s)
    for pat in STEM_TAIL:                      # underscore-style blanks
        s = re.sub(pat, "", s)
        s = re.sub(pat, "", s)
    s = re.sub(r"[\s\u3000]+", "", s)
    return s.strip()


def key_stem(text: str) -> str:
    """Normalized stem used for answer lookup / cache keys."""
    return _base_stem(text).replace(BLANK_TOKEN, "")


def clean_stem(text: str) -> str:
    """Normalized stem for display and logging (blank marker removed)."""
    return _base_stem(text).replace(BLANK_TOKEN, "").strip()


def key_of(stem: str, qtype: str = "") -> str:
    return f"{qtype}|{key_stem(stem)}"


def normalize_answer(ans: str, qtype: str = "") -> str:
    """Unify separators, drop 'answer:' prefixes, and canonicalise the shape.

    Beyond separator cleanup this now also maps the forms a bank can actually
    return onto what the page expects:

      * 判断题  正确/√/T/true -> 对, 错误/×/F/false -> 错
      * 选择题  letters upper-cased, de-duplicated and sorted ("CAB" -> "ABC")

    Without this, an answer of "正确" or "a" finds no matching radio and is dropped
    silently, which looks exactly like the platform refusing the answer.
    """
    if ans is None:
        return ""
    s = str(ans).strip()
    s = re.sub(r"^(正确)?(答案|参考答案|答)\s*[:：]\s*", "", s)
    s = s.replace("\r", "").replace("\n", " ")
    s = s.replace("。", ".").replace("，", ",").replace("；", ";").replace("、", ",")
    s = re.sub(r"\s+", " ", s)

    if qtype in ("判断题", "判断"):
        s = s.strip(".")
        body = s.strip().strip("。.．")
        positives = ("对", "正确", "是", "√", "T", "t", "true", "True", "YES", "yes")
        negatives = ("错", "错误", "否", "×", "x", "X", "F", "f", "false", "False",
                     "NO", "no")
        if body in positives:
            return "对"
        if body in negatives:
            return "错"
        if any(w in body for w in ("正确", "对")):
            return "对"
        if any(w in body for w in ("错误", "错")):
            return "错"
        return body

    if qtype in ("单选题", "单选", "多选题", "多选"):
        letters = sorted({c.upper() for c in s if c.isalpha() and c.upper() in "ABCDEFGH"})
        if letters:
            return "".join(letters)

    return s.strip()


def letter_indexes(ans: str) -> List[int]:
    """'A' / 'AC' / 'A,C' / 'A C' -> [0] / [0, 2]. Empty when it is not letters.

    The fullmatch pattern is what keeps prose from being misread ('答案是B因为'
    fails the match), so no extra length guard is needed - a single letter is a
    perfectly valid answer for a judgement question.
    """
    if not ans:
        return []
    cand = ans.strip()
    if len(cand) > 16:
        return []
    if re.fullmatch(r"[A-Ha-h]([\s,;|/&和、]*[A-Ha-h])*", cand):
        return sorted({ord(c.upper()) - 65 for c in re.findall(r"[A-Ha-h]", cand)})
    return []


def text_indexes(ans: str, options: Sequence[str]) -> List[int]:
    """Match answer text against option texts."""
    hits: List[int] = []
    norm = key_stem(normalize_answer(ans))
    if not norm:
        return hits
    for i, opt in enumerate(options or []):
        o = re.sub(r"^[A-Ha-h]\s*[\.、\)）:：]?\s*", "", (opt or "").strip())
        o_norm = key_stem(o)
        if not o_norm:
            continue
        if o_norm == norm or o_norm in norm or norm in o_norm:
            hits.append(i)
    return hits


def split_blanks(ans: str, n_boxes: int) -> List[str]:
    """Split a fill-in-the-blank answer into per-box values."""
    if not ans:
        return [""] * max(1, n_boxes)
    # split on ASCII and Chinese punctuation alike: the caller normalises first, but
    # this must not silently collapse a two-blank answer into one when called directly
    parts = [p.strip() for p in re.split(r"[;,；，、\|]|\s{2,}|/{2,}", ans) if p.strip()]
    if not parts:
        parts = [ans.strip()]
    if n_boxes <= 1:
        return [parts[0]]
    while len(parts) < n_boxes:
        parts.append(parts[-1] if parts else "")
    return parts[:n_boxes]


def dig(obj: Any, path: str) -> Any:
    """Read 'data.answer' style paths from nested dicts/lists."""
    cur = obj
    for seg in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(seg)
        elif isinstance(cur, list):
            try:
                cur = cur[int(seg)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def to_answer_string(value: Any) -> str:
    """Coerce whatever an API returned into a single answer string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "".join(str(v) for v in value)
    if isinstance(value, dict):
        for k in ("answer", "content", "text", "value", "data"):
            if k in value:
                return to_answer_string(value[k])
        return json.dumps(value, ensure_ascii=False)
    return str(value)


class Bank:
    def __init__(self, cfg: Dict[str, Any], root: str = ".", log=print):
        self.cfg = cfg or {}
        self.root = root
        self.log = log
        self.sess = requests.Session() if requests else None
        if self.sess is not None:
            self.sess.headers.update({
                "User-Agent": UA,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
            })
        rel = self.cfg.get("CACHE_PATH") or os.path.join("evidence", "bank.json")
        self.cache_path = rel if os.path.isabs(rel) else os.path.join(root, rel)
        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
        self.cache: Dict[str, Dict[str, Any]] = {}
        self.flat: Dict[str, str] = {}
        self._tiku = None
        self._tiku_path = None
        self._ai_layer = None          # lazy ai_answers.AI handle
        self._load_local()
        self._load_files()
        self._load_sql()
        self.stats: Dict[str, int] = {}

    # ---------------- local layers ----------------
    def _load_local(self) -> None:
        if os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, encoding="utf-8") as fh:
                    self.cache = json.load(fh)
            except Exception as exc:
                self.log(f"[bank] cache unreadable: {exc}")

    def _load_files(self) -> None:
        for rel in self.cfg.get("BANK_FILES", []) or []:
            path = rel if os.path.isabs(rel) else os.path.join(self.root, rel)
            if not os.path.exists(path):
                continue
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception as exc:
                self.log(f"[bank] {rel} unreadable: {exc}")
                continue
            if isinstance(data, dict):
                for k, v in data.items():
                    # a bank file may map stem -> answer, or stem -> {answer row}
                    if isinstance(v, dict):
                        stem = v.get("q") or v.get("question") or k
                        ans = to_answer_string(v.get("a") or v.get("answer") or "")
                        typ = v.get("type") or ""
                        if stem and ans:
                            self.flat[key_of(stem, typ)] = ans
                        continue
                    self.flat[key_of(k)] = to_answer_string(v)
            elif isinstance(data, list):
                for row in data:
                    if not isinstance(row, dict):
                        continue
                    stem = row.get("question") or row.get("title") or row.get("q") or ""
                    ans = to_answer_string(row.get("answer") or row.get("a") or "")
                    typ = row.get("type") or row.get("qtype") or ""
                    if stem and ans:
                        self.flat[key_of(stem, typ)] = ans
            self.log(f"[bank] loaded {rel}: {len(self.flat)} entries total")

    def _load_sql(self) -> None:
        rel = self.cfg.get("BANK_SQL")
        if not rel:
            return
        path = rel if os.path.isabs(rel) else os.path.join(self.root, rel)
        if not os.path.exists(path):
            return
        try:
            con = sqlite3.connect(path)
            cur = con.cursor()
            tables = [r[0] for r in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            for t in tables:
                cols = [r[1] for r in cur.execute(f'PRAGMA table_info("{t}")').fetchall()]
                qcol = next((c for c in ("question", "title", "q", "stem") if c in cols), None)
                acol = next((c for c in ("answer", "a", "value") if c in cols), None)
                tcol = next((c for c in ("type", "qtype") if c in cols), None)
                if not qcol or not acol:
                    continue
                sql = f'SELECT "{qcol}", "{acol}"' + (f', "{tcol}"' if tcol else "") + f' FROM "{t}"'
                for row in cur.execute(sql):
                    stem = row[0]
                    ans = to_answer_string(row[1])
                    typ = row[2] if tcol and len(row) > 2 else ""
                    if stem and ans:
                        self.flat[key_of(str(stem), str(typ or ""))] = ans
            con.close()
            self.log(f"[bank] loaded sqlite {rel}: {len(self.flat)} entries total")
        except Exception as exc:
            self.log(f"[bank] sqlite skipped: {exc}")

    # ---------------- write back ----------------
    def remember(self, stem: str, qtype: str, answer: str, source: str) -> None:
        k = key_of(stem, qtype)
        self.cache[k] = {
            "q": stem, "type": qtype, "a": answer,
            "source": source, "t": int(time.time()),
        }
        self.flat[k] = answer
        try:
            try:
                import safejson as _sj
                _sj.save(self.cache_path, self.cache)
            except ImportError:
                os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
                with open(self.cache_path, "w", encoding="utf-8") as fh:
                    json.dump(self.cache, fh, ensure_ascii=False, indent=1)
        except Exception as exc:
            self.log(f"[bank] cache write failed: {exc}")

    # ---------------- remote layers ----------------
    def _remote(self, stem: str, qtype: str, options: Sequence[str]) -> Tuple[str, str]:
        code = TYPE_CODE.get(qtype, "0")
        opts = "|".join(re.sub(r"^[A-Ha-h]\s*[\.、\)）]?\s*", "", (o or "").strip())
                      for o in (options or []))
        if self.sess is None:
            self.log("[bank] requests missing; remote answer APIs disabled")
            return "", "miss"
        for api in self.cfg.get("ANSWER_APIS", []) or []:
            if not api.get("url"):
                continue
            name = api.get("name") or api["url"]
            subs = {"{question}": stem, "{type}": code, "{options}": opts}
            url = api["url"]
            for k, v in subs.items():
                url = url.replace(k, v)
            params = {k: str(v).replace("{question}", stem)
                            .replace("{type}", code)
                            .replace("{options}", opts)
                      for k, v in (api.get("params") or {}).items()}
            body = {k: str(v).replace("{question}", stem)
                           .replace("{type}", code)
                           .replace("{options}", opts)
                     for k, v in (api.get("body") or {}).items()}
            headers = dict(api.get("headers") or {})
            for key in ("token", "key", "apikey"):
                if key in api:
                    headers.setdefault("Authorization", str(api[key]))
            try:
                if (api.get("transport") or "get").lower() == "post":
                    r = self.sess.post(url, data=body, headers=headers or None, timeout=2)
                else:
                    r = self.sess.get(url, params=params, headers=headers or None, timeout=2)
                if r.status_code != 200:
                    self.log(f"[bank] {name} http {r.status_code}")
                    continue
                payload = r.json()
            except Exception as exc:
                self.log(f"[bank] {name} err {type(exc).__name__}")
                continue
            for path in (api.get("paths") or ["data.answer", "answer", "data"]):
                val = to_answer_string(dig(payload, path))
                if val and val.strip() and val.strip() not in ("None", "null"):
                    return normalize_answer(val, qtype), f"api:{name}:{path}"
        return "", "miss"

    def _ai(self, stem: str, qtype: str, options: Sequence[str]) -> Tuple[str, str]:
        """Local AI fallback.

        Two ways in:
          * the explicit AI_ANSWER block in config.json (any OpenAI-compatible
            endpoint with an env-var key), or
          * ai_answers.AI, which reads the local credential file when no env key
            is set - that is what makes quiz answers work in this environment.
        """
        conf = (self.cfg.get("AI_ANSWER") or {})
        base = conf.get("openai_base") or ""
        env_key = conf.get("env_key") or "OPENAI_API_KEY"
        token = os.environ.get(env_key, "")
        explicit = bool(base and token and "PLACEHOLDER" not in base
                        and "COMPAT_BASE" not in base)

        if not explicit:
            # fall back to the shared AI layer (reads ~/.dsh/.credentials.yaml)
            if self._ai_layer is None:
                try:
                    from ai_answers import AI
                    self._ai_layer = AI(log=lambda m: None)
                except Exception:
                    self._ai_layer = False
            if not self._ai_layer or not self._ai_layer.ready:
                return "", "miss"
            text, src = self._ai_layer.ask(stem, qtype, options)
            return (normalize_answer(text, qtype), "ai" if text else "miss")

        if self.sess is None:
            return "", "miss"
        prompt = (
            "Answer this exam question. Reply with ONLY the answer.\n"
            "For single/multiple choice reply with letters, e.g. A or AC.\n"
            "For fill-in-the-blank reply with the values separated by ';'.\n\n"
            f"Type: {qtype}\nQuestion: {stem}\n"
            + ("Options: " + " | ".join(options) if options else "")
        )
        try:
            r = self.sess.post(
                base.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                json={"model": conf.get("model") or "gpt-4o-mini", "temperature": 0,
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=25,
            )
            data = r.json()
            txt = dig(data, "choices.0.message.content") or ""
            txt = normalize_answer(txt, qtype)
            if txt:
                return txt, "ai"
        except Exception as exc:
            self.log(f"[bank] ai err {type(exc).__name__}")
        return "", "miss"

    # ---------------- local sqlite question bank ----------------
    def _tiku_db(self):
        """Lazy handle on evidence/tiku.db (tikuAdapter-compatible schema)."""
        rel = self.cfg.get("LOCAL_DB")
        if not rel:
            return None
        path = rel if os.path.isabs(rel) else os.path.join(self.root, rel)
        if self._tiku is not None and self._tiku_path == path:
            return self._tiku
        if not os.path.exists(path):
            return None
        try:
            from bank_db import TikuDB          # local module, avoids a hard dep
            self._tiku = TikuDB(path, log=lambda m: None)
            self._tiku_path = path
            self.log(f"[bank] local db {rel}: {self._tiku.count()} entries")
        except Exception as exc:
            self.log(f"[bank] local db unavailable: {exc}")
            self._tiku = None
        return self._tiku

    def _ask_local_db(self, stem: str, qtype: str,
                      options: Sequence[str]) -> Tuple[str, str]:
        db = self._tiku_db()
        if db is None:
            return "", "miss"
        try:
            hit = db.search(stem, qtype, list(options))
        except Exception as exc:
            self.log(f"[bank] db search failed: {exc}")
            return "", "miss"
        if not hit:
            return "", "miss"
        answers, how = hit
        if not answers:
            return "", "miss"
        # tikuAdapter convention is [["B"], ["text"]]; prefer a variant that maps
        # onto the question's options, otherwise fall back to the first one.
        joined = ",".join(answers)
        if options and not letter_indexes(joined):
            for a in answers:
                if text_indexes(a, options):
                    return a, f"db:{how}"
        return joined, f"db:{how}"

    # ---------------- public API ----------------
    def query(self, stem: str, qtype: str = "", options: Optional[Sequence[str]] = None) -> Answer:
        options = list(options or [])
        k = key_of(stem, qtype)
        self.stats[k] = self.stats.get(k, 0) + 1

        if k in self.flat and self.flat[k]:
            return self._pack(self.flat[k], "cache", qtype, options)
        if k in self.cache and self.cache[k].get("a"):
            return self._pack(self.cache[k]["a"], "cache", qtype, options)

        text, src = self._ask_local_db(stem, qtype, options)
        if not text:
            text, src = self._remote(stem, qtype, options)
        if not text:
            text, src = self._ai(stem, qtype, options)
        if text:
            return self._pack(text, src, qtype, options)
        return Answer(text="", source="miss", options=options)

    def _pack(self, text: str, src: str, qtype: str, options: Sequence[str]) -> Answer:
        text = normalize_answer(text, qtype)
        ans = Answer(text=text, source=src, confidence=0.9 if "api" in src else 0.5,
                     options=list(options))
        if qtype in ("判断题", "判断"):
            ans.indexes = [0] if text in ("对", "正确", "是", "T", "true", "A", "√") else [1]
            return ans
        if options:
            idx = letter_indexes(text) or text_indexes(text, options)
            if idx:
                ans.indexes = idx
        return ans
