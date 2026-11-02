# -*- coding: utf-8 -*-
"""Vision-assisted quiz repair, generalized.

Usage: python quiz_repair4.py <courseId> <clazzid> <chapterId> [chapterId ...]

Opens each chapter, switches to the 课后练习 tab (changeDisplayContent(3,...),
falling back to clicking a 章节测验/练习 tab), screenshots each question (the work
page obfuscates text with a custom font), asks the vision model, clicks options,
then submits via the page's own buttons (a.btnSubmit -> workPop 提交 -> captcha).
"""
import asyncio
import json
import os
import re
import sys

sys.path.insert(0, r"d:\ke\cx-engine")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from playwright.async_api import async_playwright

import oc_eyes_go

CID = sys.argv[1]
CLZ = sys.argv[2]
CHAPTERS = sys.argv[3:]
CPI = "577718561"
TMP = os.path.dirname(os.path.abspath(__file__))
MODEL = "qwen3.8-flash"

PROMPT = (
    "这是学习通课程测验的一道题（可能是单选、多选或判断题）。"
    "请像助教一样逐项分析每个选项，并在最后一行只写最终答案，格式为："
    "答案：X（单选题只写一个字母；多选题按字母顺序连写多个字母；判断题写 对 或 错）。"
)


async def open_chapter(p, chapter):
    await p.goto(f"https://mooc1.chaoxing.com/mooc-ans/visit/stucoursemiddle?"
                 f"courseid={CID}&clazzid={CLZ}&vc=1&cpi={CPI}&ismooc2=1&v=2",
                 wait_until="domcontentloaded", timeout=60000)
    await p.wait_for_timeout(6000)
    href = await p.evaluate("""() => {
        const a = [...document.querySelectorAll('a')]
            .find(a => /mycourse\\/stu/.test(a.href || ''));
        return a ? a.href : null;
    }""")
    await p.goto(href, wait_until="domcontentloaded", timeout=60000)
    await p.wait_for_timeout(8000)
    await p.evaluate("""() => {
        const el = [...document.querySelectorAll('a,li,div,span')]
            .find(e => (e.innerText || '').trim() === '章节' && e.offsetParent);
        if (el) el.click();
    }""")
    await p.wait_for_timeout(9000)
    f = next((fr for fr in p.frames if "studentcourse" in (fr.url or "")), None)
    if not f:
        return None
    await f.evaluate("(id) => { const el = document.getElementById('cur' + id); if (el) el.click(); }", chapter)
    await p.wait_for_timeout(15000)
    # 课后练习 tab loads doHomeWorkNew directly; call its own function
    try:
        await p.evaluate("""(args) => {
            try {
                changeDisplayContent(3, 4, args[0], args[1], args[2], '');
                return 'called';
            } catch (e) { return String(e); }
        }""", [chapter, CID, CLZ])
    except Exception:
        pass
    await p.wait_for_timeout(9000)
    fc = None
    for fr in p.frames:
        u = fr.url or ""
        if "doHomeWorkNew" in u or "selectWorkQuestionYiPiYue" in u:
            fc = fr
            break
    if fc is None:
        if "selectWorkQuestionYiPiYue" in (fc.url if fc else ""):
            pass
    if fc is None:
        tab = await p.evaluate("""() => {
            const els = [...document.querySelectorAll('li,div,span,a')]
                .filter(e => e.offsetParent && /课后练习|章节测验|测验|练习|测试/.test((e.innerText || '').trim())
                    && (e.innerText || '').trim().length < 14);
            if (!els.length) return null;
            const e = els[els.length - 1];
            const r = e.getBoundingClientRect();
            return {x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2)};
        }""")
        if not tab:
            return None
        await p.mouse.click(tab["x"], tab["y"])
        await p.wait_for_timeout(9000)
        for fr in p.frames:
            u = fr.url or ""
            if "doHomeWorkNew" in u or "selectWorkQuestionYiPiYue" in u:
                fc = fr
                break
    if fc is None:
        return None
    if "selectWorkQuestionYiPiYue" in (fc.url or ""):
        clicked = await fc.evaluate("""() => {
            const cands = [...document.querySelectorAll('a,button,span,div')]
                .filter(e => e.offsetWidth && e.offsetHeight)
                .filter(e => /重做|再做|重新作答/.test((e.innerText || '').trim())
                    && (e.innerText || '').trim().length < 20);
            if (!cands.length) return false;
            cands[cands.length - 1].click();
            return true;
        }""")
        print("  retake click:", clicked)
        if not clicked:
            return fc
        await p.wait_for_timeout(2500)
        confirmed = await fc.evaluate("""() => {
            const ok = document.querySelector('#popok');
            if (ok && (ok.offsetWidth || ok.offsetHeight)) { ok.click(); return true; }
            return false;
        }""")
        print("  retake confirm:", confirmed)
        await p.wait_for_timeout(7000)
        for fr in p.frames:
            u = fr.url or ""
            if "doHomeWorkNew" in u:
                return fr
    return fc


async def ask_answer(path, key):
    m = None
    for attempt in range(3):
        prompt = PROMPT if attempt == 0 else PROMPT + " 请直接给出最终答案这一行。"
        res = oc_eyes_go.ask_anthropic(MODEL, path, prompt, key)
        text = oc_eyes_go.extract(res)
        m = re.findall(r"答案[:：]\s*([A-Fa-f对错√×、,，\s]+)", text)
        if m:
            break
    if not m:
        return None
    raw = m[-1].replace(" ", "").replace("、", "").replace(",", "").replace("，", "")
    if "对" in raw or "√" in raw:
        return "对"
    if "错" in raw or "×" in raw:
        return "错"
    letters = "".join(sorted(set(re.findall(r"[A-F]", raw.upper()))))
    return letters or None


FILL_PROMPT = (
    "这是学习通测验的一道填空题（题干中可能有多个空格，用下划线表示）。"
    "请像助教一样分析，最后一行只写答案：每个空一个值，不要带任何单位或标点，"
    "多个空用 && 连接。格式：答案：值1&&值2"
)


async def ask_fill(path, key):
    m = None
    for attempt in range(3):
        prompt = FILL_PROMPT if attempt == 0 else FILL_PROMPT + " 只输出答案这一行。"
        res = oc_eyes_go.ask_anthropic(MODEL, path, prompt, key)
        text = oc_eyes_go.extract(res)
        m = re.findall(r"答案[:：]\s*(.+)", text)
        if m:
            break
    if not m:
        return None
    raw = m[-1].strip()
    parts = [x.strip() for x in raw.split("&&") if x.strip()]
    return parts or None


async def fill_blank(frame, qi, text):
    return await frame.evaluate("""([qi, v]) => {
        const q = document.querySelectorAll('.TiMu')[qi];
        if (!q) return 'noq';
        const boxes = [...q.querySelectorAll('textarea[name^=answerEditor]')];
        if (!boxes.length) {
            const inp = q.querySelector('input[type=text]');
            if (inp) {
                inp.value = v;
                inp.dispatchEvent(new Event('input', {bubbles: true}));
                inp.dispatchEvent(new Event('change', {bubbles: true}));
                return 'input';
            }
            return 'none';
        }
        const el = boxes[0];
        try { el.value = v; } catch (e) {}
        try { el.dispatchEvent(new Event('input', {bubbles: true})); } catch (e) {}
        try { el.dispatchEvent(new Event('change', {bubbles: true})); } catch (e) {}
        let ed = el.parentElement;
        for (let i = 0; i < 4 && ed; i++) {
            const f = ed.querySelector && ed.querySelector('iframe');
            if (f && f.contentDocument && f.contentDocument.body) {
                f.contentDocument.body.innerHTML = v;
                try { f.contentDocument.body.dispatchEvent(
                    new Event('input', {bubbles: true})); } catch (e) {}
                return 'iframe';
            }
            ed = ed.parentElement;
        }
        return 'textarea';
    }""", [qi, text])


async def pick(frame, qi, letters):
    return await frame.evaluate("""([qi, letters]) => {
        const q = document.querySelectorAll('.TiMu')[qi];
        if (!q) return 'noq';
        const lis = q.querySelectorAll('.Zy_ulTop > li');
        let clicked = 0;
        if (letters === '对' || letters === '错') {
            const idx = letters === '对' ? 0 : 1;
            if (lis[idx]) { lis[idx].click(); return 'ok'; }
            return 'nojudge';
        }
        [...letters].forEach(ch => {
            const idx = ch.charCodeAt(0) - 65;
            if (lis[idx]) { lis[idx].click(); clicked++; }
        });
        return clicked ? 'ok' : 'noli';
    }""", [qi, letters])


async def submit(frame, page):
    clicked = await frame.evaluate("""() => {
        const a = [...document.querySelectorAll('a.btnSubmit, a[onclick*="btnBlueSubmit"]')]
            .find(e => e.offsetParent);
        if (!a) return false;
        a.click();
        return true;
    }""")
    if not clicked:
        return "no_button"
    await page.wait_for_timeout(3000)
    ok = await page.evaluate("""() => {
        const a = [...document.querySelectorAll('a.jb_btn')]
            .find(e => (e.innerText || '').trim() === '提交' && e.offsetWidth);
        if (!a) return false;
        a.click();
        return true;
    }""")
    if not ok:
        return "no_popup"
    await page.wait_for_timeout(3500)
    return "submitted"


async def captcha(frame, page, key):
    st = await frame.evaluate("""() => {
        const img = document.querySelector('#workVerifyImg');
        const inp = document.querySelector('#inputCode');
        const vis = el => el && !!(el.offsetWidth || el.offsetHeight);
        return {img: img ? img.src : null, imgVis: vis(img), inpVis: vis(inp)};
    }""")
    if not (st.get("imgVis") and st.get("inpVis")):
        return None
    src = st["img"]
    if src.startswith("/"):
        src = "https://mooc1.chaoxing.com" + src
    import requests
    cookies = await page.context.cookies()
    r = requests.get(src, cookies={c["name"]: c["value"] for c in cookies},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    path = os.path.join(TMP, "captcha_now.png")
    open(path, "wb").write(r.content)
    res = oc_eyes_go.ask_anthropic(MODEL, path,
                                   "这是验证码图片，请逐字读出图中字符（通常4个），只输出字符。", key)
    code = re.sub(r"[^A-Za-z0-9]", "", oc_eyes_go.extract(res))[:6]
    await frame.evaluate("""(code) => {
        const inp = document.querySelector('#inputCode');
        if (inp) {
            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            setter.call(inp, code);
            inp.dispatchEvent(new Event('input', {bubbles: true}));
        }
    }""", code)
    await frame.evaluate("""() => {
        const btns = [...document.querySelectorAll('a,button,div,span')]
            .filter(e => e.offsetWidth && /确定|确认/.test((e.innerText || '').trim())
                && (e.innerText || '').trim().length <= 4);
        if (btns.length) btns[btns.length - 1].click();
    }""")
    return code


async def score_of(frame):
    try:
        return await frame.evaluate("""() => {
            const t = document.body.innerText || '';
            const m = t.match(/本次成绩[:：]?\\s*([\\d.]+)/);
            return m ? Number(m[1]) : null;
        }""")
    except Exception:
        return None


async def main():
    key = oc_eyes_go.load_key()
    async with async_playwright() as pw:
        b = await pw.chromium.launch(
            headless=True,
            executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            args=["--autoplay-policy=no-user-gesture-required",
                  "--disable-blink-features=AutomationControlled",
                  "--mute-audio",
                  "--disable-features=IsolateOrigins,site-per-process"])
        ctx = await b.new_context(
            storage_state=r"D:\ke\cx-engine\evidence\storage_state.json",
            viewport={"width": 1440, "height": 900},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/122.0.0.0 Safari/537.36"),
            locale="zh-CN", timezone_id="Asia/Shanghai")
        p = await ctx.new_page()
        p.on("dialog", lambda d: asyncio.create_task(d.accept()))
        for ch in CHAPTERS:
            print(f"==== chapter {ch} ====")
            fc = await open_chapter(p, ch)
            if fc is None:
                print("  no quiz frame")
                continue
            title = await fc.evaluate("() => (document.body.innerText || '').replace(/\\s+/g, ' ').slice(0, 110)")
            print("  page:", title)
            if "已完成" in title and "本次成绩" in title:
                sc = await score_of(fc)
                print(f"  already done, score={sc}")
                continue
            nq = await fc.evaluate("() => document.querySelectorAll('.TiMu').length")
            print(f"  questions: {nq}")
            answered = 0
            for i in range(nq):
                q = fc.locator(".TiMu").nth(i)
                try:
                    await q.scroll_into_view_if_needed(timeout=8000)
                    png = os.path.join(TMP, f"q_{ch}_{i}.png")
                    try:
                        await q.screenshot(path=png, timeout=15000)
                    except Exception:
                        box = await q.bounding_box()
                        if not box or box["height"] < 10:
                            raise
                        await p.screenshot(path=png, clip={
                            "x": max(0, box["x"]),
                            "y": max(0, box["y"]),
                            "width": min(1200, box["width"]),
                            "height": min(1600, box["height"])})
                    kind = await fc.evaluate("""(qi) => {
                        const q = document.querySelectorAll('.TiMu')[qi];
                        if (!q) return 'none';
                        const lis = q.querySelectorAll('.Zy_ulTop > li');
                        const blanks = q.querySelectorAll(
                            'textarea[name^=answerEditor], input[type=text]');
                        if (lis.length >= 2) return 'choice';
                        if (blanks.length) return 'fill';
                        return 'none';
                    }""", i)
                    if kind == "fill":
                        vals = await ask_fill(png, key)
                        if not vals:
                            print(f"  q{i+1}: no fill answer from vision")
                            continue
                        got = await fill_blank(fc, i, vals[0])
                        print(f"  q{i+1}: fill {vals[0]!r} -> {got}")
                        if got not in ("none", "noq"):
                            answered += 1
                    else:
                        ans = await ask_answer(png, key)
                        if not ans:
                            print(f"  q{i+1}: no answer from vision")
                            continue
                        got = await pick(fc, i, ans)
                        print(f"  q{i+1}: {ans} -> {got}")
                        if got == "ok":
                            answered += 1
                    await p.wait_for_timeout(400)
                except Exception as exc:
                    print(f"  q{i+1}: error {type(exc).__name__}")
            print(f"  answered {answered}/{nq}")
            if answered == 0:
                print("  nothing answered - leaving the quiz untouched")
                continue
            res = await submit(fc, p)
            print("  submit:", res)
            if res == "submitted":
                await captcha(fc, p, key)
                await p.wait_for_timeout(5000)
                sc = await score_of(fc)
                print(f"  result score: {sc}")
        await b.close()


asyncio.run(main())
