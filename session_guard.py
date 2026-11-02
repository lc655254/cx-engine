# -*- coding: utf-8 -*-
"""Session watchdog: stop a run the moment the platform stops trusting us.

Tonight's biggest loss was not a code bug - it was the session expiring while
several workers kept hammering the platform with a dead cookie. Every request then
hit the login redirect, which looks exactly like the behaviour that gets an
account flagged.

This module gives the runners one cheap, shared check:

    guard = SessionGuard(log=print)
    if not await guard.alive(page):     # probes once every N minutes
        ...stop everything and tell the human to re-scan...

A dead session is now a clean halt instead of hours of pointless requests.
"""
from __future__ import annotations

import os
import time
from typing import Any, Optional
from urllib.parse import urlparse

POST_LOGIN_HOSTS = ("i.chaoxing.com", "mooc1.chaoxing.com", "mooc2-ans.chaoxing.com",
                    "stat2-ans.chaoxing.com", "mooc1-1.chaoxing.com",
                    "noteyoudao.chaoxing.com", "home.chaoxing.com",
                    "mooc1-2.chaoxing.com")
# IMPORTANT: i.chaoxing.com/base is a poor health probe - it can redirect to the
# passport while course pages still work fine (observed 2026-10-14: the base probe
# reported "dead" while probe_play_controls.py successfully started a video).
# The course list is the page the runners actually need, so that is what we test.
PROBE_URLS = [
    "https://mooc1.chaoxing.com/visit/interaction",
    "https://i.chaoxing.com/base",
]


def looks_logged_out(url: str) -> bool:
    """True when a URL is the passport/login surface rather than a course page."""
    u = (url or "").lower()
    if "passport2.chaoxing.com" in u:
        return True
    if "/login" in u or "passport" in u:
        return True
    return False


def looks_logged_in(url: str) -> bool:
    host = urlparse(url or "").netloc.lower()
    if not host or looks_logged_out(url):
        return False
    return any(host.endswith(h) for h in POST_LOGIN_HOSTS)


class SessionGuard:
    """Rate-limited probe of whether the saved session is still accepted."""

    def __init__(self, log=print, interval_s: int = 240,
                 creds_path: Optional[str] = None):
        self.log = log
        self.interval_s = interval_s
        self.creds_path = creds_path or os.path.expanduser("~/.dsh/.credentials.yaml")
        self.last_check = 0.0
        self.checks = 0
        self.lost_at: Optional[float] = None
        self.redirects = 0

    # ------------------------------------------------------------------ probe
    async def alive(self, page, force: bool = False) -> bool:
        now = time.time()
        if not force and (now - self.last_check) < self.interval_s:
            return self.lost_at is None
        self.last_check = now
        self.checks += 1
        # try each probe URL; the course list is the one that matters, so a fail
        # there is only conclusive if every probe fails
        last_url = ""
        for probe in PROBE_URLS:
            try:
                await page.goto(probe, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(2500)
            except Exception as exc:
                self.log(f"[guard] probe {probe[:40]} failed: {type(exc).__name__}")
                continue
            last_url = page.url
            if looks_logged_in(page.url):
                self.log(f"[guard] session alive (check #{self.checks})")
                self.lost_at = None
                return True
        self.lost_at = now
        self.log(f"[guard] SESSION LOST - every probe redirected (last {last_url[:60]})")
        self.log("[guard] stopping: re-authenticate with "
                 "`login.py --qr --interactive --keep-open`")
        return False

    # ------------------------------------------------- redirect-based signal
    def note_navigation(self, url: str) -> bool:
        """Call from a navigation handler. Returns True when this looks like a kick."""
        if looks_logged_out(url):
            self.redirects += 1
            if self.redirects >= 3 and self.lost_at is None:
                self.lost_at = time.time()
                self.log(f"[guard] {self.redirects} login redirects seen - "
                         f"treating the session as lost")
                return True
        return False

    @property
    def lost(self) -> bool:
        return self.lost_at is not None

    def summary(self) -> dict:
        return {"checks": self.checks, "redirects": self.redirects,
                "lost": self.lost,
                "lost_after_s": round(self.lost_at - self.last_check, 1)
                if self.lost_at and self.last_check else None}


async def preflight(cfg: dict, log=print) -> bool:
    """Quick standalone check used before a batch is launched.

    Returns True when a saved session exists AND the platform still accepts it.
    """
    import json
    from playwright.async_api import async_playwright
    from cx_auto import UA, pick_browser, INJECT

    root = os.path.dirname(os.path.abspath(__file__))
    state = cfg.get("STORAGE_STATE") or os.path.join("evidence", "storage_state.json")
    state_path = state if os.path.isabs(state) else os.path.join(root, state)
    if not os.path.exists(state_path):
        log(f"[guard] no saved session at {state_path}")
        log("[guard] run: login.py --qr --interactive --keep-open")
        return False

    pw = await async_playwright().start()
    _c, exe = pick_browser(cfg)
    kwargs = dict(headless=True, args=["--disable-blink-features=AutomationControlled"])
    if exe:
        kwargs["executable_path"] = exe
    br = await pw.chromium.launch(**kwargs)
    ctx = await br.new_context(storage_state=state_path, user_agent=UA, locale="zh-CN")
    page = await ctx.new_page()
    guard = SessionGuard(log=log)
    ok = await guard.alive(page, force=True)
    await ctx.close(); await br.close(); await pw.stop()
    return ok


if __name__ == "__main__":
    import asyncio
    import json
    import sys
    from bank import ensure_utf8_stdout
    ensure_utf8_stdout()
    root = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(root, "config.json"), encoding="utf-8") as fh:
        cfg = json.load(fh)
    ok = asyncio.run(preflight(cfg, log=lambda m: print(m, flush=True)))
    print(f"\npreflight: {'SESSION OK' if ok else 'NO USABLE SESSION'}")
    raise SystemExit(0 if ok else 1)
