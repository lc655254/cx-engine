# -*- coding: utf-8 -*-
"""Non-media task points: pdf viewer and the page-flip reader.

Verified live (2026-10-13):
  * pdf frame /ananas/modules/pdf/index.html exposes:
        isJob, jobToken, jobid, finishFlag, bindReport,
        finishJob(), checkJobFinish(), unMaskAndFinishJob(), hasJobToFinish()
  * reader frame pan-yz.chaoxing.com/screen/v2/... exposes:
        reportFile(), _readlogaddpoint()   and renders one <li> per page (152 here)
  * audio/video frames are handled separately via __jumpToEnd()

Completion for pdf = call the module's own finish routine. For the reader =
walk the page list so the read log gains points, then let it report.
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from typing import Any, Dict, List

PDF_PROBE = r"""() => {
    const safe = (fn, dflt) => { try { return fn(); } catch (e) { return dflt; } };
    const has = (name) => { try { return typeof eval(name) !== 'undefined'; } catch (e) { return false; } };
    return {
        ready: document.readyState,
        isJob: safe(() => (typeof isJob !== 'undefined' ? isJob : null), null),
        jobid: safe(() => (typeof jobid !== 'undefined' ? jobid : null), null),
        jobToken: safe(() => (typeof jobToken !== 'undefined' ? jobToken : null), null),
        finishFlag: safe(() => (typeof finishFlag !== 'undefined' ? finishFlag : null), null),
        hasJobToFinish: safe(() => (typeof hasJobToFinish === 'function' ? hasJobToFinish() : null), null),
        checkJobFinish: safe(() => (typeof checkJobFinish === 'function' ? checkJobFinish() : null), null),
        fns: {
            finishJob: has('finishJob'),
            unMaskAndFinishJob: has('unMaskAndFinishJob'),
            bindReport: has('bindReport'),
            jobflag: has('jobflag'),
        },
        images: document.querySelectorAll('img').length,
        text: ((document.body && document.body.innerText) || '')
              .replace(/\s+/g, ' ').slice(0, 140),
    };
}"""

PDF_FINISH = r"""() => {
    const out = { called: [], errors: [] };
    const call = (name, fn) => { try { fn(); out.called.push(name); }
                                 catch (e) { out.errors.push(name + ':' + String(e).slice(0, 60)); } };
    try { out.before = (typeof checkJobFinish === 'function') ? checkJobFinish() : null; } catch (e) {}
    if (typeof finishJob === 'function') call('finishJob', () => finishJob());
    if (typeof unMaskAndFinishJob === 'function') call('unMaskAndFinishJob', () => unMaskAndFinishJob(true));
    if (typeof bindReport === 'function') call('bindReport', () => bindReport(1));
    if (typeof jobflag === 'function') call('jobflag', () => jobflag(true));
    try { out.after = (typeof checkJobFinish === 'function') ? checkJobFinish() : null; } catch (e) {}
    return out;
}"""

READER_STATE = r"""() => ({
    pages: document.querySelectorAll('li').length,
    total: (() => { const t = (document.body && document.body.innerText) || '';
                    const m = t.match(/共\s*(\d+)\s*页/); return m ? +m[1] : null; })(),
    hasAddPoint: typeof _readlogaddpoint === 'function',
    hasReportFile: typeof reportFile === 'function',
    text: ((document.body && document.body.innerText) || '')
          .replace(/\s+/g, ' ').slice(0, 140),
})"""


READY_JS = r"""() => {
    const state = document.readyState;
    const hasJobs = (typeof hasJobToFinish === 'function')
                 || (typeof finishJob === 'function')
                 || (typeof checkJobFinish === 'function')
                 || (typeof jobid !== 'undefined');
    return { ready: state, hasFns: !!hasJobs,
             images: document.querySelectorAll('img').length };
}"""


async def wait_pdf_ready(frame, page_ref, timeout_s: int = 25) -> bool:
    """The pdf module needs its script to finish booting before it is probed.

    Chapters 11/12 of 大学英语B1 returned 'probe failed: Error' purely because the
    frame had not finished initialising - the same chapter works once it has.
    """
    deadline = timeout_s * 1000
    waited = 0
    while waited < deadline:
        try:
            st = await frame.evaluate(READY_JS)
            if st.get("hasFns"):
                return True
        except Exception:
            pass
        await page_ref.wait_for_timeout(1000)
        waited += 1000
    return False


async def finish_pdf(frame, page_ref=None, attempts: int = 3) -> Dict[str, Any]:
    """Drive the pdf module's own completion path, waiting for it to boot first."""
    if page_ref is not None:
        await wait_pdf_ready(frame, page_ref)

    last_err = ""
    for attempt in range(1, attempts + 1):
        try:
            before = await frame.evaluate(PDF_PROBE)
        except Exception as exc:
            last_err = f"probe failed: {type(exc).__name__}"
            if page_ref is not None:
                await page_ref.wait_for_timeout(3000)
            continue
        try:
            res = await frame.evaluate(PDF_FINISH)
        except Exception as exc:
            last_err = f"finish failed: {type(exc).__name__}"
            continue
        res["before"] = before
        res["ok"] = True
        res["attempt"] = attempt
        # confirm the module actually flipped to finished
        try:
            after = await frame.evaluate(PDF_PROBE)
            res["after_finish"] = after.get("checkJobFinish")
        except Exception:
            pass
        return res
    return {"ok": False, "why": last_err or "unknown"}


async def walk_reader(frame, page_ref, pages: int = 0, dwell_ms: int = 260,
                      max_pages: int = 60) -> Dict[str, Any]:
    """Turn pages in the reader so the read log accumulates points.

    Clicking every <li> in a 152-page document is heavy, so the walk is bounded:
    visit up to max_pages entries spread across the whole list, dwelling briefly,
    which is what the reader's own _readlogaddpoint() counts.
    """
    try:
        state = await frame.evaluate(READER_STATE)
    except Exception as exc:
        return {"ok": False, "why": f"probe failed: {type(exc).__name__}"}

    total = pages or state.get("pages") or 0
    if not total:
        return {"ok": False, "why": "no page list", "state": state}

    step = max(1, total // max(1, max_pages))
    visited = 0
    for i in range(0, total, step):
        try:
            ok = await frame.evaluate("""(idx) => {
                const items = document.querySelectorAll('li');
                const el = items[idx];
                if (!el) return false;
                el.click();
                return true;
            }""", i)
            if ok:
                visited += 1
                await page_ref.wait_for_timeout(dwell_ms)
        except Exception:
            continue
    try:
        await frame.evaluate("() => { if (typeof _readlogaddpoint === 'function') _readlogaddpoint(); }")
    except Exception:
        pass
    return {"ok": True, "visited": visited, "total": total, "state": state}


# --------------------------------------------------------------- it video module
# The IT-course player differs from the audio modules:
#   * video.js element (class vjs-tech) with a ready "播放视频" poster layer,
#     so readyState is 0 until the poster is clicked
#   * the knowledge/cards frame states the completion rule in plain text:
#       "观看时长需 >= 总时长的 90% (未完成任务点前, 当前视频不可拖拽)"
#   * therefore seeking is blocked until the task point is already done, and the
#     honest path is: click through the poster, then play fast enough to cover
#     90% of the duration.
VIDEO_START = r"""() => {
    const out = { tried: [], ready: null, dur: null, played: false };
    const v = document.querySelector('video,audio');
    if (v) { out.ready = v.readyState; out.dur = isFinite(v.duration) ? v.duration : null; }

    const poke = (label, fn) => { try { const r = fn(); out.tried.push(label + (r ? ':ok' : ':no')); return !!r; } catch (e) { out.tried.push(label + ':err'); return false; } };

    // 1) the player's own big play button (several skins)
    const playSels = [
        '.vjs-big-play-button', '.vjs-play-control', 'button.vjs-big-play-button',
        '.pv-play-btn', '.play-btn', '.prism-big-play-btn', '.xgplayer-start',
        '.x-play-btn', '[class*=bigPlay]', '[class*=BigPlay]', '[class*=playBtn]',
        '[class*=play-btn]', '.ans-module-play',
    ];
    for (const sel of playSels) {
        const el = document.querySelector(sel);
        if (el && el.offsetParent !== null) {
            if (poke(sel, () => { el.click(); return true; })) break;
        }
    }

    // 2) videojs instance api
    try {
        if (typeof window.videojs === 'function') {
            const players = window.videojs.getPlayers ? window.videojs.getPlayers() : {};
            const keys = Object.keys(players || {});
            if (keys.length) {
                poke('videojs.play', () => { players[keys[0]].play(); out.played = true; return true; });
            }
        }
    } catch (e) {}

    // 3) any element whose text is exactly a play label
    if (!out.played) {
        const nodes = [...document.querySelectorAll('div,span,a,button')];
        for (const want of ['播放视频', '播放']) {
            for (const el of nodes) {
                if ((el.innerText || '').trim() !== want) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 20 || r.height < 10) continue;
                if (poke('text:' + want, () => { el.click(); return true; })) { out.played = true; break; }
            }
            if (out.played) break;
        }
    }

    // 4) last resort: click the geometric centre of the largest media area
    if (!out.played) {
        const host = (v && (v.closest('.video-js, [class*=player], [class*=Player]') || v.parentElement)) ||
                     document.querySelector('video,canvas');
        if (host) {
            const r = host.getBoundingClientRect();
            if (r.width > 100 && r.height > 60) {
                out.centre = { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) };
                out.tried.push('centre:' + out.centre.x + ',' + out.centre.y);
            }
        }
    }

    if (v) { try { v.muted = true; const pr = v.play(); if (pr && pr.catch) pr.catch(() => {}); } catch (e) {} }
    return out;
}"""

# Click the reported centre point with a real mouse event (JS .click() on a
# poster layer often does nothing because the handler listens for pointerdown).
CENTRE_CLICK_JS = r"""() => {
    const v = document.querySelector('video,audio');
    const host = (v && (v.closest('.video-js, [class*=player], [class*=Player]') || v.parentElement)) ||
                 document.querySelector('video,canvas');
    if (!host) return null;
    const r = host.getBoundingClientRect();
    return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2),
             w: Math.round(r.width), h: Math.round(r.height) };
}"""

VIDEO_STATE = r"""() => {
    const v = document.querySelector('video,audio');
    if (!v) return null;
    return {
        ready: v.readyState,
        dur: isFinite(v.duration) ? Math.round(v.duration) : null,
        cur: Math.round(v.currentTime || 0),
        rate: v.playbackRate,
        paused: v.paused,
        ended: v.ended,
        err: v.error ? v.error.code : null,
    };
}"""

VIDEO_FORCE = r"""(payload) => {
    try {
        const proto = HTMLMediaElement.prototype;
        const d = Object.getOwnPropertyDescriptor(proto, 'playbackRate');
        if (!window.__ratePatched) {
            Object.defineProperty(proto, 'playbackRate', {
                configurable: true,
                get() { return d.get.call(this); },
                set(v) { d.set.call(this, v); }
            });
            window.__ratePatched = true;
        }
    } catch (e) {}
    const v = document.querySelector('video,audio');
    if (!v) return { ok: false, why: 'no_video' };
    const dur = v.duration;
    if (!dur || !isFinite(dur)) return { ok: false, why: 'no_duration', ready: v.readyState };
    try { v.muted = true; } catch (e) {}
    if (payload.fraction) {
        try { v.currentTime = Math.max(0, dur * payload.fraction); }
        catch (e) { return { ok: false, why: 'seek_blocked', dur: dur }; }
        ['seeking','seeked','timeupdate','progress','ended'].forEach(t => {
            try { v.dispatchEvent(new Event(t, {bubbles: true})); } catch (e) {}
        });
    }
    if (payload.rate) { try { v.playbackRate = payload.rate; } catch (e) {} }
    try { const pr = v.play(); if (pr && pr.catch) pr.catch(() => {}); } catch (e) {}
    return { ok: true, dur: dur, cur: Math.round(v.currentTime), rate: v.playbackRate };
}"""


async def start_video(frame, page_ref, attempts: int = 3) -> Dict[str, Any]:
    """Wake the video module up with a REAL mouse click.

    Verified on 军事理论: .vjs-poster is pointer-driven, so el.click() and
    videojs.play() run happily and change NOTHING (readyState stays 0). Only a
    genuine mouse event over the player starts playback, and once started it
    advances steadily (measured +88s per 90s wall clock).

    The click must be issued through Playwright's locator/bounding_box so the
    iframe offset is applied; feeding iframe-local coordinates to page.mouse
    puts every click at (0,0).

    Returns {ok, dur, via, state}.
    """
    info = {"via": [], "ok": False, "dur": None}

    async def real_click(selector: str, label: str) -> bool:
        try:
            loc = frame.locator(selector).first
            if not await loc.count():
                return False
            box = await loc.bounding_box()
            if not box or box.get("width", 0) < 2 or box.get("height", 0) < 2:
                return False
            await page_ref.mouse.click(box["x"] + box["width"] / 2,
                                       box["y"] + box["height"] / 2)
            info["via"].append(f"{label}@({box['x'] + box['width'] / 2:.0f},"
                               f"{box['y'] + box['height'] / 2:.0f})")
            return True
        except Exception as exc:
            info["via"].append(f"{label}-fail:{type(exc).__name__}")
            return False

    for attempt in range(1, attempts + 1):
        # 1) the big play button is the fastest route
        await real_click("button.vjs-big-play-button", f"big-play#{attempt}")
        await page_ref.wait_for_timeout(3500)
        try:
            st = await frame.evaluate(VIDEO_STATE)
        except Exception:
            st = None
        if st and st.get("dur"):
            info.update({"ok": True, "dur": st["dur"], "state": st})
            return info

        # 2) the poster layer works too
        await real_click(".vjs-poster", f"poster#{attempt}")
        await page_ref.wait_for_timeout(3000)
        try:
            st = await frame.evaluate(VIDEO_STATE)
        except Exception:
            st = None
        if st and st.get("dur"):
            info.update({"ok": True, "dur": st["dur"], "state": st})
            return info

        # 3) the control-bar play button (only visible once the skin loads)
        await real_click("button.vjs-play-control", f"ctrl-play#{attempt}")
        await page_ref.wait_for_timeout(3000)
        try:
            st = await frame.evaluate(VIDEO_STATE)
        except Exception:
            st = None
        if st and st.get("dur"):
            info.update({"ok": True, "dur": st["dur"], "state": st})
            return info

        # 4) last resort: a JS click, then the geometric centre of the video
        try:
            await frame.evaluate(VIDEO_START)
        except Exception:
            pass
        try:
            box = await frame.locator("video,audio").first.bounding_box()
            if box and box.get("width", 0) > 50:
                await page_ref.mouse.click(box["x"] + box["width"] / 2,
                                           box["y"] + box["height"] / 2)
                info["via"].append(f"centre#{attempt}")
        except Exception:
            pass
        await page_ref.wait_for_timeout(3000)
        try:
            st = await frame.evaluate(VIDEO_STATE)
        except Exception:
            st = None
        if st and st.get("dur"):
            info.update({"ok": True, "dur": st["dur"], "state": st})
            return info

    try:
        info["state"] = await frame.evaluate(VIDEO_STATE)
    except Exception:
        info["state"] = None
    return info


async def try_seek(frame, fraction: float) -> Dict[str, Any]:
    """Can we move the playhead yet? Seeking stays blocked until the task point
    is already recorded, so a failure here is authoritative, not a bug."""
    return await frame.evaluate(VIDEO_FORCE, {"fraction": fraction})


async def _poke_play(frame, page_ref, log=None) -> bool:
    """Click the control-bar play button with a real mouse event.

    vjs-big-play-button only exists before playback; vjs-play-control persists. Both
    the paused branch and the stall re-assert need this, and both were using the wrong
    one, so the recovery never actually fired.
    """
    for sel in ("button.vjs-play-control", ".vjs-play-control",
                ".vjs-control-bar button"):
        try:
            loc = frame.locator(sel).first
            if not await loc.count():
                continue
            box = await loc.bounding_box()
            if not box or box.get("width", 0) < 2 or box.get("height", 0) < 2:
                continue
            await page_ref.mouse.click(box["x"] + box["width"] / 2,
                                       box["y"] + box["height"] / 2)
            return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------
# in-video popup questions (弹题)
#
# The video module overlays an ExtJS question panel (`.ans-videoquiz`) at fixed
# cue points and holds the clip paused until the question is submitted and the
# 继续 button is clicked. Verified 2026-10-18 on 军事理论: every clip that
# stalled at 0s or died with no_state had this panel on screen, and re-clicking
# play never moved the clip - only answering the question does.
# ---------------------------------------------------------------------------

POPUP_STATE = r"""() => {
    const vis = (el) => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        if (!(r.width > 2 && r.height > 2)) return false;
        const cs = getComputedStyle(el);
        return cs.display !== 'none' && cs.visibility !== 'hidden';
    };
    for (const box of document.querySelectorAll('.ans-videoquiz')) {
        if (!vis(box)) continue;
        const kindEl = box.querySelector('.tkTopic_title');
        const items = Array.from(box.querySelectorAll('.tkItem')).map((it) => ({
            stem: ((it.querySelector('.tkItem_title') || {}).innerText || '')
                .replace(/\s+/g, ' ').trim(),
            options: Array.from(it.querySelectorAll('.ans-videoquiz-opt label'))
                .map((l) => (l.innerText || '').replace(/\s+/g, ' ').trim()),
            checked: Array.from(it.querySelectorAll('.ans-videoquiz-opt'))
                .map((li) => { const i = li.querySelector('input');
                               return !!(i && i.checked); }),
        }));
        const v = document.querySelector('video, audio');
        const wrong = box.querySelector('.spanNot');
        const back = box.querySelector('.spanNotBack');
        return {
            kind: kindEl ? kindEl.innerText.trim() : '',
            items: items,
            paused: v ? !!v.paused : null,
            submitVisible: vis(box.querySelector('#videoquiz-submit')),
            continueVisible: vis(box.querySelector('.ans-videoquiz-continue')),
            correctVisible: vis(box.querySelector('.spanHas')),
            wrongVisible: vis(wrong) || vis(back),
            wrongText: ((vis(back) && back.innerText) || (vis(wrong) && wrong.innerText) || ''),
        };
    }
    return null;
}"""

# stem -> option indexes of our last attempt, so a rejected guess can be
# retried with the next option when the question is asked again (回看)
_POPUP_TRIED: Dict[str, str] = {}


def _popup_qtype(kind: str) -> str:
    k = (kind or "").strip()
    if "判断" in k:
        return "判断题"
    if "多选" in k:
        return "多选题"
    if "单选" in k or "选择" in k:
        return "单选题"
    return k or "单选题"


def _popup_pick(bank, qtype: str, stem: str, options: List[str]) -> Dict[str, Any]:
    """Decide which option indexes to click for one popup question."""
    key = (stem or "").strip()
    tried = _POPUP_TRIED.get(key, "")
    # a stored rejection outranks the bank: the platform itself refused it, so
    # the bank entry for this stem is wrong no matter where it came from
    if not tried and bank is not None and key:
        try:
            ans = bank.query(stem, qtype, options)
        except Exception:
            ans = None
        if ans is not None and getattr(ans, "hit", False) and getattr(ans, "indexes", None):
            return {"indexes": list(ans.indexes), "text": ans.text or "",
                    "source": ans.source, "tried": tried}
    # no usable answer: guess the first option, and when a previous guess for
    # this exact stem was already rejected, move on to the next one
    n = len(options) if options else 2
    idx = 0
    if tried:
        try:
            idx = (int(tried.split(",")[0]) + 1) % n
        except Exception:
            idx = 0
    text = options[idx] if 0 <= idx < len(options) else chr(65 + idx)
    return {"indexes": [idx], "text": text, "source": "guess", "tried": tried}


async def _find_popup(page_ref, frame=None):
    """Locate the media frame that currently shows a popup question panel."""
    frames = []
    if frame is not None:
        frames.append(frame)
    try:
        frames.extend(page_ref.frames)
    except Exception:
        pass
    seen = set()
    for fr in frames:
        if id(fr) in seen:
            continue
        seen.add(id(fr))
        try:
            url = fr.url or ""
        except Exception:
            continue
        if not any(h in url for h in ("/modules/video/", "/modules/audio/",
                                      "/ananas/modules/")):
            continue
        try:
            st = await fr.evaluate(POPUP_STATE)
        except Exception:
            continue
        if st:
            return fr, st
    return None, None


async def _popup_click(frame, selector: str, nth: int = 0, log=None) -> bool:
    try:
        loc = frame.locator(selector)
        if await loc.count() <= nth:
            return False
        await loc.nth(nth).click(timeout=4000)
        return True
    except Exception as exc:
        if log:
            log(f"  popup click {selector!r}: {type(exc).__name__} {str(exc)[:70]}")
        return False


def _popup_record_tried(items) -> None:
    """Remember what was checked when the platform rejected the answer (回看)."""
    for it in items or []:
        stem = (it.get("stem") or "").strip()
        chk = it.get("checked") or []
        if stem and any(chk):
            _POPUP_TRIED[stem] = str(chk.index(True))


async def _popup_reanswer(fr, page_ref, items, qtype, bank, frame, log=None) -> bool:
    """After a rejection, re-pick the flipped option and submit it again.

    The panel often keeps the question and a live 提交 button up while it shows
    the 回看 notice; standing still until the stall detector kills the clip is
    what held node 1254202863 at 0:00 for 100s on 2026-10-18. On failure the
    tried record is left untouched, so the same flip is retried instead of
    ping-ponging between the two options.
    """
    picks = []
    offset = 0
    for it in items:
        n_opts = len(it.get("options") or [])
        stem = (it.get("stem") or "").strip()
        if stem and _POPUP_TRIED.get(stem) is not None:
            pick = _popup_pick(bank, qtype, stem, it.get("options") or [])
            picks.append((offset, pick, it))
        offset += n_opts
    if not picks:
        return False
    clicked = False
    for offset, pick, it in picks:
        for idx in pick["indexes"]:
            if await _popup_click(fr, ".ans-videoquiz:visible .ans-videoquiz-opt",
                                  offset + idx, log=log):
                clicked = True
        if log:
            log(f"  popup: 改答 {pick.get('text', '')[:10]!r} 并重新提交")
    if not clicked:
        return False
    await page_ref.wait_for_timeout(1200)
    if not await _popup_click(fr, "#videoquiz-submit:visible", log=log):
        return False
    await page_ref.wait_for_timeout(1500)
    fr2, st2 = await _find_popup(page_ref, frame)
    if st2 and st2.get("correctVisible"):
        for _, pick, it in picks:
            stem = (it.get("stem") or "").strip()
            if stem:
                _POPUP_TRIED.pop(stem, None)
                if bank is not None:
                    try:
                        bank.remember(stem, qtype, pick.get("text", ""), "videoquiz")
                    except Exception:
                        pass
        return True
    return False


async def handle_video_popup(page_ref, bank=None, frame=None,
                             log=None) -> Dict[str, Any] | None:
    """Answer an in-video popup question so playback can continue.

    Returns None when no popup is showing, otherwise an action dict. Correct
    answers are written back to the bank, so the same question never needs to
    be guessed twice.
    """
    fr, st = await _find_popup(page_ref, frame)
    if not st:
        return None
    qtype = _popup_qtype(st.get("kind", ""))
    items = st.get("items") or []
    if st.get("correctVisible"):
        # accepted: write the picked answer back so later asks resolve instantly
        if bank is not None:
            for it in items:
                stem = (it.get("stem") or "").strip()
                chk = it.get("checked") or []
                opts = it.get("options") or []
                if stem and any(chk):
                    idx = chk.index(True)
                    text = opts[idx] if 0 <= idx < len(opts) else chr(65 + idx)
                    try:
                        bank.remember(stem, qtype, text, "videoquiz")
                    except Exception:
                        pass
        for it in items:
            stem = (it.get("stem") or "").strip()
            if stem:
                _POPUP_TRIED.pop(stem, None)
        if st.get("continueVisible") and st.get("paused") is not False:
            if await _popup_click(fr, ".ans-videoquiz-continue:visible", log=log):
                if log:
                    log("  popup: 回答正确 -> 继续")
                return {"action": "continued"}
        return {"action": "correct"}
    if st.get("wrongVisible"):
        _popup_record_tried(items)
        if st.get("submitVisible"):
            if await _popup_reanswer(fr, page_ref, items, qtype, bank, frame, log=log):
                return {"action": "answered", "result": "correct"}
        if st.get("continueVisible"):
            # 继续 after a rejection usually starts the 回看 rewatch or clears
            # the notice. Deliberately NOT reported as a progress action: if it
            # really resumes anything the playhead moves and the stall counter
            # resets by itself, and if it did nothing the clip should still be
            # given up instead of burning the whole hour.
            if await _popup_click(fr, ".ans-videoquiz-continue:visible", log=log) and log:
                log("  popup: 回看 -> 继续")
        if log:
            log(f"  popup: 回答错误 ({st.get('wrongText','')[:14]}) - flip queued")
        return {"action": "wrong", "text": st.get("wrongText", "")}

    if not st.get("submitVisible"):
        return {"action": "waiting"}
    pending = [it for it in items if not any(it.get("checked") or [])
               or ((it.get("stem") or "").strip() in _POPUP_TRIED)]
    if not pending:
        # options are picked but the submit never landed (a silent click failure
        # or a panel re-render): press 提交 again instead of waiting forever.
        # 2026-10-18: this exact state held clips at their cue for minutes.
        if await _popup_click(fr, "#videoquiz-submit:visible", log=log):
            if log:
                log("  popup: options already picked, re-clicked 提交")
            return {"action": "submitted"}
        return {"action": "waiting"}

    picks = []
    offset = 0
    for it in items:
        n_opts = len(it.get("options") or [])
        if not any(it.get("checked") or []):
            pick = _popup_pick(bank, qtype, it.get("stem", ""),
                               it.get("options") or [])
            picks.append((offset, pick, it))
        offset += n_opts
    clicked = False
    for offset, pick, it in picks:
        for idx in pick["indexes"]:
            if await _popup_click(fr, ".ans-videoquiz:visible .ans-videoquiz-opt",
                                  offset + idx, log=log):
                clicked = True
        if log:
            stem = (it.get("stem") or "").strip()
            log(f"  popup: {qtype} [{pick.get('source')}] {stem[:36]!r} "
                f"-> {pick.get('text', '')[:12]}")
    if not clicked:
        return {"action": "click_failed"}
    await page_ref.wait_for_timeout(1200)
    if not await _popup_click(fr, "#videoquiz-submit:visible", log=log):
        return {"action": "click_failed"}

    await page_ref.wait_for_timeout(1500)
    fr2, st2 = await _find_popup(page_ref, frame)
    result = "pending"
    if st2:
        if st2.get("correctVisible"):
            result = "correct"
        elif st2.get("wrongVisible"):
            result = "wrong"
    if result == "correct":
        if bank is not None:
            for _, pick, it in picks:
                stem = (it.get("stem") or "").strip()
                if stem:
                    try:
                        bank.remember(stem, qtype, pick.get("text", ""),
                                      pick.get("source") or "videoquiz")
                    except Exception:
                        pass
        for _, pick, it in picks:
            stem = (it.get("stem") or "").strip()
            if stem:
                _POPUP_TRIED.pop(stem, None)
        if (fr2 is not None and st2.get("continueVisible")
                and st2.get("paused") is not False
                and await _popup_click(fr2, ".ans-videoquiz-continue:visible", log=log)):
            if log:
                log("  popup: 回答正确 -> 继续")
            return {"action": "continued", "result": "correct"}
    elif result == "wrong":
        _popup_record_tried(st2.get("items") or [])
        if log:
            log(f"  popup: 回答错误 ({st2.get('wrongText','')[:14]}) - flip queued")
    return {"action": "answered", "result": result}


async def hold_playback(frame, page_ref, read_state, target: float = 0.0,
                        max_wait_s: int = 3600, on_tick=None,
                        log=None, stall_limit: int = 9, bank=None) -> Dict[str, Any]:
    """Keep a started video playing until it actually reaches the end.

    THE completion rule on these video modules is "watch the whole thing": a clip
    that is stopped at 90% of its duration stays an unfinished task point, even
    though the on-screen condition reads "观看时长需 ≥ 总时长的 90%". Verified on
    思想道德 node 1245450348: 285s of genuine 1x playback (0 -> 272/295) left the
    node unfinished, while playing through to 295/295 (`ended`) completed it and
    moved the course counter from 37 to 38.

    Other behaviours this has to live with:
      * the platform validates real watch time, so seeking does not count
      * the skin pauses itself occasionally, so playback is re-asserted with a real
        mouse click on the play control rather than a JS play()
      * rates above 1x are reverted and pause the element, so playback stays at the
        rate the player chose

    Progress is logged every 5 minutes and a video that makes NO headway for
    stall_limit consecutive ticks is abandoned, so a single wedged clip can no longer
    look like a 28-minute silence.
    """
    end = time.time() + max_wait_s
    last_cur, stall, ticks, dead_ticks, empty = -1, 0, 0, 0, 0
    pop_actions = 0
    dur_seen = 0
    hard_end = None
    while time.time() < end:
        if hard_end is not None and time.time() > hard_end:
            if log:
                log(f"  playback exceeded its budget "
                    f"({dur_seen:.0f}s clip, {ticks * 10}s spent), giving up")
            return {"ok": False, "why": "over_budget", "cur": last_cur,
                    "dur": dur_seen, "ticks": ticks}
        await page_ref.wait_for_timeout(10000)
        ticks += 1
        # A video runs for minutes; with no input at all the page looks like a bot.
        # Nudge the pointer every few ticks, away from the player so it cannot
        # register as a pause click.
        if ticks % 3 == 0:
            try:
                vp = page_ref.viewport_size or {"width": 1400, "height": 900}
                await page_ref.mouse.move(
                    random.randint(int(vp["width"] * 0.25), int(vp["width"] * 0.75)),
                    random.randint(int(vp["height"] * 0.12), int(vp["height"] * 0.28)),
                    steps=random.randint(4, 10))
            except Exception:
                pass
        # An in-video popup question holds the clip paused and NOTHING else
        # resumes it: check before reading state, because the panel re-render
        # also breaks the state reads for a while (that is why clips 1 and 3
        # died with no_state right after their first cue point).
        pop = None
        try:
            pop = await handle_video_popup(page_ref, bank=bank, frame=frame, log=log)
        except Exception:
            pop = None
        if pop and pop.get("action") in ("answered", "continued", "submitted"):
            pop_actions += 1
            if pop_actions <= 15:
                # time spent on the popup is not a stall; start over counting
                last_cur, stall, dead_ticks, empty = -1, 0, 0, 0
        try:
            st = await read_state()
        except Exception:
            st = None
        if not st:
            # No readable media state. This used to just `continue`, with no
            # counter and no log, so an unreadable frame span silently for the
            # whole VIDEO_MAX_WAIT. Count it, say so, and give up quickly.
            empty += 1
            if log and empty % 6 == 0:
                log(f"  no media state for {empty * 10}s "
                    f"(frame changed, or the element is absent)")
            # re-resolving the frame can take an extra read or two, so give
            # this more room than the original 90s before giving up
            if empty >= 18:
                if log:
                    log(f"  cannot read media state after {empty * 10}s - "
                        f"abandoning this clip")
                return {"ok": False, "why": "no_state", "ticks": ticks,
                        "dur": dur_seen, "cur": last_cur}
            continue
        empty = 0
        cur, dur = st.get("cur") or 0, st.get("dur") or 0
        if dur:
            dur_seen = dur
            # A SHORT clip can loop forever (0 -> 13 -> 0 -> 13): cur keeps changing,
            # so the stall detector never fires and the run burns the full hour.
            # Verified: 大学物理 node 1010122035 is a 13-second clip and held a worker
            # for 43 minutes. Cap the wait in proportion to the clip length instead.
            if hard_end is None:
                budget = max(90.0, dur * 2.5 + 120.0)
                hard_end = time.time() + min(float(max_wait_s), budget)
        # a heartbeat every 5 minutes so a long clip is visibly alive
        if log and ticks % 30 == 0:
            pct = (cur / dur * 100) if dur else 0
            log(f"  playing {cur:.0f}/{dur:.0f}s ({pct:.0f}%) "
                f"elapsed {ticks * 10}s")
        if on_tick:
            on_tick(ticks, cur, dur, st)
        # done only at the true end (or when the player reports ended)
        if st.get("ended") or (dur and cur >= dur - 1):
            return {"ok": True, "dur": dur, "cur": cur, "ticks": ticks,
                    "secs": ticks * 10, "rate": st.get("rate"), "ended": True}
        if st.get("paused"):
            # the skin paused itself (or the popup above just closed): click the
            # real play control
            try:
                await _poke_play(frame, page_ref)
            except Exception:
                pass
        if cur == last_cur:
            stall += 1
            dead_ticks += 1
            if stall >= 2:
                # vjs-big-play-button is REMOVED once playback starts, so the old
                # re-assert was a no-op. Use the control-bar button, which persists.
                try:
                    await _poke_play(frame, page_ref)
                except Exception:
                    pass
                # A player can wedge at one segment forever (国家安全教育 stopped at
                # 1430/1703 on every pass while the same clip plays through when
                # seeked past it). Nudge the media element forward a couple of
                # seconds to skip the dead segment instead of abandoning the clip.
                if dead_ticks % 6 == 0:
                    try:
                        await frame.evaluate(
                            "() => { const v = document.querySelector('video,audio');"
                            " if (v) { v.currentTime = (v.currentTime || 0) + 2;"
                            " v.play(); } }")
                        if log:
                            log(f"  stalled {dead_ticks * 10}s at "
                                f"{cur:.0f}s - nudged player +2s")
                    except Exception:
                        pass
                stall = 0
            # genuinely wedged: no headway for ~90s of retries
            if dead_ticks >= stall_limit:
                if log:
                    log(f"  playback stalled at {cur:.0f}/{dur_seen:.0f}s after "
                        f"{ticks * 10}s, giving up on this clip")
                    # record WHY: paused vs buffering live in different places, and
                    # without this the two are indistinguishable from the log alone
                    try:
                        why = await frame.evaluate(
                            "() => { const v = document.querySelector('video,audio');"
                            " const t = (document.body ? (document.body.innerText||'')"
                            "   : '').replace(/\\s+/g,' ').slice(0,110);"
                            " return { paused: v ? v.paused : null,"
                            "          ready: v ? v.readyState : null,"
                            "          net: v ? v.networkState : null,"
                            "          err: v && v.error ? v.error.code : null,"
                            "          text: t }; }")
                        log(f"    why: paused={why.get('paused')} "
                            f"ready={why.get('ready')} net={why.get('networkState') or why.get('net')} "
                            f"err={why.get('err')}")
                        log(f"    page: {why.get('text','')[:100]}")
                    except Exception as exc:
                        log(f"    why: could not read state ({type(exc).__name__})")
                return {"ok": False, "why": "stalled", "cur": cur, "dur": dur_seen,
                        "ticks": ticks}
        else:
            stall = 0
            dead_ticks = 0
        last_cur = cur
    return {"ok": False, "why": "timeout", "last": last_cur, "ticks": ticks,
            "dur": dur_seen}


async def finish_video_frame(frame, page_ref, rate: float = 2.0,
                             target_fraction: float = 1.0,
                             max_wait_s: int = 3600,
                             keep_alive_s: int = 0, log=None,
                             bank=None) -> Dict[str, Any]:
    """Start the video (real mouse event) then watch it through to the end.

    Deliberately has NO seek shortcut and does NOT stop at 90%. Both shortcuts
    produced false successes: the platform asks for the clip to be genuinely
    watched, a seek adds no watch time, and stopping at 90% of the duration leaves
    the task point unfinished. If the video is resuming mid-way it is rewound and
    played from the start, because position tells us nothing about watched time.
    """
    started = await start_video(frame, page_ref)
    for _ in range(4):
        if started.get("ok"):
            break
        # An unanswered popup at position 0 sits over the play button and keeps
        # the module from ever loading, so start "fails" while the real fix is
        # simply to answer the question first. 2026-10-18: node 1254202863
        # returned could_not_start on every attempt for exactly this reason.
        act = await handle_video_popup(page_ref, bank=bank, frame=frame, log=log)
        if not act:
            break
        if act.get("action") == "wrong":
            # the 回看 countdown holds the panel; give it time, then it re-asks
            await page_ref.wait_for_timeout(20000)
        else:
            await page_ref.wait_for_timeout(3000)
        started = await start_video(frame, page_ref, attempts=2)
    if not started.get("ok"):
        return {"ok": False, "why": "could_not_start", "started": started}

    dur = started["dur"]

    async def read_state():
        """Read the player state, re-finding the media frame each time.

        Binding to the `frame` captured before playback was the bug behind every
        video failing with no_state on 2026-10-17: video.js re-renders when playback
        starts, so that handle goes stale and every later evaluate throws. Looking the
        frame up by URL on each read costs a little and survives the swap.
        """
        try:
            st = await frame.evaluate(VIDEO_STATE)
            if st:
                return st
        except Exception:
            pass
        # the captured handle failed or returned nothing - re-resolve by URL
        try:
            for fr in page_ref.frames:
                url = fr.url or ""
                if not any(h in url for h in ("/modules/video/", "/modules/audio/",
                                              "/ananas/modules/")):
                    continue
                try:
                    st = await fr.evaluate(VIDEO_STATE)
                    if st:
                        return st
                except Exception:
                    continue
        except Exception:
            pass
        return None

    # Resuming mid-video? The saved position tells us nothing about how much has
    # actually been watched, so rewind and cover the whole thing.
    try:
        st = await read_state()
        cur = (st or {}).get("cur") or 0
        if cur > 5:
            rw = await rewind(frame, page_ref)
            started["rewound_from"] = cur
            started["rewind"] = rw
    except Exception:
        pass

    hold = await hold_playback(frame, page_ref, read_state, target=target_fraction,
                               max_wait_s=max_wait_s, log=log, bank=bank)
    hold["started"] = started
    return hold


REWIND_JS = r"""() => {
    const m = document.querySelector('video, audio');
    if (!m) return { ok: false, why: 'no_media' };
    try { m.currentTime = 0; } catch (e) { return { ok: false, why: 'set_failed' }; }
    return { ok: true, cur: m.currentTime, dur: isFinite(m.duration) ? m.duration : null };
}"""


async def rewind(frame, page_ref) -> dict:
    """Send the playhead back to the start.

    Needed because position is not the same thing as watched time. A seek moves the
    position without adding watch time, so a video can sit at 95% and still be an
    unfinished task point - the platform only counts time actually played through.
    Replaying from the beginning is the only way to satisfy that.
    """
    try:
        res = await frame.evaluate(REWIND_JS)
    except Exception as exc:
        return {"ok": False, "why": type(exc).__name__}
    await page_ref.wait_for_timeout(1500)
    return res
