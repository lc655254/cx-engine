# -*- coding: utf-8 -*-
"""Question answering through an LLM, with the key read from local credentials.

The key is taken from ~/.dsh/.credentials.yaml (the DEEPSEEK_API_KEY ref) at call
time and never written to disk or printed. Any OpenAI-compatible endpoint works;
override with AI_BASE / AI_MODEL / AI_KEY_FILE if you want a different provider.

Design notes that matter for quiz accuracy:
  * temperature 0
  * the prompt asks for a bare answer line, no prose, so parsing is deterministic
  * answer shape depends on the question type:
        单选 -> one letter          多选 -> several letters
        判断 -> 对/错               填空 -> values separated by ';'
  * one retry with a stricter instruction when the reply cannot be parsed
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Dict, List, Optional, Sequence, Tuple

try:
    import requests
except Exception:                       # pragma: no cover
    requests = None                     # type: ignore

DEFAULT_BASE = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"
CRED_PATHS = [
    os.path.expanduser("~/.dsh/.credentials.yaml"),
    os.path.expanduser("~/.config/dsh/.credentials.yaml"),
]
ENV_NAMES = ["DEEPSEEK_API_KEY", "OPENAI_API_KEY", "AI_API_KEY"]

_SYSTEM = (
    "你是一个严谨的答题助手。只输出答案本身，不要解释、不要标点之外的任何文字。"
    "单选题输出一个大写字母；多选题输出多个大写字母（如 ABD）；"
    "判断题只输出「对」或「错」；填空题用分号分隔多个空（如 7;七）；"
    "简答题直接给出要点。"
)


def _scrub(txt: str) -> str:
    """Never let a key reach a log."""
    return re.sub(r"(sk-[A-Za-z0-9\-_]{6})[A-Za-z0-9\-_]+", r"\1***", txt or "")


def load_key() -> Optional[str]:
    for name in ENV_NAMES:
        v = os.environ.get(name)
        if v:
            return v.strip()
    override = os.environ.get("AI_KEY_FILE")
    paths = [override] if override else CRED_PATHS
    for p in paths:
        if p and os.path.exists(p):
            try:
                raw = open(p, encoding="utf-8").read()
            except Exception:
                continue
            m = re.search(r"DEEPSEEK_API_KEY\s*:\s*[\"']?([A-Za-z0-9\-_]{16,})", raw)
            if m:
                return m.group(1)
            m2 = re.search(r"(sk-[A-Za-z0-9\-_]{16,})", raw)
            if m2:
                return m2.group(1)
    return None


class AI:
    def __init__(self, base: str = "", model: str = "", key: str = "", log=print):
        self.base = (base or os.environ.get("AI_BASE") or DEFAULT_BASE).rstrip("/")
        self.model = model or os.environ.get("AI_MODEL") or DEFAULT_MODEL
        self.key = key or load_key() or ""
        self.log = log
        self.sess = requests.Session() if requests else None
        self.calls = 0
        self.errors = 0

    @property
    def ready(self) -> bool:
        return bool(self.key and self.sess)

    def ask(self, stem: str, qtype: str = "", options: Sequence[str] = (),
            course: str = "") -> Tuple[str, str]:
        """Returns (answer_text, source). Empty answer means it could not answer."""
        if not self.ready:
            return "", "no_key"
        if qtype in ("判断题", "判断"):
            shape = "只输出「对」或「错」"
        elif qtype in ("多选题", "多选"):
            shape = "输出多个大写字母，如 ABD"
        elif qtype in ("填空题", "填空"):
            shape = "多个空用分号分隔，如 7;七"
        elif qtype in ("简答题", "简答", "论述题", "名词解释"):
            shape = "直接给出要点，无需分点符号"
        else:
            shape = "输出一个大写字母"

        opt_txt = ""
        if options:
            opt_txt = "\n选项：\n" + "\n".join(
                f"{chr(65 + i)}. {re.sub(r'^[A-Ha-h][\\.、\\)）]?\\s*', '', str(o)).strip()}"
                for i, o in enumerate(options))
        user = (f"课程：{course}\n题型：{qtype or '单选题'}\n题目：{stem}{opt_txt}\n\n"
                f"要求：{shape}。只输出答案。")

        for attempt in range(2):
            try:
                self.calls += 1
                r = self.sess.post(
                    self.base + "/chat/completions",
                    headers={"Authorization": f"Bearer {self.key}",
                             "Content-Type": "application/json"},
                    json={"model": self.model, "temperature": 0,
                          "max_tokens": 200,
                          "messages": [{"role": "system", "content": _SYSTEM},
                                       {"role": "user", "content": user}]},
                    timeout=60)
                if r.status_code != 200:
                    self.errors += 1
                    self.log(f"[ai] HTTP {r.status_code} {_scrub(r.text)[:90]}")
                    time.sleep(2)
                    continue
                data = r.json()
                txt = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
                ans = self._parse(txt or "", qtype)
                if ans:
                    return ans, "ai"
                if attempt == 0:
                    user += "\n（上一次回复无法解析，请只输出答案字符。）"
                    continue
            except Exception as exc:
                self.errors += 1
                self.log(f"[ai] {type(exc).__name__}: {_scrub(str(exc))[:80]}")
                time.sleep(2)
        return "", "ai_failed"

    @staticmethod
    def _parse(text: str, qtype: str) -> str:
        t = (text or "").strip()
        if not t:
            return ""
        t = re.sub(r"^(答案|正确答案|Answer)\s*[:：]\s*", "", t, flags=re.I)
        t = t.splitlines()[0].strip()
        t = re.sub(r"[。．\.]+$", "", t)
        if qtype in ("判断题", "判断"):
            if re.search(r"对|正确|是|√|T\b", t):
                return "对"
            if re.search(r"错|错误|否|×|F\b", t):
                return "错"
            return ""
        if qtype in ("多选题", "多选", "单选题", "单选") or not qtype:
            letters = "".join(sorted(set(re.findall(r"[A-Ha-h]", t)))).upper()
            if letters and len(t) <= 20:
                return letters
            m = re.search(r"[A-Ha-h]{1,8}", t)
            return m.group(0).upper() if m else ""
        if qtype in ("填空题", "填空"):
            parts = [p.strip() for p in re.split(r"[;；,，、]\s*", t) if p.strip()]
            return ";".join(parts) if parts else t
        return t


if __name__ == "__main__":
    import sys
    from bank import ensure_utf8_stdout
    ensure_utf8_stdout()
    ai = AI(log=lambda m: print(_scrub(m)))
    print(f"endpoint : {ai.base}")
    print(f"model    : {ai.model}")
    print(f"key      : {'loaded (' + str(len(ai.key)) + ' chars)' if ai.key else 'MISSING'}")
    if len(sys.argv) > 1:
        ans, src = ai.ask(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "单选题")
        print(f"answer   : {ans!r}  ({src})")
        print(f"calls={ai.calls} errors={ai.errors}")
