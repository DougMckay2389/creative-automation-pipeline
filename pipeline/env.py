"""Minimal .env loading, so credentials live in a file rather than a shell.

Ten lines instead of a `python-dotenv` dependency. The pipeline needs three
packages to run; adding a fourth so somebody can avoid typing `export` is a
bad trade, and this is the kind of thing every reviewer has seen enough times
to read at a glance.

Rules, in the order that matters:

* A real environment variable ALWAYS wins. `.env` fills gaps, it never
  overrides -- otherwise a stale file silently beats what you just exported,
  and you lose an afternoon.
* `.env` is gitignored. `.env.example` is committed and contains no secrets.
"""
from __future__ import annotations

import os


def load_dotenv(path: str = ".env") -> list[str]:
    """Load KEY=VALUE lines. Returns the names it set (never the values)."""
    if not os.path.isfile(path):
        return []
    loaded = []
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and key not in os.environ:      # real env wins
                os.environ[key] = val
                loaded.append(key)
    return loaded
