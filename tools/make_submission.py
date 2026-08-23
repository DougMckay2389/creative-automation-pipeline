"""Build the zip that gets handed in.

Two rules, and they pull in opposite directions:

  * the reviewer must be able to unzip it and run it, which means the
    credentials have to be inside;
  * the public repository must never contain them.

So this is deliberately NOT `git archive`. It copies the working tree, adds
`.env` (which git ignores and always will), and drops everything that is
regenerated, enormous, or nobody's business.

    python tools/make_submission.py

Writes ../creative-automation-submission.zip next to the repo.

On the credential inside: it is a Cloudflare **user API token**, which is why
it has a `cfut_` prefix -- Cloudflare publishes that prefix specifically so
scanners can spot it, and they auto-revoke tokens found in public repos. That
is exactly why this file exists rather than a commit. Scope it to
`Account - Workers AI - Read`, and roll it once the review is done.
"""
from __future__ import annotations

import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(os.path.dirname(ROOT), "creative-automation-submission.zip")

# Regenerated, huge, or private. `output/` and `.cache/` are both rebuilt by
# the first run, and shipping .cache would be worse than useless: it would
# make the reviewer's first run silently skip the generative call the README
# promises them.
SKIP_DIRS = {".git", "__pycache__", "output", ".cache", "_to_delete",
             ".venv", "venv", ".pytest_cache", ".idea", ".vscode"}
SKIP_FILES = {".DS_Store", "wrangler-login.log", "keys.txt",
              "creative-automation-submission.zip"}
SKIP_EXT = {".pyc", ".pyo", ".log"}

# Included even though git ignores it. This is the whole point of the script.
FORCE_INCLUDE = {".env"}


def wanted(rel: str) -> bool:
    parts = rel.replace("\\", "/").split("/")
    if any(p in SKIP_DIRS for p in parts[:-1]):
        return False
    name = parts[-1]
    if name in SKIP_FILES or os.path.splitext(name)[1] in SKIP_EXT:
        return False
    return True


def main() -> None:
    env = os.path.join(ROOT, ".env")
    has_key = False
    if os.path.isfile(env):
        with open(env, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("CLOUDFLARE_API_TOKEN=") and len(line.split("=", 1)[1]) > 10:
                    has_key = True

    files = []
    for base, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for n in names:
            full = os.path.join(base, n)
            rel = os.path.relpath(full, ROOT)
            if wanted(rel) or rel in FORCE_INCLUDE:
                files.append((full, rel))

    if os.path.exists(OUT):
        os.remove(OUT)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for full, rel in sorted(files, key=lambda x: x[1]):
            z.write(full, os.path.join("creative-automation", rel))

    size = os.path.getsize(OUT) / (1024 * 1024)
    print(f"\n  {OUT}")
    print(f"  {len(files)} files, {size:.1f} MB\n")
    if has_key:
        print("  .env INCLUDED, with a live Cloudflare token.")
        print("  The reviewer can unzip and run against the real model with no setup.")
        print("  Roll that token once the review is done.\n")
    else:
        print("  WARNING: no Cloudflare token found in .env.")
        print("  The zip will run on the offline provider only.\n")
    # A submission that cannot run is worse than no submission, so say what is
    # in it rather than trusting the build to have been right.
    with zipfile.ZipFile(OUT) as z:
        names = z.namelist()
    for must in ("creative-automation/app.py", "creative-automation/run.py",
                 "creative-automation/README.md",
                 "creative-automation/campaigns/aurora-spring.yaml"):
        if must not in names:
            print(f"  MISSING: {must}")
            sys.exit(1)
    leaked = [n for n in names if "/output/" in n or "/.cache/" in n or n.endswith(".git")]
    if leaked:
        print(f"  {len(leaked)} files that should not be here, e.g. {leaked[0]}")
        sys.exit(1)
    print("  contents verified.\n")


if __name__ == "__main__":
    main()
