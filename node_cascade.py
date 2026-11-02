# -*- coding: utf-8 -*-
"""Catalog-node cascade: some chapters keep their task points on child nodes.

Verified layout (军事理论 1.1, 2026-10-13):
  * the parent node shows a green tick (done) but the content pane is EMPTY
  * each child row in the catalog carries <span class="jobCount">2</span>
  * nothing loads until the child row is clicked; it then navigates to that
    node's own ?chapterId=... page where the real widgets appear

Row detection has to be precise: a row is the smallest element whose own text is
"<title> <count>" with the jobCount span as a sibling. Climbing further just
matches the whole page container.
"""
from __future__ import annotations

from typing import Any, Dict, List

# Find the smallest container that holds both the title and the jobCount badge.
ROWS_JS = r"""() => {
    const out = [];
    const seen = new Set();
    document.querySelectorAll('.jobCount').forEach(badge => {
        const n = parseInt((badge.innerText || '').trim(), 10);
        if (!n) return;
        // walk up until the accumulated text is longer than the badge itself
        let row = badge, text = '';
        for (let i = 0; i < 3; i++) {
            const t = (row.innerText || '').replace(/\s+/g, ' ').trim();
            if (t && t.length > String(n).length + 1) { text = t; break; }
            row = row.parentElement;
            if (!row) return;
        }
        if (!row) return;
        // don't let the walk run away into the page container
        if (text.length > 120) return;
        const id = row.getAttribute('chapterid') || row.getAttribute('data-chapterid') ||
                   row.getAttribute('nodeid') || row.id || '';
        const key = text + '|' + n;
        if (seen.has(key)) return;
        seen.add(key);
        out.push({
            title: text.slice(0, 70),
            count: n,
            cls: (row.className || '').toString().slice(0, 50),
            id: String(id).slice(0, 24),
            tag: row.tagName,
        });
    });
    return out;
}"""

CLICK_JS = r"""(target) => {
    const t = String(target).trim();
    const badges = [...document.querySelectorAll('.jobCount')];
    for (const badge of badges) {
        let row = badge;
        for (let i = 0; i < 3; i++) {
            const txt = (row.innerText || '').replace(/\s+/g, ' ').trim();
            if (txt && txt.length > 1) {
                if (txt.slice(0, t.length) === t || txt.indexOf(t) === 0) {
                    try { row.click(); return { ok: true, text: txt.slice(0, 50) }; } catch (e) {}
                    try {
                        const ev = new MouseEvent('click', { bubbles: true, cancelable: true });
                        row.dispatchEvent(ev);
                        return { ok: true, text: txt.slice(0, 50), via: 'dispatch' };
                    } catch (e) {}
                }
            }
            row = row.parentElement;
            if (!row) break;
        }
    }
    return { ok: false };
}"""

BADGE_JS = r"""() => {
    const badge = document.querySelector('.jobCount, .icon_Learn, [class*=jobCount]');
    if (!badge) return null;
    let el = badge;
    for (let i = 0; i < 6; i++) {
        if (el.getAttribute && el.getAttribute('chapterid')) {
            return { chapterid: el.getAttribute('chapterid'),
                     nodeid: el.getAttribute('nodeid') || null,
                     cls: (el.className || '').toString().slice(0, 40) };
        }
        const a = el.querySelector ? el.querySelector('[chapterid]') : null;
        if (a) return { chapterid: a.getAttribute('chapterid'),
                        nodeid: a.getAttribute('nodeid') || null,
                        cls: (a.className || '').toString().slice(0, 40) };
        el = el.parentElement;
        if (!el) break;
    }
    return null;
}"""


NODES_JS = r"""() => {
    const out = []; const seen = new Set();
    // the catalog rows are <h5 id="cur<chapterId>">N title</h5>, but modern
    // mooc-ans catalogs render them as <div class="posCatalog_select"
    // id="cur<chapterId>"> with either a .jobCount badge or a
    // .catalog_points_yi badge (span.orangeNew) holding the unfinished count.
    document.querySelectorAll('h5[id^="cur"], [id^="cur"]').forEach(el => {
        const id = el.id || '';
        const m = id.match(/^cur(\d+)$/);
        if (!m) return;
        const chapterId = m[1];
        if (seen.has(chapterId)) return;
        seen.add(chapterId);
        let count = 0;
        const badge = el.querySelector ? el.querySelector('.jobCount') : null;
        if (badge) count = parseInt((badge.innerText || '0').trim(), 10) || 0;
        if (!count) {
            const yi = el.querySelector ? el.querySelector('.catalog_points_yi') : null;
            if (yi) {
                const dm = (yi.innerText || '').match(/\d+/);
                count = dm ? parseInt(dm[0], 10) : 0;
            }
        }
        if (!count) {
            const unf = el.querySelector ? el.querySelector('.jobUnfinishCount') : null;
            if (unf) count = parseInt(unf.value || '0', 10) || 0;
        }
        const txt = (el.innerText || '').replace(/\s+/g, ' ').trim();
        out.push({
            chapterId: chapterId,
            count: count,
            title: txt.slice(0, 70),
        });
    });
    return out;
}"""


async def list_child_nodes(page) -> List[Dict[str, Any]]:
    """Child catalog nodes (h5 id=cur<chapterId>) that still carry task points.

    This is the reliable handle: the h5 id IS the node's chapterId, so we can
    navigate straight to it instead of relying on click coordinates.
    """
    try:
        return await page.evaluate(NODES_JS)
    except Exception:
        return []


async def list_catalog_rows(page) -> List[Dict[str, Any]]:
    """Rows in the catalog that still carry unfinished task points."""
    try:
        return await page.evaluate(ROWS_JS)
    except Exception:
        return []


async def click_row(page, title_fragment: str) -> Dict[str, Any]:
    try:
        return await page.evaluate(CLICK_JS, title_fragment)
    except Exception as exc:
        return {"ok": False, "why": type(exc).__name__}


async def find_node_ids(page) -> List[Dict[str, Any]]:
    """Read chapterid/nodeid attributes from the catalog, the reliable way in."""
    try:
        return await page.evaluate(r"""() => {
            const out = []; const seen = new Set();
            document.querySelectorAll('[chapterid]').forEach(el => {
                const id = el.getAttribute('chapterid');
                const node = el.getAttribute('nodeid') || '';
                const t = (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 60);
                if (!id || seen.has(id + node)) return;
                seen.add(id + node);
                const hasJob = !!(el.querySelector && el.querySelector('.jobCount'));
                out.push({ chapterid: id, nodeid: node, text: t, hasJob: hasJob });
            });
            return out.slice(0, 60);
        }""")
    except Exception:
        return []
