"""Discovery by driving a real browser.

Why this exists next to the Apify backend rather than instead of it: Apify is
the right default because someone else maintains the scrapers, but it is a
third party, it is metered, and there are engagements where sending a client's
market research through a vendor is not acceptable. This path keeps everything
on the machine that runs it.

Why it is NOT the default: it is slow, it is the first thing to break when a
site ships new markup, and it is the path most likely to trip bot detection.
It is a fallback with a clear reason to exist, not a preference.

Scope, stated rather than implied: public, logged-out pages only. No cookie,
no stored session, no credential for any network. Robots and rate limits are
respected by keeping the request volume to what a person browsing would
generate -- one search page per channel per product, not a crawl.

Playwright is an OPTIONAL dependency. The repo's three-package install does
not include it, `requirements.txt` is unchanged, and this module reports
itself unavailable rather than failing an import when it is missing.
"""
from __future__ import annotations

import os
import re

from .base import (CHANNEL_RATIO, Discovery, DiscoveryError, DiscoveryRequest,
                   Lookalike)

# Logged-out, public search surfaces. Kept as templates so what this touches
# is auditable in one place rather than spread through the code.
SEARCH = {
    "tiktok":    "https://www.tiktok.com/search?q={q}",
    "instagram": "https://www.instagram.com/explore/tags/{tag}/",
    "youtube":   "https://www.youtube.com/results?search_query={q}",
    "facebook":  "https://www.facebook.com/search/posts?q={q}",
}

NAV_TIMEOUT_MS = 25_000
SETTLE_MS = 2_500


def available() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except Exception:                                        # noqa: BLE001
        return False


def _int(text: str) -> int:
    """'1.2M views' -> 1200000. Social UIs never give you a plain number."""
    m = re.search(r"([\d.,]+)\s*([KMB])?", text or "", re.I)
    if not m:
        return 0
    try:
        n = float(m.group(1).replace(",", ""))
    except ValueError:
        return 0
    return int(n * {"k": 1e3, "m": 1e6, "b": 1e9}.get((m.group(2) or "").lower(), 1))


class PlaywrightDiscovery:
    name = "playwright"
    synthetic = False

    def find(self, req: DiscoveryRequest) -> list[Lookalike]:
        if not available():
            raise DiscoveryError(
                "playwright is not installed (pip install playwright "
                "&& playwright install chromium)")
        from playwright.sync_api import sync_playwright

        url = SEARCH.get(req.channel)
        if not url:
            raise DiscoveryError(f"no search surface mapped for '{req.channel}'")
        q = f"{req.category} {req.audience.split(',')[0]}".strip()
        target = url.format(q=q.replace(" ", "+"),
                            tag=re.sub(r"[^a-z0-9]", "", req.category.lower()))

        rows: list[Lookalike] = []
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM") or None)
            try:
                page = browser.new_page(
                    locale=req.locale,
                    viewport={"width": 1366, "height": 900})
                page.set_default_timeout(NAV_TIMEOUT_MS)
                page.goto(target, wait_until="domcontentloaded")
                page.wait_for_timeout(SETTLE_MS)
                rows = self._scrape(page, req)
            finally:
                browser.close()

        rows.sort(key=lambda l: l.views, reverse=True)
        return rows[:req.limit]

    def _scrape(self, page, req: DiscoveryRequest) -> list[Lookalike]:
        """Read whatever the logged-out page will show.

        Selectors are intentionally loose and per-channel fallbacks are tried
        in order. A precise selector on a site that redeploys weekly is a
        scheduled outage; a loose one degrades to fewer rows instead of zero.
        """
        out: list[Lookalike] = []
        sels = {
            "youtube": "ytd-video-renderer",
            "tiktok": "div[data-e2e='search_top-item'], div[data-e2e='search-card-desc']",
            "instagram": "article a[href*='/p/'], article a[href*='/reel/']",
            "facebook": "div[role='article']",
        }
        nodes = page.query_selector_all(sels.get(req.channel, "article"))[:40]

        for n in nodes:
            try:
                text = (n.inner_text() or "").strip()
            except Exception:                                # noqa: BLE001
                continue
            if not text:
                continue
            lines = [x for x in text.split("\n") if x.strip()]
            title = lines[0][:180] if lines else ""
            views = 0
            for ln in lines:
                if re.search(r"views|watching|次視聴|Aufrufe|vues", ln, re.I):
                    views = _int(ln)
                    break
            href = ""
            try:
                a = n.query_selector("a[href]")
                if a:
                    href = a.get_attribute("href") or ""
                    if href.startswith("/"):
                        href = f"https://www.{req.channel}.com{href}"
            except Exception:                                # noqa: BLE001
                pass
            handle = next((x for x in lines[1:4] if x.startswith("@")), "")

            out.append(Lookalike(
                channel=req.channel, handle=handle, brand=handle.lstrip("@"),
                title=title, product_category=req.category,
                posted_days_ago=0, views=views,
                # A logged-out page shows reach but not reactions, so there is
                # no engagement rate to be had. Zero and honest beats a number
                # invented to fill the column.
                engagement_rate=0.0, velocity=0.0,
                ratio=CHANNEL_RATIO.get(req.channel, "1:1"),
                format="video" if req.channel in ("tiktok", "youtube") else "image",
                hook=title.split("\n")[0][:110],
                surface_cues=[], palette=[],
                evidence_url=href, synthetic=False,
            ))
        return out


assert isinstance(PlaywrightDiscovery(), Discovery)
