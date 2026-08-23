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
import socket
import sys
import threading
import time
import traceback
import webbrowser

import yaml
from PIL import Image

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from pipeline.brief import BriefError, load_brief
from pipeline import insights
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
                 "stored_uri", "share_url")})


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
            return self._json({"briefs": briefs,
                               "default_brief": default_brief,
                               "providers": provider_status(),
                               "default_provider": default_provider(),
                               "storages": storage_status(),
                               "stale": _is_stale(),
                               "cwd": ROOT})

        if route == "/api/insights":
            """Synthetic channel history for every market in a brief.

            Marked `synthetic: true` in the payload and labelled as sample
            data everywhere it is drawn. Nothing here reaches Meta, TikTok, X
            or Google -- see pipeline/insights.py for what a real integration
            would take.
            """
            rel = unquote(urlparse(self.path).query.split("path=", 1)[-1])
            p = self._safe(rel)
            if not p or not os.path.isfile(p):
                return self._json({"error": "not found"}, 404)
            try:
                b = load_brief(p)
            except BriefError as exc:
                return self._json({"error": str(exc)}, 200)
            ratios = [r.id for r in b.ratios]
            return self._json({
                "synthetic": True,
                "ratios": ratios,
                "markets": [insights.report(m.locale, ratios) for m in b.markets],
            })

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
    print("\n  Creative Automation Pipeline")
    print(f"  running at  {url}")
    print("  press Ctrl-C to stop\n")
    if "--no-open" not in sys.argv:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped\n")


if __name__ == "__main__":
    main()
