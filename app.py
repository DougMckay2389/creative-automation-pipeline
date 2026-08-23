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
import threading
import traceback
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from pipeline.brief import BriefError, load_brief
from pipeline.env import load_dotenv
from pipeline.checks import preflight_brief
from pipeline.providers import available_providers
from pipeline.report import write_report
from pipeline.runner import run_campaign

ROOT = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(ROOT, ".env"))
HOST, PORT = "127.0.0.1", 8765

# --------------------------------------------------------------------------
# Run state. One run at a time -- this is a local demo tool, not a service,
# and a queue would be more machinery than the problem deserves.
# --------------------------------------------------------------------------

STATE: dict = {"running": False, "lines": [], "summary": None, "error": None,
               "report": None, "graph": {}, "seq": 0}
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


def _do_run(brief_path: str, provider: str) -> None:
    """Executed on a worker thread so the browser can poll for progress."""
    try:
        _emit(f"starting  brief={os.path.basename(brief_path)}  provider={provider}")
        summary = run_campaign(brief_path, provider_name=provider, quiet=True,
                               on_event=_on_event)
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
                               "providers": available_providers(),
                               "cwd": ROOT})

        if route == "/api/brief":
            rel = unquote(urlparse(self.path).query.split("path=", 1)[-1])
            p = self._safe(rel)
            if not p or not os.path.isfile(p):
                return self._json({"error": "not found"}, 404)
            with open(p, "r", encoding="utf-8") as fh:
                return self._json({"path": rel, "text": fh.read()})

        if route == "/api/progress":
            with LOCK:
                return self._json({"running": STATE["running"],
                                   "lines": STATE["lines"],
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
                             report=None, graph={}, seq=0)
            p = self._safe(body.get("path", ""))
            if not p:
                with LOCK:
                    STATE["running"] = False
                return self._json({"error": "bad path"}, 400)
            threading.Thread(target=_do_run, args=(p, body.get("provider", "mock")),
                             daemon=True).start()
            return self._json({"ok": True})

        return self._json({"error": "unknown route"}, 404)


def main() -> None:
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print("\n  Creative Automation Pipeline")
    print(f"  running at  {url}")
    print("  press Ctrl-C to stop\n")
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
