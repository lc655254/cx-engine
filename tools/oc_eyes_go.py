# -*- coding: utf-8 -*-
"""opencode Go eyes: image -> vision model via /inference/go/ endpoints."""
from __future__ import annotations

import base64
import json
import os
import random

import requests

GO_OPENAI = "https://opencode.ai/inference/go/openai/v1/chat/completions"
GO_ANTHROPIC = "https://opencode.ai/inference/go/anthropic/v1/messages"
KEY_FILE = os.path.expanduser(r"~\.dsh\opencode_key.txt")


def load_key() -> str:
    k = os.environ.get("OPENCODE_API_KEY", "").strip()
    if k:
        return k
    try:
        return open(KEY_FILE, encoding="utf-8").read().strip()
    except Exception:
        return ""


def _hdrs(key: str) -> dict:
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "User-Agent": "my-coding-agent/1.0",
        "x-opencode-session": "sess-%08x" % random.getrandbits(32),
    }


def ask_openai(model, image_path, prompt, key, max_tokens=900):
    b64 = base64.b64encode(open(image_path, "rb").read()).decode()
    payload = {"model": model,
               "messages": [{"role": "user", "content": [
                   {"type": "text", "text": prompt},
                   {"type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"}}]}],
               "temperature": 0, "max_tokens": max_tokens}
    r = requests.post(GO_OPENAI, headers=_hdrs(key), json=payload, timeout=120)
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text[:300]}
    return {"status": r.status_code, "data": data}


def ask_anthropic(model, image_path, prompt, key, max_tokens=900):
    b64 = base64.b64encode(open(image_path, "rb").read()).decode()
    payload = {"model": model, "max_tokens": max_tokens,
               "messages": [{"role": "user", "content": [
                   {"type": "image",
                    "source": {"type": "base64", "media_type": "image/png",
                               "data": b64}},
                   {"type": "text", "text": prompt}]}]}
    r = requests.post(GO_ANTHROPIC, headers=_hdrs(key), json=payload, timeout=120)
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text[:300]}
    return {"status": r.status_code, "data": data}


def extract(res) -> str:
    d = res.get("data") or {}
    try:
        if res["status"] == 200:
            if "choices" in d:
                return str(d["choices"][0]["message"]["content"])
            if "content" in d:
                parts = d["content"]
                if isinstance(parts, list):
                    return "".join(p.get("text", "") for p in parts
                                   if isinstance(p, dict))
                return str(parts)
    except Exception:
        pass
    return json.dumps(d, ensure_ascii=False)[:300]
