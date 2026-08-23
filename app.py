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

import glob
import json
import mimetypes
import os
import sys
import threading
import traceback
import webbrowser

import yaml

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from pipeline.brief import BriefError, load_brief
from pipeline.env import load_dotenv
from pipeline.checks import preflight_brief
from pipeline.providers import (CREDENTIALS, default_provider,
                                provider_status)
from pipeline.storage import (STORAGE_CREDENTIALS, StorageError,
                              get_storage, storage_status)
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
# Comparing the mtime we started with against the file now is enough to catch
# it, needs no version constants to keep in sync, and is reported to the UI so
# the person sees it instead of debugging a ghost.
_LOADED_AT = os.path.getmtime(os.path.abspath(__file__))


def _is_stale() -> bool:
    try:
        return os.path.getmtime(os.path.abspath(__file__)) > _LOADED_AT
    except OSError:
        return False

# --------------------------------------------------------------------------
# Run state. One run at a time -- this is a local demo tool, not a service,
# and a queue would be more machinery than the problem deserves.
# --------------------------------------------------------------------------

STATE: dict = {"running": False, "lines": [], "summary": None, "error": None,
               "report": None, "graph": {}, "seq": 0, "landed": []}
LOCK = threading.Lock()

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
        if rec.get("event") == "variant":
            STATE["landed"].insert(0, {
                k: rec.get(k) for k in
                ("variant", "verdict", "product", "locale", "ratio",
                 "message", "path", "out_dir", "findings")})


def _do_run(brief_path: str, provider: str, regen: bool = False,
            storage: str = "local", model: str = "") -> None:
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
            return self._json({"briefs": briefs,
                               "providers": provider_status(),
                               "default_provider": default_provider(),
                               "storages": storage_status(),
                               "stale": _is_stale(),
                               "cwd": ROOT})

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
            if not key.startswith("runs/") or ".." in key:
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
                "products": [{"id": x.id, "reuse": x.has_asset()} for x in b.products],
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
                             report=None, graph={}, seq=0, landed=[])
            p = self._safe(body.get("path", ""))
            if not p:
                with LOCK:
                    STATE["running"] = False
                return self._json({"error": "bad path"}, 400)
            threading.Thread(
                target=_do_run,
                args=(p, body.get("provider", "mock"), bool(body.get("regen")),
                      str(body.get("storage", "local")), str(body.get("model", ""))),
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


def main() -> None:
    port = PORT
    for i, a in enumerate(sys.argv):                       # --port 8766
        if a == "--port" and i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1])

    try:
        srv = ThreadingHTTPServer((HOST, port), Handler)
    except OSError as exc:
        # Fail LOUDLY on a port clash.
        #
        # The default here is quietly dangerous: the new process dies, the old
        # one keeps serving, and the browser shows an app that looks fine and
        # is running yesterday's code. That has been mistaken for three
        # different bugs -- an empty provider list, a missing form, a toggle
        # that "does nothing" -- each time because the server answering was
        # not the server just started.
        print(f"\n  Port {port} is already in use.\n")
        print("  An older copy of this app is almost certainly still running.")
        print("  It will keep answering on this port and it is serving the OLD")
        print("  code, so anything you just changed will appear to have no effect.\n")
        print("  Stop it first:")
        print(f"    Windows   for /f \"tokens=5\" %a in "
              f"('netstat -ano ^| findstr :{port}') do taskkill /PID %a /F")
        print(f"    macOS/Linux   kill $(lsof -ti tcp:{port})\n")
        print(f"  ...or run this one somewhere else:  python app.py --port {port + 1}\n")
        raise SystemExit(2) from exc

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
