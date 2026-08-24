"""Discovery through Apify's hosted actors.

Apify runs and maintains the per-network scrapers, which is the reason to use
it rather than to write four crawlers: these sites change their markup
constantly and break anything hand-rolled within weeks. Paying someone to
keep four scrapers alive is a better trade than owning them.

WHAT THIS COSTS, because it is a real bill and should not be a surprise: each
`find()` is one actor run per channel, billed in Apify compute units, and
runs take tens of seconds. The engine calls this once per product x channel
and caches the result to `.cache/discovery/`, so a re-run of the same plan
costs nothing -- the same argument as the master-image cache in `assets.py`.

WHAT IT IS ALLOWED TO SEE: public posts only. There is no login, no cookie
and no credential for any social network in this repo, and adding one would
mean scraping as a user, which is a different thing legally and ethically.
Anything behind a login is out of scope by construction, not by omission.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .base import (CHANNEL_RATIO, Discovery, DiscoveryError, DiscoveryRequest,
                   Lookalike)

API = "https://api.apify.com/v2"

# The public actors used per channel. Named as constants rather than inlined
# so swapping one out -- which happens when an actor is deprecated -- is a
# one-line change in an obvious place.
ACTORS = {
    "tiktok":    "clockworks~tiktok-scraper",
    "instagram": "apify~instagram-scraper",
    "youtube":   "streamers~youtube-scraper",
    "facebook":  "apify~facebook-posts-scraper",
}

RUN_TIMEOUT_S = 180
POLL_EVERY_S = 3


def _token() -> str:
    tok = (os.environ.get("APIFY_TOKEN") or "").strip()
    if not tok:
        raise DiscoveryError("APIFY_TOKEN is not set")
    return tok


def _post(url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {_token()}"},
        method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:      # noqa: S310
        return json.loads(r.read())


def _get(url: str) -> dict | list:
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {_token()}"})
    with urllib.request.urlopen(req, timeout=60) as r:      # noqa: S310
        return json.loads(r.read())


def _search_terms(req: DiscoveryRequest) -> list[str]:
    """What to actually go and look for.

    The product NAME is deliberately not one of the terms. Searching a brand's
    own product name finds that brand's own posts, which is the one thing this
    is not for. Category plus the market's language of the benefit is what
    surfaces the competitor doing well.
    """
    cat = req.category.strip() or "skincare"
    terms = [cat, f"best {cat}", f"{cat} routine"]
    aud = req.audience.split(",")[0].strip()
    if aud:
        terms.append(f"{cat} {aud}")
    return terms[:4]


class ApifyDiscovery:
    name = "apify"
    synthetic = False

    def find(self, req: DiscoveryRequest) -> list[Lookalike]:
        actor = ACTORS.get(req.channel)
        if not actor:
            raise DiscoveryError(f"no Apify actor mapped for '{req.channel}'")

        run = _post(f"{API}/acts/{actor}/runs", self._input(req))
        run_id = run["data"]["id"]
        dataset = run["data"]["defaultDatasetId"]

        deadline = time.monotonic() + RUN_TIMEOUT_S
        while True:
            status = _get(f"{API}/actor-runs/{run_id}")["data"]["status"]
            if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
                break
            if time.monotonic() > deadline:
                # Abandoned rather than waited on forever. A discovery step
                # that hangs takes the whole engine run with it, and the
                # engine's fallback to synthetic is a better outcome than a
                # spinner nobody can cancel.
                raise DiscoveryError(
                    f"apify run {run_id} still {status} after {RUN_TIMEOUT_S}s")
            time.sleep(POLL_EVERY_S)

        if status != "SUCCEEDED":
            raise DiscoveryError(f"apify run {run_id} ended {status}")

        items = _get(f"{API}/datasets/{dataset}/items?clean=true&limit=200")
        rows = [self._row(req, it) for it in items if isinstance(it, dict)]
        rows = [r for r in rows if r]
        rows.sort(key=lambda l: l.velocity * l.engagement_rate, reverse=True)
        return rows[:req.limit]

    def _input(self, req: DiscoveryRequest) -> dict:
        """Per-actor input. Each one wants a different shape for one idea --
        which is exactly the argument for this adapter existing."""
        terms = _search_terms(req)
        n = max(20, req.limit * 5)
        if req.channel == "tiktok":
            return {"searchQueries": terms, "resultsPerPage": n,
                    "shouldDownloadVideos": False,
                    "shouldDownloadCovers": False}
        if req.channel == "instagram":
            # A hashtag is one token. Passing "face serum" through as a
            # hashtag search returned zero rows on every market -- not an
            # error, just nothing, which is the worst kind of bug because the
            # fallback never fires and the channel silently has no evidence.
            tag = re.sub(r"[^a-z0-9]", "", terms[0].lower()) or "skincare"
            return {"search": tag, "searchType": "hashtag",
                    "resultsType": "posts", "resultsLimit": n,
                    "addParentData": False}
        if req.channel == "youtube":
            return {"searchKeywords": terms[0], "maxResults": n,
                    "sortingOrder": "views"}
        # Facebook: this actor scrapes named pages, it does not keyword-search
        # the network -- Facebook has no public post search worth crawling.
        # Given a page it works; given a category it cannot, so it raises and
        # discovery falls back with the reason on screen rather than
        # pretending the channel had nothing to say.
        raise DiscoveryError(
            "Facebook has no public keyword post search; that actor needs "
            "specific page URLs. Add competitor pages to crawl it for real.")

    def _row(self, req: DiscoveryRequest, it: dict) -> Lookalike | None:
        """Normalise one actor's row into a Lookalike.

        Every field is looked up across several possible key names because
        the four actors disagree about what to call the same number, and an
        actor that renames a field on a Tuesday should degrade one column
        rather than crash a run.
        """
        def pick(*keys, default=None):
            for k in keys:
                v = it.get(k)
                if v not in (None, "", []):
                    return v
            return default

        views = int(pick("playCount", "viewCount", "videoViewCount",
                         "views", default=0) or 0)
        likes = int(pick("diggCount", "likesCount", "likes", default=0) or 0)
        comments = int(pick("commentCount", "commentsCount", default=0) or 0)
        shares = int(pick("shareCount", "sharesCount", default=0) or 0)
        if not views and not likes:
            return None

        er = round((likes + comments + shares) / max(views, 1) * 100, 2)
        title = str(pick("text", "caption", "title", "description",
                         default=""))[:180]
        handle = str(pick("authorMeta.name", "ownerUsername", "channelName",
                          "pageName", "user", default="") or "")
        if isinstance(pick("authorMeta"), dict):
            handle = pick("authorMeta").get("name", handle)

        return Lookalike(
            channel=req.channel,
            handle=("@" + handle) if handle and not handle.startswith("@") else handle,
            brand=handle or "unknown",
            title=title,
            product_category=req.category,
            posted_days_ago=self._age_days(pick("createTimeISO", "timestamp",
                                                "date", "publishedAt")),
            views=views,
            engagement_rate=er,
            # Without an account baseline there is nothing to compute a true
            # velocity against, so engagement stands in and the field is
            # honest about being a proxy rather than silently wrong.
            velocity=round(min(er / 2.0, 9.9), 2),
            ratio=CHANNEL_RATIO.get(req.channel, "1:1"),
            format=self._format(it, req.channel),
            hook=title.split("\n")[0][:110],
            surface_cues=[],
            palette=[],
            evidence_url=str(pick("webVideoUrl", "url", "postUrl", "link",
                                  default="")),
            synthetic=False,
        )

    @staticmethod
    def _age_days(stamp) -> int:
        import datetime as dt
        if not stamp:
            return 0
        try:
            if isinstance(stamp, (int, float)):
                d = dt.datetime.fromtimestamp(float(stamp), dt.timezone.utc)
            else:
                d = dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            return max(0, (dt.datetime.now(dt.timezone.utc) - d).days)
        except Exception:                                    # noqa: BLE001
            return 0

    @staticmethod
    def _format(it: dict, channel: str) -> str:
        if it.get("videoUrl") or it.get("webVideoUrl") or channel == "youtube":
            return "video"
        imgs = it.get("images") or it.get("childPosts") or []
        return "carousel" if len(imgs) > 1 else "image"


assert isinstance(ApifyDiscovery(), Discovery)
