# -*- coding: utf-8 -*-
"""Log in once, persist the browser session, reuse it forever.

Writes two artifacts:
  evidence/storage_state.json  - playwright storage state (cookies + localStorage),
                                 load it with new_context(storage_state=...)
  evidence/cookies.json        - flat cookie jar for raw requests tooling

The login page has several generations (plain form, QR tab, SMS tab), so the
script detects which one is present instead of assuming. Password can come from
the environment (CX_PWD) so it never has to live in config.json.

    .venv\\Scripts\\python.exe -X utf8 login.py            # login (or refresh)
    .venv\\Scripts\\python.exe -X utf8 login.py --check    # reuse check only
    .venv\\Scripts\\python.exe -X utf8 login.py --qr       # force QR flow
    .venv\\Scripts\\python.exe -X utf8 login.py --interactive
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bank import ensure_utf8_stdout          # noqa: E402
from cx_auto import INJECT, UA, pick_browser  # noqa: E402

ensure_utf8_stdout()

ROOT = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(ROOT, "config.json")
STATE = os.path.join(ROOT, "evidence", "storage_state.json")
COOKIES = os.path.join(ROOT, "evidence", "cookies.json")
PROBE_URL = "https://i.chaoxing.com/base"
LOGIN_URL = ("https://passport2.chaoxing.com/login"
             "?fid=&newversion=true&refer=https%3A%2F%2Fi.chaoxing.com")

# selectors across chaoxing login generations
PHONE_SEL = ["#phone", "input[name=phone]", "input#uname", "input[name=uname]",
             "input[placeholder*='手机号']", "input[placeholder*='账号']"]
PWD_SEL = ["#pwd", "input[name=pwd]", "input[type=password]",
           "input[placeholder*='密码']"]
BTN_SEL = ["#loginBtn", "a#loginBtn", "button:has-text('登录')",
           "a:has-text('登录')", "input[type=submit]"]
# 验证码登录 tab + its fields
SMS_TAB_SEL = ["text=验证码登录", "text=验证码登陆", "#smsLogin", "text=短信登录"]
SMS_CODE_SEL = ["#code", "input[name=code]", "input[placeholder*='验证码']",
                "input[placeholder*='短信']"]
SMS_SEND_SEL = ["text=获取验证码", "text=发送验证码", "text=获取短信验证码",
                "#getCode", ".getCode", "a:has-text('验证码')"]

# A logged-in page shows the personal space chrome. Bare "课程" is NOT enough -
# the login page contains that word too, which produced a false positive.
OK_SEL = ["text=我的课程", "text=个人空间", "text=我的空间", ".userInfo",
          "#userInfo", "text=退出登录", "img[src*='avatar']"]
# Auth cookies chaoxing sets on a real session.
AUTH_COOKIES = {"_uid", "uid", "uf", "fid", "_d", "vc3", "p_auth_token",
                "cx_p_token", "thirdRegist", "jrose"}
# Hosts that only render after authentication.
POST_LOGIN_HOSTS = ("i.chaoxing.com", "mooc1.chaoxing.com", "mooc2-ans.chaoxing.com",
                    "stat2-ans.chaoxing.com", "mooc1-1.chaoxing.com",
                    "noteyoudao.chaoxing.com", "home.chaoxing.com")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


async def first_present(page, selectors, timeout=6000):
    """Return the first locator that matches, or None."""
    for sel in selectors:
        loc = page.locator(sel)
        try:
            if await loc.count():
                return loc.first
        except Exception:
            continue
    return None


async def logged_in(page, ctx=None) -> bool:
    """True when the session is authenticated.

    Two independent signals, either is enough:
      - the URL sits on a host that only renders post-authentication, or
      - a known logged-in DOM marker is present.
    Being on passport2/the login path short-circuits to False. The old version
    required BOTH, which stalled on pages whose header renders late.
    """
    url = (page.url or "")
    host = urllib.parse.urlparse(url).netloc.lower()
    on_auth_host = (host.endswith("passport2.chaoxing.com")
                    or "/login" in url or "passport" in url)
    if urllib.parse.urlparse(url).netloc and not on_auth_host:
        for h in POST_LOGIN_HOSTS:
            if host.endswith(h):
                return True
    if on_auth_host:
        return False
    for sel in OK_SEL:
        try:
            if await page.locator(sel).count():
                return True
        except Exception:
            continue
    return False


async def save_state(ctx, page) -> dict:
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    await ctx.storage_state(path=STATE)
    cookies = await ctx.cookies()
    with open(COOKIES, "w", encoding="utf-8") as fh:
        json.dump(cookies, fh, ensure_ascii=False, indent=1)
    names = {c["name"] for c in cookies}
    important = sorted(n for n in names if n.lower() in
                       ("_uid", "uid", "uf", "_d", "fid", "vc3", "cx_p_token",
                        "p_auth_token", "thirdRegist", "jrose"))
    info = {
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "url": page.url,
        "cookies": len(cookies),
        "session_cookies": {n: True for n in important},
        "storage_state": STATE,
        "cookies_json": COOKIES,
    }
    with open(os.path.join(ROOT, "evidence", "session_info.json"), "w",
              encoding="utf-8") as fh:
        json.dump(info, fh, ensure_ascii=False, indent=1)
    return info


async def shutdown(pw, ctx=None, br=None) -> None:
    """Close whatever exists; the persistent path has no separate browser handle."""
    for obj in (ctx, br):
        if obj is not None:
            try:
                await obj.close()
            except Exception:
                pass
    if pw is not None:
        try:
            await pw.stop()
        except Exception:
            pass


async def dump_now(ctx, page) -> bool:
    """Persist the session the instant it becomes valid, whatever happens next."""
    try:
        info = await save_state(ctx, page)
    except Exception as exc:
        log(f"immediate dump failed: {type(exc).__name__}: {exc}")
        return False
    log(f"IMMEDIATE DUMP: {info['cookies']} cookies, auth="
        f"{', '.join(info['session_cookies']) or 'none'}")
    log(f"  -> {os.path.relpath(STATE, ROOT)}")
    return os.path.exists(STATE)


async def qr_expired(page) -> bool:
    """True only when the expiry overlay is VISIBLE.

    The marker stays in the DOM while hidden, so a naive count() reports
    'expired' forever and makes the refresher spin.
    """
    for sel in ("text=二维码已失效", "text=已失效", "text=已过期"):
        try:
            loc = page.locator(sel)
            for i in range(await loc.count()):
                if await loc.nth(i).is_visible():
                    return True
        except Exception:
            continue
    return False


async def refresh_qr(page, tag: str = "", shot: bool = True) -> None:
    """Click the QR refresh button (only when actually needed)."""
    for sel in ("text=重新获取", "text=点击刷新", "text=刷新二维码",
                ".qrcode-refresh", "#qrRefresh", ".refresh"):
        loc = page.locator(sel)
        try:
            for i in range(await loc.count()):
                el = loc.nth(i)
                if await el.is_visible():
                    await el.click(timeout=4000)
                    await page.wait_for_timeout(2200)
                    if shot:
                        p = os.path.join(ROOT, "logs", f"qr_{tag or 'fresh'}.png")
                        await page.screenshot(path=p)
                        log(f"QR refreshed -> {os.path.relpath(p, ROOT)}")
                    return
        except Exception:
            continue
    if shot:
        try:
            p = os.path.join(ROOT, "logs", f"qr_{tag or 'now'}.png")
            await page.screenshot(path=p)
            log(f"QR screenshot -> {os.path.relpath(p, ROOT)}")
        except Exception:
            pass


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--qr", action="store_true", help="force QR-code login")
    ap.add_argument("--sms", action="store_true",
                    help="phone + SMS code login (asks for the code on stdin)")
    ap.add_argument("--phone", default="",
                    help="phone number for --sms / --password login")
    ap.add_argument("--interactive", action="store_true",
                    help="wait for you to finish login by hand, then save")
    ap.add_argument("--persistent", action="store_true",
                    help="use a persistent chrome profile (survives process exit)")
    ap.add_argument("--profile", default="",
                    help="profile dir for --persistent (default evidence/chrome-profile)")
    ap.add_argument("--check", action="store_true",
                    help="only verify the saved session, never log in again")
    ap.add_argument("--timeout", type=int, default=240,
                    help="seconds to wait for login to complete")
    ap.add_argument("--keep-open", action="store_true",
                    help="keep the browser window alive after saving the session")
    args = ap.parse_args()

    with open(CFG_PATH, encoding="utf-8") as fh:
        cfg = json.load(fh)
    acc = cfg.get("ACCOUNT") or ""
    pwd = ((os.environ.get("CX_PWD") if cfg.get("USE_ENV_PASSWORD") else None)
           or cfg.get("PASSWORD") or "")
    if "PLACEHOLDER" in pwd:
        pwd = ""

    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    channel, exe = pick_browser(cfg)
    kwargs = dict(headless=False, args=[
        "--disable-blink-features=AutomationControlled",
        "--mute-audio"])
    if exe:
        kwargs["executable_path"] = exe

    br = await pw.chromium.launch(**kwargs)
    ctx_kwargs = dict(viewport={"width": 1440, "height": 900},
                      user_agent=UA, locale="zh-CN", timezone_id="Asia/Shanghai")
    if os.path.exists(STATE):
        ctx_kwargs["storage_state"] = STATE
        log(f"loading saved session from {os.path.relpath(STATE, ROOT)}")

    if args.persistent:
        # a persistent profile keeps cookies in a real chrome profile dir, so the
        # session also survives this process dying - the failure mode that cost
        # us the 14:54 login
        prof = args.profile or os.path.join(ROOT, "evidence", "chrome-profile")
        os.makedirs(prof, exist_ok=True)
        pers_kwargs = dict(ctx_kwargs)
        pers_kwargs.update({
            "headless": bool(kwargs.get("headless")),
            "args": kwargs.get("args"),
        })
        if kwargs.get("executable_path"):
            pers_kwargs["executable_path"] = kwargs["executable_path"]
        ctx = await pw.chromium.launch_persistent_context(prof, **pers_kwargs)
        if br is not None:
            try:
                await br.close()          # the persistent context owns its browser
            except Exception:
                pass
            br = None
        log(f"persistent profile: {os.path.relpath(prof, ROOT)}")
        pages = ctx.pages
        page = pages[0] if pages else await ctx.new_page()
    else:
        ctx = await br.new_context(**ctx_kwargs)
        page = await ctx.new_page()
    await ctx.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")

    # ---- 1. is the saved session still good? -------------------------------
    log("checking saved session ...")
    try:
        await page.goto(PROBE_URL, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(3500)
        if await logged_in(page, ctx):
            info = await save_state(ctx, page)
            log(f"session VALID - reused. cookies={info['cookies']} url={page.url}")
            log(f"saved: {os.path.relpath(STATE, ROOT)} + "
                f"{os.path.relpath(COOKIES, ROOT)}")
            if args.keep_open:
                log("--keep-open: holding the window")
                while True:
                    await asyncio.sleep(30)
            await shutdown(pw, ctx, br)
            return 0
        log(f"saved session not valid (ended on {page.url[:70]}), need to log in")
    except Exception as exc:
        log(f"session probe failed ({type(exc).__name__}), will log in")

    if args.check:
        log("--check given and the saved session is dead; nothing more to do")
        await shutdown(pw, ctx, br)
        return 1

    # ---- 2. log in --------------------------------------------------------
    if "PHONE_OR_USERNAME" in acc or not acc:
        log("config.json ACCOUNT is still a placeholder -> interactive login "
            "in the open window")
        args.interactive = True

    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(2500)

    if args.qr:
        for sel in ("text=扫码登录", "text=二维码登录", "#qrLogin", ".qr-code"):
            loc = page.locator(sel)
            if await loc.count():
                try:
                    await loc.first.click(timeout=4000)
                    await page.wait_for_timeout(2000)
                    break
                except Exception:
                    pass
        log("QR mode: scan the code on the right of the login page "
            "(the form on the left works too - either path is saved)")
        await refresh_qr(page, tag="initial", shot=True)
        # page-side refresher: swap the code the instant it expires, so the
        # window always shows a scannable code no matter when you get to it
        try:
            await page.evaluate("""() => {
                if (window.__qrKeeper) return;
                window.__qrKeeper = true;
                const hit = () => {
                    let clicked = false;
                    document.querySelectorAll('a,button,span,div').forEach(el => {
                        const t = (el.innerText || '').trim();
                        if ((t === '重新获取' || t === '点击刷新') && el.offsetParent !== null) {
                            try { el.click(); clicked = true; } catch (e) {}
                        }
                    });
                    return clicked;
                };
                setInterval(() => {
                    let expired = false;
                    document.querySelectorAll('*').forEach(el => {
                        if (expired) return;
                        const t = (el.innerText || '').trim();
                        if ((t === '二维码已失效' || t === '二维码已过期')
                            && el.offsetParent !== null) expired = true;
                    });
                    if (expired) hit();
                }, 4000);
            }""")
            log("page-side QR keeper installed (auto refresh every 4s)")
        except Exception as exc:
            log(f"page-side keeper failed: {type(exc).__name__}")

    if not args.interactive and not args.qr:
        phone = await first_present(page, PHONE_SEL)
        pwdbox = await first_present(page, PWD_SEL)
        if not phone or not pwdbox:
            log("could not find the username/password fields -> switching to "
                "interactive mode")
            args.interactive = True
        else:
            acc = args.phone or acc
            if not pwd:
                log("no password available: set $env:CX_PWD or config.PASSWORD")
                await shutdown(pw, ctx, br)
                return 2
            await phone.click()
            await phone.fill(acc)
            await pwdbox.click()
            await pwdbox.fill(pwd)
            btn = await first_present(page, BTN_SEL)
            if btn:
                await btn.click()
            else:
                await pwdbox.press("Enter")
            log(f"submitted credentials for {acc}")

    # ---- 2b. SMS-code login ----------------------------------------------
    if args.sms:
        acc = args.phone or acc
        if "PHONE_OR_USERNAME" in acc or not acc:
            log("--sms needs a phone number: pass --phone 1xxxxxxxxxx")
            await shutdown(pw, ctx, br)
            return 2
        tab = await first_present(page, SMS_TAB_SEL)
        if tab:
            try:
                await tab.click(timeout=4000)
                await page.wait_for_timeout(1800)
                log("switched to the SMS-code tab")
            except Exception:
                pass
        box = await first_present(page, PHONE_SEL)
        if box:
            await box.click()
            await box.fill(acc)
        send = await first_present(page, SMS_SEND_SEL)
        if send:
            await send.click()
            log(f"SMS requested for {acc} - check your phone")
        else:
            log("send-code button not found; click 获取验证码 in the window")
        try:
            code = input("enter the SMS code (4-8 digits) then press Enter: ").strip()
        except EOFError:
            code = ""
        if code:
            field = await first_present(page, SMS_CODE_SEL)
            if field:
                await field.click()
                await field.fill(code)
                btn = await first_present(page, BTN_SEL)
                if btn:
                    await btn.click()
                else:
                    await field.press("Enter")
                log("SMS code submitted")
        else:
            log("no code entered; waiting for you to finish in the window")

    # ---- 3. wait for the login to land ------------------------------------
    deadline = time.time() + args.timeout
    ok = False
    last_report = 0.0
    warned_captcha = False
    qr_check = 0.0
    while time.time() < deadline:
        await page.wait_for_timeout(2000)
        if await logged_in(page, ctx):
            ok = True
            # persist immediately - do not wait for the tail of this function
            await dump_now(ctx, page)
            break
        now = time.time()
        # keep the QR code alive: chaoxing expires it and only swaps on click
        if args.qr and now - qr_check > 5:
            qr_check = now
            try:
                if await qr_expired(page):
                    log("QR expired (visible) -> clicking refresh")
                    await refresh_qr(page, tag=f"{int(now) % 10000}")
            except Exception:
                pass
        # report state every ~8s so a stalled login is diagnosable live
        if now - last_report > 8:
            last_report = now
            try:
                names = sorted({c["name"] for c in await ctx.cookies()})
                auth = sorted(set(names) & AUTH_COOKIES)
            except Exception:
                names, auth = [], []
            left = int(deadline - now)
            log(f"waiting: url={page.url[:64]} | cookies={len(names)} "
                f"auth={auth or 'none'} | {left}s left")
        if not warned_captcha and await page.locator("text=验证码").count():
            warned_captcha = True
            log("captcha/verification shown - complete it in the browser window")

    if not ok:
        try:
            await page.screenshot(path=os.path.join(ROOT, "logs", "login_failed.png"))
        except Exception:
            pass
        log("login did not complete within the timeout "
            "(screenshot saved to logs/login_failed.png)")
        log("retry with: python login.py --interactive")
        await shutdown(pw, ctx, br)
        return 3

    log("login OK - settling cookies")
    await page.wait_for_timeout(5000)
    try:
        await page.goto(PROBE_URL, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(2500)
    except Exception:
        pass

    info = await save_state(ctx, page)
    log(f"session saved: {info['cookies']} cookies, key cookies: "
        f"{', '.join(info['session_cookies']) or 'none detected'}")
    log(f"  {os.path.relpath(STATE, ROOT)}")
    log(f"  {os.path.relpath(COOKIES, ROOT)}")
    # verify the artifact landed before tearing the browser down - losing a live
    # session because the dump was skipped is the one mistake worth guarding
    if not os.path.exists(STATE):
        log("ERROR: storage_state.json was not written; keeping the window open")
        await asyncio.sleep(600)
    else:
        log("next runs reuse this session automatically; "
            "re-run with --check to verify it later")
    if args.keep_open:
        log("--keep-open: leaving the browser up; Ctrl+C or close the window to end")
        while True:
            await asyncio.sleep(30)
    await shutdown(pw, ctx, br)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
