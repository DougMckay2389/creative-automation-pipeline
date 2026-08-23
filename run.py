#!/usr/bin/env python3
"""Command-line entry point.

    python run.py plan   campaigns/aurora-spring.yaml
    python run.py run    campaigns/aurora-spring.yaml
    python run.py run    campaigns/aurora-spring.yaml --provider gemini
    python run.py providers

`plan` costs nothing and tells you what a run WOULD do -- how many
deliverables, how many generative calls, what pre-flight already objects to.
Anyone about to spend credits at 4 requests/minute should run it first.
"""
from __future__ import annotations

import argparse
import os
import sys
import webbrowser

from pipeline.brief import BriefError, load_brief
from pipeline.env import load_dotenv
from pipeline.checks import preflight_brief
from pipeline.providers import available_providers
from pipeline.report import write_report
from pipeline.runner import run_campaign


load_dotenv()          # credentials from .env, if present


def cmd_plan(args) -> int:
    b = load_brief(args.brief)
    print(f"\ncampaign      {b.campaign_id}  ({b.campaign_name})")
    print(f"products      {len(b.products)}  ({', '.join(p.id for p in b.products)})")
    print(f"markets       {len(b.markets)}  ({', '.join(m.locale for m in b.markets)})")
    print(f"ratios        {len(b.ratios)}  ({', '.join(r.id for r in b.ratios)})")
    print(f"\ndeliverables  {b.variant_count}")
    print(f"generative    {b.generation_count}   <- what this run actually costs")
    for p in b.products:
        state = "reuse (on disk)" if p.has_asset() else "GENERATE"
        print(f"                {p.id:<24} {state}")
    pre = preflight_brief(b)
    if pre:
        print("\npre-flight")
        for f in pre:
            print(f"  [{f.severity.value:<7}] {f.rule_id}  {f.message}")
    else:
        print("\npre-flight    clean")
    print()
    return 0


def cmd_run(args) -> int:
    summary = run_campaign(
        brief_path=args.brief, brand_path=args.brand,
        provider_name=args.provider, out_root=args.out,
        rpm=args.rpm, quiet=args.quiet, force_generate=args.regen,
        storage_name=args.storage)
    report = write_report(summary, summary.output_dir)
    c = summary.counts
    print("\n" + "-" * 62)
    print(f"  {summary.variants_planned} creatives from "
          f"{summary.generative_calls} generative call(s)")
    print(f"  pass {c.get('pass',0)}   review {c.get('review',0)}   block {c.get('block',0)}")
    print(f"  output   {summary.output_dir}")
    print(f"  report   {report}")
    print("-" * 62 + "\n")
    if args.open:
        webbrowser.open("file://" + os.path.abspath(report))
    return 1 if c.get("block") else 0


def cmd_providers(_args) -> int:
    print("\navailable providers:")
    for p in available_providers():
        print(f"  {p}")
    print("\n  mock        always available, no credentials, deterministic")
    print("  cloudflare  needs CLOUDFLARE_ACCOUNT_ID + CLOUDFLARE_API_TOKEN")
    print("  gemini      needs GEMINI_API_KEY")
    print("  firefly     needs FIREFLY_CLIENT_ID + FIREFLY_CLIENT_SECRET")
    print("\n  Credentials can live in a .env file -- see .env.example\n")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="run.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="cost a brief without spending anything")
    p.add_argument("brief")
    p.set_defaults(fn=cmd_plan)

    r = sub.add_parser("run", help="produce the creatives")
    r.add_argument("brief")
    r.add_argument("--brand", default="brandkit/brand.yaml")
    r.add_argument("--provider", default="mock", help="mock | gemini | firefly")
    r.add_argument("--out", default="output")
    r.add_argument("--storage", default="local",
                   help="where to mirror artifacts: local | s3 (local is always "
                        "written either way)")
    r.add_argument("--regen", action="store_true",
                   help="regenerate every product this run, ignoring the asset "
                        "on disk and the cache (seeded from the run id, so the "
                        "manifest can still reproduce it)")
    r.add_argument("--rpm", type=float, default=None,
                   help="override the provider rate limit (requests/minute)")
    r.add_argument("--open", action="store_true", help="open the report when done")
    r.add_argument("--quiet", action="store_true")
    r.set_defaults(fn=cmd_run)

    v = sub.add_parser("providers", help="list generation backends")
    v.set_defaults(fn=cmd_providers)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except BriefError as exc:
        print(f"\nbrief error: {exc}\n", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
