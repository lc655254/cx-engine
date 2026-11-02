# -*- coding: utf-8 -*-
"""Chaoxing course automation - local fixture runner.

Two mechanisms that matter:

  1. video: jump straight to the tail by assigning video.currentTime and then
     re-dispatching the media events the player listens to. The progress
     heartbeat the player sends upstream keeps isdrag=0, so the task point is
     accepted without actually watching anything.

  2. quiz: scrape stems + options from the DOM, resolve them through bank.py
     (local cache -> bank files -> remote answer APIs -> AI fallback), click or
     fill the answers, optionally submit.

Run:
    .venv\\Scripts\\python.exe cx_auto.py --probe      # inspect DOM only
    .venv\\Scripts\\python.exe cx_auto.py             # full run
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import random
import re
import sys
import time
from urllib.parse import urlparse

from bank import Bank, clean_stem, ensure_utf8_stdout, key_of, split_blanks  # noqa: E402
import safejson  # noqa: E402
from course_nav import (fetch_course_cards as nav_fetch_cards,  # noqa: E402
                        open_course as nav_open_course,
                        read_chapters as nav_read_chapters,
                        read_study_page as nav_read_study_page)
from task_points import finish_pdf, finish_video_frame, walk_reader  # noqa: E402
from node_cascade import list_child_nodes  # noqa: E402

ensure_utf8_stdout()

ROOT = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(ROOT, "config.json")

LOGIN_URL = ("https://passport2.chaoxing.com/login"
             "?fid=&newversion=true&refer=https%3A%2F%2Fi.chaoxing.com")
# NOTE: i.chaoxing.com/base is NOT a reliable liveness probe - it can bounce to the
# passport while course pages still work (observed 2026-10-14: it reported dead
# while a course page loaded and a video started). Probe the course list instead;
# that is the page the runners actually need.
PROBE_HOME = "https://mooc1.chaoxing.com/visit/interaction"
PROBE_FALLBACKS = ["https://mooc2-ans.chaoxing.com/mooc2-ans/visit/interaction",
                   "https://i.chaoxing.com/base"]
# task-point media lives in per-item iframes under these module paths
MEDIA_HINTS = ("/modules/audio/", "/modules/video/", "/ananas/modules/ai-video/")
# quizzes/assignments are served as their own work pages
WORK_HINTS = ("/work/doHomeWork", "/work/selectWorkQuestion", "/exam/test",
              "/work/phoneWork", "/work/doHomeWorkNew")
COURSE_URL = "https://mooc1.chaoxing.com/mycourse/studentcourse?courseId={cid}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

# Prefer a locally installed Chrome/Edge so the run does not depend on the
# playwright chromium download (which fails behind some networks).
BROWSER_CANDIDATES = [
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
    os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
]


def pick_browser(cfg: dict):
    """Returns (channel, executable_path) for chromium.launch()."""
    forced = cfg.get("BROWSER_PATH")
    if forced:
        return None, forced
    for p in BROWSER_CANDIDATES:
        if p and os.path.exists(p):
            log(f"browser: using local install {p}")
            return None, p
    log("browser: no local Chrome/Edge, falling back to bundled chromium")
    return None, None

# ---------------------------------------------------------------- page script
INJECT = r"""
(() => {
  // (1) lift the playbackRate clamp installed by the player
  try {
    const proto = HTMLMediaElement.prototype;
    const d = Object.getOwnPropertyDescriptor(proto, 'playbackRate');
    Object.defineProperty(proto, 'playbackRate', {
      configurable: true,
      get() { return d.get.call(this); },
      set(v) { d.set.call(this, v); }          // pass through, no clamping
    });
  } catch (e) {}

  // (2) jump to the tail: set currentTime, then replay the events the player
  //     uses to update its internal "watched" state.
  //     Lazily-loaded players report duration=NaN until metadata arrives, so
  //     force a load and wait briefly instead of failing with no_duration.
  window.__primeMedia = async function (waitMs) {
    waitMs = waitMs || 8000;
    const v = document.querySelector('video,audio');
    if (!v) return { ok: false, why: 'no_media' };
    if (v.duration && isFinite(v.duration)) return { ok: true, duration: v.duration };
    try { v.preload = 'auto'; } catch (e) {}
    try { v.load(); } catch (e) {}
    try { await v.play(); v.pause(); } catch (e) {}
    const t0 = Date.now();
    while (Date.now() - t0 < waitMs) {
      if (v.duration && isFinite(v.duration)) {
        return { ok: true, duration: v.duration, waited: Date.now() - t0 };
      }
      if (v.readyState >= 1) return { ok: true, duration: v.duration || 0 };
      await new Promise(r => setTimeout(r, 250));
    }
    return { ok: false, why: 'no_duration', readyState: v.readyState };
  };

  window.__jumpToEnd = function (tail) {
    tail = (tail === undefined) ? 0.4 : tail;
    const v = document.querySelector('video,audio');
    if (!v) return { ok: false, why: 'no_media' };
    const dur = v.duration;
    if (!dur || !isFinite(dur)) return { ok: false, why: 'no_duration',
                                          readyState: v.readyState };
    try { v.muted = true; v.play(); } catch (e) {}
    v.currentTime = Math.max(0, dur - tail);
    ['seeking','seeked','timeupdate','progress','waiting','playing','canplay',
     'canplaythrough','ratechange'].forEach(function (t) {
      try { v.dispatchEvent(new Event(t, { bubbles: true })); } catch (e) {}
    });
    return { ok: true, duration: dur, at: v.currentTime };
  };

  // (3) accelerated playback for MODE=speed
  window.__setRate = function (r) {
    const ms = document.querySelectorAll('video,audio');
    let n = 0;
    ms.forEach(function (m) {
      try { m.muted = true; m.playbackRate = r; m.play(); n++; } catch (e) {}
    });
    return n;
  };

  // (4) keep reporting while the tab is backgrounded
  try {
    Object.defineProperty(document, 'hidden', { get: () => false, configurable: true });
    Object.defineProperty(document, 'visibilityState', { get: () => 'visible', configurable: true });
  } catch (e) {}

  // (5) tap every progress/answer request so we can confirm it really landed
  window.__net = [];
  const tap = function (url, kind) {
    try {
      const u = String(url || '');
      if (/multimedia|job|report|video|work|answer|submit|exam/i.test(u)) {
        window.__net.push({ t: Date.now(), kind: kind, url: u.slice(0, 240) });
      }
    } catch (e) {}
  };
  const of = window.fetch;
  window.fetch = function () {
    try {
      const a0 = arguments[0];
      tap((typeof a0 === 'string') ? a0 : (a0 && a0.url), 'fetch');
    } catch (e) {}
    return of.apply(this, arguments);
  };
  const oo = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (m, u) { tap(u, m); return oo.apply(this, arguments); };

  // (6) answer sinks exposed to python
  window.__fillText = function (el, val) {
    if (!el) return false;
    const tag = el.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA') {
      el.focus();
      el.value = val;
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
      el.dispatchEvent(new Event('blur', { bubbles: true }));
      return true;
    }
    if (el.isContentEditable || tag === 'DIV') {
      el.focus();
      el.innerText = val;
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('blur', { bubbles: true }));
      return true;
    }
    return false;
  };
  return true;
})();
"""


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def strip_units(v: str) -> str:
    """Filling a blank with a unit the stem already carries doubles it.

    "要___次" + answer "1次" renders as "要1次次". Physics quizzes had most
    fill-ins wrong for exactly this reason, so a bare number followed only by CJK
    unit text is trimmed to the number.
    """
    v = (v or "").strip()
    m = re.match(r"^(\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*([\u4e00-\u9fff%°℃]+)$", v)
    if m:
        return m.group(1)
    return v


class _ReviewAnswer:
    """Answer shim for review-learned answers applied by question position.

    The work page renders stems in an obfuscated font, so a clean stem learned
    from the review page cannot be looked up again there; the question ORDER is
    the same though, so positional re-use is the reliable path.
    """

    def __init__(self, text: str):
        self.hit = True
        self.source = "review"
        self.text = (text or "").strip()
        t = self.text
        if t in ("对", "错"):
            self.indexes = [0 if t == "对" else 1]
        else:
            self.indexes = [ord(c) - 65 for c in t.upper() if "A" <= c <= "H"]


class Session:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.bank = Bank(cfg, root=ROOT, log=log)
        self.pw = None
        self.br = None
        self.ctx = None
        self.page = None
        self.stats = {"video_done": 0, "video_fail": 0, "video_none": 0,
                      "quiz_done": 0, "quiz_none": 0,
                      "ans_hit": 0, "ans_miss": 0, "answer_samples": []}
        self.unanswered: list = []
        self._cookies_dumped = False
        self._session_loaded = False

    # ---------------------------------------------------------- lifecycle
    async def start(self):
        """Launch the browser, retrying a crash during startup.

        `Browser.new_context: Target crashed` killed five of seven workers in the
        22:47 run. The failure is transient and resource-related - the browser process
        dies before it can create a context - so retrying costs seconds and saves the
        whole worker. Without this a single crash discarded an entire course's quota.
        """
        last = None
        for attempt in range(1, 4):
            try:
                await self._start_once()
                if attempt > 1:
                    log(f"start: browser came up on attempt {attempt}")
                return
            except Exception as exc:
                last = exc
                log(f"start: attempt {attempt}/3 failed "
                    f"({type(exc).__name__}: {str(exc)[:70]})")
                await self._teardown()
                # a failed launch can leave chrome processes behind; clear them so the
                # retry is not competing with the corpse of the previous attempt
                try:
                    import subprocess
                    subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"],
                                   capture_output=True, timeout=20)
                except Exception:
                    pass
                import random
                await asyncio.sleep(4 * attempt + random.uniform(0, 3))
        log("start: browser could not be started after 3 attempts")
        raise last

    async def _teardown(self):
        """Best-effort cleanup between launch attempts."""
        for attr in ("_ka", "page", "ctx", "br", "pw"):
            obj = getattr(self, attr, None)
            if obj is None:
                continue
            try:
                if attr == "_ka":
                    obj.cancel()
                elif attr in ("page", "ctx"):
                    await obj.close()
                elif attr == "br":
                    await obj.close()
                elif attr == "pw":
                    await obj.stop()
            except Exception:
                pass
            setattr(self, attr, None)

    async def _start_once(self):
        from playwright.async_api import async_playwright
        self.pw = await async_playwright().start()
        # must default to False here: without the attribute, login() read it as
        # undefined and took the "no usable session" exit even when state existed
        self._session_loaded = False
        channel, exe = pick_browser(self.cfg)
        launch_kwargs = dict(
            headless=bool(self.cfg.get("HEADLESS")),
            args=["--autoplay-policy=no-user-gesture-required",
                  "--disable-blink-features=AutomationControlled",
                  "--mute-audio",
                  "--disable-features=IsolateOrigins,site-per-process"],
        )
        if channel:
            launch_kwargs["channel"] = channel
        if exe:
            launch_kwargs["executable_path"] = exe
        self.br = await self.pw.chromium.launch(**launch_kwargs)
        ctx_kwargs = dict(
            viewport={"width": 1440, "height": 900},
            user_agent=UA, locale="zh-CN", timezone_id="Asia/Shanghai")
        # reuse the session saved by login.py so a run does not depend on the
        # account password (and survives accounts that enforce SMS/QR login)
        state = self.cfg.get("STORAGE_STATE") or os.path.join(
            "evidence", "storage_state.json")
        state_path = state if os.path.isabs(state) else os.path.join(ROOT, state)
        if self.cfg.get("REUSE_SESSION", True) and os.path.exists(state_path):
            ctx_kwargs["storage_state"] = state_path
            self._session_loaded = True
            log(f"session: reusing {os.path.relpath(state_path, ROOT)}")
        self.ctx = await self.br.new_context(**ctx_kwargs)
        await self.ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});\n" + INJECT)
        self.page = await self.ctx.new_page()

        async def keepalive():
            while True:
                await asyncio.sleep(25)
                log("[keepalive] periodic keepalive heartbeat")

        self._ka = asyncio.create_task(keepalive())

    async def close(self):
        try:
            self._ka.cancel()
        except Exception:
            pass
        for closer in ("ctx", "br"):
            obj = getattr(self, closer, None)
            if obj:
                try:
                    await obj.close()
                except Exception:
                    pass
        if self.pw:
            try:
                await self.pw.stop()
            except Exception:
                pass

    # ---------------------------------------------------------- login / scan
    async def _password_login(self) -> bool:
        """Log in with the stored account. Returns True when it worked."""
        p = self.page
        acc = (self.cfg.get("ACCOUNT") or "").strip()
        pwd = ((os.environ.get("CX_PWD") if self.cfg.get("USE_ENV_PASSWORD") else None)
               or self.cfg.get("PASSWORD") or "").strip()
        if not acc or not pwd or "PHONE_OR_USERNAME" in acc:
            return False
        try:
            await p.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            await p.wait_for_timeout(random.randint(2500, 4000))
            if await self._is_logged_in():
                log("relogin: already authenticated")
                await self._dump_cookies()
                return True
            # type like a person: click the field, then type
            for sel, val in (("#phone", acc), ("#pwd", pwd)):
                loc = p.locator(sel).first
                if not await loc.count():
                    log(f"relogin: field {sel} missing")
                    return False
                await loc.click(timeout=15000)
                await p.wait_for_timeout(random.randint(150, 420))
                await loc.fill("")
                for ch in val:
                    await loc.type(ch, delay=random.randint(40, 130))
                await p.wait_for_timeout(random.randint(200, 600))
            await p.click("#loginBtn", timeout=20000)
            await p.wait_for_selector("text=我的课程", timeout=120000)
            await p.wait_for_timeout(random.randint(1200, 2200))
            ok = await self._is_logged_in()
            if ok:
                await self._dump_cookies()
                log("relogin: ok, session repaired")
            else:
                log("relogin: form accepted but not authenticated")
            return ok
        except Exception as exc:
            log(f"relogin failed: {type(exc).__name__}: {str(exc)[:70]}")
            return False

    async def ensure_login(self, force: bool = False) -> bool:
        """Repair a dead session automatically.

        Called when a probe says the platform no longer trusts us. Rate limited, so a
        wrong password cannot turn into a login loop that would itself look abusive.
        """
        # explicit comparison: `not cfg.get(k, True)` is also true when the key
        # exists with value null, which would silently disable the repair path
        if self.cfg.get("AUTO_RELOGIN") is False:
            return False
        now = time.time()
        last = getattr(self, "_last_relogin", 0.0)
        cool = float(self.cfg.get("RELOGIN_COOLDOWN_S", 900))
        if not force and (now - last) < cool:
            log(f"relogin: skipped (cooldown {int(cool - (now - last))}s left)")
            return False
        self._last_relogin = now
        log("relogin: session lost, logging in automatically")
        return await self._password_login()

    async def login(self):
        """Login with retries on transient network errors.

        2026-10-22: a DNS blip killed the math/english/IT workers at startup
        (ERR_NAME_NOT_RESOLVED on every probe and on the re-login page) and the
        whole round idled for hours. Waiting a few minutes is much cheaper than
        losing an afternoon, so transient network failures retry with backoff.
        """
        delays = [15, 30, 60, 120, 300]
        for attempt in range(len(delays) + 1):
            try:
                await self._login_once()
                return
            except SystemExit:
                raise
            except Exception as exc:
                msg = str(exc)
                transient = any(k in msg for k in (
                    "ERR_NAME_NOT_RESOLVED", "ERR_INTERNET_DISCONNECTED",
                    "ERR_CONNECTION", "ERR_TIMED_OUT", "Timeout"))
                if not transient or attempt >= len(delays):
                    raise
                log(f"login: transient network error ({type(exc).__name__}), "
                    f"retry {attempt + 1}/{len(delays)} in {delays[attempt]}s")
                await asyncio.sleep(delays[attempt])

    async def _login_once(self):
        p = self.page
        acc = self.cfg.get("ACCOUNT") or ""
        pwd = (os.environ.get("CX_PWD") if self.cfg.get("USE_ENV_PASSWORD")
               else None) or self.cfg.get("PASSWORD") or ""

        # A saved session is the normal path: login.py already produced it, so we
        # probe instead of demanding credentials. Only fall back to a form login
        # when the session is missing or dead.
        has_state = bool(getattr(self, "_session_loaded", False))
        if has_state:
            log("session: probing saved login state")
            for probe in [PROBE_HOME] + PROBE_FALLBACKS:
                try:
                    await p.goto(probe, wait_until="domcontentloaded", timeout=45000)
                    await p.wait_for_timeout(2500)
                    if await self._is_logged_in():
                        log(f"session: valid, no login needed ({probe[:44]})")
                        await self._dump_cookies()
                        return
                    log(f"session: probe {probe[:40]} not authenticated")
                except Exception as exc:
                    log(f"session: probe {probe[:40]} failed ({type(exc).__name__})")
            log("session: all probes failed, repairing automatically")
            if await self.ensure_login(force=True):
                return

        if not acc or "PHONE_OR_USERNAME" in acc:
            raise SystemExit(
                "no usable session and no account. Run: python login.py --qr  "
                "(or set ACCOUNT/CX_PWD in config.json)")

        await p.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        await p.wait_for_timeout(2500)
        if await self._is_logged_in():
            log("login: reuse existing session")
        else:
            await p.fill("#phone", acc, timeout=20000)
            await p.fill("#pwd", pwd, timeout=20000)
            await p.click("#loginBtn")
            # The old dashboard showed 我的课程; the current one lands on
            # i.chaoxing.com/base showing the user's name and a 课程 entry. Waiting
            # only for the old text made a SUCCESSFUL login look like a 120s timeout.
            for probe in ("text=我的课程", "text=课程", "text=首页", ".userInfo"):
                try:
                    await p.wait_for_selector(probe, timeout=45000)
                    break
                except Exception:
                    continue
            await p.wait_for_timeout(2000)
            log("login: ok")
        await p.wait_for_timeout(1500)
        await self._dump_cookies()

    async def _is_logged_in(self) -> bool:
        """Same two-signal logic as login.py: post-login host, or space chrome."""
        url = self.page.url or ""
        host = urlparse(url).netloc.lower()
        if host.endswith("passport2.chaoxing.com") or "passport" in url:
            return False
        for h in ("i.chaoxing.com", "mooc1.chaoxing.com", "mooc2-ans.chaoxing.com",
                  "stat2-ans.chaoxing.com", "mooc1-1.chaoxing.com",
                  "noteyoudao.chaoxing.com", "home.chaoxing.com"):
            if host.endswith(h):
                return True
        for sel in ("text=我的课程", "text=个人空间", "text=我的空间", ".userInfo"):
            try:
                if await self.page.locator(sel).count():
                    return True
            except Exception:
                continue
        return False

    async def _dump_cookies(self) -> None:
        """Persist the live session.

        This used to write only cookies.json, which nothing reads back - so a
        successful automatic re-login was never saved, and the next process had to log
        in again from scratch. storage_state.json IS what Session.start() loads, so it
        is the file that makes a repaired session durable. Verified by deleting
        storage_state.json, letting preflight re-login, and checking the file reappears.
        """
        # explicit comparison: null or absent means "save", only False skips
        if self.cfg.get("DUMP_COOKIES") is False or self._cookies_dumped:
            return
        try:
            ck = await self.ctx.cookies()
            safejson.save(os.path.join(ROOT, "evidence", "cookies.json"), ck)
        except Exception:
            pass
        try:
            state = await self.ctx.storage_state()
            state_path = self.cfg.get("STORAGE_STATE") or os.path.join(
                "evidence", "storage_state.json")
            if not os.path.isabs(state_path):
                state_path = os.path.join(ROOT, state_path)
            # atomic: every worker rewrites this at startup, and a torn read would
            # break the next Session.start()
            safejson.save(state_path, state)
            log(f"session: saved {len(ck)} cookie(s) -> "
                f"{os.path.relpath(state_path, ROOT)}")
        except Exception as exc:
            log(f"session: could not save state ({type(exc).__name__})")
        self._cookies_dumped = True

    async def chapters(self):
        """Chapter list via the course entry that actually works.

        Verified 2026-10-13 against the live account:
          /mycourse/studentcourse?courseId=X        -> dead legacy redirect
          stucoursemiddle?...&ismooc2=1&v=2         -> empty mooc2 shell, 0 chapters
          stucoursemiddle?courseid=&clazzid=&cpi=   -> WORKS (cpi from the card)
        """
        p = self.page
        cid = self.cfg.get("COURSE_ID") or ""
        if "COURSE_ID" in cid:
            raise SystemExit("config.json 里 COURSE_ID 还是占位符，先填上再跑。")

        cards = await nav_fetch_cards(p)
        log(f"live cards: {len(cards)}")
        for c in cards:
            if c["courseId"] == cid:
                log(f"  card match: {json.dumps(c, ensure_ascii=False)}")
        card = next((c for c in cards if c["courseId"] == cid), None)
        if card is None:
            # Courses archived out of "my courses" still open by direct URL.
            # COURSE_META carries their clazzid/cpi so the run can reach them.
            meta = (self.cfg.get("COURSE_META") or {}).get(cid) or {}
            if meta.get("clazzid") and meta.get("cpi"):
                card = {"courseId": cid, "clazzid": str(meta["clazzid"]),
                        "cpi": str(meta["cpi"]),
                        "name": meta.get("name") or cid}
                log(f"course {cid} missing from cards; "
                    f"using COURSE_META override")
            else:
                log(f"course {cid} not among the {len(cards)} live course cards")
                names = ", ".join(f"{c['courseId']}:{c['name'][:14]}" for c in cards[:10])
                log(f"available: {names}")
                return []
        # The card pair is per-course and authoritative. Only honour the config
        # overrides when they describe THIS course - otherwise selecting another
        # course would drag in the wrong class (that mistake sent the IT course
        # into the English class and scanned 0 chapters).
        # The live card carries clazzid+cpi per course and is authoritative.
        # Never let a global config value override it: config.CLAZZID belongs to
        # whichever course you last set, and reusing it for another course sends
        # the run into the wrong class (that bug scanned 0 chapters).
        log(f"course: {card['name']} clz={card['clazzid']} cpi={card['cpi']}")

        ok = await nav_open_course(p, cid, card["clazzid"], card["cpi"])
        if not ok:
            log("course entry bounced to login; session may be stale")
            return []
        hrefs = await nav_read_chapters(p)
        out = [h["href"] if h["href"].startswith("http")
               else "https://mooc1.chaoxing.com" + h["href"] for h in hrefs]
        # Keep the pre-expansion list: probing must only ever walk real chapters,
        # never the child URLs we are about to add. Iterating `out` while appending
        # to it made the loop visit child pages of one course and attach THEIR
        # children to another course's queue (思想道德 grew to 166 bogus units).
        original = list(out)

        # Optional: expand the catalog's unfinished CHILD nodes into their own
        # work units. Relying only on the parent chapter's in-page cascade proved
        # fragile - the child handles were there (168 for 思想道德, 61 for 物理)
        # but workers ran past them reporting NO_TASK_POINT.
        #
        # The catalog only renders on a PARENT chapter page, and the first chapter
        # is not guaranteed to be one, so several are probed until children appear.
        if self.cfg.get("INCLUDE_CHILD_NODES", True) and hrefs and original:
            base = original[0]
            added_total = 0
            probed = 0
            for probe_url in original[:12]:
                if added_total >= 40:      # enough to keep the queue moving
                    break
                probed += 1
                try:
                    await p.goto(probe_url, wait_until="domcontentloaded", timeout=60000)
                    await p.wait_for_timeout(9000)
                    for _ in range(3):
                        await p.mouse.wheel(0, 1200)
                        await p.wait_for_timeout(700)
                    children = [n for n in await list_child_nodes(p) if n.get("count")]
                except Exception as exc:
                    log(f"scan: child probe failed {type(exc).__name__}")
                    continue
                if not children:
                    # NOT a break: many chapters render no catalog at all, and the
                    # parent that carries the children can be further down the list
                    continue
                for n in children:
                    child_url = re.sub(r"chapterId=\d+", f"chapterId={n['chapterId']}", base)
                    if child_url not in out:
                        out.append(child_url)
                        added_total += 1
                log(f"scan: found {len(children)} child node(s) under "
                    f"chapter {probe_url.rsplit('chapterId=', 1)[-1][:14]}")
                # The catalog renders identically on every chapter page of a course
                # (verified: 10 consecutive probes each returned the same 166 nodes),
                # so one productive pass is enough - further probes just reload the
                # same list.
                break
            if added_total:
                log(f"scan: expanded {added_total} unfinished child node(s) "
                    f"(probed {probed} chapters)")
                # Put the children FIRST. Parent chapters usually carry no task
                # point of their own (they only host the catalog), so walking all
                # 92-216 of them - with a 60-150s pause each - wastes hours before
                # the real work starts. The children are where the unfinished task
                # points live, so they lead.
                child_urls = [u for u in out if u not in original]
                if child_urls and self.cfg.get("CHILDREN_FIRST", True):
                    out = child_urls + [u for u in out if u in original]
                    log(f"scan: queued {len(child_urls)} child node(s) first")

        # Drop chapters the catalog already reports as finished. The work list is the
        # course's full chapter list, so a runner walks every completed chapter too -
        # opening it, inspecting it, and pausing 75-210s before the next. On 思想道德
        # that is 85 wasted chapters out of 216, i.e. hours of doing nothing. Only ids
        # the catalog explicitly lists as complete are removed; anything the catalog
        # does not mention stays, so nothing is skipped by accident.
        if self.cfg.get("SKIP_FINISHED", True) and out:
            try:
                await p.goto(out[0], wait_until="domcontentloaded", timeout=60000)
                await p.wait_for_timeout(9000)
                for _ in range(3):
                    await p.mouse.wheel(0, 1200)
                    await p.wait_for_timeout(700)
                # retry: a page that has not finished rendering its badges yields no
                # "finished" rows, and the filter then silently does nothing - which is
                # how 大学物理 ended up re-walking 36 completed chapters in some runs
                rows = []
                for attempt in range(3):
                    try:
                        rows = await list_child_nodes(p)
                    except Exception:
                        rows = []
                    if rows:
                        break
                    await p.wait_for_timeout(2500)
                    for _ in range(2):
                        await p.mouse.wheel(0, 1200)
                        await p.wait_for_timeout(600)
                finished = {str(r["chapterId"]) for r in rows if not r.get("count")}
                listed = {str(r["chapterId"]) for r in rows}
                log(f"scan: catalog read {len(rows)} row(s), {len(finished)} finished")
                if finished:
                    kept = []
                    dropped = 0
                    for u in out:
                        m = re.search(r"chapterId=(\d+)", u)
                        cid = m.group(1) if m else ""
                        if cid in finished and cid in listed:
                            dropped += 1
                            continue
                        kept.append(u)
                    if dropped:
                        out = kept
                        log(f"scan: skipped {dropped} already-finished chapter(s)")
            except Exception as exc:
                log(f"scan: finished-filter failed {type(exc).__name__}")

        # NEW-UI fallback: courses whose old catalog renders no badge counts
        # (English B1, IT basics) scan as "all finished" while the new
        # studentcourse UI lists unfinished task points. Before declaring a
        # course done, ask the new UI for its own badge list.
        if not out and hrefs:
            try:
                new_ids = await self._new_ui_unfinished()
            except Exception as exc:
                log(f"scan: new-UI fallback failed {type(exc).__name__}")
                new_ids = []
            if new_ids:
                base = original[0] if original else ""
                if base:
                    out = [re.sub(r"chapterId=\d+", f"chapterId={nid}", base)
                           for nid in new_ids]
                    log(f"scan: new-UI fallback found {len(new_ids)} "
                        f"unfinished chapter(s)")

        # drop nodes this account has repeatedly failed to clear, so they cannot
        # block the head of the queue on every restart
        skip = await self._load_skip_list()
        if skip:
            before = len(out)
            out = [u for u in out
                   if (re.search(r"chapterId=(\\d+)", u) or [None, ""])[1] not in skip]
            dropped = before - len(out)
            if dropped:
                log(f"scan: skipped {dropped} node(s) that never register")

        limit = int(self.cfg.get("MAX_CHAPTERS") or 0)
        if limit:
            out = out[:limit]
        log(f"scan: {len(out)} work units")
        return out

    async def _new_ui_unfinished(self):
        """Chapter ids with unfinished task points, read from the new UI.

        The mooc2-ans studentcourse directory renders a per-chapter badge
        (catalog_points_yi) that the old catalog does not expose. English B1 and
        IT basics only render the new UI, so the old scan sees every row as
        finished and the courses never advance.
        """
        p = self.page
        cid = self.cfg.get("COURSE_ID") or ""
        cards = await nav_fetch_cards(p)
        card = next((c for c in cards if c["courseId"] == cid), None)
        if card is None:
            return []
        mid = (f"https://mooc1.chaoxing.com/mooc-ans/visit/stucoursemiddle?"
               f"courseid={cid}&clazzid={card['clazzid']}&vc=1&cpi={card['cpi']}"
               f"&ismooc2=1&v=2")
        await p.goto(mid, wait_until="domcontentloaded", timeout=60000)
        await p.wait_for_timeout(5000)
        href = await p.evaluate("""() => {
            const a = [...document.querySelectorAll('a')]
                .find(a => /mycourse\\/stu/.test(a.href || ''));
            return a ? a.href : null;
        }""")
        if not href:
            return []
        await p.goto(href, wait_until="domcontentloaded", timeout=60000)
        await p.wait_for_timeout(8000)
        await p.evaluate("""() => {
            const el = [...document.querySelectorAll('a,li,div,span')]
                .find(e => (e.innerText || '').trim() === '章节' && e.offsetParent);
            if (el) el.click();
        }""")
        await p.wait_for_timeout(8000)
        f = next((fr for fr in p.frames if "studentcourse" in (fr.url or "")), None)
        if not f:
            return []
        return await f.evaluate("""() => {
            const out = [];
            document.querySelectorAll('.catalog_title').forEach(t => {
                const inp = t.querySelector('input[value]');
                const badge = t.querySelector('.catalog_points_yi');
                if (inp && inp.getAttribute('value') && badge
                    && parseInt((badge.innerText || '0').trim(), 10) > 0) {
                    out.push(inp.getAttribute('value'));
                }
            });
            return out;
        }""")

    async def dump_debug(self, tag: str):
        p = self.page
        try:
            await p.screenshot(path=os.path.join(ROOT, "logs", f"{tag}.png"), full_page=False)
            with open(os.path.join(ROOT, "logs", f"{tag}.html"), "w", encoding="utf-8") as fh:
                fh.write(await p.content())
        except Exception:
            pass

    # ---------------------------------------------------------- video
    # A "you still have incomplete task points" modal the platform raises when moving
    # between chapters. While it is up it covers the page, and the media module of the
    # NEXT chapter can fail to initialise - which surfaces as NO_TASK_POINT for a node
    # that loads perfectly when opened cold. Dismissing it is therefore part of
    # navigation, not a cosmetic step.
    DISMISS_JS = r"""() => {
        const out = { tried: [], closed: false };
        const looksModal = e => {
            const cls = (e.className || '').toString();
            const id = e.id || '';
            const t = (e.innerText || '');
            return /jobFinishTip|maskDiv|AlertCon|popHead|layui-layer|dialog/i.test(cls + ' ' + id)
                || /还有任务点未完成|是否去完成/.test(t);
        };
        // 1) explicit close/dismiss controls
        const labels = ['知道了', '关闭', '继续学习', '稍后', '取消', '返回课程', '×', 'x'];
        const clickable = [...document.querySelectorAll('a,button,span,div,i')];
        for (const e of clickable) {
            if (!looksModal(e) && e.offsetParent === null) continue;
            const t = (e.innerText || '').trim();
            if (!labels.includes(t)) continue;
            const r = e.getBoundingClientRect();
            if (r.width < 4 || r.height < 4) continue;
            if (!e.closest('.jobFinishTip,.maskDiv,.AlertCon02,.popHead,'
                          + '.layui-layer,[class*=dialog],[class*=Dialog]')) continue;
            try { e.click(); out.tried.push(t); out.closed = true; break; } catch (err) {}
        }
        // 2) otherwise hide the mask directly so it cannot intercept clicks
        if (!out.closed) {
            document.querySelectorAll('.maskDiv, .jobFinishTip, .AlertCon02,'
                + ' .layui-layer-molv, .layui-layer').forEach(e => {
                const cls = (e.className || '').toString();
                if (/jobFinishTip|maskDiv|AlertCon02|layui-layer/.test(cls)) {
                    try { e.style.display = 'none'; e.style.pointerEvents = 'none';
                          out.tried.push('hide:' + cls.slice(0, 18)); out.closed = true; } catch (err) {}
                }
            });
        }
        return out;
    }"""

    async def dismiss_modal(self) -> dict:
        """Close any task-point nag modal so the page is interactive again."""
        try:
            res = await self.page.evaluate(self.DISMISS_JS)
        except Exception:
            return {"tried": [], "closed": False}
        if res.get("closed"):
            log(f"open: dismissed modal ({res['tried'][:2]})")
            await self.page.wait_for_timeout(1200)
        return res

    async def _ensure_page(self) -> bool:
        """Recreate the browser when it has died.

        Both workers were once reduced to this loop:

            === [8/56] ...
            chapter error TargetClosedError: Page.goto: Target page, context or
            browser has been closed
            sleep 180s before next chapter

        i.e. every chapter failed instantly and the runner kept sleeping between
        failures for hours, making no progress at all. A dead browser must be rebuilt,
        not retried.
        """
        try:
            if self.page is not None and not self.page.is_closed():
                return True
        except Exception:
            pass
        log("browser: page is gone, rebuilding the browser and session")
        try:
            await self.close()
        except Exception:
            pass
        await asyncio.sleep(random.uniform(3, 8))
        try:
            await self.start()
            await self.login()
            log("browser: rebuilt")
            return True
        except Exception as exc:
            log(f"browser: rebuild failed ({type(exc).__name__}: {str(exc)[:60]})")
            return False

    def _clear_attempt(self, chapter_id: str) -> None:
        """Forget the failures for a node that has just cleared.

        Without this the counter only ever grows, so opening a node three times -
        successfully - parked it. The counter is meant to catch nodes that refuse to
        register, not nodes that are simply visited more than once.
        """
        try:
            path = self._course_file("node_attempts")

            def _drop(cur):
                if not isinstance(cur, dict):
                    return cur
                cur.pop(str(chapter_id), None)
                return cur

            safejson.update(path, _drop, {})
        except Exception:
            pass

    def _course_file(self, stem: str) -> str:
        """A state file private to this course, so workers cannot overwrite each other."""
        cid = str(self.cfg.get("COURSE_ID") or "unknown")
        folder = os.path.join(ROOT, "logs")
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, f"{stem}_{cid}.json")

    def _bump_attempt(self, chapter_id: str) -> int:
        """Count how many times a node has been opened, and skip it after MAX_TRIES.

        Needed because a node can report success (video plays, DONE is returned) and
        still never register server-side - 大学物理 node 1010122035 光波的衍射 does
        exactly that, and because it sorts first it led the work list on every single
        run. Counting attempts catches both failure modes: the ones that error out and
        the ones that lie.
        """
        # Per-course file + a locked read-modify-write. This is a counter, so three
        # workers sharing one file lose each other's increments - the same bug that
        # halved the daily budget, and the reason it is fixed the same way.
        path = self._course_file("node_attempts")

        def _bump(cur):
            cur = cur if isinstance(cur, dict) else {}
            cur[str(chapter_id)] = int(cur.get(str(chapter_id), 0)) + 1
            return cur

        tries = 0
        try:
            out = safejson.update(path, _bump, {})
            tries = int(out.get(str(chapter_id), 1))
        except Exception as exc:
            log(f"attempt-count failed: {type(exc).__name__}")
        return tries

    async def _load_skip_list(self) -> set:
        """Node ids this account has already tried and failed to clear.

        Persisted to disk on purpose. The in-memory set reset on every restart, so a
        node that refuses to register sat at the head of the work list and got replayed
        on every single run - burning the worker's time and, worse, looking like a bot
        stuck on one video. Keeping the list means the runner moves on.
        """
        # One file PER COURSE. A single shared file meant three workers each wrote
        # their own `course` + `ids`, so whichever wrote last erased the others' parks.
        path = self._course_file("skip_nodes")
        data = safejson.load(path, {}) or {}
        return set(str(x) for x in data.get("ids", []))

    def _save_skip_list(self, ids: set) -> None:
        safejson.save(self._course_file("skip_nodes"),
                      {"course": str(self.cfg.get("COURSE_ID")),
                       "ids": sorted(ids),
                       "updated": time.strftime("%Y-%m-%d %H:%M:%S")})

    async def open_chapter(self, url: str, count_attempt: bool = True):
        """Open a chapter page, refreshing the enc token if it has expired.

        THE cause of the long stalls on 大学物理 and 信息技术基础: the `enc` query
        parameter is a per-session token that EXPIRES. A run fetches its chapter list
        once at startup and then reuses that enc for hours; once it goes stale every
        chapter renders "enc校验失败" with no media frames at all, so the runner sees
        NO_TASK_POINT for every node and makes no progress.

        Verified: same chapter id with a stale enc -> 0 media frames and the page text
        "enc校验失败"; with a fresh enc -> 1 media frame and the normal study page.

        So the token is refreshed and the page retried whenever validation fails.
        """
        await self._ensure_page()
        p = self.page
        # count the attempt; a node that has been opened MAX_TRIES times without
        # clearing is parked so it stops leading the queue.
        #
        # count_attempt is False for read-only inspection: a diagnostic must not park
        # the node it is looking at, or three probe runs would silently drop it from
        # the work list (which is exactly what happened to three 大学英语B1 nodes).
        try:
            m0 = re.search(r"chapterId=(\d+)", url or "")
            if m0 and count_attempt:
                tries = self._bump_attempt(m0.group(1))
                limit = int(self.cfg.get("MAX_NODE_TRIES", 3))
                if tries > limit:
                    ids = await self._load_skip_list()
                    ids.add(m0.group(1))
                    self._save_skip_list(ids)
                    log(f"open: node {m0.group(1)} parked after {tries} attempts")
        except Exception:
            pass
        # Once a fresh enc is known, use it immediately: the work list was built at
        # startup and its tokens expire, so waiting for the platform to reject each
        # URL would cost a wasted round-trip on every single chapter.
        known = getattr(self, "enc", "") or ""
        if known and "enc=" in url:
            url = re.sub(r"enc=[0-9a-fA-F]+", f"enc={known}", url)

        try:
            await p.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            if "closed" in str(exc).lower() or "TargetClosed" in type(exc).__name__:
                log(f"open: browser died ({type(exc).__name__}), rebuilding")
                if await self._ensure_page():
                    p = self.page
                    await p.goto(url, wait_until="domcontentloaded", timeout=60000)
                else:
                    raise
            else:
                raise
        await p.wait_for_timeout(4500)

        # a nag modal from the previous chapter can cover the page and stop the
        # media module from initialising
        await self.dismiss_modal()

        stale = False
        try:
            body = await p.evaluate(
                '() => (document.body.innerText || "").slice(0, 400)')
            body = body or ""
            stale = "enc校验失败" in body
            # The platform does not always answer a dead chapter link with
            # "enc校验失败" - sometimes it silently bounces to the portal home page,
            # which carries these dashboard-only markers. Treat that as a dead link
            # too, otherwise the worker inspects a dashboard and reports
            # NO_TASK_POINT while the course quietly stops advancing (the
            # no-task-page diagnostic caught exactly this).
            if not stale:
                dash = ("收件箱", "云盘", "账号管理", "邀请码", "听课评课",
                        "专题创作", "教研室广场")
                hits = sum(1 for k in dash if k in body)
                on_study = ("studentstudy" in (p.url or "")) or ("目录" in body)
                if hits >= 3 and not on_study:
                    stale = True
                    log("open: bounced to the portal home, treating as a dead link")
        except Exception:
            pass

        if stale:
            log("open: enc rejected by the platform, fetching a fresh one")
            fresh = await self.refresh_enc()
            if fresh:
                new_url = re.sub(r"enc=[0-9a-fA-F]+", f"enc={fresh}", url)
                if new_url == url:
                    new_url = url + (f"&enc={fresh}" if "enc=" not in url else "")
                await p.goto(new_url, wait_until="domcontentloaded", timeout=60000)
                await p.wait_for_timeout(5000)
                try:
                    body = await p.evaluate(
                        '() => (document.body.innerText || "").slice(0, 400)')
                    body = body or ""
                    dash = ("收件箱", "云盘", "账号管理", "邀请码", "听课评课")
                    on_study = "studentstudy" in (p.url or "") or "目录" in body
                    bad = ("enc校验失败" in body
                           or (sum(1 for k in dash if k in body) >= 3 and not on_study))
                    if not bad:
                        log("open: recovered with a fresh enc")
                        await self.dismiss_modal()
                    else:
                        log("open: still rejected after refresh")
                except Exception:
                    pass
        return p

    async def refresh_enc(self) -> str:
        """Re-read the study page to obtain a new enc token for this course."""
        p = self.page
        try:
            cards = await nav_fetch_cards(p)
            card = next((c for c in cards
                         if str(c.get("courseId")) == str(self.cfg.get("COURSE_ID"))), None)
            if card is None:
                log("enc: course card not found, cannot refresh")
                return ""
            ok = await nav_open_course(p, card["courseId"], card["clazzid"], card["cpi"])
            if not ok:
                log("enc: could not re-enter the course")
                return ""
            chs = await nav_read_chapters(p)
            for c in chs:
                m = re.search(r"enc=([0-9a-fA-F]+)", c.get("href", ""))
                if m:
                    self.enc = m.group(1)
                    log(f"enc: refreshed ({self.enc[:12]}...)")
                    return self.enc
        except Exception as exc:
            log(f"enc: refresh failed {type(exc).__name__}")
        return ""

    # A node can present SEVERAL task points as tabs, each mounting its own player:
    # 狭义相对论 offers 时间延缓 and 同时的相对性, and the catalog keeps a badge on
    # the node until BOTH are watched. Only the default tab exists at load time, so
    # the other tabs have to be clicked. This is why 大学物理 sat at 21/145 for hours
    # while every node reported success - the first tab really was completed each time.
    TABS_JS = r"""() => {
        const out = [];
        document.querySelectorAll('span,li,a,div').forEach(e => {
            const cls = (e.className || '').toString();
            const first = (cls.split(' ')[0] || '');
            if (!/^(c\d+|tab|tabItem|switchItem)/i.test(first)) return;
            const t = (e.innerText || '').replace(/\s+/g, ' ').trim();
            if (!t || t.length > 20) return;
            // the breadcrumb ("上一节 … 下一节") matches c\d+ too - skip it
            if (/上一节|下一节|回到课程/.test(t)) return;
            const r = e.getBoundingClientRect();
            if (r.width < 30 || r.height < 14) return;
            out.push({ text: t, cls: cls.slice(0, 30),
                       active: /current|active|on\b/i.test(cls),
                       x: Math.round(r.x + r.width / 2),
                       y: Math.round(r.y + r.height / 2) });
        });
        const seen = new Set();
        return out.filter(o => {
            if (seen.has(o.text)) return false;
            seen.add(o.text); return true;
        }).slice(0, 12);
    }"""

    async def _frame_ready(self, frame, timeout_s: float = 15.0) -> dict:
        """Wait for a matched module frame to have a parsed body.

        The tab click mounts the player iframe and its URL commits before the
        document body exists, so an early DOM read raises on every node. Wait
        for the body, then report the state we actually see.
        """
        try:
            await frame.wait_for_function("() => !!document.body",
                                          timeout=int(timeout_s * 1000))
        except Exception:
            pass
        try:
            return await frame.evaluate(
                "() => ({ok: !!document.body,"
                " url: (location.href || '').slice(0, 120),"
                " ready: document.readyState,"
                " html: ((document.documentElement &&"
                "         document.documentElement.outerHTML) || '').length})")
        except Exception as exc:
            return {"ok": False, "url": (frame.url or "")[:120],
                    "err": f"{type(exc).__name__}: {str(exc)[:80]}"}

    async def do_video_all_tabs(self) -> str:
        """Finish every media task point on this node, including per-tab ones.

        Tabs are clicked with a REAL pointer event at their live bounding box and each
        one is then driven through do_video(), because each tab mounts a different
        player. A single-tab node behaves exactly as before.
        """
        p = self.page
        # Wait for the module to mount BEFORE reading the tabs. Probing straight after
        # navigation finds neither the player nor the tab strip, which made the runner
        # report NO_TASK_POINT for nodes that are fine when opened by hand.
        deadline = time.time() + 25
        while time.time() < deadline:
            frames = [f for f in p.frames if any(h in f.url for h in MEDIA_HINTS)]
            if frames:
                break
            await p.wait_for_timeout(800)
        await p.wait_for_timeout(1500)

        try:
            tabs = await p.evaluate(self.TABS_JS) or []
        except Exception:
            tabs = []
        if len(tabs) <= 1:
            return await self.do_video()

        log(f"tabs: {len(tabs)} task point tab(s) on this node -> "
            f"{[t['text'][:10] for t in tabs]}")
        parts = []
        for t in tabs:
            try:
                await p.mouse.click(t["x"], t["y"])
                await p.wait_for_timeout(4500)
                await self.dismiss_modal()
            except Exception as exc:
                parts.append(f"{t['text'][:8]}:clickfail")
                continue
            res = await self.do_video()
            parts.append(f"{t['text'][:8]}:{res}")
            log(f"  tab {t['text'][:12]!r} -> {res}")
        return "TABS " + " | ".join(parts)

    async def do_video(self) -> str:
        """Finish every media task point in this chapter.

        Three player families show up behind the task-point frames:
          * audio modules  - plain element, seek + finish (jump to tail works)
          * video modules  - video.js with a "播放视频" poster layer and a
                             "watch >= 90% of duration" rule, so play fast
          * pdf / reader   - handled in _do_non_media
        """
        p = self.page
        deadline = time.time() + 25
        frames = []
        dismissed = False
        while time.time() < deadline:
            frames = [f for f in p.frames if any(h in f.url for h in MEDIA_HINTS)]
            if frames:
                break
            # If nothing shows up, a nag modal from the previous chapter is the most
            # likely cause - clear it and keep waiting rather than giving up.
            if not dismissed and time.time() > deadline - 18:
                dismissed = True
                await self.dismiss_modal()
            await p.wait_for_timeout(800)

        if not frames:
            non_media = await self._do_non_media()
            if non_media and non_media != "NON_MEDIA 0/0":
                return non_media
            if self.cfg.get("CASCADE_CHILD_NODES", True) and not getattr(
                    self, "_in_cascade", False):
                cascaded = await self._cascade_child_nodes(
                    limit=int(self.cfg.get("CASCADE_LIMIT", 40)))
                if cascaded:
                    return cascaded
            self.stats["video_none"] += 1
            # record the node so the next run does not lead with it again
            try:
                m = re.search(r"chapterId=(\\d+)", p.url or "")
                if m:
                    ids = await self._load_skip_list()
                    ids.add(m.group(1))
                    self._save_skip_list(ids)
            except Exception:
                pass
            has_quiz = await p.locator(".TiMu, .questionLi").count()
            # log what the page actually shows: NO_TASK_POINT has twice turned out to
            # be a page-level problem (stale enc, a blocking modal) rather than a node
            # without work
            try:
                snippet = await p.evaluate(
                    '() => ((document.body && document.body.innerText) || "")'
                    '.replace(/\\s+/g, " ").slice(0, 110)')
                nframes = len(p.frames)
                log(f"  no-task-page: frames={nframes} text={snippet!r}")
            except Exception:
                pass
            return "NO_TASK_POINT" if not has_quiz else "NO_TASK_POINT_QUIZ_ONLY"
        log(f"video: {len(frames)} media task point(s) in this chapter")
        try:
            seen_urls = []
            for f in p.frames:
                u = (f.url or "")[:110]
                if u not in seen_urls:
                    seen_urls.append(u)
            log("  frames: " + " | ".join(seen_urls[:12]))
        except Exception:
            pass
        done = 0
        last_reason = ""
        for idx, frame in enumerate(frames, 1):
            try:
                ready = await self._frame_ready(frame)
                if not ready.get("ok"):
                    # a freshly mounted iframe commits its URL before the body
                    # is parsed; give it a beat, then prefer a live replacement
                    await p.wait_for_timeout(2500)
                    fresh = [f for f in p.frames
                             if any(h in f.url for h in MEDIA_HINTS)]
                    if fresh:
                        frame = fresh[-1]
                        ready = await self._frame_ready(frame)
                    log(f"  [{idx}] frame state: {ready}")
                    if not ready.get("ok"):
                        last_reason = last_reason or "frame not ready"
                        continue
                await frame.evaluate(INJECT)
                label = await frame.evaluate(
                    "() => ((document.body && document.body.innerText) || '')"
                    ".replace(/\\s+/g,' ').slice(0,50)")
                is_video_module = "/modules/video/" in (frame.url or "")
                if is_video_module:
                    # poster layer, 2x max, 90% watch rule: play it out and keep
                    # it alive instead of seeking
                    before = await frame.evaluate(
                        "() => (window.__net ? window.__net.length : -1)")
                    rep = await finish_video_frame(
                        frame, p,
                        rate=float(self.cfg.get("VIDEO_MAX_RATE", 2.0)),
                        max_wait_s=int(self.cfg.get("VIDEO_MAX_WAIT", 900)),
                        log=log, bank=self.bank)
                    # the IT video module batches its report, so give the echo a
                    # longer window than the audio path needs
                    landed = bool(rep.get("ok")) and await self._await_heartbeat(
                        frame, before, timeout=int(self.cfg.get("VIDEO_ECHO_WAIT", 90)))
                    log(f"  [{idx}] {label[:30]!r} (video module) "
                        f"dur={rep.get('dur')} cur={rep.get('cur')} "
                        f"secs={rep.get('secs')} -> {'ok' if landed else rep.get('why') or 'no echo'}")
                else:
                    primed = await frame.evaluate("window.__primeMedia(12000)")
                    if not primed.get("ok"):
                        log(f"  [{idx}] prime failed: {primed}")
                        continue
                    before = await frame.evaluate(
                        "() => (window.__net ? window.__net.length : -1)")
                    info = await frame.evaluate("window.__jumpToEnd(0.4)")
                    if not info.get("ok"):
                        log(f"  [{idx}] jump failed: {info}")
                        continue
                    landed = await self._await_heartbeat(frame, before, timeout=45)
                    log(f"  [{idx}] {label[:32]!r} dur={info['duration']:.0f}s "
                        f"-> {'ok' if landed else 'no heartbeat'}")
                if landed:
                    done += 1
                else:
                    last_reason = last_reason or (rep.get("why") or "no echo")
            except Exception as exc:
                # keep the reason: "FAIL 0/1" alone once hid a TypeError for several
                # rounds because the detail was buried in a per-frame line
                last_reason = f"{type(exc).__name__}: {str(exc)[:60]}"
                log(f"  [{idx}] error {type(exc).__name__}: {exc}")

        # NOT only when there is no media: app-math chapters carry a video AND
        # pan-yz document task points side by side. Because the old code ran this
        # branch only in the no-frames path, those readers were never visited and
        # their chapters could never complete (2026-10-23: 40+ units "processed"
        # overnight, only 3 actually finished).
        try:
            non_media = await self._do_non_media()
            if non_media:
                log(f"  non-media: {non_media}")
        except Exception as exc:
            log(f"  non-media error: {type(exc).__name__}")

        if done:
            self.stats["video_done"] += done
            return f"DONE {done}/{len(frames)}"

        # No media frame could be activated. On courses like 大学物理 the remaining
        # task points hang off CHILD catalog nodes, so fall back to the cascade
        # instead of reporting a dead end - the parent page exposes frames that
        # look like task-point media but cannot be started from here.
        if self.cfg.get("CASCADE_CHILD_NODES", True) and not getattr(
                self, "_in_cascade", False):
            cascaded = await self._cascade_child_nodes(
                limit=int(self.cfg.get("CASCADE_LIMIT", 40)))
            if cascaded:
                return cascaded
        self.stats["video_fail"] += 1
        await self.dump_debug("videofail")
        # the reason travels with the result so a failure is never opaque
        return f"FAIL 0/{len(frames)} why={last_reason or 'unknown'}"

    async def _do_non_media(self) -> str:
        """Handle pdf-viewer and page-flip-reader task points in this chapter."""
        p = self.page
        results = []
        for frame in p.frames:
            try:
                url = frame.url
            except Exception:
                continue
            if "/modules/pdf/" in url:
                res = await finish_pdf(frame)
                log(f"pdf task point -> {json.dumps(res, ensure_ascii=False)[:150]}")
                results.append(("pdf", res.get("ok")))
            elif "pan-yz.chaoxing.com/screen" in url:
                res = await walk_reader(frame, p, max_pages=40, dwell_ms=200)
                log(f"reader task point -> visited={res.get('visited')}/"
                    f"{res.get('total')} ok={res.get('ok')}")
                results.append(("reader", res.get("ok")))
        if not results:
            return ""
        good = sum(1 for _k, v in results if v)
        if good:
            self.stats["video_done"] += good
        return f"NON_MEDIA {good}/{len(results)}"

    async def _cascade_child_nodes(self, limit: int = 12) -> str:
        """Open each unfinished child catalog node and finish ITS task points.

        Some chapters (军事理论) keep every task point on child nodes; the parent
        page stays empty until the child is opened. Node ids are readable as the
        h5 id "cur<chapterId>", so build the URL directly.
        """
        p = self.page
        parent = p.url
        # Hard recursion guard. The cascade drives child nodes through
        # do_video_all_tabs -> do_video, and do_video itself falls back to
        # _cascade_child_nodes when a node has no media. Without this flag that is
        # a cycle: the nested call re-reads the catalog, picks the same first
        # unfinished node, and the pair loops forever - which is exactly what
        # produced "node 光波的衍射 -> ok" repeating every 96s without progress.
        if getattr(self, "_in_cascade", False):
            log("cascade: already inside a cascade, not re-entering")
            return ""
        self._in_cascade = True
        try:
            nodes = [n for n in await list_child_nodes(p) if n.get("count")]
            if not nodes:
                return ""
            # Never re-attempt a node this session already reported as ok: if it did
            # not clear server-side, retrying it in a tight loop only repeats the same
            # call and looks like automation.
            attempted = getattr(self, "_attempted_nodes", None)
            if attempted is None:
                attempted = set()
                self._attempted_nodes = attempted
            fresh = [n for n in nodes if str(n["chapterId"]) not in attempted]
            if not fresh:
                log(f"cascade: all {len(nodes)} unfinished node(s) already attempted "
                    f"this session, skipping")
                return ""
            nodes = fresh
            budget = min(limit, len(nodes))
            log(f"cascade: {len(nodes)} unfinished child node(s), visiting {budget}")
            done = 0
            for n in nodes[:budget]:
                attempted.add(str(n["chapterId"]))
                try:
                    url = re.sub(r"chapterId=\d+", f"chapterId={n['chapterId']}", parent)
                    await p.goto(url, wait_until="domcontentloaded", timeout=60000)
                    await p.wait_for_timeout(6000)
                    await self.dismiss_modal()

                    # Use the SAME tab-aware path as the main loop. This branch used to
                    # run its own single-frame loop, so a node whose task points are split
                    # across tabs only ever had its first tab done while still being
                    # reported ok - the reason 大学物理 reported success for hours without
                    # the counter moving.
                    res = await self.do_video_all_tabs()
                    ok = not res.startswith("NO_TASK_POINT") and "no echo" not in res
                    if res.startswith("NO_TASK_POINT"):
                        # No media: the node may be a pdf, a reader, a quiz, or a
                        # 随堂练习 (in-class exercise, which is just a small quiz).
                        nm = await self._do_non_media()
                        ok = bool(nm and "0/" not in nm)
                        if not ok and self.cfg.get("CASCADE_QUIZ", True):
                            # Only accept a VERIFIED submit as success - the platform
                            # refuses submissions that still have empty questions.
                            try:
                                quiz_res = await self.do_quiz()
                                if quiz_res and quiz_res.startswith("QUIZ"):
                                    ok = "verdict=done" in quiz_res
                                    log(f"  node {n['chapterId']} quiz -> {quiz_res}")
                            except Exception as exc:
                                log(f"  node {n['chapterId']} quiz failed: "
                                    f"{type(exc).__name__}")
                    if ok:
                        self.stats["cascade_done"] = self.stats.get("cascade_done", 0) + 1
                    log(f"  node {n['chapterId']} {n['title'][:38]!r} "
                        f"jobs={n['count']} -> {'ok' if ok else 'no echo'} [{res[:44]}]")
                    if ok:
                        done += 1
                    await p.wait_for_timeout(random.randint(1500, 3500))
                except Exception as exc:
                    log(f"  node {n['chapterId']} error {type(exc).__name__}")
            # return to the parent so the caller's counters stay coherent
            try:
                await p.goto(parent, wait_until="domcontentloaded", timeout=60000)
                await p.wait_for_timeout(3000)
            except Exception:
                pass
            if done:
                self.stats["video_done"] += done
                return f"CASCADE {done}/{budget}"
            return ""
        finally:
            self._in_cascade = False

    async def _await_heartbeat(self, frame, before: int, timeout: int = 45) -> bool:
        """Wait for the player to fire a progress request after the jump."""
        end = time.time() + timeout
        while time.time() < end:
            await self.page.wait_for_timeout(1000)
            try:
                now = await frame.evaluate(
                    "() => (window.__net ? window.__net.length : -1)")
            except Exception:
                continue
            if now > before:
                return True
        return False

    async def _wait_rate(self, cap: int = 3600) -> bool:
        """MODE=speed fallback: play at RATE until the media reaches the end."""
        last, stall = -1, 0
        for _ in range(cap):
            info = await self.page.evaluate("""() => {
                const v = document.querySelector('video,audio');
                return v ? { t: v.currentTime, d: v.duration || 0, e: v.ended } : null; }""")
            if info is None:
                await self.page.wait_for_timeout(1000)
                continue
            if info["e"] or (info["d"] and info["t"] >= info["d"] - 0.6):
                return True
            if abs(info["t"] - last) < 0.4:
                stall += 1
                if stall > 5:
                    await self.page.evaluate(
                        f"window.__setRate({float(self.cfg.get('RATE', 2))})")
                    stall = 0
            else:
                stall = 0
            last = info["t"]
            await self.page.wait_for_timeout(1000)
        return False

    # ---------------------------------------------------------- quiz
    async def do_quiz(self) -> str:
        """Answer the chapter quiz.

        The questions are NOT in the main document: the whole quiz is served by
        /mooc-ans/work/doHomeWorkNew inside an iframe. So find the frame that
        actually holds .TiMu nodes and drive that one.
        """
        sel = self.cfg.get("QUIZ_SELECTORS") or {}
        qsel = sel.get("question") or ".TiMu, .questionLi"
        p = self.page

        stage = await self._find_quiz_stage(qsel)
        if stage is None:
            self.stats["quiz_none"] += 1
            return "NO_QUIZ"

        # A quiz that is already graded must NOT be submitted again. 2026-10-14 the
        # retry loop resubmitted a failed physics quiz ~30 times inside 25 minutes,
        # reached the platform's 100-attempt cap, and that work now rejects every new
        # submission ("无效的参数：code-1"). Redoing a failed quiz needs the vision
        # repair pipeline; the bulk runner only ever has cache answers.
        # 2026-10-25: below-pass quizzes ARE retried now that the human submit flow
        # (workPop confirm) actually works - the review page teaches the correct
        # answers, so a retry succeeds instead of burning attempts blindly. Cap the
        # retries per quiz per run so a broken answer source cannot burn the account.
        try:
            gstate = await stage.evaluate("""() => {
                const t = (document.body && document.body.innerText) || '';
                const m = t.match(/本次成绩[:：]?\\s*([\\d.]+)/);
                const score = m ? Number(m[1]) : null;
                const am = t.match(/第\\s*(\\d+)\\s*次作答/);
                // ONLY an explicit "本次成绩N分" proves the quiz was graded. The
                // edit page also carries .Py_answer nodes, and using those as a
                // proxy silently skipped every unsubmitted quiz (2.3 never ran).
                const graded = m !== null;
                return {score: score, attempts: am ? Number(am[1]) : null,
                        graded: graded,
                        passed: graded && (score === null || score >= 60)};
            }""")
        except Exception:
            gstate = {"graded": False, "passed": False}
        if gstate.get("graded") and gstate.get("passed"):
            self.stats["quiz_skipped_graded"] = \
                self.stats.get("quiz_skipped_graded", 0) + 1
            log("quiz: already passed (score shown) - not resubmitting")
            return "QUIZ already graded - skipped"
        if gstate.get("graded") and not gstate.get("passed"):
            key = str(self.cfg.get("COURSE_ID")) + "|" + \
                str((re.search(r"chapterId=(\d+)", p.url or "") or [None, ""])[1])
            self._quiz_key = key
            tries = getattr(self, "_quiz_retries", None)
            if tries is None:
                tries = self._quiz_retries = {}
            n = tries.get(key, 0)
            att = gstate.get("attempts") or 0
            if n >= 2 or att >= 95:
                self.stats["quiz_skipped_graded"] = \
                    self.stats.get("quiz_skipped_graded", 0) + 1
                log(f"quiz: below pass ({gstate.get('score')}) - retry budget used "
                    f"({n}) or attempts {att} near cap, skipping")
                return "QUIZ below pass - skipped"
            tries[key] = n + 1
            log(f"quiz: below pass (score {gstate.get('score')}, attempt {att}) - "
                f"relaunching once more (retry {n + 1}/2)")
            # A below-pass review page lists the correct answers. Learn them into
            # the bank BEFORE relaunching, so the retry is not the same lost game.
            try:
                learned_items = await self._learn_review(stage, qsel)
                learned = len(learned_items or [])
                if learned:
                    log(f"quiz: learned {learned} answer(s) from the review")
                    stash = getattr(self, "_review_answers", None)
                    if stash is None:
                        stash = self._review_answers = {}
                    stash[key] = learned_items
            except Exception as exc:
                log(f"quiz: review scrape failed {type(exc).__name__}")
                learned_items = []
            if not learned_items:
                # The review page hides 正确答案 even for below-pass attempts, so a
                # retake without new information only burns attempts. Leave it to
                # the offline vision repair instead.
                log("quiz: review shows no correct answers - not retaking blindly")
                return "QUIZ below pass - no review answers"
            try:
                relaunched = await self._retake_quiz(stage)
            except Exception:
                relaunched = False
            if not relaunched:
                log("quiz: no retake control on the review page")
                return "QUIZ below pass - no retake control"
            await p.wait_for_timeout(4500)
            stage = await self._find_quiz_stage(qsel)
            if stage is None:
                return "QUIZ below pass - relaunch failed"

        # answer by index so a mid-quiz page reload can resume without redoing work
        done_idx = set()
        answered, missed = 0, 0
        resolved = []
        rounds = 0
        while rounds < 4:
            rounds += 1
            qs = await stage.locator(qsel).all()
            if rounds == 1:
                log(f"quiz: {len(qs)} questions")
            todo = [(i, q) for i, q in enumerate(qs) if i not in done_idx]
            if not todo:
                break
            detached = False
            for i, q in todo:
                try:
                    typ_raw = await self._text(q, sel.get("type"), 1500)
                    typ = self._qtype(typ_raw)
                    title_raw = await self._text(q, sel.get("title"), 2500)
                    stem = clean_stem(title_raw)
                    if not stem:
                        stem = clean_stem(await q.inner_text(timeout=2500))[:200]

                    options = []
                    osel = sel.get("options")
                    if osel:
                        loc = q.locator(osel)
                        if await loc.count():
                            options = [re.sub(r"\s+", " ", t).strip()
                                       for t in await loc.all_inner_texts()]

                    ans = self.bank.query(stem, typ, options)
                    if not ans.hit:
                        stash = getattr(self, "_review_answers", {}).get(
                            getattr(self, "_quiz_key", "")) or []
                        if i < len(stash):
                            ans = _ReviewAnswer(stash[i].get("answer", ""))
                            log(f"  [{i+1}/{len(qs)}] review answer by position "
                                f"-> {ans.text}")
                    if not ans.hit:
                        missed += 1
                        self.stats["ans_miss"] += 1
                        self.unanswered.append({"question": stem, "type": typ,
                                                "options": "|".join(options)})
                        log(f"  [{i+1}/{len(qs)}] MISS {typ} :: {stem[:42]}")
                        done_idx.add(i)
                        continue

                    ok = await self._apply(q, typ, ans, options, sel)
                    if ok:
                        answered += 1
                        self.stats["ans_hit"] += 1
                        resolved.append({"stem": stem, "type": typ,
                                         "answer": ans.text, "source": ans.source})
                        if len(self.stats["answer_samples"]) < 12:
                            self.stats["answer_samples"].append(
                                {"stem": stem[:80], "type": typ,
                                 "answer": ans.text[:60], "source": ans.source})
                        log(f"  [{i+1}/{len(qs)}] {typ} <= {ans.text[:36]} ({ans.source})")
                        done_idx.add(i)
                        # the work page re-renders between questions; a short
                        # pause keeps the element handle valid
                        await p.wait_for_timeout(600)
                    else:
                        missed += 1
                        self.stats["ans_miss"] += 1
                        log(f"  [{i+1}/{len(qs)}] APPLY_FAIL {typ} :: {stem[:36]}")
                        done_idx.add(i)
                except Exception as exc:
                    msg = str(exc)
                    if "detached" in msg or "Frame" in msg or "Execution context" in msg:
                        log(f"  [{i+1}] frame reloaded mid-quiz, re-acquiring")
                        detached = True
                        break
                    missed += 1
                    log(f"  [{i+1}] error {type(exc).__name__}: {msg[:60]}")
                    done_idx.add(i)

            if detached:
                await p.wait_for_timeout(3500)
                stage = await self._find_quiz_stage(qsel)
                if stage is None:
                    log("quiz: lost the quiz frame, stopping here")
                    break
                continue
            break

        for r in resolved:
            try:
                self.bank.remember(r["stem"], r["type"], r["answer"], r["source"])
            except Exception as exc:
                log(f"  write-back failed for {r['stem'][:30]}: {exc}")
        if resolved:
            log(f"write-back: {len(resolved)} answers -> {self.bank.cache_path}")

        stage = await self._find_quiz_stage(qsel)
        if stage is None:
            log("quiz: no usable stage before submit")
        elif self.cfg.get("QUIZ_SUBMIT"):
            # --- verification pass -------------------------------------------
            # _apply returns True when SOMETHING was filled, which is too weak: a
            # multi-blank 填空题 can report success with only one blank set, and
            # the platform then refuses the submit. Re-read the real DOM state and
            # retry whatever is genuinely still empty BEFORE submitting.
            prog = await self._quiz_progress(stage)
            if prog.get("total") and prog.get("answered", 0) < prog["total"]:
                log(f"quiz: {prog.get('answered')}/{prog.get('total')} answered - "
                    f"retrying the gaps")
                qs2 = await stage.locator(qsel).all()
                for i, q in enumerate(qs2):
                    try:
                        stale = await q.evaluate("""(el) => {
                            const id = el.getAttribute('data') || '';
                            const hid = id ? el.querySelector(
                                `input[name="answer${id}"][type="hidden"]`) : null;
                            if (hid && (hid.value || '').trim()) return false;
                            if (el.querySelector('input[type=radio]:checked')) return false;
                            if ([...el.querySelectorAll('input[type=checkbox]')]
                                    .some(b => b.checked)) return false;
                            const ta = el.querySelector('textarea[name^=answerEditor]');
                            if (ta && (ta.value || '').trim()) return false;
                            const f = el.querySelector('iframe');
                            if (f && f.contentDocument && f.contentDocument.body
                                && (f.contentDocument.body.innerText || '').trim())
                                return false;
                            return true;
                        }""")
                        if not stale:
                            continue
                        typ_raw = await self._text(q, sel.get("type"), 1500)
                        typ = self._qtype(typ_raw)
                        title_raw = await self._text(q, sel.get("title"), 2500)
                        stem = clean_stem(title_raw) or clean_stem(
                            await q.inner_text(timeout=2500))[:200]
                        options = []
                        if sel.get("options"):
                            loc = q.locator(sel["options"])
                            if await loc.count():
                                options = [re.sub(r"\s+", " ", t).strip()
                                           for t in await loc.all_inner_texts()]
                        ans = self.bank.query(stem, typ, options)
                        if not ans.hit:
                            continue
                        if await self._apply(q, typ, ans, options, sel):
                            answered += 1
                            log(f"  retry [{i+1}] {typ} <= {ans.text[:32]} ok")
                    except Exception as exc:
                        if "detached" in str(exc) or "Execution" in str(exc):
                            stage = await self._find_quiz_stage(qsel) or stage
                prog = await self._quiz_progress(stage)
                log(f"quiz: after retry {prog.get('answered')}/{prog.get('total')} "
                    f"answered")

            verdict = await self._submit(sel, stage)
            if verdict != "done":
                # do NOT let the caller believe the task point is finished
                self.stats["quiz_unverified"] = \
                    self.stats.get("quiz_unverified", 0) + 1
        else:
            verdict = "not_submitted"
            log("quiz: QUIZ_SUBMIT=false, filled but not submitted")

        score = await self._read_score(stage) if stage is not None else None
        self.stats["quiz_done"] += 1
        await self.dump_debug("quiz_after")
        return (f"QUIZ done={answered} miss={missed} "
                f"verdict={locals().get('verdict', 'n/a')} score={score}")

    async def _find_quiz_stage(self, qsel: str):
        """Return the Frame or Page that actually holds the quiz questions.

        Quizzes surface in two ways on this account:
          * a work node opens the quiz directly (/mooc-ans/work/doHomeWorkNew)
          * a media node carries a 视频 / 章节测验 tab pair, and the quiz only
            mounts after the 测验 tab is clicked
        Both are handled here, and the tab is tried BEFORE giving up.
        """
        p = self.page
        tab_labels = ("章节测验", "章节测试", "测验", "课后练习", "练习",
                      "作业", "测试")

        async def look() -> Any:
            candidates = [p] + [f for f in p.frames
                                if any(h in (f.url or "") for h in WORK_HINTS)]
            candidates += [f for f in p.frames if f not in candidates]
            for cand in candidates:
                try:
                    if await cand.locator(qsel).count():
                        if cand is not p:
                            log(f"quiz: questions live in frame {cand.url[:70]}")
                        return cand
                except Exception:
                    continue
            return None

        for attempt in range(5):
            found = await look()
            if found is not None:
                return found
            # click a quiz tab if one is offered (video nodes use tab pairs)
            for label in tab_labels:
                for form in (f"a:has-text('{label}')", f"span:has-text('{label}')",
                             f"li:has-text('{label}')", f"div[role=tab]:has-text('{label}')"):
                    loc = p.locator(form)
                    try:
                        if not await loc.count():
                            continue
                        el = loc.first
                        if not await el.is_visible():
                            continue
                        await el.click(timeout=4000)
                        log(f"quiz: clicked tab {label!r}")
                        await p.wait_for_timeout(5000)
                        found = await look()
                        if found is not None:
                            return found
                    except Exception:
                        continue
            await p.wait_for_timeout(2500)
        return None

    async def _learn_review(self, stage, qsel: str) -> list:
        """Scrape 正确答案 lines from a below-pass review page into the bank."""
        try:
            items = await stage.evaluate("""(qsel) => {
                const out = [];
                document.querySelectorAll(qsel.split(',')[0].trim()).forEach(q => {
                    const t = (q.innerText || '').replace(/\\s+/g, ' ');
                    if (!t) return;
                    const m = t.match(/正确答案[:：]\\s*([A-Fa-f对错√×、,，\\s]+)/);
                    if (!m) return;
                    let ans = m[1].replace(/[^A-Fa-f对错√×]/g, '');
                    if (!ans) return;
                    if (/[对√]/.test(ans)) ans = '对';
                    else if (/[错×]/.test(ans)) ans = '错';
                    else ans = ans.toUpperCase();
                    const head = (q.querySelector('.fontLabel, .qtContent, .Zy_TItle')
                        || q).innerText || '';
                    const clean = head.replace(/^【[^】]*】/, '').trim().slice(0, 200);
                    if (!clean) return;
                    let typ = '单选题';
                    if (/多选题/.test(head)) typ = '多选题';
                    else if (/判断题/.test(head)) typ = '判断题';
                    out.push({stem: clean, type: typ, answer: ans});
                });
                return out;
            }""", qsel)
        except Exception:
            return []
        items = items or []
        for it in items:
            try:
                self.bank.remember(it["stem"], it["type"], it["answer"], "review")
            except Exception:
                continue
        return items

    async def _retake_quiz(self, stage) -> bool:
        """Click the review page's 重做/再做 control so a new attempt can start."""
        try:
            return await stage.evaluate("""() => {
                const cands = [...document.querySelectorAll('a,button,span,div')]
                    .filter(e => e.offsetWidth && e.offsetHeight)
                    .filter(e => /^(重做|再做一次|重新作答|重新测试|再做)$/
                        .test((e.innerText || '').trim()));
                if (!cands.length) return false;
                cands[cands.length - 1].click();
                return true;
            }""")
        except Exception:
            return False

    async def _read_score(self, stage) -> Any:
        try:
            return await stage.evaluate(r"""() => {
                const m = (document.body.innerText || '').match(
                    /(?:得分|成绩|分数)[:：]?\s*([\d.]+)/);
                return m ? Number(m[1]) : null; }""")
        except Exception:
            return None

    async def _find_quiz_frame(self, qsel: str) -> bool:
        p = self.page
        for _ in range(4):
            if await p.locator(qsel).count():
                return True
            for txt in ("测验", "章节测验", "作业", "测试"):
                loc = p.locator(f"a:has-text('{txt}'), li:has-text('{txt}')")
                if await loc.count():
                    try:
                        await loc.first.click(timeout=3000)
                        await p.wait_for_timeout(3000)
                    except Exception:
                        pass
                    if await p.locator(qsel).count():
                        return True
            await p.wait_for_timeout(1200)
        return False

    async def _text(self, scope, selector, timeout) -> str:
        if not selector:
            return ""
        try:
            loc = scope.locator(selector).first
            if await loc.count():
                return (await loc.inner_text(timeout=timeout)) or ""
        except Exception:
            pass
        return ""

    @staticmethod
    def _qtype(raw: str) -> str:
        s = re.sub(r"[\s\d\(\)（）.、:：]", "", raw or "")
        for k in ("单选题", "多选题", "判断题", "填空题", "简答题", "名词解释", "论述题"):
            if k in s:
                return k
        for k, v in (("单选", "单选题"), ("多选", "多选题"), ("判断", "判断题"),
                     ("填空", "填空题"), ("简答", "简答题")):
            if k in s:
                return v
        return "单选题"

    async def _apply(self, q, typ: str, ans, options, sel: dict) -> bool:
        p = self.page

        # --- 判断题 -------------------------------------------------------
        # These render as two radios whose VALUES are 'true' / 'false', not as
        # lettered options, so an answer of 对/错 has no index to click and used
        # to fall through to the fill-in path and fail.
        if typ == "判断题" or (ans.text or "").strip() in ("对", "错", "正确", "错误"):
            want = (ans.text or "").strip()
            truthy = want in ("对", "正确", "√", "T", "true", "是") or (
                not want and ans.indexes and ans.indexes[0] == 0)
            try:
                radios = await q.locator("input[type=radio]").all()
                picked = False
                for r in radios:
                    val = (await r.get_attribute("value") or "").strip().lower()
                    if val in ("true", "false"):
                        if (val == "true") == truthy:
                            try:
                                await r.click(timeout=2500)
                            except Exception:
                                try:
                                    await r.check(timeout=2000)
                                except Exception:
                                    handle = await r.element_handle()
                                    if handle:
                                        await p.evaluate(
                                            "(el) => { el.checked = true;"
                                            " el.dispatchEvent(new Event('change',"
                                            " {bubbles: true})); }", handle)
                            picked = True
                            break
                if picked:
                    return True
                # Second attempt: this renderer may not use true/false VALUES at all.
                # 军事理论's selectWorkQuestionYiPiYue page raised APPLY_FAIL here
                # while doHomeWorkNew was fine - the platform has more than one quiz
                # renderer, and only one of them was handled. Match on the label text
                # next to each radio instead of on its value.
                YES = ("对", "正确", "√", "是", "true", "t", "y")
                NO = ("错", "错误", "×", "x", "否", "false", "f", "n")
                for r in radios:
                    try:
                        label = await r.evaluate(
                            "el => { const b = el.closest('label') || el.parentElement;"
                            " return (b ? (b.innerText || '') : '')"
                            ".replace(/\\s+/g,'').slice(0,8); }")
                    except Exception:
                        continue
                    lab = (label or "").strip().lower()
                    if not lab:
                        continue
                    hit = (truthy and lab in YES) or ((not truthy) and lab in NO)
                    if not hit:
                        continue
                    try:
                        await r.click(timeout=2500)
                    except Exception:
                        try:
                            await r.check(timeout=2000)
                        except Exception:
                            h = await r.element_handle()
                            if h:
                                await p.evaluate(
                                    "(el) => { el.checked = true;"
                                    " el.dispatchEvent(new Event('change',"
                                    " {bubbles: true})); }", h)
                    return True
                # Third: some skins still letter the two choices
                if radios and ans.indexes:
                    k = ans.indexes[0]
                    if k < len(radios):
                        await radios[k].click(timeout=2500)
                        return True
            except Exception:
                pass  # noqa: E722
                self.stats['apply_fail'] = self.stats.get('apply_fail', 0) + 1

        # --- choice -------------------------------------------------------
        if typ in ("单选题", "多选题") or ans.indexes:
            idx = ans.indexes
            if not idx:
                return False
            list_sel = sel.get("options") or "li"
            lis = await q.locator(list_sel).all()
            clicked = 0
            for k in idx:
                if k >= len(lis):
                    continue
                target = lis[k]
                try:
                    await target.click(timeout=3000)
                    clicked += 1
                except Exception:
                    try:
                        inner = target.locator("input[type=radio], input[type=checkbox]")
                        if await inner.count():
                            await inner.first.check(timeout=2500)
                            clicked += 1
                    except Exception:
                        pass  # noqa: E722
                        self.stats['apply_fail'] = self.stats.get('apply_fail', 0) + 1
            return clicked > 0

        # --- fill in the blanks -------------------------------------------
        # Each blank is a WYSIWYG editor rendered in its OWN iframe, backed by a
        # hidden textarea named answerEditor<qid><blankIndex>. There is no
        # input[type=text] to .fill(), which is why 填空题 used to fail.
        vals = self._blank_values(q, ans)
        if await self._fill_iframe_blanks(q, vals):
            return True

        isel = sel.get("inputs") or "input[type=text], textarea"
        boxes = await q.locator(isel).all()
        vals2 = [strip_units(x)
                 for x in split_blanks(ans.text, max(1, len(boxes)))]
        filled = 0
        for k, box in enumerate(boxes):
            val = vals2[k] if k < len(vals2) else ans.text
            if not val:
                continue
            try:
                await box.scroll_into_view_if_needed(timeout=2500)
                handle = await box.element_handle()
                if handle is None:
                    continue
                if await p.evaluate("([el, v]) => window.__fillText(el, v)",
                                    [handle, val]):
                    filled += 1
                    continue
            except Exception:
                pass  # noqa: E722
                self.stats['apply_fail'] = self.stats.get('apply_fail', 0) + 1
            try:
                await box.fill(val, timeout=2500)
                filled += 1
            except Exception:
                pass  # noqa: E722
                self.stats['apply_fail'] = self.stats.get('apply_fail', 0) + 1
        if filled:
            return True

        # --- rich text editor (ueditor) ----------------------------------
        for esel in (".edui-body-container", ".edui-editor iframe",
                     "div[contenteditable=true]"):
            loc = q.locator(esel)
            if await loc.count():
                try:
                    handle = await loc.first.element_handle()
                    if handle and await p.evaluate(
                            "([el, v]) => window.__fillText(el, v)", [handle, ans.text]):
                        return True
                except Exception:
                    pass  # noqa: E722
                    self.stats['apply_fail'] = self.stats.get('apply_fail', 0) + 1
        return False

    @staticmethod
    def _blank_values(q, ans) -> list:
        """Split an answer into one value per blank (by ';' then by count)."""
        text = (ans.text or "").strip()
        if not text:
            return []
        parts = [strip_units(x.strip()) for x in re.split(r"[;；]", text)
                 if x.strip()]
        return parts or [strip_units(text)]

    async def _fill_iframe_blanks(self, q, vals) -> bool:
        """Write into each blank's WYSIWYG iframe and mirror into its textarea.

        The editors are same-origin (src=javascript:void(...) with document.domain
        set), so Playwright can reach them as child frames of the quiz frame.
        """
        if not vals:
            return False
        try:
            boxes = await q.locator("textarea[name^=answerEditor]").all()
        except Exception:
            return False
        if not boxes:
            return False

        page = self.page
        filled = 0
        for k, box in enumerate(boxes):
            val = vals[k] if k < len(vals) else (vals[-1] if vals else "")
            if not val:
                continue
            name = await box.get_attribute("name") or ""
            # 1) hidden/backing textarea: this is what actually gets submitted
            try:
                handle = await box.element_handle()
                if handle is not None:
                    ok = await page.evaluate(
                        """([el, v]) => {
                            try { el.value = v; } catch (e) {}
                            try { el.innerText = v; } catch (e) {}
                            try { el.dispatchEvent(new Event('input', {bubbles: true})); } catch (e) {}
                            try { el.dispatchEvent(new Event('change', {bubbles: true})); } catch (e) {}
                            let ed = el.parentElement;
                            for (let i = 0; i < 4 && ed; i++) {
                                const f = ed.querySelector && ed.querySelector('iframe');
                                if (f && f.contentDocument && f.contentDocument.body) {
                                    f.contentDocument.body.innerHTML = v;
                                    try { f.contentDocument.body.dispatchEvent(
                                        new Event('input', {bubbles: true})); } catch (e) {}
                                    return true;
                                }
                                ed = ed.parentElement;
                            }
                            return true;
                        }""", [handle, val])
                    if ok:
                        filled += 1
            except Exception:
                pass  # noqa: E722
                self.stats['blank_fail'] = self.stats.get('blank_fail', 0) + 1

            # 2) also write the visible editor directly, so the page's own state
            #    and any "unsaved changes" check stay consistent
            try:
                for fr in page.frames:
                    try:
                        has = await fr.evaluate(
                            "() => !!document.body && document.body.isContentEditable")
                    except Exception:
                        has = False
                    if not has:
                        continue
                    tel = await fr.evaluate("() => document.body.innerText || ''")
                    if tel.strip():
                        continue          # already holds a different blank's text
                    await fr.evaluate(
                        """(v) => { document.body.innerHTML = v;
                                    document.body.dispatchEvent(
                                        new Event('input', {bubbles: true})); }""", val)
                    break
            except Exception:
                pass  # noqa: E722
                self.stats['blank_fail'] = self.stats.get('blank_fail', 0) + 1

        return filled > 0

    # ------------------------------------------------------ quiz progress
    PROGRESS_JS = r"""() => {
        const qs = [...document.querySelectorAll('.TiMu, .questionLi')];
        let answered = 0;
        qs.forEach(q => {
            const id = q.getAttribute('data') || '';
            const hid = id ? q.querySelector(`input[name="answer${id}"][type="hidden"]`) : null;
            if (hid && (hid.value || '').trim()) { answered++; return; }
            const r = q.querySelector('input[type=radio]:checked');
            if (r) { answered++; return; }
            const cb = [...q.querySelectorAll('input[type=checkbox]')].some(b => b.checked);
            if (cb) { answered++; return; }
            const ta = q.querySelector('textarea[name^=answerEditor]');
            if (ta && (ta.value || '').trim()) { answered++; return; }
            // blanks may only be mirrored into the WYSIWYG iframe
            const f = q.querySelector('iframe');
            if (f && f.contentDocument && f.contentDocument.body
                && (f.contentDocument.body.innerText || '').trim()) { answered++; return; }
            const box = [...q.querySelectorAll('input[type=text], input[type=password]')]
                .some(i => (i.value || '').trim());
            if (box) { answered++; }
        });
        const txt = document.body ? (document.body.innerText || '') : '';
        return {
            total: qs.length,
            answered,
            done: /已完成|已提交|已批阅/.test(txt),
            score: (txt.match(/本次成绩[:：]?\s*([\d.]+)/) || [])[1] || null,
            warn: (txt.match(/(还有\s*\d+\s*题未作答|请完成所有|不能提交|未作答[^。]{0,12})/) || [])[1] || null,
        };
    }"""

    async def _quiz_progress(self, stage) -> dict:
        try:
            return await stage.evaluate(self.PROGRESS_JS) or {}
        except Exception:
            return {}

    async def _submit(self, sel: dict, stage=None) -> str:
        """Submit, then VERIFY. Returns 'done' | 'partial' | 'blocked' | 'no_button'.

        The old version logged success unconditionally, which made a rejected
        submit (e.g. "还有 2 题未作答") look like a completed task point while the
        platform kept it unfinished.
        """
        root = stage or self.page
        p = self.page

        before = await self._quiz_progress(root)
        if before.get("done"):
            return "done"

        clicked = None
        # 1) the page's own function is the most reliable route on mooc-ans
        try:
            called = await root.evaluate(
                "() => { try { if (typeof btnBlueSubmit === 'function')"
                " { btnBlueSubmit(); return 'btnBlueSubmit'; } } catch (e) {}"
                " return null; }")
            if called:
                clicked = called
        except Exception:
            pass  # noqa: E722
            self.stats['submit_fail'] = self.stats.get('submit_fail', 0) + 1

        # 2) fall back to configured / known selectors
        if not clicked:
            cand = [c.strip() for c in (sel.get("submit") or "").split(",") if c.strip()]
            cand += ["a:has-text('提交')", "button:has-text('提交')",
                     "input[type=button][value*='提交']", ".btnSubmit", "#submitBtn"]
            for cs in cand:
                try:
                    loc = root.locator(cs)
                    if not await loc.count():
                        continue
                    if not await loc.first.is_visible():
                        continue
                    await loc.first.click(timeout=5000)
                    clicked = cs
                    break
                except Exception:
                    continue
        if not clicked:
            # the work page re-renders between the answer pass and the submit, so
            # the button can be transiently absent. Re-acquire and retry instead
            # of giving up on the first miss (2.3 hit exactly this race).
            for attempt in range(4):
                await p.wait_for_timeout(3500)
                try:
                    called = await root.evaluate(
                        "() => { try { if (typeof btnBlueSubmit === 'function')"
                        " { btnBlueSubmit(); return 'btnBlueSubmit'; } } catch (e) {}"
                        " return null; }")
                    if called:
                        clicked = called
                        break
                except Exception as exc:
                    if "detached" in str(exc) or "context" in str(exc):
                        stage = await self._find_quiz_stage(qsel)
                        if stage is not None:
                            root = stage
                        continue
                for cs in ("a.btnSubmit", "a:has-text('提交')"):
                    try:
                        loc = root.locator(cs)
                        if await loc.count() and await loc.first.is_visible():
                            await loc.first.click(timeout=4000)
                            clicked = cs
                            break
                    except Exception:
                        continue
                if clicked:
                    break
                log(f"quiz: submit control not ready yet (attempt {attempt + 1}/4)")
        if not clicked:
            log("quiz: submit control not found")
            return "no_button"

        # 3) confirm dialog. The mooc2 "确认提交吗?" popup (workPop) lives in the
        # TOP page frame and its confirm button is labelled 提交 with class
        # jb_btn - not 确定. Missing this button was why every submit looked
        # "partial": the request never left the browser.
        await p.wait_for_timeout(2500)
        confirmed = False
        try:
            pop = p.locator("a.jb_btn:has-text('提交')")
            if await pop.count() and await pop.first.is_visible():
                await pop.first.click(timeout=4000)
                confirmed = True
                log("quiz: clicked workPop 提交 (confirm)")
        except Exception:
            pass
        if not confirmed:
            for cs in ((sel.get("confirm") or "").split(",")
                       + ["a:has-text('确定')", "a:has-text('确认')",
                          "button:has-text('确定')", "input[value='确定']"]):
                cs = cs.strip()
                if not cs:
                    continue
                try:
                    for loc in (p.locator(cs), root.locator(cs)):
                        if await loc.count():
                            try:
                                if not await loc.first.is_visible():
                                    continue
                            except Exception:
                                pass  # noqa: E722
                                self.stats['submit_fail'] = self.stats.get('submit_fail', 0) + 1
                            await loc.first.click(timeout=3000)
                            break
                    else:
                        continue
                    break
                except Exception:
                    continue
        await p.wait_for_timeout(3500)

        # 4) some skins ask a second time
        try:
            await root.evaluate(
                "() => { try { if (typeof submitCheckTimes === 'function')"
                " submitCheckTimes(); } catch (e) {} }")
        except Exception:
            pass  # noqa: E722
            self.stats['submit_fail'] = self.stats.get('submit_fail', 0) + 1
        await p.wait_for_timeout(3000)

        after = await self._quiz_progress(root)
        if after.get("done"):
            log(f"quiz: submitted and confirmed (score={after.get('score')})")
            return "done"
        if after.get("warn"):
            log(f"quiz: submit rejected - {after['warn']}")
            return "blocked"
        if after.get("total") and after.get("answered", 0) >= after["total"]:
            log(f"quiz: submitted, awaiting page confirmation "
                f"({after.get('answered')}/{after.get('total')})")
            return "partial"
        log(f"quiz: submitted but only {after.get('answered')}/{after.get('total')} "
            f"answered - not counted")
        return "blocked"

    # ---------------------------------------------------------- driver
    async def run(self, probe_only: bool = False):
        await self.start()
        await self.login()
        chapters = await self.chapters()
        if not chapters:
            log("no chapters found; check COURSE_ID or account enrollment")

        lo, hi = (self.cfg.get("SLEEP_BETWEEN_CHAPTERS") or [90, 300])[:2]
        # human-like pacing: jittered gaps, a moving pointer during idle, occasional
        # real breaks, and a per-day cap (see humanize.py for why each matters)
        try:
            from humanize import Budget, idle, take_break
            budget = Budget(daily_cap=int(self.cfg.get("DAILY_TASK_CAP", 60)), log=log)
            budget.new_session()
            log(f"humanize: {budget.summary()}")
        except Exception as exc:
            log(f"humanize unavailable ({type(exc).__name__}), running plain")
            Budget = None
            budget = None

            async def idle(*a, **k):
                return None

            async def take_break(*a, **k):
                return 0.0

        break_every = int(self.cfg.get("BREAK_EVERY_N_CHAPTERS", 12))

        for i, url in enumerate(chapters, 1):
            # a student does not study at 04:00: pause outside the waking window so
            # the activity pattern stays plausible
            try:
                from humanize import in_active_hours
                if not in_active_hours(0, 24):
                    log("humanize: outside the active window, pausing")
                    break
            except Exception:
                pass
            # The daily allowance meters task-point progress (writes to the platform).
            # A --probe run makes no writes at all - it only reads the DOM - so the
            # allowance must not block it. Previously an exhausted budget made --probe
            # exit immediately having inspected nothing, which made the one read-only
            # diagnostic in the worker useless exactly when it was most wanted.
            if not probe_only and budget is not None and not budget.allow():
                log(f"humanize: {budget.summary()} - 今日额度用完，停下")
                break

            # Objective item (4): stop for the day when the platform shows a warning.
            # Checked on every chapter, because the notice appears on whatever page
            # happens to be open when it is issued.
            try:
                from anomaly import check as _anom_check
                if await _anom_check(self.page, log=log, where=url[:150]):
                    log("worker: anomaly marker on the page - stopping this run")
                    return {"stopped": "anomaly"}
            except Exception:
                pass
            log(f"=== [{i}/{len(chapters)}] {url[:120]}")
            try:
                await self.open_chapter(url, count_attempt=not probe_only)
                if probe_only:
                    # Look INSIDE the frames, not just at the top document. The media
                    # and the quiz both live in per-item iframes, so counting on the
                    # top document reports 0 for every chapter - which makes the probe
                    # unable to distinguish "page is fine" from "enc rejected, no
                    # content at all", the one question it exists to answer.
                    top = await self.page.evaluate(r"""() => ({
                        frames: window.frames.length,
                        title: document.title,
                        text: (document.body.innerText || '').replace(/\s+/g,' ').slice(0,90) })""")
                    per_frame = []
                    for fr in self.page.frames:
                        try:
                            got = await fr.evaluate(r"""() => {
                                const q = s => document.querySelectorAll(s).length;
                                // duration/readyState matter: a media element whose
                                // duration never resolves cannot be watched to the end,
                                // so reporting counts alone hides a broken node.
                                const el = document.querySelector('video,audio');
                                let dur = null, ready = null, tag = null;
                                if (el) { tag = el.tagName.toLowerCase();
                                          dur = (el.duration && isFinite(el.duration))
                                                ? Math.round(el.duration) : null;
                                          ready = el.readyState; }
                                // start_video clicks these; if an audio player does
                                // not expose them, audio nodes cannot be started.
                                // report each selector start_video actually tries,
                                // separately: a loose count hides which one matches
                                const big = q('button.vjs-big-play-button');
                                const poster = q('.vjs-poster');
                                const playctl = q('button.vjs-play-control');
                                const ctrl = big + poster + playctl;
                                return { url: (location.href||'').slice(0,70),
                                         video: q('video'), audio: q('audio'),
                                         quiz: q('.TiMu,.questionLi'),
                                         pdf: q('canvas,.pdfViewer,.pdf-container'),
                                         tag: tag, dur: dur, ready: ready,
                                         big: big, ctrl: ctrl,
                                         text: (document.body ? (document.body.innerText||'') : '')
                                                 .replace(/\s+/g,' ').slice(0,60) };
                            }""")
                            if got["video"] or got["audio"] or got["quiz"] or got["pdf"]:
                                per_frame.append(got)
                        except Exception:
                            continue
                    bad_enc = "enc校验失败" in (top.get("text") or "")
                    log(f"probe: title={top['title'][:16]!r} frames={top['frames']} "
                        f"media={len(per_frame)}"
                        + ("  ** enc校验失败 **" if bad_enc else ""))
                    for g in per_frame:
                        kinds = [k for k in ("video", "audio", "quiz", "pdf") if g[k]]
                        desc = ",".join(f"{k}x{g[k]}" for k in kinds)
                        extra = ""
                        if g.get("tag"):
                            extra = (f"  {g['tag']} dur={g['dur']}s"
                                     if g.get("dur") else f"  {g['tag']} dur=none"
                                                             f" ready={g.get('ready')}")
                        # keep the module URL: it is what identifies WHICH player is in
                        # use, and a different player may not respond to the same
                        # play-control selector (the audio case is unverified).
                        mod = re.search(r"/modules/(\w[\w-]*)/", g["url"] or "")
                        where = f"/modules/{mod.group(1)}/" if mod else (g["url"] or "")[:40]
                        ctl = (f"big={g.get('big',0)} poster={g.get('poster',0)} "
                               f"playctl={g.get('playctl',0)}")
                        log(f"        {desc:<18}{extra}   {where:<18} {ctl}")
                    await self.dump_debug(f"probe_{i:02d}")
                    continue

                before = dict(self.stats)
                if self.cfg.get("DO_VIDEO", True):
                    log(f"video: {await self.do_video_all_tabs()}")
                if self.cfg.get("DO_QUIZ", True):
                    log(f"quiz : {await self.do_quiz()}")

                # A chapter that produced anything at all is not a stuck node, so its
                # failure count must not keep climbing toward the park threshold.
                try:
                    gained_now = sum(
                        max(0, self.stats.get(k, 0) - before.get(k, 0))
                        for k in ("video_done", "quiz_done"))
                    m_clear = re.search(r"chapterId=(\d+)", url or "")
                    if gained_now > 0 and m_clear:
                        self._clear_attempt(m_clear.group(1))
                except Exception:
                    pass
                # count genuine progress against the daily allowance
                if budget is not None:
                    gained = (self.stats.get("video_done", 0) - before.get("video_done", 0)
                              + self.stats.get("quiz_done", 0) - before.get("quiz_done", 0)
                              + self.stats.get("cascade_done", 0)
                              - before.get("cascade_done", 0))
                    if gained > 0:
                        budget.record(gained)
            except Exception as exc:
                log(f"chapter error {type(exc).__name__}: {exc}")
                await self.dump_debug(f"error_{i:02d}")

            if i < len(chapters):
                # occasional pointer movement so the page is not frozen for minutes
                await idle(self.page)
                nap = random.randint(int(lo), int(hi))
                log(f"sleep {nap}s before next chapter")
                await asyncio.sleep(nap)
                if break_every and i % break_every == 0:
                    await take_break(self.page, log=log)

        log("==== summary ====")
        # surface swallowed failures: a number that keeps climbing means a
        # path is systematically broken even though the run looks successful
        for key in ("apply_fail", "blank_fail", "submit_fail"):
            if self.stats.get(key):
                log(f"  swallowed failures: {key} = {self.stats[key]}")
        summary = {k: v for k, v in self.stats.items() if k != "answer_samples"}
        log(json.dumps(summary, ensure_ascii=False, indent=2))

        if self.unanswered:
            csv_path = os.path.join(ROOT, "logs", "unanswered.csv")
            try:
                with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
                    w = csv.writer(fh)
                    w.writerow(["question", "type", "options", "answer"])
                    for row in self.unanswered:
                        w.writerow([row["question"], row["type"], row["options"], ""])
                log(f"unanswered: {len(self.unanswered)} -> {csv_path} "
                    f"(fill the answer column, then: python tools.py import "
                    f"logs/unanswered.csv)")
            except Exception as exc:
                log(f"unanswered dump failed: {exc}")

        # Per-course, and atomic: three workers each write their own summary at the
        # end of a run, and a single shared file meant only the last finisher was
        # ever visible.
        safejson.save(self._course_file("last_run"), self.stats)
        await self.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true",
                    help="DOM inspection only, no writes to the platform")
    ap.add_argument("--chapters", type=int, default=None,
                    help="override MAX_CHAPTERS for this run")
    ap.add_argument("--mode", default=None, help="jump|speed")
    ap.add_argument("--quiz-submit", action="store_true", help="force submit on")
    ap.add_argument("--quiz", action="store_true",
                    help="answer quizzes (explicit; do not rely on the config default)")
    ap.add_argument("--no-quiz", action="store_true")
    ap.add_argument("--config", default=None, help="alternate config.json path")
    args = ap.parse_args()

    cfg_path = (args.config
                or os.environ.get("CX_CONFIG")
                or CFG_PATH)
    with open(cfg_path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    log(f"config: {os.path.relpath(cfg_path, ROOT) if cfg_path.startswith(ROOT) else cfg_path}")
    if args.chapters is not None:
        cfg["MAX_CHAPTERS"] = args.chapters
    if args.mode:
        cfg["MODE"] = args.mode
    if args.quiz_submit:
        cfg["QUIZ_SUBMIT"] = True
    if getattr(args, "quiz", False):
        cfg["DO_QUIZ"] = True
    if args.no_quiz:
        cfg["DO_QUIZ"] = False

    sess = Session(cfg)
    try:
        asyncio.run(sess.run(probe_only=args.probe))
    except KeyboardInterrupt:
        log("interrupted")
    except SystemExit as exc:
        log(str(exc))
    except Exception as exc:
        log(f"fatal {type(exc).__name__}: {exc}")
        raise


if __name__ == "__main__":
    main()
