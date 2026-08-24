"""A small local web app for the pipeline.

Deliberately built on Python's standard library `http.server` -- no Flask, no
FastAPI, no npm. The whole point of this app is that somebody can be handed
one folder, double-click one file, and see the pipeline work. Every dependency
you add is another thing that fails on their machine.

It is also a *thin* layer. Every button here calls the same functions the CLI
calls -- `load_brief`, `preflight_brief`, `run_campaign`. There is no pipeline
logic in this file at all, which is the property that matters: the demo and
the product cannot drift apart.

    python app.py            # then open http://127.0.0.1:8765
"""
from __future__ import annotations

import base64
import glob
import io
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser

import yaml
from PIL import Image

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import parse_qsl, unquote, urlparse
from urllib.request import Request, urlopen

from pipeline.brief import BriefError, load_brief
from pipeline import engine, insights, motion
from pipeline.discovery import CHANNELS, CHANNEL_NAMES, discovery_status
from pipeline.discovery import default_discovery as _default_discovery
from pipeline.env import load_dotenv
from pipeline.checks import preflight_brief
from pipeline.providers import (CREDENTIALS, default_provider,
                                provider_status)
from pipeline.storage import (STORAGE_CREDENTIALS, StorageError,
                              default_storage, get_storage, storage_status)
from pipeline.report import write_report
from pipeline.runner import run_campaign

ROOT = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(ROOT, ".env"))
HOST, PORT = "127.0.0.1", 8765

# When this process loaded its own source.
#
# A server that is still running yesterday's code is invisible from the
# browser: index.html is read from disk on every request, so a stale process
# happily serves the NEW page with the OLD Python behind it. That has been
# mistaken for four different bugs -- an empty provider list, a missing form,
# a toggle that does nothing, a checkbox that is ignored -- and every time the
# page looked perfectly current.
#
# Comparing the mtime we started with against the files now is enough to catch
# it, needs no version constants to keep in sync, and is reported to the UI so
# the person sees it instead of debugging a ghost.
#
# It watches EVERY module this process actually imported, not just app.py.
# Watching one file was close to useless in practice: almost all the code
# lives in pipeline/, so editing the resolver, a provider or the runner --
# exactly the edits whose effect you are trying to see -- left the check
# perfectly happy. Walking `sys.modules` and filtering to this tree answers
# the precise question ("is anything I have in memory older than the file on
# disk?") with no list to maintain and no false positives from files this
# process never loaded, like tests/ or tools/.
#
# Deliberately NOT included: webui/index.html. It is read from disk on every
# request, so an edit to it is already live and flagging it would be a lie.

def _newest_loaded_mtime() -> float:
    newest = 0.0
    for mod in list(sys.modules.values()):
        path = getattr(mod, "__file__", None)
        if not path:
            continue
        path = os.path.abspath(path)
        # __main__ is app.py itself, which lives at ROOT rather than under it.
        if not (path.startswith(ROOT + os.sep) or path == os.path.abspath(__file__)):
            continue
        try:
            newest = max(newest, os.path.getmtime(path))
        except OSError:
            continue
    return newest


_LOADED_AT = _newest_loaded_mtime()


def _is_stale() -> bool:
    try:
        # A small tolerance, because filesystem timestamp granularity and the
        # gap between "import finished" and "we measured" are both real, and a
        # banner that cries wolf gets ignored exactly like a check that never
        # fires at all.
        return _newest_loaded_mtime() > _LOADED_AT + 0.5
    except OSError:
        return False

# --------------------------------------------------------------------------
# Run state. One run at a time -- this is a local demo tool, not a service,
# and a queue would be more machinery than the problem deserves.
# --------------------------------------------------------------------------

STATE: dict = {"running": False, "lines": [], "summary": None, "error": None,
               "report": None, "graph": {}, "seq": 0, "landed": [],
               "stages": {}}
LOCK = threading.Lock()

# The live server object, so /api/shutdown can stop it. Set in main().
SERVER = None

# Identifies THIS program to another copy of itself starting on the same port.
# A bare "is something listening?" is not enough to justify stopping it -- the
# whole point is to be certain it is us before doing anything.
APP_ID = "creative-automation-pipeline"
APP_VERSION = "1.0"

# Where uploaded product photography lands.
#
# The same folder the sample brief already points at, deliberately: an upload
# and a file someone dropped in by hand are the same kind of thing, and giving
# uploads their own special directory would create two places to look for one
# concept.
ASSET_DIR = os.path.join(ROOT, "campaigns", "assets")

# What Pillow can open AND the pipeline can composite. WEBP and JPEG are here
# because that is what phones and stock libraries actually produce; the list
# is checked against the decoded bytes as well, never the extension alone.
ASSET_EXTS = {".png", ".jpg", ".jpeg", ".webp"}

MAX_UPLOAD_MB = 25
# base64 inflates by 4/3. Comparing against the ENCODED length lets an
# oversize upload be rejected before it is decoded into memory.
MAX_UPLOAD_B64 = int(MAX_UPLOAD_MB * 1024 * 1024 * 4 / 3) + 1024

# Which pipeline event advances which node on the flow canvas. The graph is
# driven entirely by events the pipeline actually emits -- the same records
# that go into run.log.jsonl -- so the animation cannot show a stage that did
# not happen. An animation driven by a timer is a lie, and somebody will ask.
EVENT_NODE = {
    "run-start":     "brief",
    "preflight":     "preflight",
    "reuse":         "src_disk",
    "cache-hit":     "src_cache",
    "generate":      "src_gen",
    "masters-ready": "master",
    "variant-start": "variant",
    "run-end":       "report",
    "storage-open":  "store",
}
STAGE_NODE = {"crop": "crop", "scrim": "scrim", "message": "message",
              "logo": "logo", "measure": "measure", "checks": "checks"}

# What each pipeline stage is called on screen, and the order they run in.
#
# The internal names are what the code does; these are what a creative person
# would call it. "scrim" is jargon for the gradient that keeps type readable,
# so the panel says "legibility" -- the stage's PURPOSE, which is what someone
# reviewing the output needs, while the code keeps the name of the mechanism.
STAGE_LABELS = [
    ("crop",    "cut to spec",  "#e2a13d"),
    ("scrim",   "legibility",   "#5b8dd6"),
    ("message", "market copy",  "#c77bd0"),
    ("logo",    "brand mark",   "#4bbfa4"),
    ("measure", "measure",      "#d8c04a"),
    ("checks",  "checks",       "#7f8a99"),
]


def _reduce(rec: dict) -> None:
    """Fold one pipeline event into the flow-graph state."""
    g = STATE["graph"]

    def bump(node: str, state: str = "done", n: int = 1):
        cur = g.setdefault(node, {"state": "idle", "count": 0})
        cur["count"] += n
        cur["state"] = state
        cur["seq"] = STATE["seq"]

    STATE["seq"] += 1
    ev = rec.get("event")

    # One object per upload, so the node's count is the number of objects that
    # actually landed -- not the number we attempted.
    if ev == "variant" and rec.get("stored_uri"):
        bump("store")
        return
    if ev == "storage-error":
        bump("store", "err", 0)
        return

    if ev == "stage":
        node = STAGE_NODE.get(rec.get("stage"))
        if node:
            bump(node)
        return

    if ev == "variant":
        bump("verdict")
        v = rec.get("verdict")
        if v:
            key = "verdict_" + v
            cur = g.setdefault(key, {"state": "idle", "count": 0})
            cur["count"] += 1
            cur["state"] = "done"
        return

    node = EVENT_NODE.get(ev)
    if node:
        bump(node)


def _emit(msg: str) -> None:
    with LOCK:
        STATE["lines"].append(msg)


def _on_event(rec: dict) -> None:
    """Sink handed to the runner. Called on the worker thread."""
    with LOCK:
        _reduce(rec)
        # Finished creatives are kept so the browser can draw them as they
        # land instead of waiting for the whole run. Newest first, because
        # that is the order the gallery shows them in and doing it here means
        # the page does not have to re-sort on every poll.
        # Stage records, kept per variant so the results panel can show what
        # each stage did to THIS creative. Keyed by variant id because stages
        # from eighteen deliverables arrive interleaved -- they are produced in
        # order per creative, but the stream is one flat sequence.
        if rec.get("event") == "stage" and rec.get("variant"):
            STATE["stages"].setdefault(rec["variant"], []).append({
                "stage": rec.get("stage"),
                "params": rec.get("params") or {},
                "ms": rec.get("ms", 0),
                "preview": rec.get("preview", ""),
                "rules": rec.get("rules") or [],
            })

        if rec.get("event") == "variant":
            # stored_uri and share_url were missing from this list, which is
            # why the cloud chip only ever appeared after a run finished: the
            # live card and the final card are built by the same function, and
            # the live one was being handed a record with the fields stripped
            # out. The gallery is not supposed to change when the run ends.
            STATE["landed"].insert(0, {
                k: rec.get(k) for k in
                ("variant", "verdict", "score", "product", "locale", "ratio",
                 "message", "path", "out_dir", "findings",
                 "stored_uri", "share_url",
                 "layered", "layered_uri", "layered_share")})


def _render_sample(brief, product, market, ratio, surface: str,
                   provider_name: str = "") -> dict:
    """One creative, from a suggested surface prompt.

    Calls the SAME resolver and compositor a full run calls. That is the whole
    reason this is trustworthy: if the sample were rendered by a shortcut path
    it would be a picture of what the pipeline might do, and adopting it would
    be a guess. Here, adopting it produces exactly this.

    `dataclasses.replace` rather than mutating: Product is frozen, and the
    brief on disk must not acquire a surface somebody was only auditioning.
    """
    from dataclasses import replace

    from pipeline.assets import AssetResolver
    from pipeline.brief import Variant, stable_seed
    from pipeline.compose import Composer
    from pipeline.checks import compliance_score, evaluate
    from pipeline.providers import default_provider, get_provider
    from pipeline.runner import load_brand

    trial = replace(product, surface=surface)
    variant = Variant(trial, market, ratio)

    name = provider_name or default_provider()
    provider = get_provider(name)
    brand = load_brand("brandkit/brand.yaml")

    out_dir = os.path.join(ROOT, ".cache", "samples")
    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%H%M%S")
    out_path = os.path.join(out_dir, f"{trial.id}-{market.locale}-"
                                     f"{ratio.slug}-{stamp}.jpg")

    t0 = time.monotonic()
    resolver = AssetResolver(provider, cache_dir=os.path.join(ROOT, ".cache", "masters"))
    master = resolver.resolve(trial, stable_seed(brief.campaign_id, trial.id, surface))
    comp = Composer(brand).compose(master.path, variant, out_path)
    res = evaluate(comp, variant, brand, brief.prohibited_terms)

    return {
        "ok": True,
        "path": os.path.relpath(out_path, ROOT).replace("\\", "/"),
        "product": trial.id,
        "locale": market.locale,
        "ratio": ratio.id,
        "surface": surface,
        "origin": master.origin,
        "provider": getattr(provider, "name", name),
        "model": master.model or getattr(provider, "model", ""),
        "verdict": res.verdict.value,
        "score": compliance_score(res.findings),
        "findings": [f.as_dict() for f in res.findings],
        "seconds": round(time.monotonic() - t0, 1),
    }

# --------------------------------------------------------------------------
# The social content engine
#
# Kept alongside the pipeline run rather than replacing it: the CLI run is the
# thing a scheduler calls, and the engine is a campaign planner that happens
# to use it. They share STATE and the same "one run at a time" rule, because
# they compete for the same provider quota and the same output folder.
# --------------------------------------------------------------------------

# `products` is keyed by product id and holds {plan, summary} for each one
# that has been run, because the UI gives every product its own master tab and
# those tabs have to survive the next product being processed.
ENGINE: dict = {"running": False, "lines": [], "products": {}, "order": [],
                "current": "", "error": None, "events": [], "seq": 0}
ENGINE_LOCK = threading.Lock()


def _engine_event(rec: dict) -> None:
    """Collected for the UI, and trimmed so a long run cannot grow forever."""
    with ENGINE_LOCK:
        ENGINE["seq"] += 1
        rec = dict(rec, seq=ENGINE["seq"])
        ENGINE["events"].append(rec)
        if len(ENGINE["events"]) > 4000:
            del ENGINE["events"][:1000]
        line = _engine_line(rec)
        if line:
            ENGINE["lines"].append(line)


def _engine_line(rec: dict) -> str:
    ev = rec.get("event")
    if ev == "discover":
        return f"discover  {rec['locale']:6} {rec['channel']}"
    if ev == "discovered":
        tag = "synthetic" if rec.get("synthetic") else rec.get("backend", "")
        cache = " (cached)" if rec.get("cached") else ""
        return (f"found     {rec['locale']:6} {rec['channel']:10} "
                f"{rec['count']} look-alikes via {tag}{cache}")
    if ev == "strategy":
        return (f"strategy  {rec['locale']:6} {rec['slots']} slots, "
                f"{rec['calls']} generative call(s)")
    if ev == "master":
        return f"master    {rec['locale']:6} {rec['channel']:10} {rec['surface'][:44]}"
    if ev == "mastered":
        return f"          {rec['locale']:6} {rec['channel']:10} {rec['origin']}"
    if ev == "slot":
        v = (rec.get("verdict") or "").upper()
        vid = " +mp4" if rec.get("video") else ""
        return (f"{v:8}  {rec['date']} {rec['channel']:10} "
                f"{rec['kind']:5} {rec['ratio']:5} {rec.get('score', 0):3}{vid}")
    if ev == "slot_error":
        return f"ERROR     {rec.get('slot','')} {rec.get('error','')}"
    return ""


def _stage_brief(text: str, name: str) -> str:
    """Write on-screen YAML to a scratch file, exactly as /api/run does.

    Same reasoning: the engine must plan what is on screen without rewriting
    the user's brief through the YAML emitter, which strips its comments.
    """
    scratch_dir = os.path.join(ROOT, ".cache")
    os.makedirs(scratch_dir, exist_ok=True)
    p = os.path.join(scratch_dir, f"engine-{os.path.basename(name) or 'brief.yaml'}")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)
    return p


def _engine_history(brief) -> dict:
    """Our own channel performance, folded in as evidence.

    This is what the Analytics tab used to show on its own. It is not a
    destination any more -- it is one of the two inputs the strategy cites,
    which is what it was always actually for.
    """
    out: dict[str, dict] = {}
    for m in brief.markets:
        per_channel: dict[str, dict] = {}
        for ch in CHANNELS:
            # Derived from CHANNELS, never a second hand-written list -- two
            # lists of channels drift, and the one that drifts is always the
            # one nobody is looking at.
            #
            # insights reports GA/facebook/tiktok/youtube, so Instagram's
            # history rides on the facebook feed. That is not a fudge: Meta
            # reports the two together, which is exactly why the mapping is
            # needed and why it is written down here rather than assumed.
            key = "facebook" if ch == "instagram" else ch
            if key not in insights.CHANNEL_IDS:
                continue
            try:
                cal = insights.calendar(m.locale, key, ROOT)
                ext = insights.external(m.locale, key)
                sug = insights.suggest(m.locale, key, ROOT)
            except Exception:                                # noqa: BLE001
                continue
            best_ratio, best_er = "", 0.0
            by_ratio: dict[str, list] = {}
            for p in cal["posts"]:
                by_ratio.setdefault(p["ratio"], []).append(p)
            for r, posts in by_ratio.items():
                tot = sum(x["impressions"] for x in posts) or 1
                er = sum(x["engagement_rate"] * x["impressions"] for x in posts) / tot
                if er > best_er:
                    best_ratio, best_er = r, round(er, 2)
            per_channel[ch] = {
                "synthetic": True,
                "best_treatment": sug.get("surface", ""),
                "best_ratio": best_ratio,
                "best_ratio_er": best_er,
                "posts": cal["totals"]["posts"],
                "engagement_rate": cal["totals"]["engagement_rate"],
                "top_term": ext.get("top_term", ""),
                "virality": ext.get("virality", 0),
            }
        out[m.locale] = per_channel
    return out


def _do_engine(path: str, product_ids: list, days: int, ipd: int, vpd: int,
               start: str, backend: str, provider: str,
               render_video: bool) -> None:
    """Plan then run, per product, on a worker thread.

    Products are processed one after another and each is published to
    ENGINE["products"] the moment it finishes, so its master tab appears while
    the next one is still working. Batching them all to the end would mean
    staring at a log for several minutes with nothing to look at.

    One product failing does not take the others with it. A brief with four
    products and one bad asset path should give you three campaigns and one
    clear error, not nothing.
    """
    try:
        brief = load_brief(path)
        history = _engine_history(brief)
        for pid in product_ids:
            with ENGINE_LOCK:
                ENGINE["current"] = pid
            _engine_event({"event": "begin", "product": pid,
                           "days": days, "ipd": ipd, "vpd": vpd})
            try:
                planned = engine.plan(
                    brief, pid, days=days, images_per_day=ipd,
                    videos_per_day=vpd, root=ROOT, start=start,
                    backend=backend, history=history, on_event=_engine_event)
                with ENGINE_LOCK:
                    ENGINE["products"][pid] = {"plan": planned, "summary": None}
                    if pid not in ENGINE["order"]:
                        ENGINE["order"].append(pid)
                summary = engine.run(
                    brief, planned,
                    out_root=os.path.join(ROOT, "output", brief.campaign_id),
                    root=ROOT, provider_name=provider,
                    render_video=render_video, on_event=_engine_event)
                with ENGINE_LOCK:
                    ENGINE["products"][pid] = {"plan": planned,
                                               "summary": summary}
            except Exception as exc:                         # noqa: BLE001
                traceback.print_exc()
                msg = f"{type(exc).__name__}: {exc}"
                _engine_event({"event": "slot_error", "slot": pid, "error": msg})
                with ENGINE_LOCK:
                    ENGINE["products"].setdefault(pid, {})["error"] = msg
                    if pid not in ENGINE["order"]:
                        ENGINE["order"].append(pid)
    except Exception as exc:                                 # noqa: BLE001
        traceback.print_exc()
        with ENGINE_LOCK:
            ENGINE["error"] = f"{type(exc).__name__}: {exc}"
            ENGINE["lines"].append(f"ERROR     {ENGINE['error']}")
    finally:
        with ENGINE_LOCK:
            ENGINE["running"] = False
            ENGINE["current"] = ""
        with LOCK:
            STATE["running"] = False



def _do_run(brief_path: str, provider: str, regen: bool = False,
            storage: str = "", model: str = "") -> None:
    """Executed on a worker thread so the browser can poll for progress."""
    try:
        _emit(f"starting  brief={os.path.basename(brief_path)}  provider={provider}")
        summary = run_campaign(brief_path, provider_name=provider, quiet=True,
                               on_event=_on_event, force_generate=regen,
                               storage_name=storage, model=model)
        report = write_report(summary, summary.output_dir)
        c = summary.counts or {}
        _emit(f"masters   generated={summary.generative_calls} "
              f"reused-from-disk={summary.reused_from_brief} "
              f"reused-from-cache={summary.reused_from_cache}")
        for r in summary.results:
            _emit(f"  {r.verdict.upper():<6} {r.product_id} · {r.locale} · {r.ratio}")
        _emit(f"done      {summary.variants_planned} creatives · "
              f"pass {c.get('pass',0)} / review {c.get('review',0)} / block {c.get('block',0)}")
        # The one link worth copying: the manifest lists every creative and
        # its own URL, so handing somebody this is handing them the whole run.
        st = summary.storage or {}
        if st.get("objects"):
            _emit(f"mirrored  {st['objects']} objects to {st['backend']} "
                  f"({st['bytes'] // 1024} KB)")
        if st.get("share_url"):
            _emit(f"share     {st['share_url']}")
            if not st.get("public"):
                _emit("          (signed link -- expires; run tools/make_public.py "
                      "for permanent links)")
        with LOCK:
            STATE["summary"] = json.loads(json.dumps(summary, default=lambda o: o.__dict__))
            STATE["report"] = os.path.relpath(report, ROOT).replace("\\", "/")
    except Exception as exc:                                      # noqa: BLE001
        _emit(f"ERROR     {type(exc).__name__}: {exc}")
        with LOCK:
            STATE["error"] = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
    finally:
        with LOCK:
            STATE["running"] = False


# --------------------------------------------------------------------------

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def log_message(self, *_a):                                    # quiet console
        pass

    # -- helpers ----------------------------------------------------------

    def _json(self, payload, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    @staticmethod
    def _safe(rel: str) -> str | None:
        """Resolve a path and refuse anything outside the project folder.

        This binds to localhost only, but path traversal is one line to
        prevent and embarrassing to be missing from a file-serving demo.
        """
        p = os.path.abspath(os.path.join(ROOT, rel.lstrip("/\\")))
        return p if p.startswith(ROOT + os.sep) or p == ROOT else None

    # -- GET --------------------------------------------------------------

    def do_GET(self):                                              # noqa: N802
        route = urlparse(self.path).path

        if route in ("/", "/index.html"):
            self.path = "/webui/index.html"
            return SimpleHTTPRequestHandler.do_GET(self)

        if route == "/api/init":
            briefs = sorted(os.path.relpath(p, ROOT).replace("\\", "/")
                            for p in glob.glob(os.path.join(ROOT, "campaigns", "*.yaml")))
            # Which brief opens by default.
            #
            # It used to be "whichever sorts first", which is not a decision --
            # it just happened to be alphabetical, so dropping a scratch file
            # called `aurora-experiment.yaml` into campaigns/ silently made
            # THAT the thing the app loads, and "press Run" stopped meaning
            # what the README says it means. The shipped sample brief is named
            # explicitly and everything else is a fallback.
            default_brief = ("campaigns/aurora-spring.yaml"
                             if "campaigns/aurora-spring.yaml" in briefs
                             else (briefs[0] if briefs else ""))
            return self._json({"version": APP_VERSION,
                               # The page only sends heartbeats when the
                               # server owns the window. In a browser tab
                               # nobody asked us to manage the lifetime, so
                               # closing the tab must not stop the server.
                               "app_window": APP_MODE["on"],
                               "ping_every": PING_EVERY,
                               "briefs": briefs,
                               "default_brief": default_brief,
                               "providers": provider_status(),
                               "default_provider": default_provider(),
                               "storages": storage_status(),
                               "stale": _is_stale(),
                               "cwd": ROOT})

        if route == "/api/engine/status":
            """What the engine can do on THIS machine, before anyone starts."""
            return self._json({
                "channels": [{"id": c, "name": CHANNEL_NAMES.get(c, c)}
                             for c in CHANNELS],
                "discovery": discovery_status(),
                "default_discovery": _default_discovery(),
                "ffmpeg": motion.available(),
                "ffmpeg_note": motion.why_unavailable(),
                "running": ENGINE["running"],
            })

        if route == "/api/engine/progress":
            with ENGINE_LOCK:
                return self._json({
                    "running": ENGINE["running"],
                    "lines": ENGINE["lines"][-400:],
                    "error": ENGINE["error"],
                    "current": ENGINE["current"],
                    "order": list(ENGINE["order"]),
                    "products": ENGINE["products"],
                    "seq": ENGINE["seq"],
                })

        if route == "/api/insights":
            """One channel's history for one market, plus what it suggests.

            Marked `synthetic: true` in the payload and labelled as sample
            data everywhere it is drawn. Nothing here reaches Google, Meta,
            TikTok or YouTube -- each channel carries the name of the API a
            real integration would call, and pipeline/insights.py says what
            that would take.
            """
            q = dict(parse_qsl(urlparse(self.path).query))
            p = self._safe(q.get("path", ""))
            if not p or not os.path.isfile(p):
                return self._json({"error": "not found"}, 404)
            try:
                b = load_brief(p)
            except BriefError as exc:
                return self._json({"error": str(exc)}, 200)

            channel = q.get("channel") or insights.CHANNEL_IDS[0]
            if channel not in insights.CHANNEL_IDS:
                return self._json({"error": f"unknown channel '{channel}'"}, 400)
            # One channel at a time. Building all four for every market up
            # front is four times the work for a tab you can only look at one
            # of, and it made the payload big enough to feel slow.
            locales = [m.locale for m in b.markets]
            locale = q.get("locale") or (locales[0] if locales else "en-US")
            return self._json({
                "synthetic": True,
                "channels": insights.CHANNELS,
                "channel": channel,
                "locales": locales,
                "locale": locale,
                "ratios": [r.id for r in b.ratios],
                "report": insights.report(locale, channel, ROOT),
            })

        if route == "/api/sample":
            """Render ONE creative from a suggested surface, and nothing else.

            The point of the Analytics tab is a prompt you can act on, and
            "act on" has to mean *see it* before you commit. A full run is
            eighteen deliverables and two paid model calls; this is one of
            each, so trying a suggestion costs about what looking at it is
            worth.

            It writes to .cache/samples/ rather than output/, because a sample
            is not a deliverable and must never turn up in the folder a
            reviewer is told holds the campaign.
            """
            q = dict(parse_qsl(urlparse(self.path).query))
            p = self._safe(q.get("path", ""))
            if not p or not os.path.isfile(p):
                return self._json({"error": "not found"}, 404)
            surface = (q.get("surface") or "").strip()
            if not surface:
                return self._json({"error": "no surface prompt given"}, 400)
            try:
                b = load_brief(p)
            except BriefError as exc:
                return self._json({"error": str(exc)}, 200)
            if not b.products or not b.markets or not b.ratios:
                return self._json({"error": "brief has nothing to render"}, 400)

            locale = q.get("locale") or b.markets[0].locale
            market = next((m for m in b.markets if m.locale == locale), b.markets[0])
            ratio_id = q.get("ratio") or b.ratios[0].id
            ratio = next((r for r in b.ratios if r.id == ratio_id), b.ratios[0])
            product = b.products[0]

            try:
                return self._json(_render_sample(b, product, market, ratio,
                                                 surface,
                                                 q.get("provider", "")))
            except Exception as exc:                              # noqa: BLE001
                # Surfaced rather than logged: this is a button somebody just
                # pressed, and "nothing happened" is the worst answer.
                traceback.print_exc()
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 200)

        if route == "/api/assets":
            """Every product image already uploaded, for reuse.

            "Reuse them when available" is the brief's requirement, and reuse
            only happens if somebody can FIND the asset. A path typed from
            memory is not findable; a grid of thumbnails is. Dimensions come
            back too, because whether a source shot is 1024 square or a 300px
            thumbnail changes what the pipeline can do with it.
            """
            out = []
            for full in sorted(glob.glob(os.path.join(ASSET_DIR, "*"))):
                if not os.path.isfile(full):
                    continue
                if os.path.splitext(full)[1].lower() not in ASSET_EXTS:
                    continue
                rel = os.path.relpath(full, ROOT).replace("\\", "/")
                item = {"path": rel, "name": os.path.basename(full),
                        "bytes": os.path.getsize(full)}
                try:
                    with Image.open(full) as im:
                        item["width"], item["height"] = im.size
                except Exception:                                # noqa: BLE001
                    # Listed anyway, flagged. A file that will not open is
                    # exactly what somebody needs to be told about -- silently
                    # hiding it turns "my image vanished" into a mystery.
                    item["broken"] = True
                out.append(item)
            return self._json({"assets": out, "dir": ASSET_DIR})

        if route == "/appicon.png":
            """The app icon, for the tab and the window.

            Its own route rather than relying on static serving, because the
            favicon is requested before anything else on the page and must not
            depend on what the server's working directory happens to be.
            """
            p = os.path.join(ROOT, "webui", "appicon.png")
            if not os.path.isfile(p):
                self.send_error(404)
                return
            data = open(p, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(data)
            return

        if route == "/api/ping":
            """The page saying it is still on screen.

            Only meaningful in app-window mode, where the server owns the
            window's lifetime. Cheap on purpose -- it runs every few seconds
            and must never be the reason anything is slow.
            """
            HEARTBEAT["seen"] = True
            HEARTBEAT["last"] = time.time()
            return self._json({"ok": True})

        if route == "/api/whoami":
            """Who is answering on this port?

            Asked by a SECOND copy of this app when its bind fails. Identity
            is the whole point: "something is listening on 8765" is not a
            licence to stop it -- it might be anything. This says what program
            it is and which working copy it was started from, so the newcomer
            can decide whether taking over is safe or rude.
            """
            return self._json({"app": APP_ID, "root": ROOT, "pid": os.getpid()})

        if route == "/api/brief":
            rel = unquote(urlparse(self.path).query.split("path=", 1)[-1])
            p = self._safe(rel)
            if not p or not os.path.isfile(p):
                return self._json({"error": "not found"}, 404)
            with open(p, "r", encoding="utf-8") as fh:
                text = fh.read()
            # The form is built from `data`, parsed HERE with the same PyYAML
            # the pipeline uses. Parsing it a second time in the browser would
            # mean two parsers that can disagree about what the brief says --
            # and the one the user is editing would be the one that is wrong.
            # `data` is null when the file will not parse; the UI then keeps
            # the user in the raw YAML tab, which is the only honest place to
            # fix a syntax error.
            try:
                data = yaml.safe_load(text) or {}
                err = None
            except yaml.YAMLError as exc:
                data, err = None, str(exc)
            return self._json({"path": rel, "text": text, "data": data, "parse_error": err})

        if route == "/api/models":
            """The image models this account can actually run.

            Asked live rather than hardcoded: flux-2 and lucid-origin both
            appeared after this adapter was written, and a menu baked into the
            source is wrong the week the vendor ships something.
            """
            # Per provider, because "which models exist" is a question only
            # each vendor can answer about itself.
            which = (unquote(urlparse(self.path).query.split("provider=", 1)[-1])
                     if "provider=" in self.path else "cloudflare")
            if which == "gemini":
                from pipeline.providers.gemini import (DEFAULT_MODEL,
                                                       list_image_models)
                env_key = "GEMINI_IMAGE_MODEL"
            else:
                from pipeline.providers.cloudflare import (DEFAULT_MODEL,
                                                           list_image_models)
                env_key = "CLOUDFLARE_IMAGE_MODEL"
            return self._json({"provider": which,
                               "models": list_image_models(),
                               "default": DEFAULT_MODEL,
                               "current": os.environ.get(env_key, "")})

        if route == "/api/assetcheck":
            """Does this product's asset actually exist on disk?

            The form needs to know, because an asset that is really there
            short-circuits the resolver before the prompt is built -- so the
            subject and surface fields become inert, and the only thing worse
            than a field that does nothing is a field that does nothing
            silently. Only the browser can ask; only the server can answer.
            """
            rel = unquote(urlparse(self.path).query.split("path=", 1)[-1])
            full = self._safe(rel)
            return self._json({"path": rel,
                               "exists": bool(full and os.path.isfile(full))})

        if route == "/api/signed":
            """A link that opens one stored object, for a few minutes.

            The bucket is private and stays private -- this signs a URL rather
            than loosening anything. Restricted to keys under runs/ so it can
            only ever hand out a link to something this pipeline produced,
            never to an arbitrary object that happens to share the bucket.
            """
            key = unquote(urlparse(self.path).query.split("key=", 1)[-1])
            # Both layouts this pipeline has ever written. `public/` is where
            # runs go now; `runs/` is the older prefix, kept so a manifest
            # from an earlier run still resolves. Anything else is refused --
            # this endpoint must never become a way to sign an arbitrary
            # object that happens to share the bucket.
            if not key.startswith(("public/", "runs/")) or ".." in key:
                return self._json({"error": "only run artifacts can be signed"}, 400)
            try:
                st = get_storage("s3")
                return self._json({"url": st.presigned_url(key, 900),
                                   "expires_s": 900, "uri": st.uri(key)})
            except StorageError as exc:
                return self._json({"error": str(exc)}, 200)

        if route == "/api/progress":
            with LOCK:
                return self._json({"running": STATE["running"],
                                   "lines": STATE["lines"],
                                   "landed": STATE["landed"],
                                   "stages": STATE["stages"],
                                   "stage_labels": STAGE_LABELS,
                                   "stale": _is_stale(),
                                   "graph": STATE["graph"],
                                   "summary": STATE["summary"],
                                   "report": STATE["report"],
                                   "error": STATE["error"]})

        if route.startswith("/out/"):
            p = self._safe(route[len("/out/"):])
            if not p or not os.path.isfile(p):
                self.send_error(404)
                return
            ctype = mimetypes.guess_type(p)[0] or "application/octet-stream"
            data = open(p, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return

        return SimpleHTTPRequestHandler.do_GET(self)

    # -- POST -------------------------------------------------------------

    def do_POST(self):                                             # noqa: N802
        route = urlparse(self.path).path

        if route == "/api/bye":
            """The window is going away -- sent by navigator.sendBeacon.

            Handled before the JSON body parse because a beacon is not JSON.

            It does NOT shut anything down. `pagehide` fires on an ordinary
            reload too, and a reload that kills the server it is reloading
            from is a spectacular way to break the app. All this does is
            expire the heartbeat, so the watchdog's normal timeout arrives in
            a couple of seconds instead of twenty -- and a reload's new page
            checks in well inside that and cancels it.
            """
            HEARTBEAT["last"] = min(HEARTBEAT["last"],
                                    time.time() - CLOSE_AFTER + 3)
            return self._json({"ok": True})

        try:
            body = self._body()
        except Exception:
            return self._json({"error": "bad json"}, 400)

        if route == "/api/save":
            p = self._safe(body.get("path", ""))
            if not p:
                return self._json({"error": "bad path"}, 400)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(body.get("text", ""))
            return self._json({"ok": True})

        if route == "/api/credentials":
            """Write provider credentials into .env.

            Three things make this defensible rather than reckless, and all
            three are load-bearing:

              * the server binds 127.0.0.1 only, so nothing off this machine
                can reach it;
              * the key names are checked against CREDENTIALS, so this writes
                the four variables the adapters read and nothing else -- a
                request naming PATH or AWS_SECRET_ACCESS_KEY is refused;
              * a value goes IN and never comes back out. No route returns a
                credential, and the UI only ever learns "configured: true".

            .env is gitignored, which is checked by a test.
            """
            name = str(body.get("provider", ""))
            # One panel for both kinds of credential. A storage backend needs
            # keys for exactly the same reason a model provider does, and
            # having two places to type them in would be silly.
            allowed = CREDENTIALS.get(name) or STORAGE_CREDENTIALS.get(name)
            if not allowed:
                return self._json({"error": f"unknown provider '{name}'"}, 400)
            values = body.get("values") or {}
            unknown = [k for k in values if k not in allowed]
            if unknown:
                return self._json(
                    {"error": f"{name} does not use: {', '.join(sorted(unknown))}"}, 400)

            clean = {k: str(v).strip() for k, v in values.items() if str(v).strip()}
            if not clean:
                return self._json({"error": "nothing to save"}, 400)
            try:
                _write_env(clean)
            except OSError as exc:
                return self._json({"error": f"could not write .env: {exc}"}, 500)
            os.environ.update(clean)          # live, so no restart is needed
            return self._json({"ok": True, "providers": provider_status(),
                               "storages": storage_status(),
                               "default_provider": default_provider()})

        if route == "/api/upload":
            """Take a product image from the browser and put it on disk.

            **Base64 in JSON, not multipart.** Multipart would mean
            hand-parsing boundaries in a stdlib HTTP handler (`cgi` is
            deprecated and gone in 3.13), and a subtly wrong parser corrupts
            binary payloads in ways that surface much later as "the image
            looks funny". Base64 costs 33% on the wire for files that are a
            megabyte or two, over localhost. That is a good trade for a local
            tool, and it is the sort of trade worth making explicitly.

            The order of checks below is the design: cheap and cheerful first,
            then the one that actually proves anything.
            """
            raw = str(body.get("name") or "").strip()
            data_b64 = str(body.get("data") or "")
            if not raw or not data_b64:
                return self._json({"error": "need a name and file data"}, 400)

            # 1. Name. Basename only, so "../../.env" cannot escape, and a
            #    conservative character set so nothing downstream has to worry
            #    about quoting a filename in a path, a URL or a YAML string.
            stem, ext = os.path.splitext(os.path.basename(raw))
            ext = ext.lower()
            if ext not in ASSET_EXTS:
                return self._json(
                    {"error": f"{ext or 'that'} is not a supported image type "
                              f"({', '.join(sorted(ASSET_EXTS))})"}, 400)
            stem = re.sub(r"[^a-zA-Z0-9._-]+", "-", stem).strip("-.") or "asset"

            # 2. Size, checked BEFORE decoding. Decoding first would mean
            #    allocating whatever was sent in order to find out it was too
            #    big, which is the wrong way round.
            if len(data_b64) > MAX_UPLOAD_B64:
                return self._json(
                    # ASCII hyphen, not an em dash. This string can end up on a
                    # Windows console, where cp1252 turns a stray em dash into
                    # mojibake -- an error message that is itself broken is a
                    # bad way to tell somebody their file is too big.
                    {"error": f"too large - the limit is {MAX_UPLOAD_MB} MB"}, 413)
            try:
                blob = base64.b64decode(data_b64, validate=True)
            except Exception:                                    # noqa: BLE001
                return self._json({"error": "the upload was not valid base64"}, 400)

            # 3. Is it REALLY an image? The extension is a claim and the MIME
            #    type the browser sent is a claim; only decoding it is
            #    evidence. Everything downstream -- the thumbnail, the
            #    resolver, the compositor -- assumes Pillow can open this, so
            #    the moment to find out is now, not three stages later.
            try:
                with Image.open(io.BytesIO(blob)) as probe:
                    probe.verify()
                with Image.open(io.BytesIO(blob)) as probe:
                    width, height = probe.size
                    fmt = probe.format
            except Exception:                                    # noqa: BLE001
                return self._json(
                    {"error": "that file is not an image Pillow can read"}, 400)

            os.makedirs(ASSET_DIR, exist_ok=True)
            # 4. Never silently overwrite. Two people photographing two
            #    products both end up with "product.png", and losing the first
            #    one to the second is a data-loss bug wearing a convenience
            #    costume.
            dest = os.path.join(ASSET_DIR, stem + ext)
            n = 2
            while os.path.exists(dest):
                dest = os.path.join(ASSET_DIR, f"{stem}-{n}{ext}")
                n += 1

            tmp = dest + ".part"
            try:
                with open(tmp, "wb") as fh:
                    fh.write(blob)
                os.replace(tmp, dest)        # atomic; never a half-written file
            except OSError as exc:
                return self._json({"error": f"could not save: {exc}"}, 500)

            rel = os.path.relpath(dest, ROOT).replace("\\", "/")

            # 5. Mirror the INPUT to object storage too, when it is
            #    configured. The brief asks for storage of "generated or
            #    transient assets" -- an uploaded hero shot is the input that
            #    every deliverable descends from, so a bucket holding the
            #    outputs but not the source it came from is only half an
            #    archive. Private prefix, not the shared one: this is somebody
            #    else's product photography, not a deliverable to hand out.
            stored = ""
            try:
                st = get_storage(default_storage())
                if st.name != "local":
                    obj = st.put(f"assets/{os.path.basename(dest)}", blob,
                                 mimetypes.guess_type(dest)[0] or "image/png")
                    stored = obj.uri
            except (StorageError, Exception):                    # noqa: BLE001
                # An upload that reached disk succeeded. The mirror is a bonus
                # and must never turn a working upload into a failed one.
                stored = ""

            return self._json({"ok": True, "path": rel,
                               "name": os.path.basename(dest),
                               "width": width, "height": height,
                               "format": fmt, "bytes": len(blob),
                               "stored_uri": stored})

        if route == "/api/shutdown":
            """Stand down so a newly started copy can take the port.

            Three things make this safe enough for a local demo tool:

            * the socket is bound to 127.0.0.1, so nothing off this machine
              can reach it;
            * the caller must quote our own ROOT back to us, which a different
              working copy cannot do by accident -- two checkouts on one
              machine should NOT silently stop each other, they should be told
              to use --port;
            * it refuses while a run is in progress, because killing a server
              mid-run abandons a half-written output folder and, worse, throws
              away generative calls that have already been paid for.

            This is what turns "port already in use, here is a taskkill
            incantation" into the new instance simply starting.
            """
            if str(body.get("root", "")) != ROOT:
                return self._json({"error": "different working copy; refusing"}, 403)
            with LOCK:
                if STATE["running"]:
                    return self._json({"error": "a run is in progress"}, 409)
            # Reply FIRST, then stop. shutdown() blocks until the serve loop
            # exits, and the serve loop cannot exit until this handler returns
            # -- calling it inline would deadlock the process it is trying to
            # close. A thread lets this response finish first.
            self._json({"ok": True, "pid": os.getpid()})
            if SERVER is not None:
                threading.Thread(target=SERVER.shutdown, daemon=True).start()
            return None

        if route == "/api/plan":
            p = self._safe(body.get("path", ""))
            if not p:
                return self._json({"error": "bad path"}, 400)
            try:
                b = load_brief(p)
            except BriefError as exc:
                return self._json({"error": str(exc)}, 200)
            return self._json({
                "campaign": b.campaign_id,
                # `reuse` means "costs no generative call", which is only true
                # for a photo used exactly as shot. A resurfaced product also
                # has an asset on disk and is very much not free.
                "products": [{"id": x.id,
                              "reuse": x.uses_source_photo() and not x.regenerate_surface,
                              "mode": ("generated" if not x.uses_source_photo()
                                       else "resurfaced" if x.regenerate_surface
                                       else "as-shot")}
                             for x in b.products],
                "markets": [m.locale for m in b.markets],
                "ratios": [r.id for r in b.ratios],
                "deliverables": b.variant_count,
                "generative": b.generation_count,
                "preflight": [f.as_dict() for f in preflight_brief(b)],
            })

        if route == "/api/engine/validate":
            """Does this pasted YAML parse, and what is in it?

            Validated by the SAME loader a run uses, against a scratch file
            written the same way. "It parsed here" therefore means exactly
            what it will mean when Run is pressed -- a second, more lenient
            check in the browser would be a way to promise something the run
            then refuses.
            """
            text = body.get("text") or ""
            if not text.strip():
                return self._json({"error": "nothing pasted"}, 200)
            try:
                p = _stage_brief(text, "pasted.yaml")
                b = load_brief(p)
            except BriefError as exc:
                return self._json({"error": str(exc)}, 200)
            except OSError as exc:
                return self._json({"error": f"could not stage: {exc}"}, 200)
            return self._json({"path": os.path.relpath(p, ROOT).replace("\\", "/"),
                               "data": {
                                   "campaign": {"id": b.campaign_id,
                                                "name": b.campaign_name,
                                                "brand": b.brand},
                                   "products": [{"id": x.id, "name": x.name,
                                                 "asset": x.asset,
                                                 "surface": x.surface}
                                                for x in b.products],
                                   "markets": [{"locale": m.locale,
                                                "region": m.region,
                                                "audience": m.audience,
                                                "message": m.message}
                                               for m in b.markets],
                                   "aspect_ratios": [{"id": r.id} for r in b.ratios],
                               }})

        if route == "/api/engine/run":
            """Plan and execute a channel campaign for one product.

            One at a time, and it takes the SAME lock the pipeline run uses:
            both spend the same provider quota and write into the same output
            folder, so 'a run is already in progress' has to mean either.
            """
            with LOCK:
                if STATE["running"] or ENGINE["running"]:
                    return self._json({"error": "a run is already in progress"}, 409)
                STATE["running"] = True
            with ENGINE_LOCK:
                ENGINE.update(running=True, lines=[], products={}, order=[],
                              current="", error=None, events=[], seq=0)

            p = self._safe(body.get("path", ""))
            text = body.get("text")
            if isinstance(text, str) and text.strip():
                try:
                    p = _stage_brief(text, body.get("path", "brief.yaml"))
                except OSError as exc:
                    with LOCK:
                        STATE["running"] = False
                    with ENGINE_LOCK:
                        ENGINE["running"] = False
                    return self._json({"error": f"could not stage brief: {exc}"}, 200)
            if not p or not os.path.isfile(p):
                with LOCK:
                    STATE["running"] = False
                with ENGINE_LOCK:
                    ENGINE["running"] = False
                return self._json({"error": "bad path"}, 400)

            def _int(key, default, lo, hi):
                try:
                    return max(lo, min(hi, int(body.get(key, default))))
                except (TypeError, ValueError):
                    return default

            # Bounded, because the product of these three is how many files
            # get written, and an unbounded number typed into a form is how a
            # demo fills a disk.
            days = _int("days", 7, 1, 90)
            ipd = _int("images_per_day", 2, 0, 12)
            vpd = _int("videos_per_day", 1, 0, 12)
            if days * (ipd + vpd) == 0:
                with LOCK:
                    STATE["running"] = False
                with ENGINE_LOCK:
                    ENGINE["running"] = False
                return self._json({"error": "that schedule produces nothing"}, 400)

            # One product or several. "Every product in the brief" is the
            # normal case for a campaign, and making that a four-click loop
            # would be the wrong default.
            wanted = body.get("products")
            if not isinstance(wanted, list) or not wanted:
                one = str(body.get("product", "")).strip()
                wanted = [one] if one else []
            try:
                known = {pr.id for pr in load_brief(p).products}
            except BriefError as exc:
                with LOCK:
                    STATE["running"] = False
                with ENGINE_LOCK:
                    ENGINE["running"] = False
                return self._json({"error": str(exc)}, 200)
            wanted = [str(x) for x in wanted if str(x) in known]
            if not wanted:
                with LOCK:
                    STATE["running"] = False
                with ENGINE_LOCK:
                    ENGINE["running"] = False
                return self._json({"error": "no known product selected"}, 400)

            threading.Thread(target=_do_engine, kwargs=dict(
                path=p, product_ids=wanted,
                days=days, ipd=ipd, vpd=vpd,
                start=str(body.get("start", "")),
                backend=str(body.get("discovery", "")),
                provider=str(body.get("provider", "")),
                render_video=bool(body.get("video", True)),
            ), daemon=True).start()
            return self._json({"ok": True, "days": days,
                               "images_per_day": ipd, "videos_per_day": vpd})

        if route == "/api/run":
            with LOCK:
                if STATE["running"]:
                    return self._json({"error": "a run is already in progress"}, 409)
                STATE.update(running=True, lines=[], summary=None, error=None,
                             report=None, graph={}, seq=0, landed=[],
                             stages={})
            p = self._safe(body.get("path", ""))
            if not p:
                with LOCK:
                    STATE["running"] = False
                return self._json({"error": "bad path"}, 400)

            # Run what is ON SCREEN, without writing to the user's brief.
            #
            # Run used to call Save first, so pressing Run rewrote the brief
            # file from the form's YAML emitter. That works, and it also
            # silently destroys every comment in the file -- and the sample
            # brief's comments are documentation a reviewer reads. It happened:
            # aurora-spring.yaml lost all 30 lines of them and had to be
            # restored from git.
            #
            # Editing a prompt and pressing Run must still use the edit, so the
            # text comes with the request and is written to a scratch file
            # instead. Save stays what it always was: an explicit choice to
            # change the file on disk.
            #
            # Relative asset paths keep working because they resolve against
            # the process CWD, which is ROOT, not against the brief's own
            # location -- so it does not matter where the scratch file lives.
            text = body.get("text")
            if isinstance(text, str) and text.strip():
                try:
                    scratch_dir = os.path.join(ROOT, ".cache")
                    os.makedirs(scratch_dir, exist_ok=True)
                    scratch = os.path.join(
                        scratch_dir, "run-" + os.path.basename(p))
                    tmp = scratch + ".part"
                    with open(tmp, "w", encoding="utf-8") as fh:
                        fh.write(text)
                    os.replace(tmp, scratch)
                    p = scratch
                except OSError as exc:
                    with LOCK:
                        STATE["running"] = False
                    return self._json(
                        {"error": f"could not stage the brief for this run: {exc}"}, 500)

            threading.Thread(
                target=_do_run,
                args=(p, body.get("provider", "mock"), bool(body.get("regen")),
                      str(body.get("storage", "")), str(body.get("model", ""))),
                daemon=True).start()
            return self._json({"ok": True})

        return self._json({"error": "unknown route"}, 404)


def _write_env(values: dict[str, str], path: str = ".env") -> None:
    """Update KEY=VALUE lines in .env, leaving everything else alone.

    Rewritten in place rather than appended, so setting a key twice does not
    leave two lines where the last one silently wins. Comments and ordering
    survive, because that file is also documentation -- it is the first thing
    somebody reads when working out which variable a provider wants.
    """
    p = os.path.join(ROOT, path)
    lines = []
    if os.path.isfile(p):
        with open(p, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()

    remaining = dict(values)
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in remaining:
            lines[i] = f"{key}={remaining.pop(key)}"
    for key, val in remaining.items():                 # keys not already there
        lines.append(f"{key}={val}")

    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines).rstrip("\n") + "\n")
    os.replace(tmp, p)                                 # atomic; never a half file


class ExclusiveHTTPServer(ThreadingHTTPServer):
    """A server that REFUSES to start if the port is already being served.

    This exists because the loud port-clash message below was, for a while,
    unreachable on Windows -- and the bug it was written to prevent happened
    anyway, in the most confusing possible form.

    `HTTPServer` sets `allow_reuse_address = 1`, which on Unix means the
    sensible thing: reuse a port sitting in TIME_WAIT after a clean shutdown.
    On Windows `SO_REUSEADDR` means something quite different -- it permits
    binding a port another process is ACTIVELY LISTENING on. The second bind
    succeeds, no OSError is raised, the handler below never runs, and you end
    up with two live servers on one port. Connections then land on whichever
    the kernel picks, so the app answers from the new process sometimes and
    the old one other times.

    That is worse than the failure it was meant to replace. A dead new server
    is at least consistent; this is a server that is stale INTERMITTENTLY,
    which is indistinguishable from a flaky bug in whatever you just changed.
    Observed directly: two `python app.py` processes bound to 8765, and
    `/api/init` reporting `stale: true` from the older one while the newer
    one -- the one that had actually loaded the new code -- reported false.

    Two changes, because one is not enough:

    * `allow_reuse_address = False` stops asking for the behaviour at all.
    * `SO_EXCLUSIVEADDRUSE` is the Windows-specific opposite: it tells the OS
      that nothing else may bind this port while we hold it, which also stops
      a LATER process stealing it from us. It does not exist on Unix, hence
      the `hasattr` guard.
    """

    allow_reuse_address = False

    def server_bind(self):
        opt = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if opt is not None:
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, opt, 1)
            except OSError:
                # Not fatal: allow_reuse_address = False already produces the
                # failure we want. Better to serve than to refuse over a
                # socket option.
                pass
        super().server_bind()


def _ask_previous_copy_to_stand_down(port: int) -> str:
    """Try to free the port by asking, not by killing. Returns a status.

    "Kill whatever owns port 8765" is the obvious fix and it is wrong: the
    thing on that port might not be this app at all, and a tool that terminates
    unidentified processes on your machine to start itself is not a tool you
    should trust. So the newcomer asks first.

        GET  /api/whoami   -> is this the same program, from the same folder?
        POST /api/shutdown -> if so, please stop

    The old process closes its own socket. Nothing is killed, nothing is
    forced, and every refusal is a sentence rather than a stack trace.

    Returns one of:
        "freed"      the previous copy stood down
        "busy"       it is mid-run and declined -- do not interrupt a paid run
        "foreign"    something else owns the port, or another working copy
        "unreachable" nothing answered
    """
    base = f"http://{HOST}:{port}"
    try:
        with urlopen(f"{base}/api/whoami", timeout=2) as r:      # noqa: S310
            who = json.loads(r.read().decode("utf-8"))
    except Exception:                                            # noqa: BLE001
        return "unreachable"

    if who.get("app") != APP_ID:
        return "foreign"
    if who.get("root") != ROOT:
        return "foreign"

    req = Request(f"{base}/api/shutdown", method="POST",
                  data=json.dumps({"root": ROOT}).encode("utf-8"),
                  headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=5) as r:                       # noqa: S310
            if not json.loads(r.read().decode("utf-8")).get("ok"):
                return "foreign"
    except HTTPError as exc:
        return "busy" if exc.code == 409 else "foreign"
    except Exception:                                            # noqa: BLE001
        return "unreachable"

    # It said yes. Wait for the socket to actually close -- "shutdown
    # accepted" and "port available" are not the same instant, and binding
    # too eagerly just recreates the error we are trying to remove.
    for _ in range(40):                                          # up to ~8s
        time.sleep(0.2)
        probe = socket.socket()
        try:
            probe.settimeout(0.3)
            probe.connect((HOST, port))
        except OSError:
            return "freed"
        finally:
            probe.close()
    return "unreachable"

# --------------------------------------------------------------------------
# Opening it as an application rather than a browser tab
#
# A tab is the wrong frame for this. It puts an address bar, a bookmarks bar
# and eleven other tabs around a tool whose whole job is judging images, and
# it means the first thing an audience sees when you demo is your browsing
# history. Chromium's `--app=` mode gives a chromeless window with no tab
# strip and no omnibox, which is what "open it like Photoshop" actually means
# in a tool that has no business shipping Electron.
#
# The alternative was pywebview or Electron. Both were rejected for the same
# reason the rest of this repo has three dependencies: the README promises an
# install a reviewer can complete, and "pip install pywebview" fails
# differently on every platform (it wants pythonnet on Windows, PyGObject or
# Qt on Linux). This needs nothing that is not already on the machine, and
# degrades to a normal tab when no Chromium browser exists.
#
# `--user-data-dir` is not optional here. Without it, a Chrome that is
# already running just hands the URL to the existing process, which ignores
# --window-size and can open it as a tab anyway. With it, the window is its
# own process with its own state -- so it starts at the size asked for, and
# it carries none of the user's extensions, cookies or bookmarks into the
# screen share.
# --------------------------------------------------------------------------

APP_WINDOW = (1500, 960)
APP_PROFILE = os.path.join(ROOT, ".cache", "appwindow")

# Is the page still there? Set by /api/ping, which the page calls every
# few seconds when it was opened as an app window.
APP_MODE = {"on": False}
HEARTBEAT = {"seen": False, "last": 0.0}
PING_EVERY = 4      # seconds; the page's interval
CLOSE_AFTER = 20    # seconds of silence before the window is presumed shut
ARM_WITHIN = 120    # if nothing ever checks in, stop watching entirely



def _chromium_binaries() -> list[str]:
    """Every Chromium-family browser this machine might have, best first.

    Chrome before Edge before Brave is not a preference about browsers -- it
    is the order in which `--app` behaves most predictably, and Edge is second
    because on Windows it is the one that is definitely installed.
    """
    out: list[str] = []
    if sys.platform == "win32":
        roots = [os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                 os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                 os.environ.get("LOCALAPPDATA", "")]
        rel = [r"Google\Chrome\Application\chrome.exe",
               r"Microsoft\Edge\Application\msedge.exe",
               r"BraveSoftware\Brave-Browser\Application\brave.exe",
               r"Chromium\Application\chrome.exe"]
        for r in rel:
            for base in roots:
                if base:
                    out.append(os.path.join(base, r))
    elif sys.platform == "darwin":
        out += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
                "/Applications/Chromium.app/Contents/MacOS/Chromium"]
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium",
                     "chromium-browser", "microsoft-edge", "brave-browser"):
            found = shutil.which(name)
            if found:
                out.append(found)
    return [p for p in out if p and os.path.isfile(p)]


def _open_app_window(url: str) -> bool:
    """Launch the chromeless window. Returns False if there is nothing to launch."""
    exes = _chromium_binaries()
    if not exes:
        return False

    os.makedirs(APP_PROFILE, exist_ok=True)
    w, h = APP_WINDOW
    cmd = [exes[0],
           f"--app={url}",
           f"--user-data-dir={APP_PROFILE}",
           f"--window-size={w},{h}",
           "--no-first-run",
           "--no-default-browser-check",
           "--disable-features=Translate,MediaRouter"]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except OSError:
        return False

    APP_MODE["on"] = True
    print(f"  opened as an app window ({os.path.basename(exes[0])})")
    print("  closing the window stops the server\n")
    threading.Thread(target=_quit_when_the_page_stops_answering,
                     args=(url,), daemon=True).start()
    return True


def _quit_when_the_page_stops_answering(url: str) -> None:
    """Closing the window quits the app -- unless a run is in flight.

    This watches the PAGE, not the process it was launched from. The first
    version waited on the Popen'd Chrome and treated its exit as "the window
    closed", which is wrong in the most ordinary case there is: if a Chrome is
    already running on this profile, the new one hands it the URL and exits
    immediately. The watcher then shut the server down about a second after
    start, and the window that had just opened showed ERR_CONNECTION_REFUSED.

    That bug survived testing because the test killed the browser between
    launches, so every launch got a clean profile -- which is the one thing a
    real user never does. Process lifetime was never a sound signal for "is
    anyone looking at this"; the page answering is.

    Not shutting down while a run is going is the important half. Abandoning a
    run half-written wastes generative calls that have already been paid for,
    and leaves an output folder that looks complete and is not. Same rule the
    port handshake follows: a run in progress is never interrupted by a
    convenience.
    """
    started = time.time()
    while True:
        time.sleep(2)
        if SERVER is None:
            return
        # A run outranks everything. The window may well be shut -- the
        # timeout below will fire once the run is finished and nothing has
        # checked in since.
        if STATE.get("running"):
            continue
        # Nothing has ever checked in. Either no window opened, or the browser
        # cannot reach us. Either way, never shut down on an assumption:
        # give up watching and stay up.
        if not HEARTBEAT["seen"]:
            if time.time() - started > ARM_WITHIN:
                return
            continue
        if time.time() - HEARTBEAT["last"] > CLOSE_AFTER:
            break

    print("\n  window closed - stopping\n")
    threading.Thread(target=SERVER.shutdown, daemon=True).start()


def _open_in_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:                                             # noqa: BLE001
        pass



def main() -> None:
    port = PORT
    for i, a in enumerate(sys.argv):                       # --port 8766
        if a == "--port" and i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1])

    global SERVER
    srv = None
    try:
        srv = ExclusiveHTTPServer((HOST, port), Handler)
    except OSError:
        # The port is taken. Do not die, and do not kill anything -- ask.
        #
        # Leaving the user with a taskkill incantation was technically correct
        # and practically useless: restarting the app is the single most common
        # thing anyone does with it, and "paste this netstat pipeline" is not an
        # answer you want on screen in front of an audience.
        outcome = _ask_previous_copy_to_stand_down(port)
        if outcome == "freed":
            print("\n  An older copy was running; it has stood down.")
            for attempt in range(15):
                try:
                    srv = ExclusiveHTTPServer((HOST, port), Handler)
                    break
                except OSError:
                    # The listener is gone but the port can linger briefly.
                    time.sleep(0.4)
        if srv is None:
            print(f"\n  Port {port} is already in use.\n")
            if outcome == "busy":
                print("  The copy already running is in the MIDDLE OF A RUN, so it was")
                print("  not interrupted -- stopping it would abandon a half-written")
                print("  output folder and waste generative calls already paid for.\n")
                print("  Wait for it to finish, then start this again.")
            elif outcome == "foreign":
                print("  Something else owns that port -- either another program, or")
                print("  this app started from a DIFFERENT folder. Nothing was touched.\n")
                print("  Stop it yourself, or run this copy somewhere else:")
                print(f"    python app.py --port {port + 1}")
            else:
                print("  Something is listening but did not answer, so it was left")
                print("  alone. If it is a stuck copy of this app, stop it:\n")
                print(f"    Windows   for /f \"tokens=5\" %a in "
                      f"('netstat -ano ^| findstr :{port}') do taskkill /PID %a /F")
                print(f"    macOS/Linux   kill $(lsof -ti tcp:{port})\n")
                print(f"  ...or run this one somewhere else:  python app.py --port {port + 1}")
            print()
            raise SystemExit(2)

    SERVER = srv
    url = f"http://{HOST}:{port}"
    print("\n  FDE Social Content Agentic Automation & Analytics")
    print("  Douglas McKay - doug@dougmckay.info")
    print(f"  running at  {url}")
    print("  press Ctrl-C to stop\n")

    mode = ("none"    if "--no-open" in sys.argv else
            "browser" if "--browser" in sys.argv else
            "app"     if "--app"     in sys.argv else "auto")
    if mode in ("app", "auto"):
        opened = _open_app_window(url)
        if not opened and mode == "app":
            print("  No Chrome, Edge, Chromium or Brave was found, so there is")
            print("  no app window to open. It is running -- open it yourself:")
            print(f"    {url}\n")
        elif not opened:
            _open_in_browser(url)
    elif mode == "browser":
        _open_in_browser(url)

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped\n")


if __name__ == "__main__":
    main()
