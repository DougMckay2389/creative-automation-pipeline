"""A self-contained HTML report of one run.

Why bother, when there is already a manifest.json? Because the audience is
different. The manifest is for machines and for the audit trail; the report is
the thing you put on a screen in front of a marketing director, and "scroll
through my terminal" has never once won a room.

Self-contained on purpose: thumbnails are inlined as base64 so the file can be
emailed, dropped in Slack, or opened from a USB stick six months later with no
server and no broken relative paths.
"""
from __future__ import annotations

import base64
import html
import io
import os

from PIL import Image

CSS = """
:root{--ink:#12140f;--ink2:#5a5c55;--muted:#8b8d85;--line:#e4e2da;--bg:#faf9f6;
 --card:#fff;--pass:#0ca30c;--review:#fab219;--block:#d03b3b}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
 font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:34px 24px 80px}
h1{font-size:25px;margin:0 0 4px;letter-spacing:-.02em}
.sub{color:var(--ink2);margin:0 0 26px;font-size:14px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:14px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.kpi .n{font-size:26px;font-weight:660;letter-spacing:-.02em;line-height:1.1}
.kpi .l{font-size:11px;text-transform:uppercase;letter-spacing:.09em;color:var(--muted);
 font-weight:700;margin-top:5px}
.note{background:#fffdf3;border:1px solid #f0e4bb;border-radius:12px;padding:13px 16px;
 font-size:13.5px;color:var(--ink2);margin:14px 0 26px}
.note b{color:var(--ink)}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.11em;color:var(--muted);
 margin:30px 0 12px;font-weight:700}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(232px,1fr));gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;
 display:flex;flex-direction:column}
.card .thumbwrap{background:#eeece6;display:flex;align-items:center;justify-content:center;
 min-height:150px;padding:10px}
.card img{max-width:100%;height:auto;display:block;border-radius:4px}
.card .meta{padding:11px 13px 13px;font-size:12px;color:var(--ink2);flex:1}
.card .t{font-size:13px;font-weight:640;color:var(--ink);margin-bottom:3px}
.badge{display:inline-flex;align-items:center;gap:6px;font-size:10.5px;font-weight:700;
 text-transform:uppercase;letter-spacing:.06em;padding:3px 9px;border-radius:999px;
 border:1px solid;margin-bottom:8px}
.badge i{width:7px;height:7px;border-radius:50%;display:inline-block}
.b-pass{color:var(--pass);border-color:rgba(12,163,12,.35);background:rgba(12,163,12,.07)}
.b-review{color:#8a6100;border-color:rgba(250,178,25,.45);background:rgba(250,178,25,.10)}
.b-block{color:var(--block);border-color:rgba(208,59,59,.35);background:rgba(208,59,59,.07)}
.find{margin-top:7px;padding-top:7px;border-top:1px solid var(--line);font-size:11.5px}
.find div{margin-bottom:3px}
.find code{font-family:ui-monospace,Menlo,monospace;font-size:11px;color:var(--ink)}
.swatches{display:flex;gap:3px;margin-top:8px}
.swatches i{width:15px;height:15px;border-radius:3px;border:1px solid rgba(0,0,0,.12);display:block}
table{border-collapse:collapse;width:100%;font-size:13px;background:var(--card);
 border:1px solid var(--line);border-radius:12px;overflow:hidden}
th{text-align:left;font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;
 color:var(--muted);padding:10px 13px;border-bottom:1px solid var(--line)}
td{padding:9px 13px;border-bottom:1px solid var(--line);color:var(--ink2)}
tr:last-child td{border-bottom:0}
"""


def _thumb(path: str, max_w: int = 460) -> str:
    """Inline a downscaled JPEG as a data URI."""
    try:
        img = Image.open(path).convert("RGB")
    except Exception:
        return ""
    if img.width > max_w:
        img = img.resize((max_w, round(img.height * max_w / img.width)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=72, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def write_report(summary, out_dir: str) -> str:
    e = html.escape
    c = summary.counts or {}
    total = max(1, summary.variants_planned)
    deflected = c.get("block", 0) + c.get("review", 0)

    parts = [
        "<!doctype html><meta charset='utf-8'>",
        f"<title>{e(summary.campaign_id)} — creative run {e(summary.run_id)}</title>",
        f"<style>{CSS}</style><div class='wrap'>",
        f"<h1>{e(summary.campaign_id)}</h1>",
        f"<p class='sub'>run {e(summary.run_id)} · provider <b>{e(summary.provider)}</b>"
        f" · model {e(summary.model or '—')} · {summary.duration_s:.1f}s</p>",
        "<div class='kpis'>",
        f"<div class='kpi'><div class='n'>{summary.variants_planned}</div>"
        "<div class='l'>Creatives produced</div></div>",
        f"<div class='kpi'><div class='n'>{summary.generative_calls}</div>"
        "<div class='l'>Generative calls</div></div>",
        f"<div class='kpi'><div class='n' style='color:var(--pass)'>{c.get('pass',0)}</div>"
        "<div class='l'>Clean</div></div>",
        f"<div class='kpi'><div class='n' style='color:var(--review)'>{c.get('review',0)}</div>"
        "<div class='l'>Needs review</div></div>",
        f"<div class='kpi'><div class='n' style='color:var(--block)'>{c.get('block',0)}</div>"
        "<div class='l'>Blocked</div></div>",
        "</div>",
    ]

    ratio = (summary.variants_planned / summary.generative_calls
             if summary.generative_calls else 0)
    parts.append(
        "<div class='note'><b>Cost shape.</b> "
        f"{summary.variants_planned} deliverables from "
        f"<b>{summary.generative_calls}</b> generative call(s)"
        + (f" — {ratio:.0f} creatives per call. " if ratio else " — every product asset was reused. ")
        + f"{summary.reused_from_brief} product(s) reused the creative team's own asset, "
        f"{summary.reused_from_cache} came from cache. "
        f"{deflected} of {total} creatives carry at least one finding and reach a human; "
        "nothing here is auto-approved.</div>")

    if summary.preflight:
        parts.append("<h2>Pre-flight — checked before any credits were spent</h2><table>"
                     "<tr><th>Rule</th><th>Severity</th><th>Detail</th></tr>")
        for f in summary.preflight:
            parts.append(f"<tr><td><code>{e(f['rule'])}</code></td>"
                         f"<td>{e(f['severity'])}</td><td>{e(f['message'])}</td></tr>")
        parts.append("</table>")

    by_product: dict[str, list] = {}
    for r in summary.results:
        by_product.setdefault(r.product_id, []).append(r)

    for pid, rows in by_product.items():
        parts.append(f"<h2>{e(pid)}</h2><div class='grid'>")
        for r in rows:
            abspath = os.path.join(out_dir, r.path)
            src = _thumb(abspath)
            cls = {"pass": "b-pass", "review": "b-review", "block": "b-block"}[r.verdict]
            parts.append("<div class='card'>")
            if src:
                parts.append(f"<div class='thumbwrap'><img src='{src}' alt=''></div>")
            parts.append(
                f"<div class='meta'><span class='badge {cls}'><i></i>{e(r.verdict)}</span>"
                f"<div class='t'>{e(r.ratio)} · {e(r.locale)}</div>"
                f"<div>{e(r.message)}</div>"
                f"<div style='color:var(--muted);margin-top:4px'>font: {e(r.font_family)}"
                f" · master: {e(r.master_origin)}</div>")
            if r.dominant_hex:
                parts.append("<div class='swatches'>" + "".join(
                    f"<i style='background:{e(h)}'></i>" for h in r.dominant_hex) + "</div>")
            if r.findings:
                parts.append("<div class='find'>")
                for f in r.findings:
                    parts.append(f"<div><code>{e(f['rule'])}</code> → {e(f['routes_to'])}"
                                 f"<br>{e(f['message'])}</div>")
                parts.append("</div>")
            parts.append("</div></div>")
        parts.append("</div>")

    parts.append("</div>")
    path = os.path.join(out_dir, "report.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("".join(parts))
    return path
